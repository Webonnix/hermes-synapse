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
        cols = {row[1] for row in connection.execute("PRAGMA table_info(projects)").fetchall()}
        if "parent_id" not in cols:
            # Sub-project nesting. NULL = top-level project. Self-referential,
            # same soft-reference convention as session_metadata.project_id —
            # no FK constraint (sqlite tables here never use them), cycle
            # safety is enforced in code (see _would_cycle) instead.
            connection.execute("ALTER TABLE projects ADD COLUMN parent_id TEXT")


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


def _ancestors(connection: sqlite3.Connection, project_id: str) -> set[str]:
    """Every project_id above this one in the tree (its parent, grandparent, ...)."""
    seen: set[str] = set()
    current = project_id
    while current:
        row = connection.execute(
            "SELECT parent_id FROM projects WHERE id = ?", (current,)
        ).fetchone()
        if not row or not row["parent_id"]:
            break
        current = row["parent_id"]
        if current in seen:
            break  # already-corrupt cycle in stored data; stop rather than loop forever
        seen.add(current)
    return seen


def _would_cycle(connection: sqlite3.Connection, project_id: str, new_parent_id: Optional[str]) -> bool:
    """True if setting project_id's parent to new_parent_id would create a
    cycle — i.e. new_parent_id is project_id itself, or one of its own
    descendants (which, walked upward, includes project_id as an ancestor)."""
    if not new_parent_id:
        return False
    if new_parent_id == project_id:
        return True
    return project_id in _ancestors(connection, new_parent_id)


def create_project(
    name: str, description: str = "", is_active: bool = True, parent_id: Optional[str] = None,
) -> dict[str, Any]:
    _init_schema()
    clean_name = str(name).strip()
    if not _NAME.fullmatch(clean_name):
        raise ValueError("Project name must be 1-80 characters")

    project_id = f"proj-{uuid.uuid4().hex[:10]}"
    now = _now()
    with _connect() as connection:
        if parent_id and not connection.execute(
            "SELECT 1 FROM projects WHERE id = ?", (parent_id,)
        ).fetchone():
            raise ValueError(f"Parent project {parent_id} does not exist")
        connection.execute(
            """
            INSERT INTO projects (id, name, description, is_active, created_at, updated_at, parent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, clean_name, description or "", 1 if is_active else 0, now, now, parent_id or None),
        )
    return get_project(project_id)  # type: ignore[return-value]


def update_project(project_id: str, **fields: Any) -> dict[str, Any]:
    _init_schema()
    existing = get_project(project_id)
    if not existing:
        raise KeyError(project_id)

    allowed = {"name", "description", "is_active", "parent_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return existing
    if "name" in updates and not _NAME.fullmatch(str(updates["name"]).strip()):
        raise ValueError("Project name must be 1-80 characters")
    if "is_active" in updates:
        updates["is_active"] = 1 if updates["is_active"] else 0

    with _connect() as connection:
        if "parent_id" in updates:
            new_parent = updates["parent_id"] or None
            updates["parent_id"] = new_parent
            if new_parent and not connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (new_parent,)
            ).fetchone():
                raise ValueError(f"Parent project {new_parent} does not exist")
            if _would_cycle(connection, project_id, new_parent):
                raise ValueError("That would nest a project inside its own sub-project")

        set_clause = ", ".join(f"{col} = ?" for col in updates)
        connection.execute(
            f"UPDATE projects SET {set_clause}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), project_id),
        )
    return get_project(project_id)  # type: ignore[return-value]


def delete_project(project_id: str) -> bool:
    """Deletes a project. Its sub-projects and chat sessions are promoted to
    its own parent (or made top-level/unassigned if it had none) — deleting a
    folder never deletes what was inside it."""
    _init_schema()
    from backend.database import reassign_project_sessions

    existing = get_project(project_id)
    if not existing:
        return False
    parent_id = existing.get("parent_id")

    with _connect() as connection:
        connection.execute(
            "UPDATE projects SET parent_id = ?, updated_at = ? WHERE parent_id = ?",
            (parent_id, _now(), project_id),
        )
        cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        deleted = cursor.rowcount > 0

    if deleted:
        reassign_project_sessions(project_id, parent_id)
    return deleted


_init_schema()
