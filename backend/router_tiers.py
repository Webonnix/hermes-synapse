"""The local-orchestrated fallback chain: an ordered list of tiers the local
model can escalate a request to when it decides a task needs more than the
local model alone.

Unlike provider_governance.py, a tier holds no secret of its own — it just
points at an already-approved provider_bindings row (or the special 'local'
kind, which needs no binding at all). So, like agent_tiers.py, this is plain
owner-only CRUD (R0) with no proposal/approval flow: the sensitive part (the
API key) was already governed when the binding itself was created.

Tier 0 ("local") always exists implicitly and is never a row in this table —
it's free, always available, and is exactly what call_llm_normalized already
does when no chain is configured, so it needs no table entry to represent it.
Rows here represent tier_rank >= 1, i.e. the escalation ladder above local.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database import DB_PATH

_LABEL = re.compile(r"^.{1,80}$")
_ALLOWED_KINDS = {"local", "binding"}


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
            CREATE TABLE IF NOT EXISTS router_tiers (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                tier_rank INTEGER NOT NULL,
                kind TEXT NOT NULL,
                provider_binding_id TEXT,
                model_override TEXT NOT NULL DEFAULT '',
                quota_limit INTEGER,
                quota_window_hours REAL NOT NULL DEFAULT 24,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["is_active"] = bool(item["is_active"])
    return item


def list_tiers() -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM router_tiers ORDER BY tier_rank ASC, created_at ASC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_tier(tier_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM router_tiers WHERE id = ?", (tier_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _validate(
    label: str, tier_rank: int, kind: str, provider_binding_id: Optional[str],
    quota_limit: Optional[int], quota_window_hours: float,
) -> None:
    clean_label = str(label).strip()
    if not _LABEL.fullmatch(clean_label):
        raise ValueError("Tier label must be 1-80 characters")
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(_ALLOWED_KINDS)}")
    if int(tier_rank) < 1:
        raise ValueError("tier_rank must be >= 1 (rank 0 is the implicit local tier)")
    if kind == "binding":
        if not provider_binding_id:
            raise ValueError("provider_binding_id is required when kind='binding'")
        from backend.provider_governance import get_binding

        if not get_binding(provider_binding_id):
            raise ValueError(f"No such provider binding: {provider_binding_id}")
    if quota_limit is not None and int(quota_limit) < 1:
        raise ValueError("quota_limit must be a positive integer or null (unlimited)")
    if float(quota_window_hours) <= 0:
        raise ValueError("quota_window_hours must be > 0")


def create_tier(
    label: str,
    tier_rank: int,
    kind: str = "binding",
    provider_binding_id: Optional[str] = None,
    model_override: str = "",
    quota_limit: Optional[int] = None,
    quota_window_hours: float = 24,
    is_active: bool = True,
) -> dict[str, Any]:
    _init_schema()
    _validate(label, tier_rank, kind, provider_binding_id, quota_limit, quota_window_hours)

    tier_id = f"rtier-{uuid.uuid4().hex[:10]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO router_tiers
                (id, label, tier_rank, kind, provider_binding_id, model_override,
                 quota_limit, quota_window_hours, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tier_id, str(label).strip(), int(tier_rank), kind,
                provider_binding_id if kind == "binding" else None,
                model_override or "", quota_limit, float(quota_window_hours),
                1 if is_active else 0, now, now,
            ),
        )
    return get_tier(tier_id)  # type: ignore[return-value]


def update_tier(tier_id: str, **fields: Any) -> dict[str, Any]:
    _init_schema()
    existing = get_tier(tier_id)
    if not existing:
        raise KeyError(tier_id)

    allowed = {
        "label", "tier_rank", "kind", "provider_binding_id", "model_override",
        "quota_limit", "quota_window_hours", "is_active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return existing

    merged = {**existing, **updates}
    _validate(
        merged["label"], merged["tier_rank"], merged["kind"], merged.get("provider_binding_id"),
        merged.get("quota_limit"), merged["quota_window_hours"],
    )
    if merged["kind"] == "local":
        updates["provider_binding_id"] = None

    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE router_tiers SET {set_clause}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), tier_id),
        )
    return get_tier(tier_id)  # type: ignore[return-value]


def delete_tier(tier_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM router_tiers WHERE id = ?", (tier_id,))
        return cursor.rowcount > 0


def list_active_tiers_resolved() -> list[dict[str, Any]]:
    """Ordered (by tier_rank) list of usable escalation tiers, each with live-
    resolved (api_base, api_key) credentials for kind='binding'. A tier whose
    binding is missing/revoked/unresolvable is silently dropped — an admin
    revoking a provider binding should not break the whole chain, it should
    just remove that rung."""
    from backend.provider_governance import resolve_binding_credentials

    resolved: list[dict[str, Any]] = []
    for tier in list_tiers():
        if not tier["is_active"]:
            continue
        if tier["kind"] == "local":
            resolved.append({**tier, "api_base": None, "api_key": None})
            continue
        creds = resolve_binding_credentials(tier["provider_binding_id"])
        if not creds:
            continue
        api_base, api_key = creds
        resolved.append({**tier, "api_base": api_base, "api_key": api_key})
    return resolved


_init_schema()
