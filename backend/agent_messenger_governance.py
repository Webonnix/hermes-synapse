"""Validation, approval and activation for per-agent Telegram bot bindings.

Mirrors provider_governance.py's proposal/approve/execute shape exactly: a
binding is proposed (bot token validated against Telegram's own API, R3 review
task opened), sits `awaiting_approval` until an owner approves it (Telegram
`/approve` or the dashboard), then execute_approved_telegram_binding activates
it and starts the bot's polling loop (backend/agent_bot.py). The bot token is
written to Valkey immediately under a short TTL that's cleared (made permanent)
only once the binding is approved — never written to the SQL DB.

Giving an agent its own bot is a real capability grant (anyone who messages
that bot reaches the agent directly, bypassing the router), so it goes through
the same single-owner-approval gate as an external provider binding rather
than being plain local config like agent_tiers.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.database import DB_PATH

logger = logging.getLogger("hermes.agent_messenger_governance")

_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_PENDING_SECRET_TTL_SECONDS = 24 * 60 * 60  # abandoned proposals self-clean after a day

# A binding either drafts every reply for the owner to review and send by hand
# ('draft', the safe default — nothing reaches the other person without a human
# click), or sends automatically but with a disclosure appended so the person on
# the other end always knows they're talking to an assistant, never a silent
# impersonation of the owner ('auto_labeled'). There is no undisclosed
# auto-send mode — see the conversation that led to this file for why.
RESPONSE_MODES = ("draft", "auto_labeled")
DEFAULT_RESPONSE_MODE = "draft"
DEFAULT_DISCLOSURE_TEXT = "отвечает личный AI-ассистент, а не человек"
AUTO_REPLY_DISCLOSURE = f"\n\n— {DEFAULT_DISCLOSURE_TEXT}."
# A channel may word this its own way, but never remove it: auto-labeled mode was
# approved on the promise that no reply ever goes out looking like the owner
# typed it (see the acceptance list in create_*_binding_proposal).
MAX_DISCLOSURE_LENGTH = 200


def _format_disclosure(text: Optional[str]) -> str:
    clean = " ".join((text or "").split())[:MAX_DISCLOSURE_LENGTH].strip(" —-")
    return f"\n\n— {clean}" if clean else AUTO_REPLY_DISCLOSURE


def auto_reply_disclosure(binding_id: str) -> str:
    """The signature appended to every auto-labeled reply on this channel.

    Read at send time rather than cached in each bot manager, so an edited
    wording applies to the next message without restarting the sync loop.
    """
    binding = get_binding(binding_id) or {}
    return _format_disclosure(binding.get("auto_reply_disclosure"))



# Who is allowed to talk to a bound bot at all.
#   'owner_only'  — only the chat ids in allowed_chat_ids (today's behaviour, and
#                   the default every existing binding is migrated to).
#   'token'       — a social bot: anyone may start it, but the first thing it
#                   asks for is an access token issued in backend/bot_access.py.
#                   The owner's own chat ids still get through without one.
ACCESS_MODES = ("owner_only", "token")
DEFAULT_ACCESS_MODE = "owner_only"

DEFAULT_WELCOME_MESSAGE = (
    "Здравствуйте! Для начала работы отправьте, пожалуйста, ваш токен доступа "
    "(строка вида HRM-…). Без него я не смогу отвечать."
)


def _clean_response_mode(value: Optional[str]) -> str:
    mode = (value or DEFAULT_RESPONSE_MODE).strip().lower()
    if mode not in RESPONSE_MODES:
        raise ValueError(f"response_mode must be one of {RESPONSE_MODES}, got {value!r}")
    return mode


def _clean_access_mode(value: Optional[str]) -> str:
    mode = (value or DEFAULT_ACCESS_MODE).strip().lower()
    if mode not in ACCESS_MODES:
        raise ValueError(f"access_mode must be one of {ACCESS_MODES}, got {value!r}")
    return mode


# Matrix-only: the account this binding talks through IS the owner's own
# account (see [[matrix-mas-device-login]]), so a message the owner types
# themselves in the same room is trivially distinguishable from the bot's own
# replies — every other channel's bot has its own separate identity/token, so
# there is no equally reliable way to tell "the owner, typing personally" apart
# from any other sender there.
DEFAULT_HUMAN_TAKEOVER_PAUSE_MINUTES = 120
MAX_HUMAN_TAKEOVER_PAUSE_MINUTES = 1440  # 24h — a hard ceiling against "silent forever" by typo


def _clean_pause_minutes(value: Optional[int]) -> int:
    if value is None:
        return DEFAULT_HUMAN_TAKEOVER_PAUSE_MINUTES
    minutes = int(value)
    if not (0 <= minutes <= MAX_HUMAN_TAKEOVER_PAUSE_MINUTES):
        raise ValueError(f"human_takeover_pause_minutes must be between 0 and {MAX_HUMAN_TAKEOVER_PAUSE_MINUTES}")
    return minutes


def _runtime_module_for_platform(platform: str):
    import importlib

    module_by_platform = {
        "telegram": "backend.agent_bot",
        "matrix": "backend.agent_matrix_bot",
        "discord": "backend.agent_discord_bot",
        "slack": "backend.agent_slack_bot",
        "email": "backend.agent_email_channel",
        # The social channels declare their own runtime in SOCIAL_PLATFORMS
        # (below) so adding one is a single dict entry, not an edit here too.
        **{name: spec["runtime"] for name, spec in SOCIAL_PLATFORMS.items()},
    }
    module_name = module_by_platform.get(platform)
    if not module_name:
        raise ValueError(f"Unknown platform: {platform}")
    return importlib.import_module(module_name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_messenger_bindings (
                id TEXT PRIMARY KEY,
                subagent_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'telegram',
                bot_username TEXT NOT NULL,
                valkey_secret_key TEXT NOT NULL,
                allowed_chat_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                control_task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Per-channel overrides of the subagent's own system_prompt/model — added after
        # the table already existed on deployed installs, so migrated defensively rather
        # than baked into CREATE TABLE (see [[deploy_compose_divergence]]-style gotcha:
        # SQLite doesn't retroactively apply a changed CREATE TABLE to existing tables).
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_messenger_bindings)")}
        for column in ("system_prompt_override", "model_override", "model_provider_override"):
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE agent_messenger_bindings ADD COLUMN {column} TEXT")
        if "response_mode" not in existing_columns:
            connection.execute(
                f"ALTER TABLE agent_messenger_bindings ADD COLUMN response_mode TEXT NOT NULL DEFAULT '{DEFAULT_RESPONSE_MODE}'"
            )
        # Public token access (backend/bot_access.py). 'owner_only' is the
        # pre-existing behaviour — the allowed_chat_ids whitelist and nothing
        # else — so every binding that already exists keeps it on migration.
        if "access_mode" not in existing_columns:
            connection.execute(
                f"ALTER TABLE agent_messenger_bindings ADD COLUMN access_mode TEXT NOT NULL DEFAULT '{DEFAULT_ACCESS_MODE}'"
            )
        if "default_plan_id" not in existing_columns:
            connection.execute("ALTER TABLE agent_messenger_bindings ADD COLUMN default_plan_id TEXT")
        if "welcome_message" not in existing_columns:
            connection.execute("ALTER TABLE agent_messenger_bindings ADD COLUMN welcome_message TEXT")
        if "last_error" not in existing_columns:
            connection.execute("ALTER TABLE agent_messenger_bindings ADD COLUMN last_error TEXT")
        if "auto_reply_disclosure" not in existing_columns:
            connection.execute("ALTER TABLE agent_messenger_bindings ADD COLUMN auto_reply_disclosure TEXT")
        if "human_takeover_pause_minutes" not in existing_columns:
            connection.execute("ALTER TABLE agent_messenger_bindings ADD COLUMN human_takeover_pause_minutes INTEGER")
        if "escalation_enabled" not in existing_columns:
            connection.execute(
                "ALTER TABLE agent_messenger_bindings ADD COLUMN escalation_enabled INTEGER NOT NULL DEFAULT 0"
            )
        # Off by default — bot_access_gate.py only shows the "talk to the AI
        # now / wait for the owner" first-contact menu (see conversation_state.py)
        # when a binding opts in, so every existing draft-mode binding keeps
        # its current silent-until-reviewed behaviour unchanged.
        if "handling_menu_enabled" not in existing_columns:
            connection.execute(
                "ALTER TABLE agent_messenger_bindings ADD COLUMN handling_menu_enabled INTEGER NOT NULL DEFAULT 0"
            )
        # The account id the provider itself uses (VK community id, Facebook
        # page id, Instagram business id, WhatsApp phone number id). Webhook
        # platforms carry it in every payload, and it is the only thing an
        # incoming event can be routed to a binding by — see
        # find_social_binding_by_account. Public information, unlike the
        # credential, so it lives in SQL next to the row it identifies.
        if "external_account_id" not in existing_columns:
            connection.execute("ALTER TABLE agent_messenger_bindings ADD COLUMN external_account_id TEXT")


def _verify_bot_token(bot_token: str) -> str:
    """Calls Telegram's getMe to confirm the token is real and fetch its username.
    Raises ValueError on anything that isn't a working bot token."""
    clean_token = str(bot_token).strip()
    if not _TOKEN.fullmatch(clean_token):
        raise ValueError("bot_token doesn't look like a BotFather token (expected '<digits>:<35+ chars>')")
    try:
        response = httpx.get(f"https://api.telegram.org/bot{clean_token}/getMe", timeout=10)
        data = response.json()
    except Exception as exc:
        raise ValueError(f"Could not reach Telegram to verify the bot token: {exc}") from exc
    if not data.get("ok"):
        raise ValueError(f"Telegram rejected this bot token: {data.get('description', 'unknown error')}")
    username = (data.get("result") or {}).get("username")
    if not username:
        raise ValueError("Telegram accepted the token but returned no bot username")
    return username


