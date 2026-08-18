"""Access tokens, plans, subscriber records and per-token usage accounting for
public-facing agent bots.

The messenger bindings in ``agent_messenger_governance.py`` were built for the
owner's own channels: a binding either whitelists specific chat ids or answers
nobody. This module adds the other mode — a binding switched to
``access_mode='token'`` becomes a *social* bot anyone can start, but the first
thing it asks for is an access token. Redeeming a token materialises a
subscriber record ("учётка"): its own conversation, its own card of notes, and
its own spend ledger.

Three layers of limit stack on top of each other, cheapest check first:

1. **rate limit** — messages per minute, in Valkey (falls back to an in-process
   counter when Valkey is down, so the gate never fails open on infrastructure);
2. **token quota** — USD, LLM tokens and message count over a rolling period,
   kept as running counters on the token row so a check is O(1);
3. **agent budget** — the pre-existing ``subagents.budget_usd_limit`` gate in
   ``agent.py::_respond_as_subagent``, which protects the owner's wallet no
   matter how many tokens are outstanding.

Plaintext tokens are never stored: only a SHA-256 hash plus a short display
prefix. A newly issued token's plaintext is returned exactly once, at creation.

Like ``agent_tiers.py`` this is plain owner-only CRUD rather than a Control
Plane proposal — issuing a token grants no new external capability and holds no
credential. Flipping a *binding* to token mode does widen exposure, so that
switch lives in ``agent_messenger_governance.py`` and notifies the owner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.database import DB_PATH

logger = logging.getLogger("hermes.bot_access")

TOKEN_PREFIX = "HRM-"
TOKEN_BODY_LENGTH = 28  # base62 ⇒ ~166 bits, far past brute-forcing over a chat
_TOKEN_ALPHABET = string.ascii_letters + string.digits

# No trailing \b: a token may legitimately end in '-' or '_' (older tokens were
# generated with token_urlsafe), and \b after a non-word character never
# matches — which would silently reject a perfectly valid token. The leading
# look-behind does the same job as \b without that trap.
TOKEN_PATTERN = re.compile(rf"(?<![A-Za-z0-9_-]){TOKEN_PREFIX}[A-Za-z0-9_-]{{22,}}")

PERIODS = ("daily", "weekly", "monthly", "lifetime")
TOKEN_STATUSES = ("active", "suspended", "revoked")
SUBSCRIBER_STATUSES = ("active", "blocked")
SUBSCRIPTION_STATUSES = ("active", "expired", "canceled")

# How long one paid period lasts, when a plan doesn't override it. 'lifetime'
# has no end date at all — it is a one-off purchase, not a subscription.
DEFAULT_DURATION_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "lifetime": None}

DEFAULT_RATE_LIMIT_PER_MIN = 6
DEFAULT_MAX_MESSAGE_CHARS = 2000

# How many wrong token guesses a chat gets before it is muted for a while.
TOKEN_ATTEMPT_LIMIT = 5
TOKEN_ATTEMPT_WINDOW_SECONDS = 900


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
            CREATE TABLE IF NOT EXISTS access_plans (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                period TEXT NOT NULL DEFAULT 'monthly',
                limit_usd REAL,
                limit_tokens INTEGER,
                limit_messages INTEGER,
                rate_limit_per_min INTEGER NOT NULL DEFAULT 6,
                max_message_chars INTEGER NOT NULL DEFAULT 2000,
                allowed_tools TEXT,
                system_prompt_suffix TEXT NOT NULL DEFAULT '',
                welcome_message TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Commercial side of a plan, added after the table shipped — a plan with
        # price_usd set and is_purchasable on is a *subscription* a stranger can
        # buy from the bot itself (backend/payments.py); one with no price stays
        # what it was, an internal quota preset the owner hands out by hand.
        plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(access_plans)")}
        for column, definition in (
            ("price_usd", "REAL"),
            ("is_purchasable", "INTEGER NOT NULL DEFAULT 0"),
            ("duration_days", "INTEGER"),
            # NULL means "any agent" (a shared preset, today's only behaviour).
            # Set, it scopes the tariff to one subagent — issuing a token or an
            # invoice for a different agent against this plan is refused, so the
            # assignment actually constrains something rather than just labeling it.
            ("subagent_id", "TEXT"),
        ):
            if column not in plan_columns:
                connection.execute(f"ALTER TABLE access_plans ADD COLUMN {column} {definition}")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS access_subscriptions (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                subagent_id TEXT NOT NULL,
                customer_ref TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                auto_renew INTEGER NOT NULL DEFAULT 0,
                price_usd REAL,
                started_at TEXT NOT NULL,
                current_period_start TEXT NOT NULL,
                current_period_end TEXT,
                canceled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_token ON access_subscriptions (token_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_access_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                binding_id TEXT NOT NULL,
                subagent_id TEXT NOT NULL,
                plan_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                max_chats INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                period_started_at TEXT NOT NULL,
                used_usd REAL NOT NULL DEFAULT 0,
                used_tokens_in INTEGER NOT NULL DEFAULT 0,
                used_tokens_out INTEGER NOT NULL DEFAULT 0,
                used_messages INTEGER NOT NULL DEFAULT 0,
                limit_usd REAL,
                limit_tokens INTEGER,
                limit_messages INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_subscribers (
                id TEXT PRIMARY KEY,
                token_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                profile TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT '',
                messages_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE (binding_id, platform, chat_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriber_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                subscriber_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                subagent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok',
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriber_usage_token ON subscriber_usage (token_id, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriber_usage_subscriber ON subscriber_usage (subscriber_id, id DESC)"
        )


# ── plans ────────────────────────────────────────────────────────────────────

def _plan_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    plan = dict(row)
    plan["is_active"] = bool(plan["is_active"])
    plan["is_purchasable"] = bool(plan.get("is_purchasable"))
    plan["duration_days"] = plan.get("duration_days") or DEFAULT_DURATION_DAYS.get(plan["period"])
    raw_tools = plan.get("allowed_tools")
    if raw_tools:
        try:
            plan["allowed_tools"] = json.loads(raw_tools)
        except Exception:
            plan["allowed_tools"] = None
    else:
        plan["allowed_tools"] = None
    return plan


def list_plans() -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM access_plans ORDER BY created_at ASC").fetchall()
    return [_plan_row_to_dict(row) for row in rows]


def get_plan(plan_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not plan_id:
        return None
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM access_plans WHERE id = ?", (plan_id,)).fetchone()
    return _plan_row_to_dict(row) if row else None


def _validate_plan_fields(period: str, allowed_tools: Optional[list[str]]) -> Optional[str]:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    if allowed_tools is None:
        return None
    from backend.tool_permissions import PUBLIC_CHANNEL_CEILING

    unknown = [tool for tool in allowed_tools if tool not in PUBLIC_CHANNEL_CEILING]
    if unknown:
        raise ValueError(
            "A plan can only narrow the public tool ceiling; these are not available "
            f"on public channels: {', '.join(sorted(unknown))}"
        )
    return json.dumps(sorted(set(allowed_tools)), ensure_ascii=False)


def create_plan(
    name: str,
    description: str = "",
    period: str = "monthly",
    limit_usd: Optional[float] = None,
    limit_tokens: Optional[int] = None,
    limit_messages: Optional[int] = None,
    rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    allowed_tools: Optional[list[str]] = None,
    system_prompt_suffix: str = "",
    welcome_message: str = "",
    is_active: bool = True,
    price_usd: Optional[float] = None,
    is_purchasable: bool = False,
    duration_days: Optional[int] = None,
    subagent_id: Optional[str] = None,
) -> dict[str, Any]:
    _init_schema()
    clean_name = str(name).strip()
    if not 1 <= len(clean_name) <= 80:
        raise ValueError("Plan name must be 1-80 characters")
    tools_json = _validate_plan_fields(period, allowed_tools)
    if is_purchasable and not price_usd:
        raise ValueError("A purchasable plan needs a price")
    clean_subagent_id = _validated_plan_agent(subagent_id)

    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO access_plans
                (id, name, description, period, limit_usd, limit_tokens, limit_messages,
                 rate_limit_per_min, max_message_chars, allowed_tools, system_prompt_suffix,
                 welcome_message, is_active, created_at, updated_at,
                 price_usd, is_purchasable, duration_days, subagent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id, clean_name, description or "", period, limit_usd, limit_tokens, limit_messages,
                max(0, int(rate_limit_per_min or 0)), max(1, int(max_message_chars or DEFAULT_MAX_MESSAGE_CHARS)),
                tools_json, system_prompt_suffix or "", welcome_message or "",
                1 if is_active else 0, now, now,
                price_usd, 1 if is_purchasable else 0, duration_days, clean_subagent_id,
            ),
        )
    return get_plan(plan_id)  # type: ignore[return-value]


def _validated_plan_agent(subagent_id: Optional[str]) -> Optional[str]:
    """None means "any agent" — the only shape a plan could have before this
    field existed, and still the right default for a shared preset. A non-empty
    value must name a real subagent, and never the main agent (Vexa is never
    reachable through a token in the first place — see tool_permissions.py)."""
    clean = (subagent_id or "").strip()
    if not clean:
        return None
    from backend.tool_permissions import MAIN_AGENT_IDS

    if clean in MAIN_AGENT_IDS:
        raise ValueError("A tariff cannot be assigned to the main agent.")
    from backend.database import get_subagent

    if not get_subagent(clean):
        raise KeyError(f"Unknown agent: {clean}")
    return clean


def update_plan(plan_id: str, **fields: Any) -> dict[str, Any]:
    """Partial update: a field left out (or passed as ``None``) keeps its
    current value. ``allowed_tools`` and ``subagent_id`` are the exception —
    ``None`` there is a meaningful value ("default tool set" / "any agent"), so
    they are only left alone when the key is absent entirely."""
    _init_schema()
    existing = get_plan(plan_id)
    if not existing:
        raise KeyError(plan_id)

    allowed = {
        "name", "description", "period", "limit_usd", "limit_tokens", "limit_messages",
        "rate_limit_per_min", "max_message_chars", "system_prompt_suffix",
        "welcome_message", "is_active", "price_usd", "is_purchasable", "duration_days",
    }
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if "allowed_tools" in fields:
        tools = fields["allowed_tools"]
        updates["allowed_tools"] = _validate_plan_fields(
            fields.get("period") or existing["period"], None if tools is None else list(tools)
        )
    if "subagent_id" in fields:
        updates["subagent_id"] = _validated_plan_agent(fields["subagent_id"])
    if not updates:
        return existing
    if "period" in updates and updates["period"] not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    for flag in ("is_active", "is_purchasable"):
        if flag in updates:
            updates[flag] = 1 if updates[flag] else 0
    if updates.get("is_purchasable") and not (updates.get("price_usd") or existing.get("price_usd")):
        raise ValueError("A purchasable plan needs a price")

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE access_plans SET {set_clause}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), plan_id),
        )
    return get_plan(plan_id)  # type: ignore[return-value]


def delete_plan(plan_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        in_use = connection.execute(
            "SELECT COUNT(*) FROM bot_access_tokens WHERE plan_id = ? AND status != 'revoked'",
            (plan_id,),
        ).fetchone()[0]
        if in_use:
            raise ValueError(f"{in_use} active token(s) still use this plan — reassign or revoke them first")
        cursor = connection.execute("DELETE FROM access_plans WHERE id = ?", (plan_id,))
        return cursor.rowcount > 0


# ── tokens ───────────────────────────────────────────────────────────────────

def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()


def _generate_token() -> tuple[str, str, str]:
    """Returns (plaintext, hash, display prefix).

    Base62 rather than ``token_urlsafe`` so a token never contains '-' or '_':
    people paste these into chat windows and email bodies, where a leading or
    trailing separator reads like punctuation and gets dropped.
    """
    body = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(TOKEN_BODY_LENGTH))
    plaintext = f"{TOKEN_PREFIX}{body}"
    return plaintext, _hash_token(plaintext), body[:6]


def _token_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    token = dict(row)
    token.pop("token_hash", None)  # never leaves this module
    token["display"] = f"{TOKEN_PREFIX}{token['token_prefix']}…"
    return token


def _period_start(period: str, reference: Optional[datetime] = None) -> datetime:
    moment = reference or datetime.now(timezone.utc)
    if period == "daily":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        start_of_day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day - timedelta(days=start_of_day.weekday())
    if period == "monthly":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return datetime(1970, 1, 1, tzinfo=timezone.utc)  # lifetime


def issue_token(
    binding_id: str,
    subagent_id: str,
    plan_id: Optional[str] = None,
    label: str = "",
    max_chats: int = 1,
    expires_at: Optional[str] = None,
    limit_usd: Optional[float] = None,
    limit_tokens: Optional[int] = None,
    limit_messages: Optional[int] = None,
    notes: str = "",
) -> dict[str, Any]:
    """Creates a token. The returned dict carries ``plaintext`` — the only time
    it exists outside the holder's hands."""
    _init_schema()
    from backend.tool_permissions import MAIN_AGENT_IDS

    if subagent_id in MAIN_AGENT_IDS:
        raise ValueError(
            "Access tokens cannot target the main agent — Vexa is reachable only by the owner."
        )
    plan = get_plan(plan_id) if plan_id else None
    if plan_id and not plan:
        raise KeyError(f"Unknown plan: {plan_id}")
    if plan and plan.get("subagent_id") and plan["subagent_id"] != subagent_id:
        raise ValueError(
            f"Plan '{plan['name']}' is assigned to a different agent and cannot be used for '{subagent_id}'."
        )

    plaintext, token_hash, prefix = _generate_token()
    token_id = f"tok-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO bot_access_tokens
                (id, token_hash, token_prefix, label, binding_id, subagent_id, plan_id,
                 status, max_chats, expires_at, period_started_at, limit_usd, limit_tokens,
                 limit_messages, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id, token_hash, prefix, label or "", binding_id, subagent_id, plan_id,
                max(1, int(max_chats or 1)), expires_at, now, limit_usd, limit_tokens,
                limit_messages, notes or "", now, now,
            ),
        )
    result = get_token(token_id)
    assert result is not None
    result["plaintext"] = plaintext
    return result


