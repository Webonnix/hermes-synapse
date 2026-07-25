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

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.database import DB_PATH

_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_PENDING_SECRET_TTL_SECONDS = 24 * 60 * 60  # abandoned proposals self-clean after a day


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
    """Raises ValueError if the subagent's tier has opted out of messenger bindings."""
    tier_id = subagent.get("tier_id")
    if not tier_id:
        return
    from backend.agent_tiers import get_tier

    tier = get_tier(tier_id)
    if tier and not tier["allow_messenger"]:
        raise ValueError(f"Tier '{tier['name']}' does not allow messenger bindings for this agent")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("valkey_secret_key", None)  # never surfaced outside this module
    try:
        item["allowed_chat_ids"] = json.loads(item.get("allowed_chat_ids") or "[]")
    except Exception:
        item["allowed_chat_ids"] = []
    return item


def create_telegram_binding_proposal(
    subagent_id: str,
    bot_token: str,
    allowed_chat_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

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

    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to its own Telegram bot (@{bot_username})",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "bot_username": bot_username,
            "allowed_chat_ids": clean_chat_ids or "unrestricted — anyone who finds the bot can message it",
            "secret_policy": "Bot token stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Telegram confirmed the token belongs to a real bot",
            "Bot token never appears in the SQL database or application logs",
            "Once active, messages to this bot are answered ONLY by this one agent",
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
                 system_prompt_override, model_override, model_provider_override)
            VALUES (?, ?, 'telegram', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, bot_username, secret_key, json.dumps(clean_chat_ids), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None,
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
            "SELECT * FROM agent_messenger_bindings WHERE control_task_id = ? ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def _set_status(binding_id: str, status: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE agent_messenger_bindings SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), binding_id),
        )


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
        await agent_bot.manager.start(binding["id"], binding["subagent_id"], token, allowed_chat_ids, overrides)

        result = {"status": "active", "id": binding["id"], "bot_username": binding["bot_username"]}
    except Exception as exc:
        _set_status(binding["id"], "failed")
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_telegram_bindings(subagent_id: Optional[str] = None) -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        if subagent_id:
            rows = connection.execute(
                "SELECT * FROM agent_messenger_bindings WHERE subagent_id = ? ORDER BY created_at DESC",
                (subagent_id,),
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM agent_messenger_bindings ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(row) for row in rows]


def get_telegram_binding(binding_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    return _row_to_dict(row) if row else None


def resolve_telegram_binding_token(binding_id: str) -> Optional[str]:
    """Returns the bot token for an active binding, or None if inactive/missing."""
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_messenger_bindings WHERE id = ?", (binding_id,)).fetchone()
    if not row or dict(row)["status"] != "active":
        return None
    from backend.valkey_client import get_value

    return get_value(dict(row)["valkey_secret_key"])


async def revoke_telegram_binding(binding_id: str) -> None:
    binding = get_telegram_binding(binding_id)
    if not binding:
        raise KeyError(binding_id)
    from backend import agent_bot
    from backend.valkey_client import delete_value

    await agent_bot.manager.stop(binding_id)
    delete_value(f"agent_bot_secret:{binding_id}")
    _set_status(binding_id, "revoked")


def _verify_matrix_credentials(
    homeserver_url: str, user_id: str, password: str = "", access_token: str = ""
) -> tuple[str, str]:
    """Logs into Matrix (or confirms an existing access token) to prove the credential
    works before proposing. Returns (resolved_user_id, access_token)."""
    from nio import AsyncClient, LoginError, WhoamiError

    async def _run() -> tuple[str, str]:
        client = AsyncClient(homeserver_url, user_id)
        try:
            if access_token:
                client.access_token = access_token
                client.user_id = user_id
                response = await client.whoami()
                if isinstance(response, WhoamiError):
                    raise ValueError(f"Matrix rejected this access token: {response.message}")
                return response.user_id, access_token
            if not password:
                raise ValueError("Provide either a password or an access_token")
            response = await client.login(password)
            if isinstance(response, LoginError):
                raise ValueError(f"Matrix login failed: {response.message}")
            return response.user_id, response.access_token
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not reach the Matrix homeserver: {exc}") from exc
        finally:
            await client.close()

    return asyncio.run(_run())


def create_matrix_binding_proposal(
    subagent_id: str,
    homeserver_url: str,
    user_id: str,
    password: str = "",
    access_token: str = "",
    allowed_room_ids: Optional[list[str]] = None,
    system_prompt_override: str = "",
    model_override: str = "",
    model_provider_override: str = "",
) -> dict[str, Any]:
    _init_schema()
    from backend.database import get_subagent

    subagent = get_subagent(subagent_id)
    if not subagent:
        raise ValueError(f"No such agent: {subagent_id}")
    _check_messenger_allowed(subagent)

    resolved_user_id, resolved_token = _verify_matrix_credentials(
        homeserver_url, user_id, password=password, access_token=access_token
    )
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

    task = create_review_task(
        goal=f"Connect agent '{subagent['name']}' to its own Matrix account ({resolved_user_id})",
        arguments={
            "subagent_id": subagent_id,
            "subagent_name": subagent["name"],
            "matrix_user_id": resolved_user_id,
            "homeserver_url": homeserver_url,
            "allowed_room_ids": clean_room_ids or "unrestricted — any room this account is in can reach the agent",
            "secret_policy": "Access token stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Matrix confirmed the credential belongs to a real account",
            "Access token never appears in the SQL database or application logs",
            "Once active, messages to this account are answered ONLY by this one agent",
        ],
        rollback=f"Revoke the Matrix binding for '{subagent['name']}' — stops the sync loop and deletes its token.",
        requester="autonomy:agent_messenger",
    )

    from backend.valkey_client import set_value

    now = _now()
    credentials_json = json.dumps({"homeserver_url": homeserver_url, "user_id": resolved_user_id, "access_token": resolved_token})
    set_value(secret_key, credentials_json, ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at,
                 system_prompt_override, model_override, model_provider_override)
            VALUES (?, ?, 'matrix', ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, subagent_id, resolved_user_id, secret_key, json.dumps(clean_room_ids), task["id"], now, now,
                system_prompt_override.strip() or None, model_override.strip() or None,
                model_provider_override.strip() or None,
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
        await agent_matrix_bot.manager.start(
            binding["id"], binding["subagent_id"], json.loads(credentials_json), allowed_room_ids
        )

        result = {"status": "active", "id": binding["id"], "matrix_user_id": binding["bot_username"]}
    except Exception as exc:
        _set_status(binding["id"], "failed")
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


async def revoke_matrix_binding(binding_id: str) -> None:
    binding = get_matrix_binding(binding_id)
    if not binding:
        raise KeyError(binding_id)
    from backend import agent_matrix_bot
    from backend.valkey_client import delete_value

    await agent_matrix_bot.manager.stop(binding_id)
    delete_value(binding.get("valkey_secret_key") or f"agent_matrix_secret:{binding_id}")
    _set_status(binding_id, "revoked")


_init_schema()