def apply_binding_overrides(subagent: dict[str, Any], overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Returns a shallow copy of `subagent` with this binding's per-channel system
    prompt / model / model_provider overrides applied, falling back to the subagent's
    own defaults for anything not overridden. `subagent["id"]` is left untouched so
    budget enforcement, redaction and cost/token logging in `_respond_as_subagent`
    still attribute everything to the real agent — a binding changes *how* the agent
    talks on one channel, not *which* agent it is."""
    if not overrides:
        return subagent
    effective = dict(subagent)
    if overrides.get("system_prompt"):
        effective["system_prompt"] = overrides["system_prompt"]
    if overrides.get("model"):
        effective["model"] = overrides["model"]
    if overrides.get("model_provider"):
        effective["model_provider"] = overrides["model_provider"]
    return effective


def _check_messenger_allowed(subagent: dict[str, Any]) -> None:
    """Raises ValueError if this agent may not be bound to a messenger at all —
    either because it is the main agent (Vexa answers to the owner only, never
    over a channel someone else can reach) or because its tier opted out."""
    from backend.tool_permissions import MAIN_AGENT_IDS

    if subagent.get("id") in MAIN_AGENT_IDS:
        raise ValueError(
            "The main agent cannot be bound to a messenger channel — Vexa is reachable "
            "only by the owner, through the dashboard or the admin-gated Telegram bot."
        )

    tier_id = subagent.get("tier_id")
    if not tier_id:
        return
    from backend.agent_tiers import get_tier

    tier = get_tier(tier_id)
    if tier and not tier["allow_messenger"]:
        raise ValueError(f"Tier '{tier['name']}' does not allow messenger bindings for this agent")


def _check_public_access_allowed(binding: dict[str, Any]) -> None:
    """Guards the one-way door of opening a bot to strangers."""
    from backend.tool_permissions import MAIN_AGENT_IDS

    if binding.get("subagent_id") in MAIN_AGENT_IDS:
        raise ValueError("The main agent can never be opened to token holders.")

    from backend.database import get_subagent

    subagent = get_subagent(binding["subagent_id"])
    if not subagent:
        raise KeyError(f"Unknown agent: {binding['subagent_id']}")
    _check_messenger_allowed(subagent)


def _notify_owner_access_mode_changed(binding: dict[str, Any], new_mode: str) -> None:
    """Opening a bot to anyone with a token widens its exposure well beyond what
    the original binding approval covered, so the owner always hears about the
    switch — same best-effort owner ping channel_replies.py uses for drafts."""
    import asyncio

    async def _send() -> None:
        try:
            import backend.bot as bot

            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
            if not chat_id or not getattr(bot, "telegram_app", None) or not bot.telegram_app.bot:
                return
            if new_mode == "token":
                text = (
                    f"🔓 Канал @{binding.get('bot_username') or binding['id']} ({binding['platform']}) "
                    f"переведён в режим доступа по токенам: писать боту теперь может любой, "
                    f"у кого есть выданный вами токен. Агент: {binding['subagent_id']}."
                )
            else:
                text = (
                    f"🔒 Канал @{binding.get('bot_username') or binding['id']} ({binding['platform']}) "
                    f"снова закрыт — отвечает только вам."
                )
            await bot.telegram_app.bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:
            logger.warning("Access-mode owner notification failed: %s", exc)

    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        pass


def notify_owner_escalation(
    binding: dict[str, Any], platform: str, chat_id: str, requester: str, excerpt: str, reason: str = "",
) -> bool:
    """Pings the owner on the main Telegram bot when a conversation needs them
    personally: either bot_access_gate's escalation classifier decided so and
    the other person confirmed (the default ``reason``), or that person picked
    "срочно" from the handling menu themselves (which passes its own
    ``reason``). Despite the "Позвонить?" wording shown to them
    (backend/bot_access_gate.py's _ESCALATION_OFFER_TEXT) there is no telephony
    integration anywhere in this codebase — this notification is what actually
    happens, same best-effort admin-ping channel used for new drafts
    (channel_replies.py) and dev-run failures (dev_runs.py).

    Returns whether the ping actually went out, so a caller that promises the
    other person "уведомил Альберта" can tell the truth about it.

    Called from bot_access_gate.authorize(), which runs inside a worker thread
    (asyncio.to_thread from the channel manager) — there is no running event
    loop to schedule a task on, so this blocks briefly on its own loop instead
    of the fire-and-forget pattern _notify_owner_access_mode_changed uses.
    """
    async def _send() -> bool:
        try:
            import backend.bot as bot

            chat_ids = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
            if not chat_ids or not getattr(bot, "telegram_app", None) or not bot.telegram_app.bot:
                return False
            preview = (excerpt or "").strip().replace("\n", " ")[:300]
            why = reason or (
                f"просит связаться лично — агент «{binding.get('subagent_id')}» предложил позвать вас, "
                "собеседник согласился"
            )
            text = (
                f"📞 {requester or 'Собеседник'} ({platform}, {binding.get('bot_username') or binding['id']}) {why}."
            )
            if preview:
                text += f"\n\n«{preview}»"
            await bot.telegram_app.bot.send_message(chat_id=int(chat_ids[0]), text=text)
            return True
        except Exception as exc:
            logger.warning("Escalation owner notification failed: %s", exc)
            return False

    try:
        return asyncio.run(_send())
    except Exception:
        logger.exception("Escalation owner notification crashed for binding %s", binding.get("id"))
        return False


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("valkey_secret_key", None)  # never surfaced outside this module
    try:
        item["allowed_chat_ids"] = json.loads(item.get("allowed_chat_ids") or "[]")
    except Exception:
        item["allowed_chat_ids"] = []
    item["access_mode"] = item.get("access_mode") or DEFAULT_ACCESS_MODE
    item["escalation_enabled"] = bool(item.get("escalation_enabled"))
    return item


def create_telegram_binding_proposal(
    subagent_id: str,
    bot_token: str,
    allowed_chat_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
    response_mode: str = DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    clean_response_mode = _clean_response_mode(response_mode)
    bot_username = _verify_bot_token(bot_token)
    clean_chat_ids = [str(c).strip() for c in (allowed_chat_ids or []) if str(c).strip()]

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'telegram' AND status IN ('awaiting_approval', 'active') ORDER BY created_at DESC LIMIT 1",
            (subagent_id,),
        ).fetchone()
    if existing:
        raise ValueError(
            f"Agent '{subagent_id}' already has a {dict(existing)['status']} Telegram binding — revoke it first"
        )

    from backend.control_plane import create_review_task

    binding_id = f"tgbind-{uuid.uuid4().hex[:12]}"
    secret_key = f"agent_bot_secret:{binding_id}"

    mode_note = (
        "DRAFT — every reply is queued for the owner to review and send by hand; nothing reaches the other person automatically."
        if clean_response_mode == "draft"
        else "AUTO-LABELED — replies send automatically, each one tagged as coming from the Vexa assistant, never silently as the owner."
    )
    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to its own Telegram bot (@{bot_username})",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "bot_username": bot_username,
            "allowed_chat_ids": clean_chat_ids or "unrestricted — anyone who finds the bot can message it",
            "response_mode": mode_note,
            "secret_policy": "Bot token stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Telegram confirmed the token belongs to a real bot",
            "Bot token never appears in the SQL database or application logs",
            "Once active, messages to this bot are answered ONLY by this one agent",
            "No reply is ever sent under the owner's identity without either a human send-click (draft mode) or an explicit assistant disclosure (auto-labeled mode)",
        ],
        rollback=f"Revoke the Telegram binding for '{subagent['name']}' — stops the bot and deletes its token.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    set_value(secret_key, str(bot_token).strip(), ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override, response_mode)
            VALUES (?, ?, 'telegram', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, bot_username, secret_key, json.dumps(clean_chat_ids), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None, clean_response_mode,
            ),
        )
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _row_to_dict(row)
    result["control_task"] = task
    return result


def get_telegram_binding_proposal(control_task_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'telegram' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def _set_status(binding_id: str, status: str) -> None:
    with _connect() as connection:
        if status == "active":
            # Clear any stale failure reason from a previous life of this binding —
            # otherwise a fixed connection still shows its old error in the UI.
            connection.execute(
                "UPDATE agent_messenger_bindings SET status = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (status, _now(), binding_id),
            )
        else:
            connection.execute(
                "UPDATE agent_messenger_bindings SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), binding_id),
            )


def mark_binding_failed(binding_id: str, reason: str) -> None:
    """Called by a running channel manager (today: agent_matrix_bot.py) when it
    detects its own credentials have gone bad mid-flight — e.g. an access token
    that was valid at connect time but expired later. Without this, that kind
    of failure was invisible: the sync/poll loop just kept silently retrying
    and the binding stayed 'active' in the UI while doing nothing."""
    _init_schema()
    with _connect() as connection:
        connection.execute(
            "UPDATE agent_messenger_bindings SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
            (reason, _now(), binding_id),
        )


def update_matrix_binding_credentials(binding_id: str, credentials: dict[str, Any]) -> None:
    """Called by agent_matrix_bot.py after it exchanges a dying MAS-issued access
    token for a fresh one. Persists the whole renewed credential (tokens rotate,
    so the refresh_token and expiry move too) into the same Valkey slot the
    binding already uses (never the SQL DB) — this is a renewal of a capability
    already approved, not a new grant, so no status change and no owner
    re-approval, same reasoning as _reconnect_binding()."""
    binding = _binding_or_raise(binding_id)
    if binding["platform"] != "matrix":
        raise ValueError(f"Binding {binding_id} is not a matrix binding")

    from backend.valkey_client import set_value

    set_value(binding["valkey_secret_key"], json.dumps(credentials), ttl_seconds=None)


async def execute_approved_telegram_binding(control_task_id: str) -> dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Telegram binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"Telegram binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        token = get_value(binding["valkey_secret_key"])
        if not token:
            raise RuntimeError("Bot token expired before approval — re-create the binding")
        set_value(binding["valkey_secret_key"], token, ttl_seconds=None)
        _set_status(binding["id"], "active")

        from backend import agent_bot

        allowed_chat_ids = json.loads(binding.get("allowed_chat_ids") or "[]")
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        response_mode = _clean_response_mode(binding.get("response_mode"))
        await agent_bot.manager.start(
            binding["id"], binding["subagent_id"], token, allowed_chat_ids, overrides, response_mode
        )

        result = {"status": "active", "id": binding["id"], "bot_username": binding["bot_username"]}
    except Exception as exc:
        mark_binding_failed(binding["id"], str(exc))
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_telegram_bindings(subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        if subagent_id:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'telegram' ORDER BY created_at DESC",
                (subagent_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE platform = 'telegram' ORDER BY created_at DESC"
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_telegram_binding(binding_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_binding(binding_id: str) -> Optional[dict[str, Any]]:
    """Platform-agnostic lookup — all five channels share one table. Same query
    as get_telegram_binding(), named for what it actually does; used by
    backend/bot_access_gate.py, which doesn't care which platform it's on."""
    return get_telegram_binding(binding_id)


def resolve_telegram_binding_token(binding_id: str) -> Optional[str]:
    """Returns the bot token for an active binding, or None if inactive/missing."""
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    return get_value(dict(row)["valkey_secret_key"])



def _resolve_matrix_credentials(
    homeserver_url: str, user_id: str, password: str = "", access_token: str = "", refresh_token: str = "",
    device_flow_id: str = "",
) -> dict[str, Any]:
    """Turns whatever the owner supplied into a verified credential dict.

    Three ways in, in descending order of how long the result survives:
      * device_flow_id — a finished OAuth device login (MAS servers): renewable
        forever, the only one that does not need re-pasting;
      * password — compat login, asking for a refresh token so it renews too;
      * access_token — a token pasted out of Element. On a MAS homeserver these
        expire within minutes, so unless a refresh_token is pasted alongside it
        (Element does not show one), this binding *will* fall over — that is the
        whole point of offering the device login above.
    """
    from backend import matrix_oauth

    if device_flow_id:
        credentials = _pop_completed_matrix_login(device_flow_id)
    elif password:
        credentials = matrix_oauth.password_login(homeserver_url, user_id, password)
    elif access_token:
        homeserver_url = matrix_oauth.resolve_homeserver_base_url(homeserver_url)
        resolved_user_id = matrix_oauth.whoami(homeserver_url, access_token)
        credentials = {
            "homeserver_url": homeserver_url, "user_id": resolved_user_id,
            "access_token": access_token.strip(), "refresh_token": refresh_token.strip(),
            "auth_kind": "token",
        }
    else:
        raise ValueError("Provide a browser login, a password or an access_token")
    return credentials


def _verify_matrix_credentials(credentials: dict[str, Any]) -> str:
    """Liveness probe for an already-stored credential (health check): proves the
    access token still works. Raises ValueError if the homeserver rejects it."""
    from backend import matrix_oauth

    return matrix_oauth.whoami(credentials["homeserver_url"], credentials.get("access_token", ""))


_COMPLETED_LOGIN_KEY_PREFIX = "matrix_completed_login:"
_COMPLETED_LOGIN_TTL_SECONDS = 900


def poll_matrix_device_login(flow_id: str) -> dict[str, Any]:
    """Polled by the dashboard while the owner confirms the login in a browser.
    The finished tokens are parked in Valkey under a one-shot handle instead of
    being handed to the browser — the UI only ever learns the resulting user id."""
    from backend import matrix_oauth
    from backend.valkey_client import set_value

    credentials = matrix_oauth.poll_device_login(flow_id)
    if credentials is None:
        # The code travels back on every poll, so the dashboard can keep showing
        # it even if the page was reloaded or the panel re-rendered.
        return {"status": "pending", **matrix_oauth.describe_flow(flow_id)}
    set_value(
        f"{_COMPLETED_LOGIN_KEY_PREFIX}{flow_id}", json.dumps(credentials),
        ttl_seconds=_COMPLETED_LOGIN_TTL_SECONDS,
    )
    return {"status": "complete", "user_id": credentials["user_id"], "device_flow_id": flow_id}


def _pop_completed_matrix_login(flow_id: str) -> dict[str, Any]:
    from backend.valkey_client import delete_value, get_value

    raw = get_value(f"{_COMPLETED_LOGIN_KEY_PREFIX}{flow_id}")
    if not raw:
        raise ValueError("Вход через браузер истёк — начните заново")
    delete_value(f"{_COMPLETED_LOGIN_KEY_PREFIX}{flow_id}")
    return json.loads(raw)


def create_matrix_binding_proposal(
    subagent_id: str,
    homeserver_url: str,
    user_id: str,
    password: str = "",
    access_token: str = "",
    refresh_token: str = "",
    device_flow_id: str = "",
    allowed_room_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
    response_mode: str = DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    clean_response_mode = _clean_response_mode(response_mode)
    credentials = _resolve_matrix_credentials(
        homeserver_url, user_id, password=password, access_token=access_token, refresh_token=refresh_token,
        device_flow_id=device_flow_id,
    )
    resolved_user_id = credentials["user_id"]
    homeserver_url = credentials["homeserver_url"]  # post-.well-known, the one actually used
    clean_room_ids = [str(r).strip() for r in (allowed_room_ids or []) if str(r).strip()]

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'matrix' AND status IN ('awaiting_approval', 'active') ORDER BY created_at DESC LIMIT 1",
            (subagent_id,),
        ).fetchone()
    if existing:
        raise ValueError(
            f"Agent '{subagent_id}' already has a {dict(existing)['status']} Matrix binding — revoke it first"
        )

    from backend.control_plane import create_review_task

    binding_id = f"mxbind-{uuid.uuid4().hex[:12]}"
    secret_key = f"agent_matrix_secret:{binding_id}"

    mode_note = (
        "DRAFT — every reply is queued for the owner to review and send by hand; nothing reaches the other person automatically."
        if clean_response_mode == "draft"
        else "AUTO-LABELED — replies send automatically, each one tagged as coming from the Vexa assistant, never silently as the owner."
    )
    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to its own Matrix account ({resolved_user_id})",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "matrix_user_id": resolved_user_id,
            "homeserver_url": homeserver_url,
            "allowed_room_ids": clean_room_ids or "unrestricted — any room this account is in can reach the agent",
            "response_mode": mode_note,
            "auth_kind": credentials.get("auth_kind", "token"),
            "secret_policy": "Access token stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Matrix confirmed the credential belongs to a real account",
            "Access token never appears in the SQL database or application logs",
            "Once active, messages to this account are answered ONLY by this one agent",
            "No reply is ever sent under the owner's identity without either a human send-click (draft mode) or an explicit assistant disclosure (auto-labeled mode)",
        ],
        rollback=f"Revoke the Matrix binding for '{subagent['name']}' — stops the sync loop and deletes its token.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    credentials_json = json.dumps(credentials)
    set_value(secret_key, credentials_json, ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override, response_mode)
            VALUES (?, ?, 'matrix', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, resolved_user_id, secret_key, json.dumps(clean_room_ids), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None, clean_response_mode,
            ),
        )
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _row_to_dict(row)
    result["control_task"] = task
    return result


def get_matrix_binding_proposal(control_task_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'matrix' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


async def execute_approved_matrix_binding(control_task_id: str) -> dict[str, Any]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'matrix' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Matrix binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"Matrix binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        credentials_json = get_value(binding["valkey_secret_key"])
        if not credentials_json:
            raise RuntimeError("Matrix credentials expired before approval — re-create the binding")
        set_value(binding["valkey_secret_key"], credentials_json, ttl_seconds=None)
        _set_status(binding["id"], "active")

        from backend import agent_matrix_bot

        allowed_room_ids = json.loads(binding.get("allowed_chat_ids") or "[]")
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        response_mode = _clean_response_mode(binding.get("response_mode"))
        await agent_matrix_bot.manager.start(
            binding["id"], binding["subagent_id"], json.loads(credentials_json), allowed_room_ids,
            overrides, response_mode,
        )
        agent_matrix_bot.manager.set_human_takeover_pause(
            binding["id"], _clean_pause_minutes(binding.get("human_takeover_pause_minutes"))
        )

        result = {"status": "active", "id": binding["id"], "matrix_user_id": binding["bot_username"]}
    except Exception as exc:
        mark_binding_failed(binding["id"], str(exc))
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_matrix_bindings(subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    with _connect() as connection:
        if subagent_id:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'matrix' ORDER BY created_at DESC",
                (subagent_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE platform = 'matrix' ORDER BY created_at DESC"
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_matrix_binding(binding_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE id = ? AND platform = 'matrix'", (binding_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def resolve_matrix_binding_credentials(binding_id: str) -> Optional[dict[str, Any]]:
    """Returns the {homeserver_url, user_id, access_token} dict for an active binding, or None."""
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    credentials_json = get_value(dict(row)["valkey_secret_key"])
    return json.loads(credentials_json) if credentials_json else None



async def update_binding_settings(
    binding_id: str,
    system_prompt_override: Optional[str] = None,
    response_mode: Optional[str] = None,
    access_mode: Optional[str] = None,
    default_plan_id: Optional[str] = None,
    welcome_message: Optional[str] = None,
    allowed_chat_ids: Optional[list[str]] = None,
    auto_reply_disclosure_text: Optional[str] = None,
    human_takeover_pause_minutes: Optional[int] = None,
    escalation_enabled: Optional[bool] = None,
    handling_menu_enabled: Optional[bool] = None,
    model_override: Optional[str] = None,
    model_provider_override: Optional[str] = None,
) -> dict[str, Any]:
    """Edits an existing binding's per-channel prompt, response mode and model
    in place. Doesn't touch the bot token/credential or require a new owner
    approval — that gate is for granting the capability in the first place,
    not for tuning tone or flipping between draft/auto-labeled afterwards.
    Applies immediately to the live bot if it's currently running.

    The model pair was writable only at creation until now, and the connect
    form never sent it — so a channel was pinned to whatever its agent used, for
    life, and moving one channel to an external model meant editing the table by
    hand. It is not a new capability: every runtime here has always read these
    two columns (see the `overrides` dict below), and the provider itself is
    still an already-approved `provider_bindings` row."""
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row:
        raise KeyError(binding_id)
    binding = dict(row)

    updates: dict[str, Any] = {}
    if system_prompt_override is not None:
        updates["system_prompt_override"] = system_prompt_override.strip() or None
    if response_mode is not None:
        updates["response_mode"] = _clean_response_mode(response_mode)
    if allowed_chat_ids is not None:
        # bot_access_gate.authorize() reads this straight from the DB on every
        # incoming message, so no live-bot restart is needed for this to take
        # effect. An empty list means "owner_only" answers anyone who DMs it —
        # see the comment on ACCESS_MODES above.
        updates["allowed_chat_ids"] = json.dumps([str(c).strip() for c in allowed_chat_ids if str(c).strip()])
    if welcome_message is not None:
        updates["welcome_message"] = welcome_message.strip() or None
    if auto_reply_disclosure_text is not None:
        # Empty means "use the default wording", not "send no disclosure at
        # all" — see _format_disclosure(). Auto-labeled mode was approved on
        # the condition that a reply always identifies itself as automated;
        # removing the signature outright isn't a wording choice this endpoint
        # is allowed to make.
        updates["auto_reply_disclosure"] = auto_reply_disclosure_text.strip()[:MAX_DISCLOSURE_LENGTH] or None
    if human_takeover_pause_minutes is not None:
        updates["human_takeover_pause_minutes"] = _clean_pause_minutes(human_takeover_pause_minutes)
    if escalation_enabled is not None:
        updates["escalation_enabled"] = 1 if escalation_enabled else 0
    if handling_menu_enabled is not None:
        updates["handling_menu_enabled"] = 1 if handling_menu_enabled else 0
    if model_override is not None:
        updates["model_override"] = model_override.strip()[:200] or None
    if model_provider_override is not None:
        clean_provider = model_provider_override.strip()
        # '' clears the override (the channel goes back to its agent's own
        # provider); 'local' pins it to the free local model; anything else
        # must name a binding that is approved *and* still active, so a revoked
        # provider cannot be smuggled back in through a channel edit.
        from backend.llamacpp_client import is_local_provider_id

        if clean_provider and not is_local_provider_id(clean_provider):
            from backend.provider_governance import get_binding

            provider_binding = get_binding(clean_provider)
            if not provider_binding or provider_binding["status"] != "active":
                raise ValueError(f"Unknown or inactive provider binding: {clean_provider}")
        updates["model_provider_override"] = clean_provider or None
    if default_plan_id is not None:
        clean_plan_id = default_plan_id.strip() or None
        if clean_plan_id:
            from backend.bot_access import get_plan

            if not get_plan(clean_plan_id):
                raise KeyError(f"Unknown access plan: {clean_plan_id}")
        updates["default_plan_id"] = clean_plan_id
    if access_mode is not None:
        new_access_mode = _clean_access_mode(access_mode)
        if new_access_mode != _clean_access_mode(binding.get("access_mode")):
            if new_access_mode == "token":
                _check_public_access_allowed(binding)
                # Draft mode means a human must click "send" on every reply, which
                # a stranger-facing consulting bot can't work with. Flip to the
                # disclosed auto-reply mode unless the owner already chose it.
                if _clean_response_mode(updates.get("response_mode") or binding.get("response_mode")) == "draft":
                    updates["response_mode"] = "auto_labeled"
                if not (updates.get("welcome_message") or binding.get("welcome_message")):
                    updates["welcome_message"] = DEFAULT_WELCOME_MESSAGE
            updates["access_mode"] = new_access_mode
            _notify_owner_access_mode_changed(binding, new_access_mode)

    if updates:
        set_clause = ", ".join(f"{key} = ?" for key in updates)
        with _connect() as connection:
            connection.execute(
                f"UPDATE agent_messenger_bindings SET {set_clause}, updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), binding_id),
            )
        binding.update(updates)

    if binding["status"] == "active":
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        mode = _clean_response_mode(binding.get("response_mode"))
        runtime = _runtime_module_for_platform(binding["platform"])
        runtime.manager.update_live_settings(binding_id, overrides, mode)
        if binding["platform"] == "matrix":
            runtime.manager.set_human_takeover_pause(
                binding_id, _clean_pause_minutes(binding.get("human_takeover_pause_minutes"))
            )

    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    return _row_to_dict(row)


def _binding_or_raise(binding_id: str) -> dict[str, Any]:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row:
        raise KeyError(binding_id)
    return dict(row)


async def disable_binding(binding_id: str) -> dict[str, Any]:
    """Stops a binding's bot without touching its stored credential, so it can be
    re-enabled later without the owner re-entering a token/password. This is the
    generic, platform-agnostic replacement for the old per-platform
    revoke_*_binding() calls, which also destroyed the Valkey secret — making
    'disable' actually irreversible and indistinguishable from a real delete."""
    binding = _binding_or_raise(binding_id)
    runtime = _runtime_module_for_platform(binding["platform"])
    await runtime.manager.stop(binding_id)
    _set_status(binding_id, "revoked")
    return _row_to_dict(_binding_or_raise(binding_id))


async def enable_binding(binding_id: str) -> dict[str, Any]:
    """Restarts a disabled ('revoked') or errored ('failed') binding's bot using
    its still-stored credential — a low-friction "try again" for a transient
    failure (network blip, homeserver restart) that doesn't need a new
    credential. Raises ValueError if there's nothing to resolve (e.g. the
    binding predates this function and had its secret destroyed by the old
    revoke_*_binding behaviour — the channel must be reconnected instead)."""
    binding = _binding_or_raise(binding_id)
    if binding["status"] == "active":
        return _row_to_dict(binding)
    if binding["status"] not in ("revoked", "failed"):
        raise ValueError(f"Cannot enable a binding with status '{binding['status']}'")

    platform = binding["platform"]
    runtime = _runtime_module_for_platform(platform)
    overrides = {
        "system_prompt": binding.get("system_prompt_override"),
        "model": binding.get("model_override"),
        "model_provider": binding.get("model_provider_override"),
    }
    allowed = json.loads(binding.get("allowed_chat_ids") or "[]")
    response_mode = _clean_response_mode(binding.get("response_mode"))

    # The resolve_*_credentials/token helpers only return a value for a row whose
    # status is already 'active' (the same guard that keeps a disabled binding's
    # secret from being read elsewhere) — flip the status first, then resolve.
    _set_status(binding_id, "active")
    try:
        if platform == "telegram":
            credential_or_token = resolve_telegram_binding_token(binding_id)
        elif platform == "discord":
            credential_or_token = resolve_discord_binding_token(binding_id)
        elif platform == "matrix":
            credential_or_token = resolve_matrix_binding_credentials(binding_id)
        elif platform == "slack":
            credential_or_token = resolve_slack_binding_credentials(binding_id)
        elif platform == "email":
            credential_or_token = resolve_email_binding_credentials(binding_id)
        elif platform in SOCIAL_PLATFORMS:
            credential_or_token = resolve_social_binding_credentials(binding_id)
        else:
            raise ValueError(f"Unknown platform: {platform}")
        if not credential_or_token:
            raise ValueError("Stored credential is no longer available — reconnect this channel instead.")
        await runtime.manager.start(
            binding_id, binding["subagent_id"], credential_or_token, allowed, overrides, response_mode,
        )
        if platform == "matrix":
            runtime.manager.set_human_takeover_pause(
                binding_id, _clean_pause_minutes(binding.get("human_takeover_pause_minutes"))
            )
    except Exception as exc:
        mark_binding_failed(binding_id, str(exc))
        raise
    return _row_to_dict(_binding_or_raise(binding_id))


async def _reconnect_binding(
    binding_id: str, platform: str, credential_for_start: Any, bot_username: str, secret_value: str,
) -> dict[str, Any]:
    """Shared plumbing behind reconnect_*_binding(): swaps a binding's stored
    credential in place and restarts its live bot — same binding id, same
    prompt override/access mode/whitelist/response mode, no owner re-approval.
    A dead or rotated token isn't a new capability grant, just a refreshed one
    the owner already approved, so this deliberately skips create_review_task
    entirely (unlike create_*_binding_proposal)."""
    binding = _binding_or_raise(binding_id)
    if binding["platform"] != platform:
        raise ValueError(f"Binding {binding_id} is not a {platform} binding")

    runtime = _runtime_module_for_platform(platform)
    await runtime.manager.stop(binding_id)

    from backend.valkey_client import set_value

    set_value(binding["valkey_secret_key"], secret_value, ttl_seconds=None)
    with _connect() as connection:
        connection.execute(
            "UPDATE agent_messenger_bindings SET bot_username = ?, updated_at = ? WHERE id = ?",
            (bot_username, _now(), binding_id),
        )
    _set_status(binding_id, "active")  # also clears any stale last_error

    binding = _binding_or_raise(binding_id)
    overrides = {
        "system_prompt": binding.get("system_prompt_override"),
        "model": binding.get("model_override"),
        "model_provider": binding.get("model_provider_override"),
    }
    allowed = json.loads(binding.get("allowed_chat_ids") or "[]")
    response_mode = _clean_response_mode(binding.get("response_mode"))
    await runtime.manager.start(
        binding_id, binding["subagent_id"], credential_for_start, allowed, overrides, response_mode,
    )
    if platform == "matrix":
        runtime.manager.set_human_takeover_pause(
            binding_id, _clean_pause_minutes(binding.get("human_takeover_pause_minutes"))
        )
    return _row_to_dict(_binding_or_raise(binding_id))


async def reconnect_telegram_binding(binding_id: str, bot_token: str) -> dict[str, Any]:
    bot_username = _verify_bot_token(bot_token)
    clean_token = str(bot_token).strip()
    return await _reconnect_binding(binding_id, "telegram", clean_token, bot_username, clean_token)


async def reconnect_discord_binding(binding_id: str, bot_token: str) -> dict[str, Any]:
    bot_username = _verify_discord_token(bot_token)
    clean_token = str(bot_token).strip()
    return await _reconnect_binding(binding_id, "discord", clean_token, bot_username, clean_token)


async def reconnect_matrix_binding(
    binding_id: str, homeserver_url: str, user_id: str, password: str = "", access_token: str = "",
    refresh_token: str = "", device_flow_id: str = "",
) -> dict[str, Any]:
    credentials = await asyncio.to_thread(
        _resolve_matrix_credentials, homeserver_url, user_id, password, access_token, refresh_token, device_flow_id,
    )
    return await _reconnect_binding(
        binding_id, "matrix", credentials, credentials["user_id"], json.dumps(credentials)
    )


async def reconnect_slack_binding(binding_id: str, bot_token: str, app_token: str) -> dict[str, Any]:
    identity = _verify_slack_tokens(bot_token, app_token)
    credentials = {"bot_token": bot_token.strip(), "app_token": app_token.strip()}
    return await _reconnect_binding(binding_id, "slack", credentials, identity, json.dumps(credentials))


async def reconnect_email_binding(
    binding_id: str, imap_host: str, imap_port: int, smtp_host: str, smtp_port: int, address: str, password: str,
) -> dict[str, Any]:
    identity = _verify_email_credentials(imap_host, imap_port, smtp_host, smtp_port, address, password)
    credentials = {
        "imap_host": imap_host, "imap_port": int(imap_port),
        "smtp_host": smtp_host, "smtp_port": int(smtp_port),
        "address": address, "password": password,
    }
    return await _reconnect_binding(binding_id, "email", credentials, identity, json.dumps(credentials))


async def delete_binding_permanently(binding_id: str) -> None:
    """Actually removes a binding: stops its bot if running, destroys the stored
    credential, and deletes the row — unlike disable_binding(), this cannot be
    undone from the dashboard; the channel would need to be reconnected from
    scratch with fresh credentials."""
    binding = _binding_or_raise(binding_id)
    runtime = _runtime_module_for_platform(binding["platform"])
    await runtime.manager.stop(binding_id)
    from backend.valkey_client import delete_value
    delete_value(binding["valkey_secret_key"])
    with _connect() as connection:
        connection.execute("DELETE FROM agent_messenger_bindings WHERE id = ?", (binding_id,))


def _verify_discord_token(bot_token: str) -> str:
    """Calls Discord's /users/@me to confirm the token is real and fetch its username."""
    clean_token = str(bot_token).strip()
    try:
        response = httpx.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {clean_token}"},
            timeout=10,
        )
    except Exception as exc:
        raise ValueError(f"Could not reach Discord to verify the bot token: {exc}") from exc
    if response.status_code != 200:
        raise ValueError(f"Discord rejected this bot token (HTTP {response.status_code})")
    data = response.json()
    username = data.get("username")
    if not username:
        raise ValueError("Discord accepted the token but returned no bot username")
    return f"{username}#{data.get('discriminator', '0')}" if data.get("discriminator") not in (None, "0") else username


