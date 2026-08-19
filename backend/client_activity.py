"""Audit trail for the commercial client module.

One append-only table (`client_activity_log`) behind one writer, shared by
`backend/currency.py`, `backend/clients.py` and `backend/client_billing.py`.
It lives in its own module so those three can all log without importing each
other — currency settings changes have to be auditable even though currency
knows nothing about clients.

This is deliberately *not* `backend/activity_logger.py`: that one is the
operational feed of what the agent network is doing right now (200 entries in
memory, broadcast over the websocket). This one is a permanent business record
— who changed a rate, who marked an invoice paid — and is queried by client,
not tailed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.database import DB_PATH

logger = logging.getLogger("hermes.client_activity")

# The event vocabulary. Anything not in here is still stored (the table takes
# free text) but callers should add the constant rather than pass a literal, so
# the set of things the business log can say stays greppable.
CLIENT_CREATED = "CLIENT_CREATED"
CLIENT_UPDATED = "CLIENT_UPDATED"
CLIENT_ARCHIVED = "CLIENT_ARCHIVED"
SERVICE_CREATED = "SERVICE_CREATED"
SERVICE_UPDATED = "SERVICE_UPDATED"
CONTACT_CREATED = "CONTACT_CREATED"
CONTACT_UPDATED = "CONTACT_UPDATED"
AGENT_CONNECTED = "AGENT_CONNECTED"
AGENT_DISCONNECTED = "AGENT_DISCONNECTED"
AGENT_RECONNECTED = "AGENT_RECONNECTED"
AGENT_ERROR = "AGENT_ERROR"
AGENT_HEALTH_CHECKED = "AGENT_HEALTH_CHECKED"
INVOICE_CREATED = "INVOICE_CREATED"
INVOICE_ISSUED = "INVOICE_ISSUED"
INVOICE_PAID = "INVOICE_PAID"
INVOICE_CANCELLED = "INVOICE_CANCELLED"
PAYMENT_CREATED = "PAYMENT_CREATED"
CURRENCY_RATE_CHANGED = "CURRENCY_RATE_CHANGED"
DISPLAY_CURRENCY_CHANGED = "DISPLAY_CURRENCY_CHANGED"

_schema_ready = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """Creates the table, reusing the caller's connection when there is one.

    That reuse is not an optimisation: `log_event` is called from inside other
    modules' write transactions, and opening a second connection to run CREATE
    TABLE while the first holds the write lock deadlocks against it.
    """
    global _schema_ready
    if _schema_ready:
        return
    owned = conn is None
    conn = conn or _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_activity_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                client_id TEXT,
                entity_type TEXT NOT NULL DEFAULT '',
                entity_id TEXT,
                summary TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                actor_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_activity_client "
            "ON client_activity_log (client_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_client_activity_type "
            "ON client_activity_log (event_type, created_at DESC)"
        )
        if owned:
            conn.commit()
    finally:
        if owned:
            conn.close()
    _schema_ready = True


def ensure_schema() -> None:
    """Creates this module's tables now, outside any caller transaction.

    Modules that write inside a `BEGIN IMMEDIATE` call this first: lazily
    running CREATE TABLE on a second connection while the first holds the
    write lock deadlocks, so the DDL has to happen before the transaction
    opens, not during it."""
    _init_schema()


def reset_schema_cache() -> None:
    """Tests point DB_PATH at a fresh tmp file between cases; the cached
    "already created" flag would otherwise skip CREATE TABLE on the new file."""
    global _schema_ready
    _schema_ready = False


def log_event(
    event_type: str,
    *,
    client_id: Optional[str] = None,
    entity_type: str = "",
    entity_id: Optional[str] = None,
    summary: str = "",
    payload: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Appends one business event.

    Pass `conn` to enlist in the caller's open transaction — an invoice and its
    "INVOICE_CREATED" row must commit or roll back together, never separately.
    """
    _init_schema(conn)
    entry = {
        "id": f"cal-{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "client_id": client_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "summary": summary,
        "payload": payload or {},
        "actor_id": actor_id,
        "created_at": _now(),
    }
    sql = (
        "INSERT INTO client_activity_log "
        "(id, event_type, client_id, entity_type, entity_id, summary, payload, actor_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        entry["id"], event_type, client_id, entity_type, entity_id, summary,
        json.dumps(entry["payload"], ensure_ascii=False), actor_id, entry["created_at"],
    )
    if conn is not None:
        conn.execute(sql, params)
    else:
        with _connect() as own:
            own.execute(sql, params)
    logger.info("client audit: %s %s %s", event_type, entity_type or "-", summary)
    return entry


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    entry = dict(row)
    try:
        entry["payload"] = json.loads(entry.get("payload") or "{}")
    except (TypeError, ValueError):
        entry["payload"] = {}
    return entry


def list_events(
    *,
    client_id: Optional[str] = None,
    event_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    _init_schema()
    where: List[str] = []
    params: List[Any] = []
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if entity_id:
        where.append("entity_id = ?")
        params.append(entity_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            # rowid, not id: ids are random hex, and several events routinely
            # land in the same second (an invoice and its payment), so the
            # timestamp alone cannot order them. rowid is insertion order.
            f"SELECT * FROM client_activity_log {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
