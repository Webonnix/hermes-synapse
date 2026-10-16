"""Durable journal of agent runs: write-ahead turns, status, and cost.

Why this exists
───────────────
A chat turn crosses several durability boundaries — the browser submits, the
agent starts streaming, tokens are relayed, the assistant message is persisted.
If the process dies anywhere in the middle, nothing on disk says the turn was
ever submitted: the message row is written at the end, and the in-memory stream
buffer goes with the process. Recovery had to be guessed at, and the browser was
left spinning on a run that no longer existed.

The journal records the submitted turn **before** any provider work starts, then
advances it. After a restart, ``reconcile_interrupted_runs()`` can say exactly
which turns were in flight instead of inferring it.

Scope
─────
This is deliberately *not* a token-level log — replaying tokens is the
websocket ring buffer's job (see :mod:`backend.websocket_manager`). The journal
holds one row per run plus a handful of lifecycle events, which also gives the
durable ``run_id``/``trace_id``/cost/latency record that run metadata previously
only had in process memory.

Failure policy
──────────────
Every write is best-effort: a journal that cannot write must never take a user's
chat turn down with it. Failures are logged at warning level, never swallowed
silently.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.run_journal")

# Statuses a run can hold. 'submitted' and 'running' are the only non-terminal
# ones, which is what makes "unfinished after a restart" a decidable question.
STATUS_SUBMITTED = "submitted"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
OPEN_STATUSES = (STATUS_SUBMITTED, STATUS_RUNNING)

# Lifecycle events are small; a cap keeps a pathological producer from growing
# the table without bound.
MAX_EVENT_PAYLOAD_CHARS = 8000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_trace_id() -> str:
    return uuid.uuid4().hex


# ── schema ──────────────────────────────────────────────────────────────
# One definition, rendered for both dialects. Keeping the DDL here rather than
# copied into each backend's init function means the two schemas cannot drift.
def schema_statements(dialect: str) -> List[str]:
    serial = "SERIAL PRIMARY KEY" if dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
        f"""
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            agent_id TEXT,
            status TEXT NOT NULL DEFAULT '{STATUS_SUBMITTED}',
            initiator TEXT NOT NULL DEFAULT 'web',
            model TEXT,
            provider TEXT,
            trace_id TEXT,
            user_text TEXT,
            submitted_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            latency_ms INTEGER,
            finish_reason TEXT,
            error_code TEXT,
            last_seq INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_chat ON agent_runs (chat_id, submitted_at)",
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs (status, submitted_at)",
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_trace ON agent_runs (trace_id)",
        f"""
        CREATE TABLE IF NOT EXISTS run_events (
            id {serial},
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events (run_id, seq)",
    ]


def init_schema(cursor, dialect: str) -> None:
    """Create the journal tables using an already-open cursor."""
    for statement in schema_statements(dialect):
        cursor.execute(statement)


# ── write path ──────────────────────────────────────────────────────────
def _connect():
    from backend.database import _get_backend

    return _get_backend()


def _execute(sql: str, params: tuple = ()) -> None:
    backend = _connect()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(backend.translate_placeholder(sql), params)
        conn.commit()


def _query(sql: str, params: tuple = ()) -> List[tuple]:
    backend = _connect()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(backend.translate_placeholder(sql), params)
        return cursor.fetchall()


def record_submitted(
    run_id: str,
    chat_id: str,
    *,
    user_text: str = "",
    initiator: str = "web",
    agent_id: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """Record a turn before any provider work starts. Returns the trace id.

    Called on the hot path, so it must not raise: a journal outage degrades
    observability, it does not cost the user their message.
    """
    if not run_id:
        return None
    trace_id = trace_id or new_trace_id()
    try:
        _execute(
            """
            INSERT INTO agent_runs
                (run_id, chat_id, agent_id, status, initiator, model, provider,
                 trace_id, user_text, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, str(chat_id), agent_id, STATUS_SUBMITTED, initiator, model,
             provider, trace_id, user_text, _now()),
        )
        append_event(run_id, "submitted", {"chat_id": str(chat_id), "initiator": initiator})
    except Exception as exc:  # pragma: no cover - exercised via the failure test
        logger.warning("Run journal: could not record submission for %s: %s", run_id, exc)
    return trace_id


def record_started(run_id: str, *, model: Optional[str] = None, provider: Optional[str] = None) -> None:
    if not run_id:
        return
    try:
        _execute(
            """
            UPDATE agent_runs
               SET status = ?, started_at = ?,
                   model = COALESCE(?, model), provider = COALESCE(?, provider)
             WHERE run_id = ?
            """,
            (STATUS_RUNNING, _now(), model, provider, run_id),
        )
        append_event(run_id, "started", {"model": model, "provider": provider})
    except Exception as exc:
        logger.warning("Run journal: could not record start for %s: %s", run_id, exc)


