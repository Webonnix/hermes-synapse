"""Follow-ups and reminders: the CRM's answer to "what do I do next."

A client book and a sales pipeline both accumulate facts — a deal's stage, an
invoice's status — but neither one tells anyone to actually *do* something.
This module is the one place that does: a task with a due date, optionally
tied to a client or a deal (or neither — a follow-up does not have to be
about anyone in particular), that shows up as overdue or due-today until it
is marked done.

Deliberately not merged into `client_activity`'s audit log: that table is
append-only history of what already happened, and a task is the opposite — a
thing that has not happened yet and can be edited, rescheduled or cancelled
right up until it is.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend import client_activity
from backend.database import DB_PATH

logger = logging.getLogger("hermes.client_tasks")

TASK_STATUSES = ("OPEN", "DONE", "CANCELLED")
TASK_PRIORITIES = ("LOW", "NORMAL", "HIGH")

_schema_ready = False


def _now() -> str:
    # Microsecond precision, unlike the rest of the commercial model's
    # second-precision timestamps: this module orders rows by "which one was
    # touched more recently" (the kanban board, the task list), and two edits
    # a person makes back to back land in the same second often enough that a
    # second-precision tie would visibly reorder cards on screen.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _new_id() -> str:
    return f"task-{uuid.uuid4().hex[:12]}"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _check_enum(value: Any, allowed: tuple, field: str, default: Optional[str] = None) -> str:
    text = str(value or "").strip().upper()
    if not text and default is not None:
        return default
    if text not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)} (got {value!r})")
    return text


def _init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    client_activity.ensure_schema()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_tasks (
                id TEXT PRIMARY KEY,
                client_id TEXT,
                deal_id TEXT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                due_at TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                priority TEXT NOT NULL DEFAULT 'NORMAL',
                assignee TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_client_tasks_client ON client_tasks (client_id);
            CREATE INDEX IF NOT EXISTS idx_client_tasks_deal ON client_tasks (deal_id);
            CREATE INDEX IF NOT EXISTS idx_client_tasks_status_due ON client_tasks (status, due_at);
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
        "dealId": row["deal_id"],
        "title": row["title"],
        "description": row["description"],
        "dueAt": row["due_at"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee": row["assignee"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "completedAt": row["completed_at"],
        # Derived, not stored: "overdue" depends on the clock, not on anything
        # that was true when the row was written.
        "overdue": bool(row["due_at"] and row["due_at"][:10] < _today() and row["status"] == "OPEN"),
    }


def create_task(payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    title = _clean(payload.get("title"))
    if not title:
        raise ValueError("Название задачи обязательно")
    priority = _check_enum(payload.get("priority"), TASK_PRIORITIES, "priority", default="NORMAL")
    now = _now()
    task_id = _new_id()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO client_tasks
                (id, client_id, deal_id, title, description, due_at, status, priority, assignee,
                 created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, NULL)
            """,
            (
                task_id, _clean(payload.get("clientId")) or None, _clean(payload.get("dealId")) or None,
                title, _clean(payload.get("description")), _clean(payload.get("dueAt")) or None,
                priority, _clean(payload.get("assignee")), now, now,
            ),
        )
        client_activity.log_event(
            client_activity.TASK_CREATED, client_id=_clean(payload.get("clientId")) or None,
            entity_type="task", entity_id=task_id, summary=f"Задача создана: {title}",
            payload={"dueAt": _clean(payload.get("dueAt")) or None}, actor_id=actor_id, conn=conn,
        )
    return get_task(task_id)  # type: ignore[return-value]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM client_tasks WHERE id = ?", (task_id,)).fetchone()
    return _row(row) if row else None