def create_discord_binding_proposal(
    subagent_id: str,
    bot_token: str,
    allowed_channel_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
    response_mode: str = DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    clean_response_mode = _clean_response_mode(response_mode)
    bot_username = _verify_discord_token(bot_token)
    clean_channel_ids = [str(c).strip() for c in (allowed_channel_ids or []) if str(c).strip()]

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'discord' AND status IN ('awaiting_approval', 'active') ORDER BY created_at DESC LIMIT 1",
            (subagent_id,),
        ).fetchone()
    if existing:
        raise ValueError(
            f"Agent '{subagent_id}' already has a {dict(existing)['status']} Discord binding — revoke it first"
        )

    from backend.control_plane import create_review_task

    binding_id = f"dcbind-{uuid.uuid4().hex[:12]}"
    secret_key = f"agent_discord_secret:{binding_id}"

    mode_note = (
        "DRAFT — every reply is queued for the owner to review and send by hand; nothing reaches the other person automatically."
        if clean_response_mode == "draft"
        else "AUTO-LABELED — replies send automatically, each one tagged as coming from the Vexa assistant, never silently as the owner."
    )
    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to its own Discord bot ({bot_username})",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "bot_username": bot_username,
            "allowed_channel_ids": clean_channel_ids or "unrestricted — any DM or channel this bot can see can reach the agent",
            "response_mode": mode_note,
            "secret_policy": "Bot token stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Discord confirmed the token belongs to a real bot",
            "Bot token never appears in the SQL database or application logs",
            "Once active, messages to this bot are answered ONLY by this one agent",
            "No reply is ever sent under the owner's identity without either a human send-click (draft mode) or an explicit assistant disclosure (auto-labeled mode)",
        ],
        rollback=f"Revoke the Discord binding for '{subagent['name']}' — stops the bot and deletes its token.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    set_value(secret_key, str(bot_token).strip(), ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override, response_mode)
            VALUES (?, ?, 'discord', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, bot_username, secret_key, json.dumps(clean_channel_ids), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None, clean_response_mode,
            ),
        )
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _row_to_dict(row)
    result["control_task"] = task
    return result


