"""Per-chat settings for the simple chat: system prompt, sampling, reasoning, incognito.

The chat UI lets a conversation carry its own instructions and generation
settings, the way a local-model studio does, instead of every chat sharing the
one global configuration. They live here, keyed by session id, and are applied
by ``VexaAgent.respond`` for the duration of one turn (see ``_CHAT_SETTINGS`` in
agent.py) — never written back into the agent's global attributes.

``ephemeral`` marks an incognito chat: it is hidden from the session list, the
turn skips long-term memory extraction, and ``purge_ephemeral_sessions`` drops
its messages at startup so nothing outlives the server process.

Same self-contained-module pattern as conversation_state.py: lazy
``_init_schema()`` at the top of every public function. Unlike it, the path is
read from ``database.DB_PATH`` on every connect rather than imported once:
``VexaAgent.respond`` reads these settings on every turn, so a test that points
the database elsewhere must point this module there too, or agent tests would
write into the real data directory.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend import database

logger = logging.getLogger("hermes.chat_settings")

MAX_SYSTEM_PROMPT_CHARS = 8000
THINK_MODES = ("on", "off")

DEFAULTS: Dict[str, Any] = {
    "system_prompt": "",
    "temperature": None,
    "top_p": None,
    "max_tokens": None,
    "think": None,
    "ephemeral": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(database.DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_session_settings (
                session_id TEXT PRIMARY KEY,
                system_prompt TEXT NOT NULL DEFAULT '',
                temperature REAL,
                top_p REAL,
                max_tokens INTEGER,
                think TEXT,
                ephemeral INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )


def _row_to_settings(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if row is None:
        return dict(DEFAULTS)
    return {
        "system_prompt": row["system_prompt"] or "",
        "temperature": row["temperature"],
        "top_p": row["top_p"],
        "max_tokens": row["max_tokens"],
        "think": row["think"] if row["think"] in THINK_MODES else None,
        "ephemeral": bool(row["ephemeral"]),
    }


def _bounded_float(value: Any, field: str, low: float, high: float) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if not low <= number <= high:
        raise ValueError(f"{field} must be between {low} and {high}")
    return number


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a client payload into stored settings. Raises ValueError."""
    system_prompt = str(payload.get("system_prompt") or "")
    if len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
        raise ValueError(f"system_prompt is longer than {MAX_SYSTEM_PROMPT_CHARS} characters")
    max_tokens = payload.get("max_tokens")
    if max_tokens is None or max_tokens == "":
        max_tokens = None
    else:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            raise ValueError("max_tokens must be an integer")
        if not 1 <= max_tokens <= 32768:
            raise ValueError("max_tokens must be between 1 and 32768")
    think = payload.get("think")
    if think in ("", None):
        think = None
    elif think not in THINK_MODES:
        raise ValueError("think must be 'on', 'off' or empty")
    return {
        "system_prompt": system_prompt.strip(),
        "temperature": _bounded_float(payload.get("temperature"), "temperature", 0.0, 2.0),
        "top_p": _bounded_float(payload.get("top_p"), "top_p", 0.0, 1.0),
        "max_tokens": max_tokens,
        "think": think,
        "ephemeral": bool(payload.get("ephemeral", False)),
    }


def get_settings(session_id: str) -> Dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM chat_session_settings WHERE session_id = ?", (session_id,)
        ).fetchone()
    return _row_to_settings(row)


def save_settings(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = validate(payload)
    _init_schema()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO chat_session_settings
                (session_id, system_prompt, temperature, top_p, max_tokens, think, ephemeral, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                system_prompt = excluded.system_prompt,
                temperature = excluded.temperature,
                top_p = excluded.top_p,
                max_tokens = excluded.max_tokens,
                think = excluded.think,
                ephemeral = excluded.ephemeral,
                updated_at = excluded.updated_at
            """,
            (
                session_id, settings["system_prompt"], settings["temperature"], settings["top_p"],
                settings["max_tokens"], settings["think"], int(settings["ephemeral"]), _now(),
            ),
        )
    return settings


def copy_settings(source_session_id: str, target_session_id: str) -> None:
    """Give a branch the same settings as the chat it was cut from."""
    settings = get_settings(source_session_id)
    if settings != DEFAULTS:
        save_settings(target_session_id, settings)


def delete_settings(session_id: str) -> None:
    _init_schema()
    with _connect() as connection:
        connection.execute("DELETE FROM chat_session_settings WHERE session_id = ?", (session_id,))


def is_ephemeral(session_id: str) -> bool:
    return bool(get_settings(session_id).get("ephemeral"))


def ephemeral_session_ids() -> List[str]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT session_id FROM chat_session_settings WHERE ephemeral = 1"
        ).fetchall()
    return [row["session_id"] for row in rows]


def purge_ephemeral_sessions() -> int:
    """Drop every incognito chat's messages and metadata. Returns how many."""
    session_ids = ephemeral_session_ids()
    if not session_ids:
        return 0
    for session_id in session_ids:
        try:
            database.clear_chat_history(session_id)
            database.delete_session_title(session_id)
            delete_settings(session_id)
        except Exception:
            logger.exception("Could not purge incognito chat %s", session_id)
    return len(session_ids)
