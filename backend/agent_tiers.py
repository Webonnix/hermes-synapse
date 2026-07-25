"""Agent tiers — named presets of feature flags/defaults an agent can be assigned to.

Unlike provider_governance.py / agent_messenger_governance.py, tiers grant no new
external capability by themselves (they don't hold secrets or open network
surface) — they're just local admin-editable defaults, so there's no
proposal/approval flow here, just plain CRUD (R0, owner-only via the dashboard
auth middleware).
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database import DB_PATH

_NAME = re.compile(r"^.{1,80}$")


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
            CREATE TABLE IF NOT EXISTS agent_tiers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_usd_limit_default REAL,
                budget_period_default TEXT NOT NULL DEFAULT 'monthly',
                allow_external_provider INTEGER NOT NULL DEFAULT 1,
                allow_messenger INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["allow_external_provider"] = bool(item["allow_external_provider"])
    item["allow_messenger"] = bool(item["allow_messenger"])
    item["is_active"] = bool(item["is_active"])
    return item


def list_tiers() -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM agent_tiers ORDER BY created_at ASC").fetchall()
    return [_row_to_dict(row) for row in rows]


def get_tier(tier_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM agent_tiers WHERE id = ?", (tier_id,)).fetchone()
    return _row_to_dict(row) if row else None


def create_tier(
    name: str,
    description: str = "",
    budget_usd_limit_default: Optional[float] = None,
    budget_period_default: str = "monthly",
    allow_external_provider: bool = True,
    allow_messenger: bool = True,
    is_active: bool = True,
) -> dict[str, Any]:
    _init_schema()
    clean_name = str(name).strip()
    if not _NAME.fullmatch(clean_name):
        raise ValueError("Tier name must be 1-80 characters")
    if budget_period_default not in ("monthly", "lifetime"):
        raise ValueError("budget_period_default must be 'monthly' or 'lifetime'")

    tier_id = f"tier-{uuid.uuid4().hex[:10]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_tiers
                (id, name, description, budget_usd_limit_default, budget_period_default,
                 allow_external_provider, allow_messenger, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tier_id, clean_name, description or "", budget_usd_limit_default, budget_period_default,
                1 if allow_external_provider else 0, 1 if allow_messenger else 0, 1 if is_active else 0,
                now, now,
            ),
        )
    return get_tier(tier_id)  # type: ignore[return-value]


def update_tier(tier_id: str, **fields: Any) -> dict[str, Any]:
    _init_schema()
    existing = get_tier(tier_id)
    if not existing:
        raise KeyError(tier_id)

    allowed = {
        "name", "description", "budget_usd_limit_default", "budget_period_default",
        "allow_external_provider", "allow_messenger", "is_active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return existing
    if "budget_period_default" in updates and updates["budget_period_default"] not in ("monthly", "lifetime"):
        raise ValueError("budget_period_default must be 'monthly' or 'lifetime'")

    for bool_field in ("allow_external_provider", "allow_messenger", "is_active"):
        if bool_field in updates:
            updates[bool_field] = 1 if updates[bool_field] else 0

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE agent_tiers SET {set_clause}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), tier_id),
        )
    return get_tier(tier_id)  # type: ignore[return-value]


def delete_tier(tier_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM agent_tiers WHERE id = ?", (tier_id,))
        return cursor.rowcount > 0


_init_schema()
