"""Per-conversation AI/owner handling mode, keyed by (binding_id, platform, chat_id).

``agent_messenger_bindings`` and ``bot_subscribers`` describe the channel and
the (optional, token-mode-only) subscriber — neither carries any state for
"is the AI answering this specific chat right now, or is the owner handling
it personally". This module adds that missing per-conversation state:

``undecided``
    No choice offered yet, or the last one is stale — bot_access_gate.py will
    show the "talk to the AI now / wait for the owner" menu on the next
    incoming message (only for bindings with ``handling_menu_enabled``).
``pending_choice``
    The menu was sent; waiting for the visitor to pick an option.
``ai``
    The AI answers normally (either the visitor chose it, or the binding
    doesn't use the menu at all).
``waiting``
    The visitor chose to wait for the owner — bot_access_gate.py stays
    silent on further messages until this changes.
``notified``
    The visitor marked their question urgent and the owner has been pinged
    on Telegram. The AI keeps helping meanwhile; the very next message may
    still be a bare digit re-picking one of the menu options.
``owner_takeover``
    The owner manually took this conversation over from the admin
    dashboard — bot_access_gate.py stays silent regardless of platform or
    binding settings until it's released back to ``ai``.

Same self-contained-module pattern as bot_access.py: lazy ``_init_schema()``
at the top of every public function, defensive ``ALTER TABLE`` for columns
added after the table already existed on deployed installs.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database import DB_PATH

logger = logging.getLogger("hermes.conversation_state")

HANDLING_MODES = ("undecided", "pending_choice", "ai", "waiting", "notified", "owner_takeover")


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
            CREATE TABLE IF NOT EXISTS conversation_states (
                id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                handling_mode TEXT NOT NULL DEFAULT 'undecided',
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (binding_id, platform, chat_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_states_binding ON conversation_states (binding_id)"
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def get_state(binding_id: str, platform: str, chat_id: str) -> Optional[dict[str, Any]]:
    try:
        _init_schema()
        with _connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_states WHERE binding_id = ? AND platform = ? AND chat_id = ?",
                (binding_id, platform, str(chat_id)),
            ).fetchone()
        return _row_to_dict(row) if row else None
    except Exception:
        logger.exception(
            "Could not read conversation state for %s/%s/%s — treating as unset", binding_id, platform, chat_id
        )
        return None


def set_handling_mode(
    binding_id: str, platform: str, chat_id: str, mode: str, display_name: Optional[str] = None,
) -> dict[str, Any]:
    if mode not in HANDLING_MODES:
        raise ValueError(f"handling_mode must be one of {HANDLING_MODES}")
    _init_schema()
    now = _now()
    chat_key = str(chat_id)
    with _connect() as connection:
        existing = connection.execute(
            "SELECT id, display_name FROM conversation_states WHERE binding_id = ? AND platform = ? AND chat_id = ?",
            (binding_id, platform, chat_key),
        ).fetchone()
        if existing:
            name = display_name if display_name else existing["display_name"]
            connection.execute(
                "UPDATE conversation_states SET handling_mode = ?, display_name = ?, updated_at = ? WHERE id = ?",
                (mode, name, now, existing["id"]),
            )
            state_id = existing["id"]
        else:
            state_id = f"conv-{uuid.uuid4().hex[:12]}"
            connection.execute(
                """
                INSERT INTO conversation_states
                    (id, binding_id, platform, chat_id, display_name, handling_mode, updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (state_id, binding_id, platform, chat_key, display_name or "", mode, now, now),
            )
        row = connection.execute("SELECT * FROM conversation_states WHERE id = ?", (state_id,)).fetchone()
    return _row_to_dict(row)


def touch_state(binding_id: str, platform: str, chat_id: str, display_name: Optional[str] = None) -> None:
    """Refreshes ``updated_at`` (and ``display_name`` if given) without
    changing the handling mode — keeps an active conversation from being
    treated as stale by the reset-after-inactivity check."""
    try:
        _init_schema()
        now = _now()
        chat_key = str(chat_id)
        with _connect() as connection:
            if display_name:
                connection.execute(
                    """
                    UPDATE conversation_states SET updated_at = ?, display_name = ?
                    WHERE binding_id = ? AND platform = ? AND chat_id = ?
                    """,
                    (now, display_name, binding_id, platform, chat_key),
                )
            else:
                connection.execute(
                    "UPDATE conversation_states SET updated_at = ? WHERE binding_id = ? AND platform = ? AND chat_id = ?",
                    (now, binding_id, platform, chat_key),
                )
    except Exception:
        logger.exception("Could not touch conversation state for %s/%s/%s", binding_id, platform, chat_id)


def session_id_for_conversation(binding_id: str, platform: str, chat_id: str) -> str:
    """The conversation's key in the shared ``messages`` table. Same formula the
    channel managers and bot_access.create_subscriber use, so the history shown
    here is literally the one the agent reads when it answers."""
    from backend.bot_access import session_id_for

    return session_id_for(platform, binding_id, str(chat_id))


def list_states(binding_id: str) -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM conversation_states WHERE binding_id = ? ORDER BY updated_at DESC", (binding_id,)
        ).fetchall()
    states = [_row_to_dict(row) for row in rows]
    for state in states:
        state["session_id"] = session_id_for_conversation(
            state["binding_id"], state["platform"], state["chat_id"]
        )
    _attach_history_stats(states)
    return states


def _attach_history_stats(states: list[dict[str, Any]]) -> None:
    """Fills in how much has been said in each conversation, in one grouped
    query for the whole list — this panel is meant to stay usable with a few
    hundred people on a binding, not to fan out into a query per row."""
    for state in states:
        state.setdefault("messages_count", 0)
        state.setdefault("last_message_at", "")
        state.setdefault("last_message_preview", "")
        state.setdefault("last_message_role", "")
    try:
        from backend.database import get_session_message_stats

        stats = get_session_message_stats([s.get("session_id", "") for s in states])
    except Exception:
        logger.exception("Could not load conversation history stats")
        return
    for state in states:
        state.update(stats.get(state.get("session_id", ""), {}))


def history(binding_id: str, platform: str, chat_id: str, limit: int = 200) -> dict[str, Any]:
    """The stored conversation with one person, newest ``limit`` messages."""
    from backend.database import get_session_transcript

    session_id = session_id_for_conversation(binding_id, platform, chat_id)
    return {
        "session_id": session_id,
        "messages": get_session_transcript(session_id, max(1, min(int(limit), 500))),
    }


def clear_history(binding_id: str, platform: str, chat_id: str) -> dict[str, Any]:
    """Wipes what the agent remembers about this person — the next message
    starts from an empty context. The handling mode is left alone on purpose:
    forgetting the conversation and deciding who answers it are separate calls."""
    from backend.database import clear_chat_history

    session_id = session_id_for_conversation(binding_id, platform, chat_id)
    clear_chat_history(session_id)
    return {"status": "success", "session_id": session_id}
