"""Freeform notes on a client — what was said on a call, what was agreed
verbally, why a discount was given.

Distinct from `client_activity`'s audit log on purpose: that log is an
append-only record the system writes about *itself* ("invoice #4 was marked
paid") and nothing may edit or delete an entry once written. A note is a
person writing about the *relationship* — it is theirs, so unlike the audit
log it can be corrected or removed.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend import client_activity
from backend.database import DB_PATH

logger = logging.getLogger("hermes.client_notes")

_schema_ready = False


def _now() -> str:
    # Microsecond precision, unlike the rest of the commercial model's
    # second-precision timestamps: this module orders rows by "which one was
    # touched more recently" (the kanban board, the task list), and two edits
    # a person makes back to back land in the same second often enough that a
    # second-precision tie would visibly reorder cards on screen.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _new_id() -> str:
    return f"note-{uuid.uuid4().hex[:12]}"


def _init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    client_activity.ensure_schema()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_notes (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_client_notes_client ON client_notes (client_id);
            """
        )
    _schema_ready = True


def reset_schema_cache() -> None:
    global _schema_ready
    _schema_ready = False


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "clientId": row["client_id"],
        "author": row["author"],
        "body": row["body"],
        "pinned": bool(row["pinned"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def create_note(client_id: str, body: str, author: str = "", pinned: bool = False,
                 actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    client_id = str(client_id or "").strip()
    body = str(body or "").strip()
    if not client_id:
        raise ValueError("client_id обязателен")
    if not body:
        raise ValueError("Текст заметки не может быть пустым")
    now = _now()
    note_id = _new_id()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO client_notes (id, client_id, author, body, pinned, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note_id, client_id, str(author or "").strip(), body, int(pinned), now, now),
        )
        client_activity.log_event(
            client_activity.NOTE_CREATED, client_id=client_id, entity_type="note", entity_id=note_id,
            summary=body[:140], actor_id=actor_id, conn=conn,
        )
    return get_note(note_id)  # type: ignore[return-value]


def get_note(note_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM client_notes WHERE id = ?", (note_id,)).fetchone()
    return _row(row) if row else None


def list_notes(client_id: str) -> List[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        # rowid as a last-resort tiebreaker — microsecond timestamps make a real
        # tie astronomically unlikely, but SQLite's sort order on an exact tie is
        # otherwise unspecified.
        rows = conn.execute(
            "SELECT * FROM client_notes WHERE client_id = ? ORDER BY pinned DESC, created_at DESC, rowid DESC",
            (client_id,),
        ).fetchall()
    return [_row(row) for row in rows]


def update_note(note_id: str, body: Optional[str] = None, pinned: Optional[bool] = None) -> Dict[str, Any]:
    existing = get_note(note_id)
    if not existing:
        raise KeyError(note_id)
    updates: Dict[str, Any] = {}
    if body is not None:
        cleaned = str(body).strip()
        if not cleaned:
            raise ValueError("Текст заметки не может быть пустым")
        updates["body"] = cleaned
    if pinned is not None:
        updates["pinned"] = int(pinned)
    if not updates:
        return existing
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with _connect() as conn:
        conn.execute(f"UPDATE client_notes SET {assignments} WHERE id = ?", (*updates.values(), note_id))
    return get_note(note_id)  # type: ignore[return-value]


def delete_note(note_id: str) -> None:
    existing = get_note(note_id)
    if not existing:
        raise KeyError(note_id)
    with _connect() as conn:
        conn.execute("DELETE FROM client_notes WHERE id = ?", (note_id,))