def issue_tokens_bulk(count: int, **kwargs: Any) -> list[dict[str, Any]]:
    if not 1 <= int(count) <= 500:
        raise ValueError("count must be between 1 and 500")
    return [issue_token(**kwargs) for _ in range(int(count))]


def get_token(token_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM bot_access_tokens WHERE id = ?", (token_id,)).fetchone()
    return _token_row_to_dict(row) if row else None


def list_tokens(
    binding_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    _init_schema()
    clauses, params = [], []
    if binding_id:
        clauses.append("binding_id = ?")
        params.append(binding_id)
    if subagent_id:
        clauses.append("subagent_id = ?")
        params.append(subagent_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM bot_access_tokens{where} ORDER BY created_at DESC", params
        ).fetchall()
    return [_token_row_to_dict(row) for row in rows]


def find_token_by_plaintext(plaintext: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM bot_access_tokens WHERE token_hash = ?", (_hash_token(plaintext),)
        ).fetchone()
    return _token_row_to_dict(row) if row else None


def update_token(token_id: str, **fields: Any) -> dict[str, Any]:
    _init_schema()
    existing = get_token(token_id)
    if not existing:
        raise KeyError(token_id)
    allowed = {
        "label", "plan_id", "status", "max_chats", "expires_at",
        "limit_usd", "limit_tokens", "limit_messages", "notes",
    }
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if not updates:
        return existing
    if "status" in updates and updates["status"] not in TOKEN_STATUSES:
        raise ValueError(f"status must be one of {TOKEN_STATUSES}")
    if "plan_id" in updates and not get_plan(updates["plan_id"]):
        raise KeyError(f"Unknown plan: {updates['plan_id']}")

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE bot_access_tokens SET {set_clause}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), token_id),
        )
    return get_token(token_id)  # type: ignore[return-value]