def list_tasks(
    client_id: Optional[str] = None,
    deal_id: Optional[str] = None,
    status: Optional[str] = None,
    overdue_only: bool = False,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    _init_schema()
    clauses: List[str] = []
    params: List[Any] = []
    if client_id:
        clauses.append("client_id = ?")
        params.append(client_id)
    if deal_id:
        clauses.append("deal_id = ?")
        params.append(deal_id)
    if status:
        clauses.append("status = ?")
        params.append(_check_enum(status, TASK_STATUSES, "status"))
    if overdue_only:
        clauses.append("status = 'OPEN' AND due_at IS NOT NULL AND substr(due_at, 1, 10) < ?")
        params.append(_today())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            # Open tasks with a due date first (soonest due first), then open
            # tasks with no due date, then everything already closed —
            # matches how a to-do list reads: what needs doing shows up top.
            f"""
            SELECT * FROM client_tasks{where}
            ORDER BY
                CASE WHEN status = 'OPEN' THEN 0 ELSE 1 END,
                CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                due_at ASC,
                created_at DESC,
                rowid DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [_row(row) for row in rows]


_UPDATABLE = ("title", "description", "dueAt", "priority", "assignee", "clientId", "dealId")
_COLUMNS = {
    "title": "title", "description": "description", "dueAt": "due_at", "priority": "priority",
    "assignee": "assignee", "clientId": "client_id", "dealId": "deal_id",
}


def update_task(task_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    existing = get_task(task_id)
    if not existing:
        raise KeyError(task_id)

    updates: Dict[str, Any] = {}
    for field in _UPDATABLE:
        if field in payload:
            value = payload[field]
            if field == "priority":
                updates["priority"] = _check_enum(value, TASK_PRIORITIES, "priority")
            elif field == "title":
                cleaned = _clean(value)
                if not cleaned:
                    raise ValueError("Название задачи обязательно")
                updates["title"] = cleaned
            else:
                updates[_COLUMNS[field]] = _clean(value) or None

    status_changed = False
    if "status" in payload:
        new_status = _check_enum(payload["status"], TASK_STATUSES, "status")
        if new_status != existing["status"]:
            status_changed = True
            updates["status"] = new_status
            updates["completed_at"] = _now() if new_status == "DONE" else None

    if not updates:
        return existing
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with _connect() as conn:
        conn.execute(f"UPDATE client_tasks SET {assignments} WHERE id = ?", (*updates.values(), task_id))
        if status_changed and updates.get("status") == "DONE":
            client_activity.log_event(
                client_activity.TASK_COMPLETED, client_id=existing["clientId"], entity_type="task",
                entity_id=task_id, summary=f"Задача выполнена: {existing['title']}", actor_id=actor_id, conn=conn,
            )
    return get_task(task_id)  # type: ignore[return-value]


def complete_task(task_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    return update_task(task_id, {"status": "DONE"}, actor_id=actor_id)


def reopen_task(task_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    return update_task(task_id, {"status": "OPEN"}, actor_id=actor_id)


def cancel_task(task_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    return update_task(task_id, {"status": "CANCELLED"}, actor_id=actor_id)


def delete_task(task_id: str) -> None:
    existing = get_task(task_id)
    if not existing:
        raise KeyError(task_id)
    with _connect() as conn:
        conn.execute("DELETE FROM client_tasks WHERE id = ?", (task_id,))


def task_stats() -> Dict[str, int]:
    """Powers the «Задачи» sidebar badge and the tasks pane's KPI row, in one
    query rather than three — every count is a `SUM(CASE …)` over one pass of
    the table instead of one query per number."""
    _init_schema()
    today = _today()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
                SUM(CASE WHEN status = 'OPEN' AND due_at IS NOT NULL AND substr(due_at, 1, 10) < ?
                    THEN 1 ELSE 0 END) AS overdue_count,
                SUM(CASE WHEN status = 'OPEN' AND due_at IS NOT NULL AND substr(due_at, 1, 10) = ?
                    THEN 1 ELSE 0 END) AS due_today_count
            FROM client_tasks
            """,
            (today, today),
        ).fetchone()
    return {
        "open": row["open_count"] or 0,
        "overdue": row["overdue_count"] or 0,
        "dueToday": row["due_today_count"] or 0,
    }
