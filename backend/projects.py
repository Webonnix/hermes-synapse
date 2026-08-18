"""Projects — named groupings a chat session and/or an agent can belong to.

Mirrors agent_tiers.py: plain admin-editable metadata, no secrets and no new
network surface, so this is plain owner-only CRUD (R0) with no proposal/
approval flow, unlike provider_governance.py.
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
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
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


def list_projects() -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM projects ORDER BY created_at ASC").fetchall()
    return [_row_to_dict(row) for row in rows]


def get_project(project_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row_to_dict(row) if row else None


def create_project(name: str, description: str = "", is_active: bool = True) -> dict[str, Any]:
    _init_schema()
    clean_name = str(name).strip()
    if not _NAME.fullmatch(clean_name):
        raise ValueError("Project name must be 1-80 characters")

    project_id = f"proj-{uuid.uuid4().hex[:10]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO projects (id, name, description, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, clean_name, description or "", 1 if is_active else 0, now, now),
        )
    return get_project(project_id)  # type: ignore[return-value]


def update_project(project_id: str, **fields: Any) -> dict[str, Any]:
    _init_schema()
    existing = get_project(project_id)
    if not existing:
        raise KeyError(project_id)

    allowed = {"name", "description", "is_active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return existing
    if "name" in updates and not _NAME.fullmatch(str(updates["name"]).strip()):
        raise ValueError("Project name must be 1-80 characters")
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE projects SET {set_clause}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), project_id),
        )
    return get_project(project_id)  # type: ignore[return-value]


def delete_project(project_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0


_init_schema()