def revoke_token(token_id: str) -> dict[str, Any]:
    """Revoking also blocks every chat that redeemed it — otherwise an already
    bound subscriber would keep talking to the bot forever."""
    token = update_token(token_id, status="revoked")
    with _connect() as connection:
        connection.execute(
            "UPDATE bot_subscribers SET status = 'blocked', last_seen_at = ? WHERE token_id = ?",
            (_now(), token_id),
        )
    return token


def reset_token_usage(token_id: str) -> dict[str, Any]:
    _init_schema()
    if not get_token(token_id):
        raise KeyError(token_id)
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE bot_access_tokens
               SET used_usd = 0, used_tokens_in = 0, used_tokens_out = 0, used_messages = 0,
                   period_started_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, token_id),
        )
    return get_token(token_id)  # type: ignore[return-value]


def delete_token(token_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        connection.execute("DELETE FROM bot_subscribers WHERE token_id = ?", (token_id,))
        cursor = connection.execute("DELETE FROM bot_access_tokens WHERE id = ?", (token_id,))
        return cursor.rowcount > 0


# ── subscriptions ────────────────────────────────────────────────────────────
# A subscription is the *commercial* record — what was bought, until when, and
# whether it renews. The token is the technical credential; the subscription is
# the reason it is still allowed to work. Keeping them apart means an owner can
# hand out a free token (no subscription at all) or renew a paid one without
# reissuing the credential the customer already saved.

def _subscription_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    subscription = dict(row)
    subscription["auto_renew"] = bool(subscription["auto_renew"])
    return subscription


def get_subscription(subscription_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM access_subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
    return _subscription_row_to_dict(row) if row else None


def get_subscription_for_token(token_id: str) -> Optional[dict[str, Any]]:
    """The newest subscription attached to a token — renewals extend this same
    row, so there is normally only one."""
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM access_subscriptions WHERE token_id = ? ORDER BY created_at DESC LIMIT 1",
            (token_id,),
        ).fetchone()
    return _subscription_row_to_dict(row) if row else None


def list_subscriptions(
    binding_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    _init_schema()
    clauses, params = [], []
    if binding_id:
        clauses.append("binding_id = ?")
        params.append(binding_id)
    if plan_id:
        clauses.append("plan_id = ?")
        params.append(plan_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM access_subscriptions{where} ORDER BY created_at DESC", params
        ).fetchall()
    return [_subscription_row_to_dict(row) for row in rows]


def _period_end(plan: dict[str, Any], start: Optional[datetime] = None) -> Optional[str]:
    days = plan.get("duration_days")
    if not days:
        return None  # lifetime / one-off purchase
    moment = start or datetime.now(timezone.utc)
    return (moment + timedelta(days=int(days))).isoformat(timespec="seconds")


def create_subscription(
    plan: dict[str, Any],
    token_id: str,
    binding_id: str,
    subagent_id: str,
    customer_ref: str = "",
    auto_renew: bool = False,
) -> dict[str, Any]:
    _init_schema()
    now = _now()
    subscription_id = f"sub_{uuid.uuid4().hex[:12]}"
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO access_subscriptions
                (id, plan_id, token_id, binding_id, subagent_id, customer_ref, status,
                 auto_renew, price_usd, started_at, current_period_start, current_period_end,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription_id, plan["id"], token_id, binding_id, subagent_id, customer_ref or "",
                1 if auto_renew else 0, plan.get("price_usd"), now, now, _period_end(plan),
                now, now,
            ),
        )
    return get_subscription(subscription_id)  # type: ignore[return-value]


def renew_subscription(subscription_id: str) -> dict[str, Any]:
    """Starts a fresh paid period and zeroes the token's usage counters.

    The new period starts from whichever is later — now, or the end of the
    period still running — so paying early tops up rather than truncating what
    the customer already has.
    """
    subscription = get_subscription(subscription_id)
    if not subscription:
        raise KeyError(subscription_id)
    plan = get_plan(subscription["plan_id"])
    if not plan:
        raise KeyError(f"Plan {subscription['plan_id']} no longer exists")

    now = datetime.now(timezone.utc)
    current_end = _parse_iso(subscription.get("current_period_end"))
    start = current_end if current_end and current_end > now else now

    with _connect() as connection:
        connection.execute(
            """
            UPDATE access_subscriptions
               SET status = 'active', current_period_start = ?, current_period_end = ?,
                   canceled_at = NULL, updated_at = ?
             WHERE id = ?
            """,
            (start.isoformat(timespec="seconds"), _period_end(plan, start), _now(), subscription_id),
        )
    # A renewed subscription gets its quota back, and un-suspends the credential
    # if a previous lapse had parked it.
    reset_token_usage(subscription["token_id"])
    token = get_token(subscription["token_id"])
    if token and token["status"] == "suspended":
        update_token(subscription["token_id"], status="active")
    return get_subscription(subscription_id)  # type: ignore[return-value]


def cancel_subscription(subscription_id: str, suspend_token: bool = True) -> dict[str, Any]:
    """Stops the renewal. By default the credential is parked immediately;
    pass suspend_token=False to let the paid period run out first."""
    subscription = get_subscription(subscription_id)
    if not subscription:
        raise KeyError(subscription_id)
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE access_subscriptions
               SET status = 'canceled', auto_renew = 0, canceled_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, subscription_id),
        )
    if suspend_token:
        token = get_token(subscription["token_id"])
        if token and token["status"] == "active":
            update_token(subscription["token_id"], status="suspended")
    return get_subscription(subscription_id)  # type: ignore[return-value]


