"""Dashboard WebSocket registry: addressed delivery + resumable run streams.

Two problems this module exists to solve, both of which used to be handled by a
single ``broadcast()`` over one flat connection set:

1. **Addressing.** Chat traffic is scoped to a ``chat_id``. Fanning every token
   of every chat out to every open socket leaks one tenant's conversation into
   another operator's browser tab and wastes bandwidth on tabs that discard it.
   ``send_chat_event`` delivers only to the connections that subscribed to that
   chat.

2. **Resume.** A dropped socket used to lose every frame emitted while it was
   gone: the answer only reappeared after a full page reload, because the
   ``chat_stream_chunk`` frames carry *deltas* and the terminal
   ``chat_message``/``chat_stream_end`` frames were gone too. Every run-scoped
   frame now carries a monotonic per-run ``seq`` and is retained in a bounded
   ring buffer, so a reconnecting client replays the tail it missed.

Protocol negotiation
────────────────────
A client that understands subscriptions and resume offers the ``hermes-v2``
subprotocol. Connections that negotiate it get strict routing. Anything older
(a stale frontend bundle served from a cached ``index.html`` — a failure mode
this deployment has hit before) stays on the legacy firehose so an out-of-date
browser degrades to the old behaviour instead of going silent. The first time a
legacy connection is fed chat traffic it is logged once, so the drift is
visible rather than invisible.
"""

import json
import logging
import os
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, Optional, Set, Tuple

from fastapi import WebSocket

logger = logging.getLogger("hermes.ws")

PROTOCOL_V2 = "hermes-v2"
PROTOCOL_V1 = "hermes-v1"


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


# A long answer streamed token-by-token can emit thousands of chunks. The cap is
# a memory bound, not a correctness bound: a client whose cursor falls off the
# front of the buffer is told ``status="gap"`` and reloads history instead of
# silently rendering a hole.
RUN_BUFFER_EVENTS = _int_env("HERMES_WS_RUN_BUFFER_EVENTS", 1500, 50)
MAX_TRACKED_RUNS = _int_env("HERMES_WS_MAX_TRACKED_RUNS", 16, 2)
# How long a finished run stays replayable. Covers a reconnect that lands after
# the answer already completed — the exact case that used to require a reload.
CLOSED_RUN_TTL_SECONDS = _int_env("HERMES_WS_CLOSED_RUN_TTL", 300, 30)


def _chat_key(chat_id: Any) -> str:
    """Routing key for a chat. Subscriptions always arrive as JSON strings, while
    some producers hold a numeric chat id, so both sides are normalized here."""
    if chat_id is None:
        return ""
    return str(chat_id)[:128]


class _Connection:
    """Per-socket state: what it can understand and what it asked for."""

    __slots__ = ("websocket", "addressed", "subscriptions", "warned_legacy", "owner")

    def __init__(self, websocket: WebSocket, addressed: bool, owner: bool = True):
        self.websocket = websocket
        self.addressed = addressed
        self.subscriptions: Set[str] = set()
        self.warned_legacy = False
        self.owner = owner


class _RunBuffer:
    """Bounded, ordered replay log for one run."""

    __slots__ = ("run_id", "chat_id", "events", "next_seq", "closed_at", "created_at")

    def __init__(self, run_id: str, chat_id: str):
        self.run_id = run_id
        self.chat_id = chat_id
        # (seq, already-serialized frame). Storing the wire string means replay
        # re-sends the exact bytes the live client saw — no second encoder to
        # keep in sync.
        self.events: Deque[Tuple[int, str]] = deque(maxlen=RUN_BUFFER_EVENTS)
        self.next_seq = 1
        self.closed_at: Optional[float] = None
        self.created_at = time.time()

    def reserve_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def append(self, seq: int, message_str: str) -> None:
        self.events.append((seq, message_str))

    @property
    def first_seq(self) -> int:
        """Lowest seq still retained; 0 when nothing has been buffered yet."""
        return self.events[0][0] if self.events else 0

    @property
    def last_seq(self) -> int:
        return self.next_seq - 1


