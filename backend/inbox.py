"""Inbox: one archive of every conversation a customer has with this installation.

What already existed, and why this module sits *on top of* it rather than beside it
────────────────────────────────────────────────────────────────────────────────────
* ``conversation_states`` (backend/conversation_state.py) owns **who answers** a
  conversation — the handling menu, ``waiting``, ``owner_takeover``. The inbox never
  stores a second copy of that: ``handling`` in every API response is derived from
  that row plus the last routing-rule outcome (see :func:`effective_handling`).
* ``messages`` (backend/database.py) is the **agent's memory** of a conversation, keyed
  by session id. It is what the model reads, and "Забыть собеседника" deletes it.
* ``pending_channel_replies`` (backend/channel_replies.py) is the draft queue.

None of those is an archive a person can work from: the memory can be wiped, holds no
delivery status and no record of what a human typed or whispered, and the activity feed
(backend/channel_activity.py) lives in process memory. So the inbox keeps its own
append-only record of **what crossed the wire** — who said it, whether it was sent,
drafted, discarded or failed — keyed by the same (binding, platform, chat) triple the
state row uses. Clearing the agent's memory deliberately leaves the archive alone.

Every writer here is best-effort. The inbox is a record *about* a conversation; a
failure to write it must never cost the customer their reply, so the hooks in
bot_access_gate / channel_replies swallow and log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("hermes.inbox")

SPEAKERS = ("customer", "agent", "human", "whisper", "system")
DELIVERIES = ("sent", "draft", "discarded", "failed")
STATUSES = ("open", "closed")
HANDLINGS = ("ai", "human", "blocked")

# conversation_states modes in which nobody automated answers: the owner took the
# conversation over, or the person asked to wait for him.
HUMAN_STATE_MODES = ("owner_takeover", "waiting")

MAX_TEXT_CHARS = 20000
PREVIEW_CHARS = 160
WS_ROUTING_KEY = "inbox"

_schema_ready_for: Optional[str] = None
_schema_lock = threading.Lock()
_fts_available: Optional[bool] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None


# ── plumbing ────────────────────────────────────────────────────────────────
def _db_path() -> str:
    # Read through the database module on every call instead of copying DB_PATH at
    # import: the gate/channel test suites repoint database.DB_PATH at a temp file,
    # and a copied path would quietly archive their fixtures into the real DB.
    from backend import database

    return database.DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    # SQLite's LOWER()/LIKE fold ASCII only; Cyrillic names would miss search in
    # another case. Same fix as backend/clients.py.
    connection.create_function(
        "unicode_lower", 1, lambda value: value.lower() if isinstance(value, str) else value
    )
    return connection


def reset_schema_cache() -> None:
    global _schema_ready_for, _fts_available
    _schema_ready_for = None
    _fts_available = None


def _probe_fts(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._inbox_fts_probe USING fts5(x)")
        connection.execute("DROP TABLE IF EXISTS temp._inbox_fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _init_schema() -> None:
    global _schema_ready_for, _fts_available
    path = _db_path()
    if _schema_ready_for == path:
        return
    with _schema_lock:
        if _schema_ready_for == path:
            return
        with _connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox_conversations (
                    id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    subagent_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT,
                    client_id TEXT,
                    contact_id TEXT,
                    display_name TEXT NOT NULL DEFAULT '',
                    external_user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    unread_count INTEGER NOT NULL DEFAULT 0,
                    last_message_at TEXT,
                    last_message_preview TEXT NOT NULL DEFAULT '',
                    last_speaker TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    cost_usd REAL NOT NULL DEFAULT 0,
                    last_rule_action TEXT,
                    last_rule_pattern TEXT,
                    channel_meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (binding_id, platform, chat_id)
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_conv_last ON inbox_conversations (last_message_at DESC);
                CREATE INDEX IF NOT EXISTS idx_inbox_conv_client ON inbox_conversations (client_id);
                CREATE INDEX IF NOT EXISTS idx_inbox_conv_session ON inbox_conversations (session_id);

                CREATE TABLE IF NOT EXISTS inbox_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    delivery TEXT NOT NULL DEFAULT 'sent',
                    pending_reply_id TEXT,
                    external_event_id TEXT,
                    consumed_at TEXT,
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_msg_conv ON inbox_messages (conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_inbox_msg_draft ON inbox_messages (pending_reply_id);
                -- A channel that redelivers the same event (a webhook retry, a
                -- reconnecting long-poll) must not create a second customer line
                -- nor a second agent run. NULL ids are exempt: not every channel
                -- carries one.
                CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_msg_event
                    ON inbox_messages (conversation_id, external_event_id)
                    WHERE external_event_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS inbox_identities (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    contact_id TEXT,
                    client_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (platform, external_id)
                );

                CREATE TABLE IF NOT EXISTS inbox_attachments (
                    id TEXT PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    mime TEXT NOT NULL DEFAULT '',
                    storage_ref TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER,
                    transcript TEXT,
                    meta TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_att_msg ON inbox_attachments (message_id);
                """
            )
            fts = _probe_fts(connection)
            if fts:
                # unicode61 folds case for Cyrillic as well as Latin, which is the
                # whole reason to prefer it over LIKE here.
                connection.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS inbox_messages_fts USING fts5(
                        text, content='inbox_messages', content_rowid='id', tokenize='unicode61'
                    );
                    CREATE TRIGGER IF NOT EXISTS inbox_messages_ai AFTER INSERT ON inbox_messages BEGIN
                        INSERT INTO inbox_messages_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS inbox_messages_ad AFTER DELETE ON inbox_messages BEGIN
                        INSERT INTO inbox_messages_fts(inbox_messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS inbox_messages_au AFTER UPDATE OF text ON inbox_messages BEGIN
                        INSERT INTO inbox_messages_fts(inbox_messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
                        INSERT INTO inbox_messages_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    """
                )
            # conversation_states belongs to backend/conversation_state.py; the list
            # query LEFT JOINs it, so make sure it exists on a fresh database.
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
        _fts_available = fts
        _schema_ready_for = path


def fts_enabled() -> bool:
    _init_schema()
    return bool(_fts_available)


def _clip(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _preview(text: str) -> str:
    return (text or "").strip().replace("\n", " ")[:PREVIEW_CHARS]


def _json_loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


# ── live updates ────────────────────────────────────────────────────────────
def bind_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once from the app lifespan. Most writers here run in worker threads
    (the gate is always behind asyncio.to_thread), where there is no loop to put a
    WebSocket send on — so they hand it to the main one."""
    global _event_loop
    _event_loop = loop


def _emit(conversation_id: str, kind: str) -> None:
    """Tells subscribed dashboards that something changed, and nothing else.

    The frame carries ids only, never message text: the WebSocket has no
    per-permission filter of its own beyond the subscribe check, so the content
    stays behind the REST endpoints, which enforce RBAC on every read."""
    loop = _event_loop
    if loop is None or loop.is_closed():
        return
    payload = {"type": "inbox_update", "conversation_id": conversation_id, "kind": kind}
    try:
        from backend.websocket_manager import manager

        asyncio.run_coroutine_threadsafe(manager.send_chat_event(WS_ROUTING_KEY, payload), loop)
    except Exception:
        logger.exception("Could not emit inbox update for %s", conversation_id)


# ── identities → CRM ────────────────────────────────────────────────────────
_PHONE = re.compile(r"^\+?[\d\s\-().]{7,20}$")


def normalize_identity(value: Any) -> str:
    """One spelling per person-identifier, so a rule or a CRM contact written as
    ``@Ivan``, ``https://t.me/ivan`` or ``ivan`` all mean the same handle, and
    ``+7 (900) 123-45-67`` matches ``79001234567``."""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "tg://resolve?domain="):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break
    if _PHONE.match(lowered) and sum(ch.isdigit() for ch in lowered) >= 7:
        return "+" + "".join(ch for ch in lowered if ch.isdigit())
    if lowered.startswith("mailto:"):
        lowered = lowered[len("mailto:"):]
    return lowered.lstrip("@")


def sender_identities(platform: str, chat_id: str, external_user_id: str = "", display_name: str = "") -> List[str]:
    """Every identifier this sender arrived with, normalized and de-duplicated. A
    display name only counts when it is a handle (``@name``) or an address — a free
    text name like "Иван" identifies nobody."""
    candidates = [chat_id, external_user_id]
    if display_name and (display_name.startswith("@") or "@" in display_name or _PHONE.match(display_name)):
        candidates.append(display_name)
    out: List[str] = []
    for item in candidates:
        norm = normalize_identity(item)
        if norm and norm not in out:
            out.append(norm)
    return out


def _match_client_contact(connection: sqlite3.Connection, platform: str, identities: Iterable[str]) -> Optional[Tuple[str, str]]:
    ids = [i for i in identities if i]
    if not ids:
        return None
    try:
        rows = connection.execute(
            "SELECT id, client_id, email, phone, telegram FROM client_contacts"
        ).fetchall()
    except sqlite3.OperationalError:
        return None  # CRM module never initialised on this database
    wanted = set(ids)
    for row in rows:
        fields = []
        if platform == "telegram":
            fields.append(row["telegram"])
        fields.extend([row["email"], row["phone"]])
        if platform != "telegram":
            fields.append(row["telegram"])
        for field in fields:
            norm = normalize_identity(field)
            if norm and norm in wanted:
                return row["id"], row["client_id"]
    return None


def _resolve_identity(connection: sqlite3.Connection, platform: str, identities: List[str]) -> Optional[Tuple[Optional[str], Optional[str]]]:
    for external_id in identities:
        row = connection.execute(
            "SELECT contact_id, client_id FROM inbox_identities WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        if row and (row["contact_id"] or row["client_id"]):
            return row["contact_id"], row["client_id"]
    matched = _match_client_contact(connection, platform, identities)
    if not matched:
        return None
    contact_id, client_id = matched
    now = _now()
    for external_id in identities:
        connection.execute(
            """
            INSERT INTO inbox_identities (id, platform, external_id, contact_id, client_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (platform, external_id) DO UPDATE SET contact_id = excluded.contact_id,
                client_id = excluded.client_id
            """,
            (f"idn-{uuid.uuid4().hex[:12]}", platform, external_id, contact_id, client_id, now),
        )
    return contact_id, client_id


# ── conversations ───────────────────────────────────────────────────────────
def effective_handling(state_mode: Optional[str], last_rule_action: Optional[str]) -> str:
    """The one place ``handling`` is computed. Nothing stores it: the state row owns
    takeover/waiting, the routing rules own block/pass, and a copy of either would be
    a second truth the moment one of them changes."""
    if last_rule_action == "block":
        return "blocked"
    if (state_mode or "") in HUMAN_STATE_MODES or last_rule_action == "pass":
        return "human"
    return "ai"


def _get_or_create_conversation(
    connection: sqlite3.Connection,
    *,
    binding_id: str,
    platform: str,
    chat_id: str,
    session_id: str = "",
    display_name: str = "",
    external_user_id: str = "",
    channel_meta: Optional[Dict[str, Any]] = None,
    fallback_subagent_id: str = "",
) -> sqlite3.Row:
    now = _now()
    row = connection.execute(
        "SELECT * FROM inbox_conversations WHERE binding_id = ? AND platform = ? AND chat_id = ?",
        (binding_id, platform, chat_id),
    ).fetchone()
    if row is None:
        # A messenger conversation takes its agent from the binding; a phone line is
        # not a messenger binding, so its caller passes the agent in.
        subagent_id, project_id = fallback_subagent_id or "", None
        try:
            binding = connection.execute(
                "SELECT subagent_id FROM agent_messenger_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
            if binding:
                subagent_id = binding["subagent_id"] or subagent_id
        except sqlite3.OperationalError:
            pass
        try:
            if subagent_id:
                agent = connection.execute("SELECT * FROM subagents WHERE id = ?", (subagent_id,)).fetchone()
                if agent is not None and "project_id" in agent.keys():
                    project_id = agent["project_id"]
        except sqlite3.OperationalError:
            pass
        conversation_id = f"ibx-{uuid.uuid4().hex[:16]}"
        connection.execute(
            """
            INSERT INTO inbox_conversations
                (id, binding_id, platform, chat_id, session_id, subagent_id, project_id, display_name,
                 external_user_id, channel_meta, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (binding_id, platform, chat_id) DO NOTHING
            """,
            (conversation_id, binding_id, platform, chat_id, session_id, subagent_id, project_id,
             display_name or "", external_user_id or "", json.dumps(channel_meta or {}, ensure_ascii=False), now, now),
        )
        row = connection.execute(
            "SELECT * FROM inbox_conversations WHERE binding_id = ? AND platform = ? AND chat_id = ?",
            (binding_id, platform, chat_id),
        ).fetchone()
    else:
        updates, params = [], []
        if session_id and session_id != row["session_id"]:
            updates.append("session_id = ?")
            params.append(session_id)
        if display_name and display_name != row["display_name"]:
            updates.append("display_name = ?")
            params.append(display_name)
        if external_user_id and external_user_id != row["external_user_id"]:
            updates.append("external_user_id = ?")
            params.append(external_user_id)
        if channel_meta:
            merged = _json_loads(row["channel_meta"], {})
            merged.update({k: v for k, v in channel_meta.items() if v not in (None, "")})
            updates.append("channel_meta = ?")
            params.append(json.dumps(merged, ensure_ascii=False))
        if updates:
            connection.execute(
                f"UPDATE inbox_conversations SET {', '.join(updates)} WHERE id = ?",
                (*params, row["id"]),
            )
            row = connection.execute("SELECT * FROM inbox_conversations WHERE id = ?", (row["id"],)).fetchone()
    return row


def _insert_message(
    connection: sqlite3.Connection,
    conversation_id: str,
    speaker: str,
    text: str,
    *,
    delivery: str = "sent",
    pending_reply_id: Optional[str] = None,
    external_event_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    unread: bool = False,
) -> Optional[int]:
    """Returns the new row id, or None when the event id was already archived."""
    if speaker not in SPEAKERS:
        raise ValueError(f"speaker must be one of {SPEAKERS}")
    if delivery not in DELIVERIES:
        raise ValueError(f"delivery must be one of {DELIVERIES}")
    now = _now()
    body = _clip(text)
    try:
        cursor = connection.execute(
            """
            INSERT INTO inbox_messages
                (conversation_id, speaker, text, delivery, pending_reply_id, external_event_id, meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, speaker, body, delivery, pending_reply_id, external_event_id or None,
             json.dumps(meta or {}, ensure_ascii=False), now),
        )
    except sqlite3.IntegrityError:
        return None
    if speaker != "whisper" and delivery not in ("draft", "discarded", "failed"):
        # A whisper is a note to the agent, not part of the exchange, and a draft
        # has not reached anyone yet: neither may become the preview line the
        # operator reads as "what was last said". A draft takes over the preview
        # at the moment it is actually sent (mark_draft).
        connection.execute(
            """
            UPDATE inbox_conversations
               SET last_message_at = ?, last_message_preview = ?, last_speaker = ?, updated_at = ?,
                   unread_count = unread_count + ?,
                   status = CASE WHEN ? = 'customer' THEN 'open' ELSE status END
             WHERE id = ?
            """,
            (now, _preview(body), speaker, now, 1 if unread else 0, speaker, conversation_id),
        )
    else:
        connection.execute("UPDATE inbox_conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
    return int(cursor.lastrowid)


def is_duplicate_event(binding_id: str, platform: str, chat_id: str, event_id: str) -> bool:
    """Cheap pre-check the gate runs before spending anything. The unique index is
    the real guarantee; this just avoids running the agent twice for the common
    case of a retried delivery that arrives after the first one was archived."""
    if not event_id:
        return False
    try:
        _init_schema()
        with _connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM inbox_messages m
                  JOIN inbox_conversations c ON c.id = m.conversation_id
                 WHERE c.binding_id = ? AND c.platform = ? AND c.chat_id = ? AND m.external_event_id = ?
                 LIMIT 1
                """,
                (binding_id, platform, str(chat_id), str(event_id)[:200]),
            ).fetchone()
        return row is not None
    except Exception:
        logger.exception("Inbox duplicate check failed for %s/%s", binding_id, platform)
        return False


def record_inbound(
    *,
    binding_id: str,
    platform: str,
    chat_id: str,
    text: str,
    session_id: str = "",
    external_user_id: str = "",
    display_name: str = "",
    event_id: str = "",
    channel_meta: Optional[Dict[str, Any]] = None,
    rule_action: Optional[str] = None,
    rule_pattern: Optional[str] = None,
    gate_reply: str = "",
) -> Optional[Dict[str, Any]]:
    """Archives one incoming customer message (and the gate's scripted answer, if
    it gave one). Returns ``{conversation_id, message_id, duplicate}`` or None when
    the archive could not be written — never raises."""
    try:
        _init_schema()
        chat_key = str(chat_id)
        identities = sender_identities(platform, chat_key, external_user_id, display_name)
        with _connect() as connection:
            conversation = _get_or_create_conversation(
                connection, binding_id=binding_id, platform=platform, chat_id=chat_key,
                session_id=session_id, display_name=display_name, external_user_id=external_user_id,
                channel_meta=channel_meta,
            )
            conversation_id = conversation["id"]
            if not conversation["client_id"]:
                resolved = _resolve_identity(connection, platform, identities)
                if resolved:
                    contact_id, client_id = resolved
                    connection.execute(
                        "UPDATE inbox_conversations SET client_id = ?, contact_id = ? WHERE id = ?",
                        (client_id, contact_id, conversation_id),
                    )
            connection.execute(
                "UPDATE inbox_conversations SET last_rule_action = ?, last_rule_pattern = ? WHERE id = ?",
                (rule_action, rule_pattern, conversation_id),
            )
            message_id = _insert_message(
                connection, conversation_id, "customer", text or "",
                external_event_id=(str(event_id)[:200] if event_id else None), unread=True,
            )
            if message_id is None:
                return {"conversation_id": conversation_id, "message_id": None, "duplicate": True}
            if gate_reply and gate_reply.strip():
                _insert_message(connection, conversation_id, "system", gate_reply, meta={"source": "gate"})
        _emit(conversation_id, "message")
        return {"conversation_id": conversation_id, "message_id": message_id, "duplicate": False}
    except Exception:
        logger.exception("Could not archive an inbound message for binding %s", binding_id)
        return None


def ensure_conversation(
    *,
    binding_id: str,
    platform: str,
    chat_id: str,
    session_id: str = "",
    display_name: str = "",
    external_user_id: str = "",
    subagent_id: str = "",
) -> Optional[Dict[str, Any]]:
    """The conversation for a channel identity, created if needed and linked to a CRM
    client when the identity is known — without archiving any message. A phone call
    needs the conversation (and the client) before the caller has said anything."""
    try:
        _init_schema()
        chat_key = str(chat_id)
        with _connect() as connection:
            conversation = _get_or_create_conversation(
                connection, binding_id=binding_id, platform=platform, chat_id=chat_key,
                session_id=session_id, display_name=display_name, external_user_id=external_user_id,
                fallback_subagent_id=subagent_id,
            )
            if not conversation["client_id"]:
                identities = sender_identities(platform, chat_key, external_user_id, display_name)
                resolved = _resolve_identity(connection, platform, identities)
                if resolved:
                    contact_id, client_id = resolved
                    connection.execute(
                        "UPDATE inbox_conversations SET client_id = ?, contact_id = ? WHERE id = ?",
                        (client_id, contact_id, conversation["id"]),
                    )
        return get_conversation(conversation["id"])
    except Exception:
        logger.exception("Could not ensure a %s conversation for %s", platform, binding_id)
        return None


def record_agent_reply(
    conversation_id: str,
    text: str,
    *,
    delivery: str = "sent",
    pending_reply_id: Optional[str] = None,
    cost_usd: float = 0.0,
    meta: Optional[Dict[str, Any]] = None,
    speaker: str = "agent",
    unread: bool = False,
) -> Optional[int]:
    if not conversation_id:
        return None
    try:
        _init_schema()
        with _connect() as connection:
            message_id = _insert_message(
                connection, conversation_id, speaker, text or "",
                delivery=delivery, pending_reply_id=pending_reply_id, meta=meta, unread=unread,
            )
            if cost_usd:
                connection.execute(
                    "UPDATE inbox_conversations SET cost_usd = cost_usd + ? WHERE id = ?",
                    (float(cost_usd), conversation_id),
                )
        _emit(conversation_id, "message")
        return message_id
    except Exception:
        logger.exception("Could not archive a %s line for conversation %s", speaker, conversation_id)
        return None


def add_cost(conversation_id: str, cost_usd: float) -> None:
    if not conversation_id or not cost_usd:
        return
    try:
        _init_schema()
        with _connect() as connection:
            connection.execute(
                "UPDATE inbox_conversations SET cost_usd = cost_usd + ? WHERE id = ?",
                (float(cost_usd), conversation_id),
            )
    except Exception:
        logger.exception("Could not add cost to conversation %s", conversation_id)


def _draft_chat_key(platform: str, chat_id: str) -> str:
    """The email channel queues a draft under a JSON thread descriptor, while the
    gate saw the bare sender address — both must land on one conversation."""
    if platform == "email":
        thread = _json_loads(chat_id, {})
        if thread.get("to"):
            return str(thread["to"])
    return str(chat_id)


def record_draft(binding_id: str, platform: str, chat_id: str, drafted_reply: str, reply_id: str) -> Optional[int]:
    try:
        _init_schema()
        chat_key = _draft_chat_key(platform, chat_id)
        with _connect() as connection:
            conversation = connection.execute(
                "SELECT id FROM inbox_conversations WHERE binding_id = ? AND platform = ? AND chat_id = ?",
                (binding_id, platform, chat_key),
            ).fetchone()
            if conversation is None and platform == "email":
                conversation = connection.execute(
                    "SELECT id FROM inbox_conversations WHERE binding_id = ? AND platform = ? AND unicode_lower(chat_id) = ?",
                    (binding_id, platform, chat_key.lower()),
                ).fetchone()
            if conversation is None:
                conversation = _get_or_create_conversation(
                    connection, binding_id=binding_id, platform=platform, chat_id=chat_key,
                )
            message_id = _insert_message(
                connection, conversation["id"], "agent", drafted_reply or "",
                delivery="draft", pending_reply_id=reply_id,
            )
        _emit(conversation["id"], "message")
        return message_id
    except Exception:
        logger.exception("Could not archive draft %s", reply_id)
        return None


def mark_draft(reply_id: str, delivery: str, final_text: Optional[str] = None) -> None:
    """The draft's archived line follows its fate: sent (with what was actually
    sent, which may be the owner's edit), discarded, or failed."""
    if delivery not in DELIVERIES:
        raise ValueError(f"delivery must be one of {DELIVERIES}")
    try:
        _init_schema()
        with _connect() as connection:
            row = connection.execute(
                "SELECT id, conversation_id, text FROM inbox_messages WHERE pending_reply_id = ? ORDER BY id DESC LIMIT 1",
                (reply_id,),
            ).fetchone()
            if row is None:
                return
            if final_text is not None and final_text != row["text"]:
                meta = {"edited": True}
                connection.execute(
                    "UPDATE inbox_messages SET delivery = ?, text = ?, meta = ? WHERE id = ?",
                    (delivery, _clip(final_text), json.dumps(meta), row["id"]),
                )
            else:
                connection.execute("UPDATE inbox_messages SET delivery = ? WHERE id = ?", (delivery, row["id"]))
            if delivery == "sent":
                now = _now()
                connection.execute(
                    "UPDATE inbox_conversations SET last_message_at = ?, last_message_preview = ?, last_speaker = 'agent', updated_at = ? WHERE id = ?",
                    (now, _preview(final_text if final_text is not None else row["text"]), now, row["conversation_id"]),
                )
        _emit(row["conversation_id"], "message")
    except Exception:
        logger.exception("Could not update archived draft %s", reply_id)


# ── reads ───────────────────────────────────────────────────────────────────
_CONVERSATION_SELECT = """
    SELECT c.*, s.handling_mode AS state_mode,
           b.bot_username AS account, b.response_mode AS response_mode,
           a.name AS agent_name,
           cl.name AS client_name
      FROM inbox_conversations c
      LEFT JOIN conversation_states s
             ON s.binding_id = c.binding_id AND s.platform = c.platform AND s.chat_id = c.chat_id
      LEFT JOIN agent_messenger_bindings b ON b.id = c.binding_id
      LEFT JOIN subagents a ON a.id = c.subagent_id
      LEFT JOIN clients cl ON cl.id = c.client_id
"""

_HANDLING_SQL = (
    "CASE WHEN c.last_rule_action = 'block' THEN 'blocked' "
    "WHEN s.handling_mode IN ('owner_takeover', 'waiting') OR c.last_rule_action = 'pass' THEN 'human' "
    "ELSE 'ai' END"
)


def _ensure_join_tables(connection: sqlite3.Connection) -> str:
    """The list query joins three tables owned by other modules. On a database where
    one of them was never created (a fresh install, a test fixture) the join would
    raise, so degrade that one join to NULLs instead."""
    select = _CONVERSATION_SELECT
    existing = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "agent_messenger_bindings" not in existing:
        select = select.replace(
            "b.bot_username AS account, b.response_mode AS response_mode",
            "NULL AS account, NULL AS response_mode",
        ).replace("LEFT JOIN agent_messenger_bindings b ON b.id = c.binding_id", "")
    if "subagents" not in existing:
        select = select.replace("a.name AS agent_name", "NULL AS agent_name").replace(
            "LEFT JOIN subagents a ON a.id = c.subagent_id", ""
        )
    if "clients" not in existing:
        select = select.replace("cl.name AS client_name", "NULL AS client_name").replace(
            "LEFT JOIN clients cl ON cl.id = c.client_id", ""
        )
    return select


def _conversation_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["channel_meta"] = _json_loads(item.get("channel_meta"), {})
    item["handling"] = effective_handling(item.get("state_mode"), item.get("last_rule_action"))
    item["cost_usd"] = float(item.get("cost_usd") or 0.0)
    return item


def _fts_query(search: str) -> str:
    tokens = re.findall(r"\w+", search or "", flags=re.UNICODE)[:8]
    return " ".join(f'"{token}"*' for token in tokens)


def list_conversations(
    *,
    search: str = "",
    platform: Optional[str] = None,
    status: Optional[str] = None,
    handling: Optional[str] = None,
    client_id: Optional[str] = None,
    project_id: Optional[str] = None,
    binding_id: Optional[str] = None,
    unread: bool = False,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    _init_schema()
    limit = max(1, min(int(limit or 50), 200))
    where, params = [], []
    if platform:
        where.append("c.platform = ?")
        params.append(platform)
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        where.append("c.status = ?")
        params.append(status)
    if handling:
        if handling not in HANDLINGS:
            raise ValueError(f"handling must be one of {HANDLINGS}")
        where.append(f"({_HANDLING_SQL}) = ?")
        params.append(handling)
    if client_id:
        where.append("c.client_id = ?")
        params.append(client_id)
    if project_id:
        where.append("c.project_id = ?")
        params.append(project_id)
    if binding_id:
        where.append("c.binding_id = ?")
        params.append(binding_id)
    if unread:
        where.append("c.unread_count > 0")
    needle = (search or "").strip()
    with _connect() as connection:
        if needle:
            like = f"%{needle.lower()}%"
            clauses = ["unicode_lower(c.display_name) LIKE ?", "unicode_lower(c.chat_id) LIKE ?"]
            clause_params: List[Any] = [like, like]
            fts = _fts_query(needle) if _fts_available else ""
            if fts:
                clauses.append(
                    "c.id IN (SELECT m.conversation_id FROM inbox_messages m "
                    "JOIN inbox_messages_fts f ON f.rowid = m.id WHERE inbox_messages_fts MATCH ? "
                    "AND m.speaker != 'whisper')"
                )
                clause_params.append(fts)
            else:
                clauses.append(
                    "c.id IN (SELECT conversation_id FROM inbox_messages WHERE speaker != 'whisper' "
                    "AND unicode_lower(text) LIKE ?)"
                )
                clause_params.append(like)
            where.append("(" + " OR ".join(clauses) + ")")
            params.extend(clause_params)
        if cursor:
            try:
                cursor_ts, cursor_id = cursor.rsplit("|", 1)
            except ValueError:
                raise ValueError("cursor is malformed")
            where.append("(COALESCE(c.last_message_at, '') < ? OR (COALESCE(c.last_message_at, '') = ? AND c.id < ?))")
            params.extend([cursor_ts, cursor_ts, cursor_id])
        select = _ensure_join_tables(connection)
        sql = select + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY COALESCE(c.last_message_at, '') DESC, c.id DESC LIMIT ?"
        rows = connection.execute(sql, (*params, limit + 1)).fetchall()
    items = [_conversation_dict(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = f"{last.get('last_message_at') or ''}|{last['id']}"
    return {"items": items, "next_cursor": next_cursor}


def get_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        select = _ensure_join_tables(connection)
        row = connection.execute(select + " WHERE c.id = ?", (conversation_id,)).fetchone()
    return _conversation_dict(row) if row else None


def find_conversation(binding_id: str, platform: str, chat_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM inbox_conversations WHERE binding_id = ? AND platform = ? AND chat_id = ?",
            (binding_id, platform, str(chat_id)),
        ).fetchone()
    return get_conversation(row["id"]) if row else None


def find_conversation_by_session(session_id: str) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM inbox_conversations WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return get_conversation(row["id"]) if row else None


def list_messages(conversation_id: str, *, before_id: Optional[int] = None, limit: int = 100) -> Dict[str, Any]:
    _init_schema()
    limit = max(1, min(int(limit or 100), 500))
    params: List[Any] = [conversation_id]
    sql = "SELECT * FROM inbox_messages WHERE conversation_id = ?"
    if before_id:
        sql += " AND id < ?"
        params.append(int(before_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit + 1)
    with _connect() as connection:
        rows = connection.execute(sql, params).fetchall()
        page = rows[:limit]
        ids = [row["id"] for row in page]
        attachments: Dict[int, List[Dict[str, Any]]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for att in connection.execute(
                f"SELECT id, message_id, kind, mime, size_bytes, duration_ms, transcript, created_at "
                f"FROM inbox_attachments WHERE message_id IN ({marks})",
                ids,
            ).fetchall():
                attachments.setdefault(att["message_id"], []).append(dict(att))
    items = []
    for row in reversed(page):
        item = dict(row)
        item["meta"] = _json_loads(item.get("meta"), {})
        item["attachments"] = attachments.get(row["id"], [])
        items.append(item)
    next_cursor = str(page[-1]["id"]) if len(rows) > limit and page else None
    return {"items": items, "next_cursor": next_cursor}


def conversations_for_client(client_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    return list_conversations(client_id=client_id, limit=limit)["items"]


def count_unread() -> int:
    """Open conversations with unread customer messages — the sidebar badge."""
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM inbox_conversations WHERE unread_count > 0 AND status = 'open'"
        ).fetchone()
    return int(row[0] or 0)


def list_bindings_brief() -> List[Dict[str, Any]]:
    """Channel accounts for the inbox filters and the rules editor — display
    fields only, never the stored credential reference."""
    _init_schema()
    with _connect() as connection:
        try:
            rows = connection.execute(
                """
                SELECT b.id, b.platform, b.bot_username, b.subagent_id, b.status, b.response_mode,
                       a.name AS agent_name
                  FROM agent_messenger_bindings b LEFT JOIN subagents a ON a.id = b.subagent_id
                 ORDER BY b.created_at DESC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        items = [dict(row) for row in rows]
        # Phone lines are channels too: the rules editor and the list filter treat
        # them exactly like a messenger account.
        try:
            for row in connection.execute(
                """
                SELECT l.id, 'phone' AS platform, (l.name || ' · ' || l.number) AS bot_username, l.subagent_id,
                       CASE WHEN l.enabled = 1 THEN 'active' ELSE 'revoked' END AS status,
                       'auto_labeled' AS response_mode, a.name AS agent_name
                  FROM phone_lines l LEFT JOIN subagents a ON a.id = l.subagent_id
                 ORDER BY l.created_at DESC
                """
            ).fetchall():
                items.append(dict(row))
        except sqlite3.OperationalError:
            pass
    return items


# ── mutations from the dashboard ────────────────────────────────────────────
def _require(conversation_id: str) -> Dict[str, Any]:
    conversation = get_conversation(conversation_id)
    if conversation is None:
        raise KeyError(conversation_id)
    return conversation


def mark_read(conversation_id: str) -> Dict[str, Any]:
    _require(conversation_id)
    with _connect() as connection:
        connection.execute("UPDATE inbox_conversations SET unread_count = 0 WHERE id = ?", (conversation_id,))
    _emit(conversation_id, "state")
    return _require(conversation_id)


def set_status(conversation_id: str, status: str) -> Dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    _require(conversation_id)
    with _connect() as connection:
        connection.execute(
            "UPDATE inbox_conversations SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), conversation_id),
        )
    _emit(conversation_id, "state")
    return _require(conversation_id)


def link_client(
    conversation_id: str,
    *,
    client_id: Optional[str] = None,
    create: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Ties a conversation to a CRM client — an existing one, or a new one built
    from what the channel knows about the person. Remembers the sender's
    identities so their next conversation on any channel links itself."""
    conversation = _require(conversation_id)
    from backend import clients

    contact_id: Optional[str] = None
    if create is not None:
        name = str(create.get("name") or conversation.get("display_name") or conversation["chat_id"]).strip()
        contact: Dict[str, Any] = {"name": name, "isPrimary": True}
        handle = normalize_identity(conversation.get("external_user_id") or conversation["chat_id"])
        platform = conversation["platform"]
        if platform == "telegram" and conversation.get("display_name", "").startswith("@"):
            contact["telegram"] = conversation["display_name"]
        elif platform == "email" and "@" in handle:
            contact["email"] = handle
        elif platform == "whatsapp" and handle.startswith("+"):
            contact["phone"] = handle
        for key in ("email", "phone", "telegram"):
            if create.get(key):
                contact[key] = str(create[key])
        created = clients.create_client(
            {"name": name, "type": str(create.get("type") or "PERSON"), "contact": contact},
            actor_id,
        )
        client_id = created["id"]
        primary = created.get("primaryContact") or {}
        contact_id = primary.get("id")
    elif client_id:
        client = clients.get_client(client_id, expand=True)
        if not client:
            raise KeyError(client_id)
        contact_id = (client.get("primaryContact") or {}).get("id")
    else:
        raise ValueError("client_id or create is required")

    identities = sender_identities(
        conversation["platform"], conversation["chat_id"],
        conversation.get("external_user_id") or "", conversation.get("display_name") or "",
    )
    now = _now()
    with _connect() as connection:
        connection.execute(
            "UPDATE inbox_conversations SET client_id = ?, contact_id = ?, updated_at = ? WHERE id = ?",
            (client_id, contact_id, now, conversation_id),
        )
        for external_id in identities:
            connection.execute(
                """
                INSERT INTO inbox_identities (id, platform, external_id, contact_id, client_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (platform, external_id) DO UPDATE SET contact_id = excluded.contact_id,
                    client_id = excluded.client_id
                """,
                (f"idn-{uuid.uuid4().hex[:12]}", conversation["platform"], external_id, contact_id, client_id, now),
            )
    _emit(conversation_id, "state")
    return _require(conversation_id)


def add_system_note(conversation_id: str, text: str, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
    return record_agent_reply(conversation_id, text, speaker="system", meta=meta)


# ── takeover, human reply, whisper ──────────────────────────────────────────
def takeover(conversation_id: str) -> Dict[str, Any]:
    """Silences the agent on this conversation by moving the *existing* state row to
    ``owner_takeover`` — the same switch the SMM Factory "Диалоги" panel flips, so the
    two screens can never disagree about who is answering."""
    conversation = _require(conversation_id)
    from backend import conversation_state

    conversation_state.set_handling_mode(
        conversation["binding_id"], conversation["platform"], conversation["chat_id"], "owner_takeover",
        conversation.get("display_name") or None,
    )
    add_system_note(conversation_id, "Оператор забрал диалог — агент молчит до возврата.", {"event": "takeover"})
    return _require(conversation_id)


def resume(conversation_id: str) -> Dict[str, Any]:
    conversation = _require(conversation_id)
    from backend import conversation_state

    conversation_state.set_handling_mode(
        conversation["binding_id"], conversation["platform"], conversation["chat_id"], "ai",
        conversation.get("display_name") or None,
    )
    add_system_note(conversation_id, "Диалог возвращён агенту.", {"event": "resume"})
    return _require(conversation_id)


def _reply_chat_id(conversation: Dict[str, Any]) -> str:
    """Where a human reply physically goes. Usually the chat id itself; the email
    channel needs a thread descriptor, and a Discord DM that the gate matched by
    author id must still be answered in the DM *channel*."""
    meta = conversation.get("channel_meta") or {}
    platform = conversation["platform"]
    if platform == "email":
        subject = str(meta.get("subject") or "")
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return json.dumps({
            "to": conversation["chat_id"], "subject": subject,
            "message_id": meta.get("message_id") or "", "references": meta.get("references") or "",
        }, ensure_ascii=False)
    return str(meta.get("reply_chat_id") or conversation["chat_id"])


async def send_human_reply(conversation_id: str, text: str) -> Dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("Пустой ответ отправить нельзя")
    if len(body) > 8000:
        raise ValueError("Ответ слишком длинный (больше 8000 символов)")
    conversation = await asyncio.to_thread(_require, conversation_id)
    if conversation["handling"] != "human":
        # Outside takeover the agent is answering this thread; a person typing into
        # it as well is two voices answering one customer.
        raise PermissionError("Ответить от имени человека можно только после «Забрать диалог»")
    from backend.channel_replies import send_to_channel

    try:
        await send_to_channel(
            conversation["binding_id"], conversation["platform"], _reply_chat_id(conversation), body,
        )
    except Exception as exc:
        await asyncio.to_thread(
            record_agent_reply, conversation_id, body, delivery="failed", speaker="human",
            meta={"error": str(exc)[:300]},
        )
        raise
    await asyncio.to_thread(record_agent_reply, conversation_id, body, speaker="human")
    session_id = conversation.get("session_id") or ""
    if session_id:
        # The agent picks the thread back up after "Вернуть агенту" and must know
        # what the business already told this person — a human line is its own
        # side of the conversation.
        from backend.database import save_message

        await asyncio.to_thread(save_message, session_id, "assistant", body)
    return {"status": "sent", "conversation": await asyncio.to_thread(_require, conversation_id)}


WHISPER_MAX_CHARS = 2000
_WHISPER_HEADER = (
    "\n\n[Служебная подсказка оператора для этого ответа. Учти её, но никогда не цитируй, "
    "не пересказывай и не упоминай собеседнику, что она существует]:\n"
)


def add_whisper(conversation_id: str, text: str) -> Dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("Подсказка пустая")
    if len(body) > WHISPER_MAX_CHARS:
        raise ValueError(f"Подсказка длиннее {WHISPER_MAX_CHARS} символов")
    _require(conversation_id)
    message_id = record_agent_reply(conversation_id, body, speaker="whisper")
    return {"status": "queued", "message_id": message_id}


def consume_whisper_block(session_id: str) -> str:
    """The pending whispers for the conversation behind ``session_id``, formatted as
    a system-prompt block, marked consumed. Empty string when there are none.

    A whisper enters the *system* message and nothing else: never the user's turn,
    never the ``messages`` table the agent re-reads as history. That is what keeps an
    operator's note from being presented to the model as something the customer said,
    or being replayed on every later turn."""
    if not session_id:
        return ""
    try:
        _init_schema()
        with _connect() as connection:
            rows = connection.execute(
                """
                SELECT m.id, m.text FROM inbox_messages m
                  JOIN inbox_conversations c ON c.id = m.conversation_id
                 WHERE c.session_id = ? AND m.speaker = 'whisper' AND m.consumed_at IS NULL
                 ORDER BY m.id ASC LIMIT 5
                """,
                (session_id,),
            ).fetchall()
            if not rows:
                return ""
            now = _now()
            connection.executemany(
                "UPDATE inbox_messages SET consumed_at = ? WHERE id = ?", [(now, row["id"]) for row in rows]
            )
        return _WHISPER_HEADER + "\n".join(f"- {row['text']}" for row in rows)
    except Exception:
        logger.exception("Could not load operator whispers for session %s", session_id)
        return ""


def pending_whisper_texts(session_id: str) -> List[str]:
    """Whisper texts consumed for this session in the last hour — what
    :func:`strip_whisper_echo` guards the outgoing reply against."""
    if not session_id:
        return []
    try:
        _init_schema()
        with _connect() as connection:
            rows = connection.execute(
                """
                SELECT m.text FROM inbox_messages m
                  JOIN inbox_conversations c ON c.id = m.conversation_id
                 WHERE c.session_id = ? AND m.speaker = 'whisper' AND m.consumed_at IS NOT NULL
                 ORDER BY m.id DESC LIMIT 5
                """,
                (session_id,),
            ).fetchall()
        return [row["text"] for row in rows]
    except Exception:
        logger.exception("Could not read whispers for session %s", session_id)
        return []


def strip_whisper_echo(reply: str, whispers: Iterable[str]) -> str:
    """Second line of defence after the prompt instruction: a model that pastes the
    operator's note back verbatim does not get to deliver it. Only long-enough
    fragments are matched, so a one-word hint cannot erase ordinary words."""
    text = reply or ""
    for whisper in whispers:
        fragment = (whisper or "").strip()
        if len(fragment) >= 20 and fragment in text:
            text = text.replace(fragment, "").strip()
    return text


# ── media attachments ───────────────────────────────────────────────────────
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/webm": ".webm", "audio/flac": ".flac", "audio/aac": ".aac",
}


def media_dir():
    from pathlib import Path

    from backend import database

    path = Path(database.DB_DIR) / "inbox_media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_attachment(
    message_id: Optional[int],
    *,
    kind: str,
    mime: str = "",
    data: bytes = b"",
    duration_ms: Optional[int] = None,
    transcript: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Keeps the bytes on disk and only a reference in SQLite. An audio reply is
    megabytes; inlined as base64 it would bloat every conversation read and every
    backup of the one database the whole dashboard shares."""
    if not message_id or not data:
        return None
    if len(data) > MAX_ATTACHMENT_BYTES:
        logger.warning("Refusing a %d-byte attachment for message %s", len(data), message_id)
        return None
    try:
        _init_schema()
        from backend.media_snapshots import resolve_within

        attachment_id = f"att-{uuid.uuid4().hex[:16]}"
        filename = attachment_id + _MIME_EXTENSIONS.get((mime or "").lower(), ".bin")
        target = resolve_within(media_dir(), filename)
        target.write_bytes(data)
        with _connect() as connection:
            connection.execute(
                """
                INSERT INTO inbox_attachments
                    (id, message_id, kind, mime, storage_ref, size_bytes, duration_ms, transcript, meta, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (attachment_id, int(message_id), kind, mime or "application/octet-stream", filename, len(data),
                 duration_ms, transcript, json.dumps(meta or {}, ensure_ascii=False), _now()),
            )
        return attachment_id
    except Exception:
        logger.exception("Could not store an attachment for message %s", message_id)
        return None


def get_attachment(attachment_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM inbox_attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        return None
    from backend.media_snapshots import resolve_within

    item = dict(row)
    try:
        item["path"] = resolve_within(media_dir(), item["storage_ref"])
    except Exception:
        return None
    return item


# ── take_message: the agent writes down who wants whom, and why ─────────────
TAKE_MESSAGE_URGENCIES = ("low", "normal", "high")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean_field(value: Any, limit: int) -> str:
    text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)[:limit]


def take_message(session_id: str, arguments: Dict[str, Any]) -> str:
    """Tool body for ``take_message``. Arguments are validated, not believed: a
    message stored under a name the model invented sends somebody to call the wrong
    person while the real one waits. Every refusal is a sentence the model can act
    on (ask again), never an exception that would end the turn."""
    args = arguments if isinstance(arguments, dict) else {}
    name = _clean_field(args.get("name"), 120)
    reason = _clean_field(args.get("reason"), 1000)
    callback = _clean_field(args.get("callback"), 120)
    urgency = str(args.get("urgency") or "normal").strip().lower()

    problems = []
    if not name or not re.search(r"[^\W\d_]", name):
        problems.append("имя собеседника (как к нему обращаться)")
    if len(reason) < 3 or not re.search(r"[^\W\d_]", reason):
        problems.append("суть вопроса")
    if urgency not in TAKE_MESSAGE_URGENCIES:
        urgency = "normal"
    if callback and not normalize_identity(callback):
        problems.append("контакт для обратной связи в понятном виде")
    if problems:
        return json.dumps({
            "status": "needs_input",
            "message": "Сообщение не записано: уточни у собеседника " + ", ".join(problems) + ".",
        }, ensure_ascii=False)

    conversation = find_conversation_by_session(session_id)
    if conversation is None:
        return json.dumps({
            "status": "unavailable",
            "message": "Записать сообщение можно только в разговоре с клиентом через подключённый канал.",
        }, ensure_ascii=False)

    urgency_label = {"low": "не срочно", "normal": "обычная", "high": "срочно"}[urgency]
    note = f"📝 Принято сообщение от {name}: {reason}"
    if callback:
        note += f"\nКонтакт: {callback}"
    note += f"\nСрочность: {urgency_label}"
    add_system_note(conversation["id"], note, {"event": "take_message", "urgency": urgency})

    task_id = None
    if conversation.get("client_id"):
        try:
            from backend import client_tasks

            task = client_tasks.create_task({
                "title": f"Связаться: {name}"[:200],
                "description": reason + (f"\nКонтакт: {callback}" if callback else "")
                + f"\nИсточник: {conversation['platform']} · {conversation.get('display_name') or conversation['chat_id']}",
                "clientId": conversation["client_id"],
                "priority": {"low": "LOW", "normal": "NORMAL", "high": "HIGH"}[urgency],
            }, actor_id="agent")
            task_id = task.get("id")
        except Exception:
            logger.exception("take_message could not create a client task for %s", conversation["id"])

    try:
        binding = {"id": conversation["binding_id"], "bot_username": conversation.get("account") or ""}
        # One ping per conversation per cooldown, whatever the wording: on a public
        # channel the arguments come from a stranger's conversation, and a key that
        # varied with the text would let a reworded prompt page the owner repeatedly.
        # Every message is still recorded above; only the ping is rate limited.
        notify_owner_once(
            f"take_message|{conversation['id']}", binding, conversation["platform"],
            conversation["chat_id"], name, reason,
            reason=f"оставил сообщение через агента ({urgency_label})" + (f", контакт: {callback}" if callback else ""),
        )
    except Exception:
        logger.exception("take_message could not notify the owner")

    return json.dumps({
        "status": "ok",
        "task_id": task_id,
        "message": "Сообщение записано и передано владельцу. Скажи собеседнику, что его передадут и с ним свяжутся.",
    }, ensure_ascii=False)


# ── owner notifications (best effort, rate limited) ─────────────────────────
_NOTIFY_COOLDOWN_SECONDS = 10 * 60
_last_notified: Dict[str, float] = {}


def notify_owner_once(key: str, binding: Dict[str, Any], platform: str, chat_id: str, requester: str,
                      excerpt: str, reason: str) -> bool:
    """Pings the owner through the same Telegram path escalations use. Called from
    worker threads (the gate, tools); the cooldown keeps a chatty customer writing
    into a taken-over thread from becoming a burst of pings."""
    now = time.time()
    last = _last_notified.get(key)
    if last and now - last < _NOTIFY_COOLDOWN_SECONDS:
        return True
    try:
        from backend.agent_messenger_governance import notify_owner_escalation

        sent = notify_owner_escalation(binding, platform, chat_id, requester, excerpt, reason=reason)
    except Exception:
        logger.exception("Inbox owner notification failed")
        return False
    if sent:
        _last_notified[key] = now
    return bool(sent)