def set_subscription_auto_renew(subscription_id: str, auto_renew: bool) -> dict[str, Any]:
    if not get_subscription(subscription_id):
        raise KeyError(subscription_id)
    with _connect() as connection:
        connection.execute(
            "UPDATE access_subscriptions SET auto_renew = ?, updated_at = ? WHERE id = ?",
            (1 if auto_renew else 0, _now(), subscription_id),
        )
    return get_subscription(subscription_id)  # type: ignore[return-value]


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except Exception:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def subscription_lapsed(subscription: Optional[dict[str, Any]]) -> bool:
    """True when a subscription exists but its paid period has run out.

    ``current_period_end`` of None is a lifetime purchase, which never lapses.
    """
    if not subscription:
        return False
    if subscription["status"] == "canceled":
        return True
    end = _parse_iso(subscription.get("current_period_end"))
    return bool(end and end <= datetime.now(timezone.utc))


def expire_lapsed_subscriptions() -> int:
    """Marks run-out subscriptions expired and parks their tokens. Called from
    the scheduler; ``check_access`` also catches a lapse in-line, so this is
    about keeping the admin view honest rather than about enforcement."""
    _init_schema()
    expired = 0
    for subscription in list_subscriptions(status="active"):
        if not subscription_lapsed(subscription):
            continue
        with _connect() as connection:
            connection.execute(
                "UPDATE access_subscriptions SET status = 'expired', updated_at = ? WHERE id = ?",
                (_now(), subscription["id"]),
            )
        token = get_token(subscription["token_id"])
        if token and token["status"] == "active":
            update_token(subscription["token_id"], status="suspended")
        expired += 1
    if expired:
        logger.info("Expired %d lapsed subscription(s)", expired)
    return expired