def record_finished(
    run_id: str,
    status: str,
    *,
    meta: Optional[Dict[str, Any]] = None,
    cost_usd: float = 0.0,
    error_code: Optional[str] = None,
    last_seq: int = 0,
) -> None:
    """Close a run out.

    ``user_text`` is cleared here on purpose: the journal holds the submitted
    turn only while it is at risk of being lost. Once the run is closed the
    ``messages`` table owns the transcript, and keeping a second copy would be a
    duplicate store of user content with no owner.
    """
    if not run_id:
        return
    meta = meta or {}
    try:
        _execute(
            """
            UPDATE agent_runs
               SET status = ?, finished_at = ?, user_text = NULL,
                   input_tokens = ?, output_tokens = ?, cost_usd = ?,
                   latency_ms = ?, finish_reason = ?, error_code = ?, last_seq = ?,
                   model = COALESCE(?, model), provider = COALESCE(?, provider)
             WHERE run_id = ?
            """,
            (
                status, _now(),
                int(meta.get("input_tokens") or 0),
                int(meta.get("output_tokens") or 0),
                float(cost_usd or 0.0),
                meta.get("latency_ms"),
                meta.get("finish_reason"),
                error_code,
                int(last_seq or 0),
                meta.get("model"), meta.get("provider"),
                run_id,
            ),
        )
        append_event(run_id, status, {"error_code": error_code} if error_code else {})
    except Exception as exc:
        logger.warning("Run journal: could not close run %s: %s", run_id, exc)


def append_event(run_id: str, kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
    if not run_id:
        return
    try:
        body = json.dumps(payload or {}, ensure_ascii=False)[:MAX_EVENT_PAYLOAD_CHARS]
        rows = _query("SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?", (run_id,))
        next_seq = int(rows[0][0] if rows and rows[0] and rows[0][0] is not None else 0) + 1
        _execute(
            "INSERT INTO run_events (run_id, seq, ts, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (run_id, next_seq, _now(), kind, body),
        )
    except Exception as exc:
        logger.warning("Run journal: could not append %s for %s: %s", kind, run_id, exc)


# ── read path ───────────────────────────────────────────────────────────
_RUN_COLUMNS = (
    "run_id", "chat_id", "agent_id", "status", "initiator", "model", "provider",
    "trace_id", "submitted_at", "started_at", "finished_at", "input_tokens",
    "output_tokens", "cost_usd", "latency_ms", "finish_reason", "error_code", "last_seq",
)


def _row_to_run(row: tuple) -> Dict[str, Any]:
    return dict(zip(_RUN_COLUMNS, row))


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    rows = _query(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM agent_runs WHERE run_id = ?", (run_id,)
    )
    return _row_to_run(rows[0]) if rows else None


def list_runs(chat_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 500))
    if chat_id:
        rows = _query(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM agent_runs WHERE chat_id = ? "
            f"ORDER BY submitted_at DESC LIMIT {limit}",
            (str(chat_id),),
        )
    else:
        rows = _query(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM agent_runs "
            f"ORDER BY submitted_at DESC LIMIT {limit}"
        )
    return [_row_to_run(row) for row in rows]


def list_open_runs() -> List[Dict[str, Any]]:
    """Runs the journal still believes are in flight."""
    placeholders = ", ".join("?" for _ in OPEN_STATUSES)
    rows = _query(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM agent_runs WHERE status IN ({placeholders}) "
        f"ORDER BY submitted_at DESC LIMIT 200",
        OPEN_STATUSES,
    )
    return [_row_to_run(row) for row in rows]


def get_run_events(run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 200), 1000))
    rows = _query(
        f"SELECT seq, ts, kind, payload FROM run_events WHERE run_id = ? ORDER BY seq LIMIT {limit}",
        (run_id,),
    )
    events = []
    for seq, ts, kind, payload in rows:
        try:
            parsed = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            parsed = {"raw": payload}
        events.append({"seq": seq, "ts": ts, "kind": kind, "payload": parsed})
    return events


# ── recovery ────────────────────────────────────────────────────────────
def reconcile_interrupted_runs() -> List[Dict[str, Any]]:
    """Close out runs left open by a process that died, and return them.

    A run in ``submitted``/``running`` at startup cannot still be executing: the
    task that owned it went with the previous process. Marking it ``interrupted``
    turns an invisible loss into a recorded, queryable fact — and stops the UI
    from waiting forever on a run nothing will ever finish.
    """
    try:
        open_runs = list_open_runs()
    except Exception as exc:
        logger.warning("Run journal: could not read open runs at startup: %s", exc)
        return []
    for run in open_runs:
        try:
            _execute(
                "UPDATE agent_runs SET status = ?, finished_at = ?, error_code = ? WHERE run_id = ?",
                (STATUS_INTERRUPTED, _now(), "process_restart", run["run_id"]),
            )
            append_event(run["run_id"], STATUS_INTERRUPTED, {"reason": "process_restart"})
        except Exception as exc:
            logger.warning("Run journal: could not reconcile %s: %s", run["run_id"], exc)
    if open_runs:
        logger.warning(
            "Run journal: %d run(s) were in flight when the process last stopped: %s",
            len(open_runs),
            ", ".join(run["run_id"] for run in open_runs[:10]),
        )
    return open_runs


def prune(max_age_days: int = 30) -> int:
    """Drop journal rows older than the retention window."""
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    try:
        stale = _query("SELECT run_id FROM agent_runs WHERE submitted_at < ?", (cutoff_iso,))
        for (run_id,) in stale:
            _execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
        _execute("DELETE FROM agent_runs WHERE submitted_at < ?", (cutoff_iso,))
        return len(stale)
    except Exception as exc:
        logger.warning("Run journal: prune failed: %s", exc)
        return 0