def get_discord_binding_proposal(control_task_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'discord' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


async def execute_approved_discord_binding(control_task_id: str) -> dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'discord' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Discord binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"Discord binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        token = get_value(binding["valkey_secret_key"])
        if not token:
            raise RuntimeError("Bot token expired before approval — re-create the binding")
        set_value(binding["valkey_secret_key"], token, ttl_seconds=None)
        _set_status(binding["id"], "active")

        from backend import agent_discord_bot

        allowed_channel_ids = json.loads(binding.get("allowed_chat_ids") or "[]")
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        response_mode = _clean_response_mode(binding.get("response_mode"))
        await agent_discord_bot.manager.start(
            binding["id"], binding["subagent_id"], token, allowed_channel_ids, overrides, response_mode
        )

        result = {"status": "active", "id": binding["id"], "bot_username": binding["bot_username"]}
    except Exception as exc:
        mark_binding_failed(binding["id"], str(exc))
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_discord_bindings(subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        if subagent_id:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'discord' ORDER BY created_at DESC",
                (subagent_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE platform = 'discord' ORDER BY created_at DESC"
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_discord_binding(binding_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE id = ? AND platform = 'discord'", (binding_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def resolve_discord_binding_token(binding_id: str) -> Optional[str]:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    return get_value(dict(row)["valkey_secret_key"])



def _verify_slack_tokens(bot_token: str, app_token: str) -> str:
    """Calls Slack's auth.test to confirm the bot token works, and sanity-checks
    the app-level token's shape (a real connectivity check happens when Socket
    Mode actually connects on activation)."""
    clean_bot_token = str(bot_token).strip()
    clean_app_token = str(app_token).strip()
    if not clean_bot_token.startswith("xoxb-"):
        raise ValueError("Bot token should start with 'xoxb-' (Slack app → OAuth & Permissions → Bot User OAuth Token)")
    if not clean_app_token.startswith("xapp-"):
        raise ValueError("App-level token should start with 'xapp-' (Slack app → Basic Information → App-Level Tokens, needs connections:write scope)")
    try:
        response = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {clean_bot_token}"},
            timeout=10,
        )
        data = response.json()
    except Exception as exc:
        raise ValueError(f"Could not reach Slack to verify the bot token: {exc}") from exc
    if not data.get("ok"):
        raise ValueError(f"Slack rejected this bot token: {data.get('error', 'unknown error')}")
    return f"{data.get('user', 'bot')} @ {data.get('team', 'workspace')}"


def create_slack_binding_proposal(
    subagent_id: str,
    bot_token: str,
    app_token: str,
    allowed_channel_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
    response_mode: str = DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    clean_response_mode = _clean_response_mode(response_mode)
    identity = _verify_slack_tokens(bot_token, app_token)
    clean_channel_ids = [str(c).strip() for c in (allowed_channel_ids or []) if str(c).strip()]

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'slack' AND status IN ('awaiting_approval', 'active') ORDER BY created_at DESC LIMIT 1",
            (subagent_id,),
        ).fetchone()
    if existing:
        raise ValueError(
            f"Agent '{subagent_id}' already has a {dict(existing)['status']} Slack binding — revoke it first"
        )

    from backend.control_plane import create_review_task

    binding_id = f"skbind-{uuid.uuid4().hex[:12]}"
    secret_key = f"agent_slack_secret:{binding_id}"

    mode_note = (
        "DRAFT — every reply is queued for the owner to review and send by hand; nothing reaches the other person automatically."
        if clean_response_mode == "draft"
        else "AUTO-LABELED — replies send automatically, each one tagged as coming from the Vexa assistant, never silently as the owner."
    )
    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to its own Slack bot ({identity})",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "slack_identity": identity,
            "allowed_channel_ids": clean_channel_ids or "unrestricted — any DM or channel this bot is in can reach the agent",
            "response_mode": mode_note,
            "secret_policy": "Tokens stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Slack confirmed the bot token belongs to a real app",
            "Tokens never appear in the SQL database or application logs",
            "Once active, messages to this bot are answered ONLY by this one agent",
            "No reply is ever sent under the owner's identity without either a human send-click (draft mode) or an explicit assistant disclosure (auto-labeled mode)",
        ],
        rollback=f"Revoke the Slack binding for '{subagent['name']}' — stops the Socket Mode connection and deletes its tokens.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    credentials_json = json.dumps({"bot_token": bot_token.strip(), "app_token": app_token.strip()})
    set_value(secret_key, credentials_json, ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override, response_mode)
            VALUES (?, ?, 'slack', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, identity, secret_key, json.dumps(clean_channel_ids), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None, clean_response_mode,
            ),
        )
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _row_to_dict(row)
    result["control_task"] = task
    return result


def get_slack_binding_proposal(control_task_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'slack' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


async def execute_approved_slack_binding(control_task_id: str) -> dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'slack' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Slack binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"Slack binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        credentials_json = get_value(binding["valkey_secret_key"])
        if not credentials_json:
            raise RuntimeError("Slack tokens expired before approval — re-create the binding")
        set_value(binding["valkey_secret_key"], credentials_json, ttl_seconds=None)
        _set_status(binding["id"], "active")

        from backend import agent_slack_bot

        allowed_channel_ids = json.loads(binding.get("allowed_chat_ids") or "[]")
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        response_mode = _clean_response_mode(binding.get("response_mode"))
        await agent_slack_bot.manager.start(
            binding["id"], binding["subagent_id"], json.loads(credentials_json), allowed_channel_ids,
            overrides, response_mode,
        )

        result = {"status": "active", "id": binding["id"], "slack_identity": binding["bot_username"]}
    except Exception as exc:
        mark_binding_failed(binding["id"], str(exc))
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_slack_bindings(subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        if subagent_id:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'slack' ORDER BY created_at DESC",
                (subagent_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE platform = 'slack' ORDER BY created_at DESC"
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_slack_binding(binding_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE id = ? AND platform = 'slack'", (binding_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def resolve_slack_binding_credentials(binding_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    credentials_json = get_value(dict(row)["valkey_secret_key"])
    return json.loads(credentials_json) if credentials_json else None



def _verify_email_credentials(
    imap_host: str, imap_port: int, smtp_host: str, smtp_port: int, address: str, password: str
) -> str:
    """Actually logs into both IMAP and SMTP with the given credentials before
    proposing — same 'prove it works' bar as Telegram/Matrix/Discord/Slack."""
    import imaplib
    import smtplib

    try:
        imap = imaplib.IMAP4_SSL(imap_host, int(imap_port))
        try:
            imap.login(address, password)
            imap.select("INBOX", readonly=True)
        finally:
            imap.logout()
    except Exception as exc:
        raise ValueError(f"IMAP login failed for {address}: {exc}") from exc

    try:
        smtp = smtplib.SMTP(smtp_host, int(smtp_port), timeout=10)
        try:
            smtp.starttls()
            smtp.login(address, password)
        finally:
            smtp.quit()
    except Exception as exc:
        raise ValueError(f"SMTP login failed for {address}: {exc}") from exc

    return address


def create_email_binding_proposal(
    subagent_id: str,
    imap_host: str,
    imap_port: int,
    smtp_host: str,
    smtp_port: int,
    address: str,
    password: str,
    allowed_senders: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
    response_mode: str = DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    clean_response_mode = _clean_response_mode(response_mode)
    identity = _verify_email_credentials(imap_host, imap_port, smtp_host, smtp_port, address, password)
    clean_senders = [str(s).strip().lower() for s in (allowed_senders or []) if str(s).strip()]

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'email' AND status IN ('awaiting_approval', 'active') ORDER BY created_at DESC LIMIT 1",
            (subagent_id,),
        ).fetchone()
    if existing:
        raise ValueError(
            f"Agent '{subagent_id}' already has a {dict(existing)['status']} email binding — revoke it first"
        )

    from backend.control_plane import create_review_task

    binding_id = f"mlbind-{uuid.uuid4().hex[:12]}"
    secret_key = f"agent_email_secret:{binding_id}"

    mode_note = (
        "DRAFT — every reply is queued for the owner to review and send by hand; nothing reaches the other person automatically."
        if clean_response_mode == "draft"
        else "AUTO-LABELED — replies send automatically, each one tagged as coming from the Vexa assistant, never silently as the owner."
    )
    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to the mailbox {address}",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "mailbox": address,
            "allowed_senders": clean_senders or "unrestricted — ANY sender that reaches this inbox can trigger a reply (strongly recommend restricting this)",
            "response_mode": mode_note,
            "secret_policy": "Mailbox password stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "IMAP and SMTP login both succeeded with the given credentials",
            "Password never appears in the SQL database or application logs",
            "Once active, this mailbox is polled and replied to ONLY by this one agent",
            "No reply is ever sent under the owner's identity without either a human send-click (draft mode) or an explicit assistant disclosure (auto-labeled mode)",
        ],
        rollback=f"Revoke the email binding for '{subagent['name']}' — stops polling and deletes the stored password.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    credentials_json = json.dumps({
        "imap_host": imap_host, "imap_port": int(imap_port),
        "smtp_host": smtp_host, "smtp_port": int(smtp_port),
        "address": address, "password": password,
    })
    set_value(secret_key, credentials_json, ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override, response_mode)
            VALUES (?, ?, 'email', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, identity, secret_key, json.dumps(clean_senders), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None, clean_response_mode,
            ),
        )
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _row_to_dict(row)
    result["control_task"] = task
    return result


def get_email_binding_proposal(control_task_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'email' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


async def execute_approved_email_binding(control_task_id: str) -> dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform = 'email' ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Email binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"Email binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        credentials_json = get_value(binding["valkey_secret_key"])
        if not credentials_json:
            raise RuntimeError("Mailbox credentials expired before approval — re-create the binding")
        set_value(binding["valkey_secret_key"], credentials_json, ttl_seconds=None)
        _set_status(binding["id"], "active")

        from backend import agent_email_channel

        allowed_senders = json.loads(binding.get("allowed_chat_ids") or "[]")
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        response_mode = _clean_response_mode(binding.get("response_mode"))
        await agent_email_channel.manager.start(
            binding["id"], binding["subagent_id"], json.loads(credentials_json), allowed_senders,
            overrides, response_mode,
        )

        result = {"status": "active", "id": binding["id"], "mailbox": binding["bot_username"]}
    except Exception as exc:
        mark_binding_failed(binding["id"], str(exc))
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_email_bindings(subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        if subagent_id:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = 'email' ORDER BY created_at DESC",
                (subagent_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE platform = 'email' ORDER BY created_at DESC"
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_email_binding(binding_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE id = ? AND platform = 'email'", (binding_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def resolve_email_binding_credentials(binding_id: str) -> Optional[dict[str, Any]]:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    credentials_json = get_value(dict(row)["valkey_secret_key"])
    return json.loads(credentials_json) if credentials_json else None


# ── Social networks: VK, Instagram, Facebook, WhatsApp ──────────────────────
#
# The five messenger platforms above each carry their own hand-written
# create/get/execute/list/resolve quartet. Four more copies of that shape would
# be ~800 lines of near-identical code, so the social channels are declared as
# data here and share one generic implementation. Everything around them is
# deliberately unchanged: the same R3 owner approval, the same Valkey-only
# secret, the same draft/auto-labeled disclosure rule, the same access gate.
# An Instagram account reaching an agent is exactly the capability grant a
# Telegram bot reaching it is, and is governed identically.
#
# The one structural difference is *how messages arrive*. VK is polled (Bots
# Long Poll, works behind NAT like every other channel here). The three Meta
# platforms have no polling API at all — they push to a webhook, so those
# bindings only receive anything once backend/social_webhooks.py is reachable
# from the internet and the account id is registered in the Meta app. That is
# a deployment fact, not a binding state: such a binding still activates, and
# its health check still proves the credential works.

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method"

# Per-platform declaration. `account_field` is the credential that identifies
# the account to the outside world — for the webhook platforms it is also the
# id Meta puts in the payload, which is how an incoming event finds its binding
# (see find_social_binding_by_account).
SOCIAL_PLATFORMS: dict[str, dict[str, Any]] = {
    "vk": {
        "label": "VKontakte",
        "id_prefix": "vkbind",
        "secret_prefix": "agent_vk_secret",
        "runtime": "backend.agent_vk_bot",
        "required": ("access_token", "group_id"),
        "account_field": "group_id",
        "delivery": "poll",
        "credential_note": "Ключ доступа сообщества и его id хранятся только в Valkey, никогда в SQL",
    },
    "instagram": {
        "label": "Instagram",
        "id_prefix": "igbind",
        "secret_prefix": "agent_instagram_secret",
        "runtime": "backend.agent_meta_channel",
        "required": ("access_token", "ig_user_id", "app_secret"),
        "account_field": "ig_user_id",
        "delivery": "webhook",
        "credential_note": "Page access token и app secret хранятся только в Valkey, никогда в SQL",
    },
    "facebook": {
        "label": "Facebook",
        "id_prefix": "fbbind",
        "secret_prefix": "agent_facebook_secret",
        "runtime": "backend.agent_meta_channel",
        "required": ("access_token", "page_id", "app_secret"),
        "account_field": "page_id",
        "delivery": "webhook",
        "credential_note": "Page access token и app secret хранятся только в Valkey, никогда в SQL",
    },
    "whatsapp": {
        "label": "WhatsApp Business",
        "id_prefix": "wabind",
        "secret_prefix": "agent_whatsapp_secret",
        "runtime": "backend.agent_whatsapp_channel",
        "required": ("access_token", "phone_number_id", "app_secret"),
        "account_field": "phone_number_id",
        "delivery": "webhook",
        "credential_note": "Cloud API token и app secret хранятся только в Valkey, никогда в SQL",
    },
}

SOCIAL_PLATFORM_IDS = tuple(SOCIAL_PLATFORMS)


def is_social_platform(platform: str) -> bool:
    return platform in SOCIAL_PLATFORMS


def _graph_get(path: str, access_token: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    """One Graph API read with Meta's error envelope turned into a ValueError the
    dashboard can show verbatim — Meta's `error.message` already says exactly
    what is wrong (expired token, missing permission, wrong id)."""
    try:
        response = httpx.get(
            f"{GRAPH_API_BASE}/{path.lstrip('/')}",
            params={**params, "access_token": access_token},
            timeout=15,
        )
        data = response.json()
    except Exception as exc:
        raise ValueError(f"Не удалось связаться с Meta Graph API для проверки {what}: {exc}") from exc
    if isinstance(data, dict) and data.get("error"):
        message = (data["error"] or {}).get("message") or "unknown error"
        raise ValueError(f"Meta отклонила эти данные ({what}): {message}")
    if not isinstance(data, dict):
        raise ValueError(f"Meta вернула неожиданный ответ при проверке {what}")
    return data


def _verify_vk_credentials(credentials: dict[str, Any]) -> tuple[str, str]:
    """groups.getById with the community key — proves the key is live, belongs to
    this community, and returns its human-readable name for the binding row."""
    access_token = str(credentials.get("access_token") or "").strip()
    group_id = str(credentials.get("group_id") or "").strip().lstrip("-")
    if not access_token or not group_id:
        raise ValueError("Нужны access_token сообщества и group_id")
    try:
        response = httpx.get(
            f"{VK_API_BASE}/groups.getById",
            params={"group_id": group_id, "access_token": access_token, "v": VK_API_VERSION},
            timeout=15,
        )
        data = response.json()
    except Exception as exc:
        raise ValueError(f"Не удалось связаться с VK API для проверки ключа: {exc}") from exc
    if data.get("error"):
        message = (data["error"] or {}).get("error_msg") or "unknown error"
        raise ValueError(f"VK отклонил этот ключ доступа: {message}")
    # VK 5.199 returns {"response": {"groups": [...]}}; older shapes returned a
    # bare list. Both are accepted so a pinned API version elsewhere can't break
    # connecting a channel.
    payload = data.get("response") or {}
    groups = payload.get("groups") if isinstance(payload, dict) else payload
    if not groups:
        raise ValueError("VK принял ключ, но не вернул сообщество — проверьте group_id")
    group = groups[0]
    name = group.get("name") or group.get("screen_name") or group_id
    resolved_id = str(group.get("id") or group_id)
    return f"{name} (@{group.get('screen_name') or resolved_id})", resolved_id


def _verify_instagram_credentials(credentials: dict[str, Any]) -> tuple[str, str]:
    access_token = str(credentials.get("access_token") or "").strip()
    ig_user_id = str(credentials.get("ig_user_id") or "").strip()
    if not access_token or not ig_user_id:
        raise ValueError("Нужны access_token и ig_user_id (Instagram Business account id)")
    data = _graph_get(ig_user_id, access_token, {"fields": "username,name"}, "аккаунта Instagram")
    username = data.get("username") or data.get("name")
    if not username:
        raise ValueError("Meta приняла токен, но не вернула username — это точно Instagram Business аккаунт?")
    return f"@{username}", ig_user_id


def _verify_facebook_credentials(credentials: dict[str, Any]) -> tuple[str, str]:
    access_token = str(credentials.get("access_token") or "").strip()
    page_id = str(credentials.get("page_id") or "").strip()
    if not access_token or not page_id:
        raise ValueError("Нужны page access token и page_id")
    data = _graph_get(page_id, access_token, {"fields": "name"}, "страницы Facebook")
    name = data.get("name")
    if not name:
        raise ValueError("Meta приняла токен, но не вернула название страницы")
    return name, page_id


def _verify_whatsapp_credentials(credentials: dict[str, Any]) -> tuple[str, str]:
    access_token = str(credentials.get("access_token") or "").strip()
    phone_number_id = str(credentials.get("phone_number_id") or "").strip()
    if not access_token or not phone_number_id:
        raise ValueError("Нужны access_token и phone_number_id из WhatsApp Cloud API")
    data = _graph_get(
        phone_number_id, access_token, {"fields": "display_phone_number,verified_name"}, "номера WhatsApp"
    )
    phone = data.get("display_phone_number") or phone_number_id
    name = data.get("verified_name")
    return f"{phone}{f' ({name})' if name else ''}", phone_number_id


_SOCIAL_VERIFIERS = {
    "vk": _verify_vk_credentials,
    "instagram": _verify_instagram_credentials,
    "facebook": _verify_facebook_credentials,
    "whatsapp": _verify_whatsapp_credentials,
}


def _social_spec(platform: str) -> dict[str, Any]:
    spec = SOCIAL_PLATFORMS.get(platform)
    if not spec:
        raise ValueError(f"Unknown social platform: {platform}")
    return spec


def _clean_social_credentials(platform: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """Whitespace-trims every field, drops empties, and fails loudly on a missing
    required one — so a half-filled form never reaches the provider as a
    confusing 'invalid OAuth token' from Meta."""
    spec = _social_spec(platform)
    clean = {key: str(value).strip() for key, value in (credentials or {}).items() if str(value or "").strip()}
    missing = [field for field in spec["required"] if not clean.get(field)]
    if missing:
        raise ValueError(f"Не заполнены обязательные поля: {', '.join(missing)}")
    if spec["delivery"] == "webhook" and not clean.get("verify_token"):
        # Meta asks for a verify token when the webhook is registered; there is
        # nothing for the owner to choose here, so one is generated rather than
        # made a form field they have to invent a value for.
        clean["verify_token"] = uuid.uuid4().hex
    return clean


def verify_social_credentials(platform: str, credentials: dict[str, Any]) -> tuple[str, str]:
    """(display identity, external account id) — raises ValueError if the
    provider rejects the credential. Same live-check bar every other platform
    clears before a proposal is even created."""
    return _SOCIAL_VERIFIERS[platform](_clean_social_credentials(platform, credentials))


def create_social_binding_proposal(
    platform: str,
    subagent_id: str,
    credentials: dict[str, Any],
    allowed_chat_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
    response_mode: str = DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    """Validates the account credential live, then opens the same single-owner
    (R3) approval every other channel goes through. Nothing is polled, no
    webhook is honoured, and no message is answered until that is approved."""
    _init_schema()
    spec = _social_spec(platform)
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    clean_response_mode = _clean_response_mode(response_mode)
    clean_credentials = _clean_social_credentials(platform, credentials)
    identity, account_id = _SOCIAL_VERIFIERS[platform](clean_credentials)
    clean_chat_ids = [str(c).strip() for c in (allowed_chat_ids or []) if str(c).strip()]

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? AND platform = ? "
            "AND status IN ('awaiting_approval', 'active') ORDER BY created_at DESC LIMIT 1",
            (subagent_id, platform),
        ).fetchone()
    if existing:
        raise ValueError(
            f"Agent '{subagent_id}' already has a {dict(existing)['status']} {spec['label']} binding — revoke it first"
        )

    from backend.control_plane import create_review_task

    binding_id = f"{spec['id_prefix']}-{uuid.uuid4().hex[:12]}"
    secret_key = f"{spec['secret_prefix']}:{binding_id}"

    mode_note = (
        "DRAFT — every reply is queued for the owner to review and send by hand; nothing reaches the other person automatically."
        if clean_response_mode == "draft"
        else "AUTO-LABELED — replies send automatically, each one tagged as coming from the Vexa assistant, never silently as the owner."
    )
    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to the {spec['label']} account {identity}",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "platform": spec["label"],
            "account": identity,
            "account_id": account_id,
            "delivery": (
                "Bots Long Poll — the backend polls the community, nothing has to be reachable from the internet"
                if spec["delivery"] == "poll"
                else "Meta webhook — messages only arrive once this backend is reachable from the internet and the "
                     "account is subscribed in the Meta app"
            ),
            "allowed_chat_ids": clean_chat_ids or "unrestricted — anyone who writes to this account can reach the agent",
            "response_mode": mode_note,
            "secret_policy": spec["credential_note"],
        },
        risk_class="R3",
        acceptance=[
            f"{spec['label']} confirmed the credential belongs to this account",
            "Credentials never appear in the SQL database or application logs",
            "Once active, messages to this account are answered ONLY by this one agent",
            "No reply is ever sent under the owner's identity without either a human send-click (draft mode) or an explicit assistant disclosure (auto-labeled mode)",
        ],
        rollback=f"Revoke the {spec['label']} binding for '{subagent['name']}' — stops the channel and deletes its credentials.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    set_value(secret_key, json.dumps(clean_credentials), ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override, response_mode,
                 external_account_id)
            VALUES (?, ?, ?, ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, platform, identity, secret_key, json.dumps(clean_chat_ids),
                task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None, clean_response_mode, account_id,
            ),
        )
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _row_to_dict(row)
    result["control_task"] = task
    return result


def get_social_binding_proposal(control_task_id: str) -> Optional[dict[str, Any]]:
    """One lookup covering all four social platforms — approval_dispatch asks
    this once instead of four times."""
    _init_schema()
    placeholders = ", ".join("?" for _ in SOCIAL_PLATFORM_IDS)
    with _connect() as connection:
        row = connection.execute(
            f"SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (control_task_id, *SOCIAL_PLATFORM_IDS),
        ).fetchone()
    return _row_to_dict(row) if row else None


async def execute_approved_social_binding(control_task_id: str) -> dict[str, Any]:
    _init_schema()
    placeholders = ", ".join("?" for _ in SOCIAL_PLATFORM_IDS)
    with _connect() as connection:
        row = connection.execute(
            f"SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? AND platform IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (control_task_id, *SOCIAL_PLATFORM_IDS),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)
    platform = binding["platform"]
    spec = _social_spec(platform)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError(f"{spec['label']} binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"{spec['label']} binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        credentials_json = get_value(binding["valkey_secret_key"])
        if not credentials_json:
            raise RuntimeError("Credentials expired before approval — re-create the binding")
        set_value(binding["valkey_secret_key"], credentials_json, ttl_seconds=None)
        _set_status(binding["id"], "active")

        runtime = _runtime_module_for_platform(platform)
        overrides = {
            "system_prompt": binding.get("system_prompt_override"),
            "model": binding.get("model_override"),
            "model_provider": binding.get("model_provider_override"),
        }
        await runtime.manager.start(
            binding["id"], binding["subagent_id"], json.loads(credentials_json),
            json.loads(binding.get("allowed_chat_ids") or "[]"), overrides,
            _clean_response_mode(binding.get("response_mode")),
        )
        result = {"status": "active", "id": binding["id"], "account": binding["bot_username"]}
    except Exception as exc:
        mark_binding_failed(binding["id"], str(exc))
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_social_bindings(platform: Optional[str] = None, subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every social binding, or only one platform's / one agent's."""
    _init_schema()
    platforms = (platform,) if platform else SOCIAL_PLATFORM_IDS
    placeholders = ", ".join("?" for _ in platforms)
    query = f"SELECT * FROM agent_messenger_bindings WHERE platform IN ({placeholders})"
    params: list[Any] = list(platforms)
    if subagent_id:
        query += " AND subagent_id = ?"
        params.append(subagent_id)
    with _connect() as connection:
        rows = connection.execute(query + " ORDER BY created_at DESC", params).fetchall()
    return [_row_to_dict(row) for row in rows]


def resolve_social_binding_credentials(binding_id: str) -> Optional[dict[str, Any]]:
    """The stored credential dict, and only for a binding that is currently
    active — same guard the other platforms' resolvers use, so a disabled
    channel's secret stays unreadable to the rest of the backend."""
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    credentials_json = get_value(dict(row)["valkey_secret_key"])
    return json.loads(credentials_json) if credentials_json else None


def find_social_binding_by_account(platform: str, external_account_id: str) -> Optional[dict[str, Any]]:
    """Which binding owns an incoming webhook event. Only ever returns an active
    one: a revoked account must not keep answering just because Meta is still
    delivering to the old subscription."""
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM agent_messenger_bindings WHERE platform = ? AND external_account_id = ? "
            "AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            (platform, str(external_account_id)),
        ).fetchone()
    return _row_to_dict(row) if row else None


def social_webhook_verify_token(platform: str, token: str) -> Optional[dict[str, Any]]:
    """The binding whose registered verify token matches — Meta's GET handshake
    carries no account id, so this is the only thing it can be matched on."""
    candidate = str(token or "").strip()
    if not candidate:
        return None
    for binding in list_social_bindings(platform):
        if binding.get("status") != "active":
            continue
        credentials = resolve_social_binding_credentials(binding["id"]) or {}
        if credentials.get("verify_token") and credentials["verify_token"] == candidate:
            return binding
    return None


def social_webhook_details(binding_id: str) -> dict[str, Any]:
    """What the owner has to paste into the Meta app console: the callback URL
    and this binding's verify token. Deliberately its own call rather than a
    field on the binding row — the verify token is a credential and stays out
    of the general list endpoint."""
    binding = _binding_or_raise(binding_id)
    platform = binding["platform"]
    spec = _social_spec(platform)
    if spec["delivery"] != "webhook":
        raise ValueError(f"{spec['label']} не использует webhook — сообщения забираются опросом")
    credentials = resolve_social_binding_credentials(binding_id) or {}

    from backend.database import get_setting

    base_url = (get_setting("smm_public_base_url") or "").strip().rstrip("/")
    path = f"/api/social/webhook/{platform}"
    return {
        "platform": platform,
        "callback_url": f"{base_url}{path}" if base_url else path,
        "base_url_configured": bool(base_url),
        "verify_token": credentials.get("verify_token", ""),
        "account_id": binding.get("external_account_id") or "",
        "app_secret_set": bool(credentials.get("app_secret")),
    }


async def reconnect_social_binding(binding_id: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """Swaps in a fresh credential (an expired Meta token, a rotated VK key) and
    restarts the channel — no new owner approval, same reasoning as
    _reconnect_binding for the messenger platforms."""
    binding = _binding_or_raise(binding_id)
    platform = binding["platform"]
    spec = _social_spec(platform)
    clean = _clean_social_credentials(platform, credentials)
    # A regenerated verify token would silently break an already-registered
    # webhook, so the existing one is carried over unless it was given explicitly.
    if spec["delivery"] == "webhook" and not str((credentials or {}).get("verify_token") or "").strip():
        existing = resolve_social_binding_credentials(binding_id) or {}
        if existing.get("verify_token"):
            clean["verify_token"] = existing["verify_token"]
    identity, account_id = _SOCIAL_VERIFIERS[platform](clean)
    with _connect() as connection:
        connection.execute(
            "UPDATE agent_messenger_bindings SET external_account_id = ?, updated_at = ? WHERE id = ?",
            (account_id, _now(), binding_id),
        )
    return await _reconnect_binding(binding_id, platform, clean, identity, json.dumps(clean))


async def _health_check_matrix_binding(binding: dict[str, Any]) -> None:
    """Matrix gets its own sweep, because on a MAS homeserver the stored token
    is *expected* to be dead minutes after it was written, and because the old
    generic path used to kill healthy channels in two ways:

    * it verified the Valkey copy of the token and then refreshed on its own,
      racing the bot's renewal loop for the same single-use refresh token;
    * a network blip or a 5xx from the homeserver counted as a dead credential.

    Now the running bot checks itself under its renewal lock, only a refresh the
    server actually refused fails the binding, and a binding that is active but
    not running (its start was postponed by an outage) gets started again."""
    from backend import agent_matrix_bot, channel_activity
    from backend.matrix_oauth import MatrixTransientError

    manager = agent_matrix_bot.manager
    binding_id = binding["id"]

    if manager.is_running(binding_id):
        outcome = await manager.check_health(binding_id)
        if outcome in (agent_matrix_bot.RENEW_OK, agent_matrix_bot.RENEW_TRANSIENT):
            if outcome == agent_matrix_bot.RENEW_TRANSIENT:
                channel_activity.record(
                    binding_id, "matrix", "warning", "Плановая проверка: Matrix временно недоступен, канал не трогаю",
                )
            return
        reason = "Плановая проверка связи не прошла: Matrix отклонил токен, продлить его не удалось. Переподключите канал."
        mark_binding_failed(binding_id, reason)
        channel_activity.record(binding_id, "matrix", "error", reason)
        await manager.stop(binding_id)
        return

    credentials = resolve_matrix_binding_credentials(binding_id)
    if not credentials:
        return
    overrides = {
        "system_prompt": binding.get("system_prompt_override"),
        "model": binding.get("model_override"),
        "model_provider": binding.get("model_provider_override"),
    }
    allowed = binding.get("allowed_chat_ids")
    if isinstance(allowed, str):
        allowed = json.loads(allowed or "[]")
    try:
        await manager.start(
            binding_id, binding["subagent_id"], credentials, allowed, overrides,
            _clean_response_mode(binding.get("response_mode")),
        )
        manager.set_human_takeover_pause(binding_id, _clean_pause_minutes(binding.get("human_takeover_pause_minutes")))
        channel_activity.record(binding_id, "matrix", "info", "Канал снова запущен после недоступности Matrix")
    except MatrixTransientError as exc:
        channel_activity.record(binding_id, "matrix", "warning", f"Запуск снова отложен: {exc}")
    except Exception as exc:
        logger.exception("Health check could not restart matrix bot %s", binding_id)
        reason = f"Плановая проверка связи не прошла: {exc}"
        mark_binding_failed(binding_id, reason)
        channel_activity.record(binding_id, "matrix", "error", reason)


async def health_check_all_active() -> None:
    """Periodic liveness probe for every active binding across all 5 platforms
    — catches a dead credential (an expired Matrix token, a revoked bot token)
    proactively instead of waiting for the admin to notice a message never
    arrived. Read-only towards each running bot: it never restarts one, it
    just re-runs the same 'prove this credential still works' check used at
    connect time, and calls mark_binding_failed() if that check now fails."""
    from backend import channel_activity

    checks: list[tuple[str, Any, Any, Any]] = [
        ("telegram", list_telegram_bindings, resolve_telegram_binding_token, _verify_bot_token),
        ("discord", list_discord_bindings, resolve_discord_binding_token, _verify_discord_token),
        ("matrix", list_matrix_bindings, resolve_matrix_binding_credentials, _verify_matrix_credentials),
        (
            "slack", list_slack_bindings, resolve_slack_binding_credentials,
            lambda creds: _verify_slack_tokens(creds["bot_token"], creds["app_token"]),
        ),
        (
            "email", list_email_bindings, resolve_email_binding_credentials,
            lambda creds: _verify_email_credentials(
                creds["imap_host"], creds["imap_port"], creds["smtp_host"], creds["smtp_port"],
                creds["address"], creds["password"],
            ),
        ),
    ]
    # The social channels share one verifier signature, so they are appended
    # from their own registry rather than spelled out four more times.
    for social_platform in SOCIAL_PLATFORM_IDS:
        checks.append((
            social_platform,
            lambda platform=social_platform: list_social_bindings(platform),
            resolve_social_binding_credentials,
            lambda creds, platform=social_platform: _SOCIAL_VERIFIERS[platform](creds),
        ))
    for platform, list_fn, resolve_fn, verify_fn in checks:
        for binding in list_fn():
            if binding.get("status") != "active":
                continue
            if platform == "matrix":
                await _health_check_matrix_binding(binding)
                continue
            credential = resolve_fn(binding["id"])
            if not credential:
                continue
            try:
                await asyncio.to_thread(verify_fn, credential)
            except Exception as exc:
                reason = f"Плановая проверка связи не прошла: {exc}"
                mark_binding_failed(binding["id"], reason)
                channel_activity.record(binding["id"], platform, "error", reason)
                try:
                    runtime = _runtime_module_for_platform(platform)
                    await runtime.manager.stop(binding["id"])
                except Exception:
                    logger.exception("Failed to stop %s bot %s after failed health check", platform, binding["id"])


_init_schema()