class ConnectionManager:
    def __init__(self):
        self._connections: Dict[int, _Connection] = {}
        self._runs: "Dict[str, _RunBuffer]" = {}

    # ── connection lifecycle ────────────────────────────────────────────
    async def connect(self, websocket: WebSocket, subprotocol: Optional[str] = None, *, owner: bool = True):
        await websocket.accept(subprotocol=subprotocol)
        self._connections[id(websocket)] = _Connection(
            websocket, addressed=(subprotocol == PROTOCOL_V2 or not owner), owner=owner
        )
        logger.info(
            "Dashboard connected (protocol=%s). Active connections: %d",
            subprotocol or "legacy",
            len(self._connections),
        )

    def disconnect(self, websocket: WebSocket):
        if self._connections.pop(id(websocket), None) is not None:
            logger.info(
                "Dashboard disconnected. Active connections: %d", len(self._connections)
            )

    @property
    def active_connections(self) -> Set[WebSocket]:
        """Live sockets. Kept as a property so existing callers and tests that
        introspect the connection set keep working after the registry moved
        from a bare set to per-connection state."""
        return {conn.websocket for conn in self._connections.values()}

    # ── subscriptions ───────────────────────────────────────────────────
    def subscribe(self, websocket: WebSocket, chat_ids: Iterable[str]) -> Set[str]:
        conn = self._connections.get(id(websocket))
        if conn is None:
            return set()
        for chat_id in chat_ids:
            key = _chat_key(chat_id)
            if not conn.owner and not key.startswith("inbox"):
                continue
            if key:
                conn.subscriptions.add(key)
        return set(conn.subscriptions)

    def unsubscribe(self, websocket: WebSocket, chat_ids: Iterable[str]) -> Set[str]:
        conn = self._connections.get(id(websocket))
        if conn is None:
            return set()
        for chat_id in chat_ids:
            conn.subscriptions.discard(_chat_key(chat_id))
        return set(conn.subscriptions)

    def subscriptions_for(self, websocket: WebSocket) -> Set[str]:
        conn = self._connections.get(id(websocket))
        return set(conn.subscriptions) if conn else set()

    # ── delivery ────────────────────────────────────────────────────────
    async def _send_str(self, conn: _Connection, message_str: str) -> bool:
        try:
            await conn.websocket.send_text(message_str)
            return True
        except Exception:
            logger.warning("Error sending on websocket, dropping connection")
            return False

    async def _deliver(self, message_str: str, chat_id: Optional[str]) -> int:
        """Send to every connection that should see this frame.

        ``chat_id is None`` means a genuinely global event (config, settings,
        logs) and goes everywhere. Otherwise addressed connections receive it
        only when subscribed; legacy connections always do.
        """
        if not self._connections:
            return 0
        delivered = 0
        dead = []
        for key, conn in list(self._connections.items()):
            if not conn.owner and (chat_id is None or not chat_id.startswith("inbox")):
                continue
            if chat_id is not None and conn.addressed and chat_id not in conn.subscriptions:
                continue
            if chat_id is not None and not conn.addressed and not conn.warned_legacy:
                conn.warned_legacy = True
                logger.warning(
                    "Legacy websocket client (no %s subprotocol) is receiving chat "
                    "traffic for every chat; it cannot resume a dropped stream. "
                    "This usually means a stale frontend bundle is being served.",
                    PROTOCOL_V2,
                )
            if await self._send_str(conn, message_str):
                delivered += 1
            else:
                dead.append(key)
        for key in dead:
            self._connections.pop(key, None)
        return delivered

    async def broadcast(self, message: Dict[str, Any]):
        """Global event: every connected dashboard sees it.

        Reserved for state that is not scoped to a conversation (config,
        settings, decision logs, activity). Chat traffic must use
        :meth:`send_chat_event` or :meth:`send_run_event`.
        """
        await self._deliver(json.dumps(message), None)

    async def send_chat_event(self, chat_id: Any, message: Dict[str, Any]):
        """Chat-scoped event with no run to replay (e.g. a bot-authored turn).

        The payload is left exactly as the caller wrote it (some producers carry
        a numeric Telegram chat id on the wire and the frontend has always seen
        it that way); only the *routing* key is normalized to a string so it can
        match a subscription, which always arrives from JSON as a string.
        """
        payload = dict(message)
        payload.setdefault("chat_id", chat_id)
        await self._deliver(json.dumps(payload), _chat_key(chat_id))

    async def send_run_event(self, run_id: str, chat_id: str, message: Dict[str, Any]):
        """Run-scoped event: stamped with a monotonic ``seq`` and retained for replay.

        Falls back to a plain chat-scoped send when there is no ``run_id`` so a
        caller never has to branch.
        """
        if not run_id:
            await self.send_chat_event(chat_id, message)
            return
        buffer = self._runs.get(run_id)
        if buffer is None:
            buffer = self._open_run(run_id, _chat_key(chat_id))
        payload = dict(message)
        payload.setdefault("chat_id", chat_id)
        payload["run_id"] = run_id
        # Reserve the seq and serialize before any await, so concurrent emits on
        # the same run cannot interleave into a non-monotonic buffer order.
        seq = buffer.reserve_seq()
        payload["seq"] = seq
        message_str = json.dumps(payload)
        buffer.append(seq, message_str)
        await self._deliver(message_str, _chat_key(chat_id))

    # ── run buffers ─────────────────────────────────────────────────────
    def _open_run(self, run_id: str, chat_id: str) -> _RunBuffer:
        self._gc_runs()
        buffer = _RunBuffer(run_id, chat_id)
        self._runs[run_id] = buffer
        # Evict the oldest run outright if we are over the cap even after GC —
        # an unbounded dict here would be a slow leak on a long-lived process.
        while len(self._runs) > MAX_TRACKED_RUNS:
            oldest = min(self._runs.values(), key=lambda b: b.created_at)
            if oldest.run_id == run_id:
                break
            self._runs.pop(oldest.run_id, None)
        return buffer

    def close_run(self, run_id: str) -> None:
        """Mark a run finished. It stays replayable for ``CLOSED_RUN_TTL_SECONDS``
        so a client that reconnects *after* the answer completed still gets it."""
        buffer = self._runs.get(run_id)
        if buffer is not None and buffer.closed_at is None:
            buffer.closed_at = time.time()

    def _gc_runs(self) -> None:
        now = time.time()
        for run_id, buffer in list(self._runs.items()):
            if buffer.closed_at is not None and now - buffer.closed_at > CLOSED_RUN_TTL_SECONDS:
                self._runs.pop(run_id, None)

    def run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        buffer = self._runs.get(run_id)
        if buffer is None:
            return None
        return {
            "run_id": run_id,
            "chat_id": buffer.chat_id,
            "first_seq": buffer.first_seq,
            "last_seq": buffer.last_seq,
            "closed": buffer.closed_at is not None,
            "buffered": len(buffer.events),
        }

    # ── resume ──────────────────────────────────────────────────────────
    async def resume(self, websocket: WebSocket, run_id: str, after_seq: int) -> Dict[str, Any]:
        """Replay everything buffered for ``run_id`` after ``after_seq``.

        Statuses the client must handle:

        ``ok``       the replay is contiguous with what the client already has.
        ``gap``      the buffer no longer reaches back to the client's cursor, or
                     the cursor is ahead of/foreign to this run. Everything still
                     retained is replayed from the start of the buffer; the client
                     must discard its partial content for this run first. A cursor
                     that is ahead of the server is deliberately treated as
                     replay-from-start rather than "nothing to send", so a stale or
                     wrong cursor can never silently skip real events.
        ``unknown``  no buffer at all (expired or never existed) — the client
                     reloads history from the API.
        """
        conn = self._connections.get(id(websocket))
        buffer = self._runs.get(run_id)
        if conn is not None and not conn.owner:
            buffer = None
        if conn is None or buffer is None:
            result = {"type": "resume_begin", "run_id": run_id, "status": "unknown",
                      "count": 0, "from_seq": 0, "to_seq": 0}
            if conn is not None:
                await self._send_str(conn, json.dumps(result))
                await self._send_str(conn, json.dumps(
                    {"type": "resume_end", "run_id": run_id, "status": "unknown", "last_seq": 0}))
            return result

        # A resuming client is by definition interested in this chat.
        conn.subscriptions.add(buffer.chat_id)

        try:
            cursor = int(after_seq)
        except (TypeError, ValueError):
            cursor = 0
        pending = [(seq, raw) for seq, raw in buffer.events if seq > cursor]
        contiguous = (
            cursor >= 0
            and cursor <= buffer.last_seq
            and (not buffer.events or cursor + 1 >= buffer.first_seq)
        )
        if not contiguous:
            # Replay everything we still hold and tell the client to reset.
            pending = list(buffer.events)
        status = "ok" if contiguous else "gap"

        begin = {
            "type": "resume_begin",
            "run_id": run_id,
            "chat_id": buffer.chat_id,
            "status": status,
            "count": len(pending),
            "from_seq": pending[0][0] if pending else cursor,
            "to_seq": pending[-1][0] if pending else cursor,
            "closed": buffer.closed_at is not None,
        }
        if not await self._send_str(conn, json.dumps(begin)):
            self._connections.pop(id(websocket), None)
            return begin
        for _seq, raw in pending:
            if not await self._send_str(conn, raw):
                self._connections.pop(id(websocket), None)
                return begin
        await self._send_str(conn, json.dumps({
            "type": "resume_end",
            "run_id": run_id,
            "status": status,
            "last_seq": buffer.last_seq,
            "closed": buffer.closed_at is not None,
        }))
        return begin


# Singleton manager
manager = ConnectionManager()