# ── subscribers ("учётка" + картотека) ───────────────────────────────────────

def _subscriber_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    subscriber = dict(row)
    try:
        subscriber["profile"] = json.loads(subscriber.get("profile") or "{}")
    except Exception:
        subscriber["profile"] = {}
    return subscriber


def get_subscriber(subscriber_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM bot_subscribers WHERE id = ?", (subscriber_id,)).fetchone()
    return _subscriber_row_to_dict(row) if row else None


def find_subscriber(binding_id: str, platform: str, chat_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM bot_subscribers WHERE binding_id = ? AND platform = ? AND chat_id = ?",
            (binding_id, platform, str(chat_id)),
        ).fetchone()
    return _subscriber_row_to_dict(row) if row else None


def list_subscribers(
    binding_id: Optional[str] = None,
    token_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    _init_schema()
    clauses, params = [], []
    if binding_id:
        clauses.append("binding_id = ?")
        params.append(binding_id)
    if token_id:
        clauses.append("token_id = ?")
        params.append(token_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM bot_subscribers{where} ORDER BY last_seen_at DESC", params
        ).fetchall()
    return [_subscriber_row_to_dict(row) for row in rows]


def session_id_for(platform: str, binding_id: str, chat_id: str) -> str:
    """Keeps the existing per-channel session key shape (``tgbot:<binding>:<chat>``
    from agent_bot.py) so a subscriber's card and the stored conversation are the
    same rows the dashboard already reads."""
    prefix = {
        "telegram": "tgbot",
        "matrix": "matrixbot",
        "discord": "discordbot",
        "slack": "slackbot",
        "email": "emailchannel",
    }.get(platform, platform)
    return f"{prefix}:{binding_id}:{chat_id}"


def create_subscriber(
    token: dict[str, Any],
    platform: str,
    chat_id: str,
    external_user_id: str = "",
    display_name: str = "",
) -> dict[str, Any]:
    _init_schema()
    now = _now()
    subscriber_id = f"sub-{uuid.uuid4().hex[:12]}"
    session_id = session_id_for(platform, token["binding_id"], chat_id)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO bot_subscribers
                (id, token_id, binding_id, platform, chat_id, external_user_id, display_name,
                 session_id, status, profile, notes, messages_count, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', '{}', '', 0, ?, ?)
            """,
            (
                subscriber_id, token["id"], token["binding_id"], platform, str(chat_id),
                external_user_id or "", display_name or "", session_id, now, now,
            ),
        )
    return get_subscriber(subscriber_id)  # type: ignore[return-value]


def update_subscriber(subscriber_id: str, **fields: Any) -> dict[str, Any]:
    _init_schema()
    existing = get_subscriber(subscriber_id)
    if not existing:
        raise KeyError(subscriber_id)
    allowed = {"display_name", "status", "profile", "notes"}
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if not updates:
        return existing
    if "status" in updates and updates["status"] not in SUBSCRIBER_STATUSES:
        raise ValueError(f"status must be one of {SUBSCRIBER_STATUSES}")
    if "profile" in updates and not isinstance(updates["profile"], str):
        updates["profile"] = json.dumps(updates["profile"], ensure_ascii=False)

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE bot_subscribers SET {set_clause} WHERE id = ?", (*updates.values(), subscriber_id)
        )
    return get_subscriber(subscriber_id)  # type: ignore[return-value]


def touch_subscriber(subscriber_id: str, display_name: str = "") -> None:
    with _connect() as connection:
        if display_name:
            connection.execute(
                "UPDATE bot_subscribers SET last_seen_at = ?, display_name = ? WHERE id = ?",
                (_now(), display_name, subscriber_id),
            )
        else:
            connection.execute(
                "UPDATE bot_subscribers SET last_seen_at = ? WHERE id = ?", (_now(), subscriber_id)
            )


def delete_subscriber(subscriber_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM bot_subscribers WHERE id = ?", (subscriber_id,))
        return cursor.rowcount > 0


# ── redemption ───────────────────────────────────────────────────────────────

class RedemptionError(Exception):
    """Carries a message that is safe to show the person on the other end —
    deliberately vague about *why* a token failed, so the bot can't be used to
    enumerate valid tokens."""


def redeem(
    binding_id: str,
    platform: str,
    chat_id: str,
    plaintext: str,
    external_user_id: str = "",
    display_name: str = "",
) -> dict[str, Any]:
    """Binds a chat to a token, creating its subscriber record. Raises
    RedemptionError with a user-facing message on any failure."""
    token = find_token_by_plaintext(plaintext)
    if not token or token["binding_id"] != binding_id:
        raise RedemptionError("Токен не распознан. Проверьте, что он скопирован целиком.")
    if token["status"] == "revoked":
        raise RedemptionError("Этот токен отозван. Обратитесь к тому, кто его выдал.")
    if token["status"] == "suspended":
        raise RedemptionError("Этот токен временно приостановлен.")
    if token.get("expires_at") and token["expires_at"] <= _now():
        raise RedemptionError("Срок действия токена истёк.")

    existing = find_subscriber(binding_id, platform, chat_id)
    if existing:
        if existing["token_id"] == token["id"]:
            if existing["status"] == "blocked":
                raise RedemptionError("Доступ для этого чата заблокирован.")
            return existing
        raise RedemptionError("Этот чат уже привязан к другому токену.")

    bound_chats = len(list_subscribers(token_id=token["id"]))
    if bound_chats >= max(1, int(token.get("max_chats") or 1)):
        raise RedemptionError("Этот токен уже используется в другом чате.")

    subscriber = create_subscriber(token, platform, chat_id, external_user_id, display_name)
    with _connect() as connection:
        connection.execute(
            "UPDATE bot_access_tokens SET last_used_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), token["id"]),
        )
    logger.info(
        "Access token redeemed: token=%s binding=%s platform=%s subscriber=%s",
        token["id"], binding_id, platform, subscriber["id"],
    )
    return subscriber


# ── limits ───────────────────────────────────────────────────────────────────

_local_rate_buckets: dict[str, list[float]] = {}

# Every Valkey miss costs a 3-second connect timeout (valkey_client.py), and a
# rate-limit check runs on the hot path of every incoming message — so a Valkey
# outage would turn into a 3s stall per message. After one failure the counters
# stay in-process for this long before Valkey is tried again.
_VALKEY_BACKOFF_SECONDS = 60
_valkey_unavailable_until = 0.0


def _counter_incr(key: str, ttl_seconds: int, window_seconds: int) -> int:
    """Increments a windowed counter and returns its new value.

    Prefers Valkey so the count is shared across workers and survives a
    restart, and degrades to a per-process bucket when it isn't reachable —
    a degraded limiter still limits, which matters more here than exactness.
    """
    global _valkey_unavailable_until

    now = time.time()
    if now >= _valkey_unavailable_until:
        try:
            from backend.valkey_client import get_client

            client = get_client()
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, ttl_seconds)
            return count
        except Exception as exc:
            _valkey_unavailable_until = now + _VALKEY_BACKOFF_SECONDS
            logger.warning("Valkey unavailable for access counters, falling back locally: %s", exc)

    bucket = [stamp for stamp in _local_rate_buckets.get(key, []) if now - stamp < window_seconds]
    bucket.append(now)
    _local_rate_buckets[key] = bucket
    return len(bucket)


def _rate_limit_hit(key: str, limit_per_min: int) -> bool:
    """True when the caller has already used up its allowance this minute."""
    if limit_per_min <= 0:
        return False
    window_key = f"botaccess:rl:{key}:{int(time.time() // 60)}"
    return _counter_incr(window_key, ttl_seconds=120, window_seconds=60) > limit_per_min


def note_failed_attempt(platform: str, chat_id: str) -> bool:
    """Counts a wrong token guess. Returns True once the chat is muted."""
    key = f"botaccess:fail:{platform}:{chat_id}"
    count = _counter_incr(
        key, ttl_seconds=TOKEN_ATTEMPT_WINDOW_SECONDS, window_seconds=TOKEN_ATTEMPT_WINDOW_SECONDS
    )
    return count > TOKEN_ATTEMPT_LIMIT


def _maybe_roll_period(token: dict[str, Any], plan: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Zeroes the running counters when the plan's period has rolled over."""
    period = (plan or {}).get("period") or "monthly"
    if period == "lifetime":
        return token
    boundary = _period_start(period)
    started_raw = token.get("period_started_at") or ""
    try:
        started = datetime.fromisoformat(started_raw)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except Exception:
        started = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if started >= boundary:
        return token
    return reset_token_usage(token["id"])


def effective_limits(token: dict[str, Any], plan: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Per-token overrides win over the plan; ``None`` anywhere means unlimited."""
    def pick(field: str) -> Any:
        value = token.get(field)
        return value if value is not None else (plan or {}).get(field)

    return {
        "limit_usd": pick("limit_usd"),
        "limit_tokens": pick("limit_tokens"),
        "limit_messages": pick("limit_messages"),
        "rate_limit_per_min": (plan or {}).get("rate_limit_per_min", DEFAULT_RATE_LIMIT_PER_MIN),
        "max_message_chars": (plan or {}).get("max_message_chars", DEFAULT_MAX_MESSAGE_CHARS),
        "period": (plan or {}).get("period", "monthly"),
    }


def check_quota(token: dict[str, Any], plan: Optional[dict[str, Any]]) -> Optional[str]:
    """Returns a user-facing refusal when the token has nothing left, else None."""
    limits = effective_limits(token, plan)
    used_tokens = int(token.get("used_tokens_in") or 0) + int(token.get("used_tokens_out") or 0)

    if limits["limit_usd"] is not None and float(token.get("used_usd") or 0) >= float(limits["limit_usd"]):
        return (
            f"Лимит по этому токену исчерпан (${float(token.get('used_usd') or 0):.4f} "
            f"из ${float(limits['limit_usd']):.2f}, период: {limits['period']}). "
            "Обратитесь к администратору, чтобы увеличить лимит."
        )
    if limits["limit_tokens"] is not None and used_tokens >= int(limits["limit_tokens"]):
        return (
            f"Лимит по количеству токенов исчерпан ({used_tokens} из {int(limits['limit_tokens'])}, "
            f"период: {limits['period']}). Обратитесь к администратору."
        )
    if limits["limit_messages"] is not None and int(token.get("used_messages") or 0) >= int(limits["limit_messages"]):
        return (
            f"Лимит по числу сообщений исчерпан ({int(token.get('used_messages') or 0)} из "
            f"{int(limits['limit_messages'])}, период: {limits['period']}). Обратитесь к администратору."
        )
    return None


def check_access(
    token_id: str,
    subscriber_id: str,
    message_chars: int,
) -> dict[str, Any]:
    """Full pre-flight check for one incoming message.

    Returns ``{"allowed": bool, "reason": str, "token": ..., "plan": ..., "limits": ...}``.
    """
    token = get_token(token_id)
    if not token:
        return {"allowed": False, "reason": "Токен доступа не найден.", "token": None, "plan": None}
    plan = get_plan(token.get("plan_id"))
    token = _maybe_roll_period(token, plan)
    limits = effective_limits(token, plan)

    if token["status"] != "active":
        reason = (
            "Этот токен отозван." if token["status"] == "revoked" else "Этот токен временно приостановлен."
        )
        return {"allowed": False, "reason": reason, "token": token, "plan": plan, "limits": limits}
    if token.get("expires_at") and token["expires_at"] <= _now():
        return {
            "allowed": False, "reason": "Срок действия токена истёк.",
            "token": token, "plan": plan, "limits": limits,
        }
    if plan is not None and not plan["is_active"]:
        return {
            "allowed": False, "reason": "Тариф этого токена отключён. Обратитесь к администратору.",
            "token": token, "plan": plan, "limits": limits,
        }

    # A paid subscription that has run out stops the token even though the
    # credential itself is still valid — checked here as well as in the
    # scheduler sweep, so a lapse bites immediately rather than at the next tick.
    subscription = get_subscription_for_token(token_id)
    if subscription_lapsed(subscription):
        reason = (
            "Подписка отменена." if subscription and subscription["status"] == "canceled"
            else "Срок подписки истёк. Продлите её, чтобы продолжить."
        )
        return {
            "allowed": False, "reason": reason, "token": token, "plan": plan,
            "limits": limits, "subscription": subscription,
        }
    if message_chars > int(limits["max_message_chars"]):
        return {
            "allowed": False,
            "reason": f"Сообщение слишком длинное (максимум {int(limits['max_message_chars'])} символов).",
            "token": token, "plan": plan, "limits": limits,
        }
    if _rate_limit_hit(subscriber_id, int(limits["rate_limit_per_min"])):
        return {
            "allowed": False, "reason": "Слишком много сообщений подряд. Подождите минуту, пожалуйста.",
            "token": token, "plan": plan, "limits": limits,
        }

    quota_refusal = check_quota(token, plan)
    if quota_refusal:
        return {"allowed": False, "reason": quota_refusal, "token": token, "plan": plan, "limits": limits}

    return {"allowed": True, "reason": "", "token": token, "plan": plan, "limits": limits}


# ── usage accounting ─────────────────────────────────────────────────────────

def record_usage(
    subscriber: dict[str, Any],
    token_id: str,
    subagent_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    model: str = "",
    provider: str = "",
    latency_ms: int = 0,
    status: str = "ok",
    detail: str = "",
) -> None:
    """Appends a ledger row and bumps the token's running counters in one
    transaction. Blocked turns are recorded too (with zero spend) so the admin
    can see refusals, not just successful calls."""
    _init_schema()
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cost_usd = max(0.0, float(cost_usd or 0.0))
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO subscriber_usage
                (ts, subscriber_id, token_id, binding_id, subagent_id, session_id, model, provider,
                 prompt_tokens, completion_tokens, cost_usd, latency_ms, status, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now, subscriber["id"], token_id, subscriber["binding_id"], subagent_id,
                subscriber["session_id"], model or "", provider or "", prompt_tokens,
                completion_tokens, cost_usd, max(0, int(latency_ms or 0)), status, detail or "",
            ),
        )
        connection.execute(
            """
            UPDATE bot_access_tokens
               SET used_usd = used_usd + ?, used_tokens_in = used_tokens_in + ?,
                   used_tokens_out = used_tokens_out + ?, used_messages = used_messages + 1,
                   last_used_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (cost_usd, prompt_tokens, completion_tokens, now, now, token_id),
        )
        connection.execute(
            "UPDATE bot_subscribers SET messages_count = messages_count + 1, last_seen_at = ? WHERE id = ?",
            (now, subscriber["id"]),
        )


def token_usage_summary(token_id: str, limit: int = 50) -> dict[str, Any]:
    _init_schema()
    token = get_token(token_id)
    if not token:
        raise KeyError(token_id)
    plan = get_plan(token.get("plan_id"))
    with _connect() as connection:
        totals = connection.execute(
            """
            SELECT COUNT(*) AS turns,
                   COALESCE(SUM(prompt_tokens), 0) AS tokens_in,
                   COALESCE(SUM(completion_tokens), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
              FROM subscriber_usage WHERE token_id = ?
            """,
            (token_id,),
        ).fetchone()
        recent = connection.execute(
            "SELECT * FROM subscriber_usage WHERE token_id = ? ORDER BY id DESC LIMIT ?",
            (token_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return {
        "token": token,
        "plan": plan,
        "subscription": get_subscription_for_token(token_id),
        "limits": effective_limits(token, plan),
        "lifetime": dict(totals),
        "recent": [dict(row) for row in recent],
        "subscribers": list_subscribers(token_id=token_id),
    }


def subscriber_card(subscriber_id: str, message_limit: int = 50) -> dict[str, Any]:
    """The картотека: who this is, which token they hold, what they've spent and
    what they've said. Conversation rows come from the shared ``messages`` table
    the dashboard already renders."""
    subscriber = get_subscriber(subscriber_id)
    if not subscriber:
        raise KeyError(subscriber_id)
    token = get_token(subscriber["token_id"])
    plan = get_plan(token.get("plan_id")) if token else None
    with _connect() as connection:
        totals = connection.execute(
            """
            SELECT COUNT(*) AS turns,
                   COALESCE(SUM(prompt_tokens), 0) AS tokens_in,
                   COALESCE(SUM(completion_tokens), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd
              FROM subscriber_usage WHERE subscriber_id = ?
            """,
            (subscriber_id,),
        ).fetchone()

    from backend.database import get_chat_history

    try:
        conversation = get_chat_history(subscriber["session_id"], limit=max(1, min(int(message_limit), 500)))
    except Exception:
        logger.exception("Could not load the conversation for subscriber %s", subscriber_id)
        conversation = []

    return {
        "subscriber": subscriber,
        "token": token,
        "plan": plan,
        "subscription": get_subscription_for_token(token["id"]) if token else None,
        "limits": effective_limits(token, plan) if token else {},
        "usage": dict(totals),
        "conversation": conversation,
    }


def overview() -> dict[str, Any]:
    """Cross-bot totals for the admin landing panel."""
    _init_schema()
    with _connect() as connection:
        tokens = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN status = 'suspended' THEN 1 ELSE 0 END) AS suspended,
                   SUM(CASE WHEN status = 'revoked' THEN 1 ELSE 0 END) AS revoked,
                   COALESCE(SUM(used_usd), 0) AS used_usd,
                   COALESCE(SUM(used_tokens_in + used_tokens_out), 0) AS used_tokens,
                   COALESCE(SUM(used_messages), 0) AS used_messages
              FROM bot_access_tokens
            """
        ).fetchone()
        subscribers = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked
              FROM bot_subscribers
            """
        ).fetchone()
        blocked_turns = connection.execute(
            "SELECT COUNT(*) FROM subscriber_usage WHERE status != 'ok'"
        ).fetchone()[0]
    return {
        "tokens": dict(tokens),
        "subscribers": dict(subscribers),
        "blocked_turns": blocked_turns,
        "plans": len(list_plans()),
    }


_init_schema()
