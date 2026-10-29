"""Client side of the Agent Client Protocol (agentclientprotocol.com).

ACP is how editors drive CLI coding agents (Grok Build, OpenCode, Gemini,
Goose, Qwen Code, Cline…): JSON-RPC 2.0, one JSON message per line, over the
agent's stdio. Here the stdio arrives through the agent desktop's WebSocket
bridge (agent_desktop/server.py), but this module only needs something that
sends and receives one line at a time, so tests drive it over a plain pipe.

What the client advertises matters. Hermes offers neither ``fs`` nor
``terminal``: the agent reads, writes and runs commands with its own tools
inside its own isolated desktop, and the client only answers what it must —
``session/request_permission`` (routed to the Control Plane by the caller).
Any other agent→client request is refused with "method not found", so a new
protocol method can never be served by accident.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

logger = logging.getLogger("hermes.agent_runtimes.acp")

PROTOCOL_VERSION = 1

# JSON-RPC / ACP error codes.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32000

INITIALIZE_TIMEOUT_S = 60.0
SESSION_TIMEOUT_S = 120.0


class LineTransport(Protocol):
    async def send(self, line: str) -> None: ...

    async def receive(self) -> Optional[str]:
        """The next line, or None once the other side has gone."""

    async def close(self) -> None: ...


class AcpError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class AcpClosed(RuntimeError):
    """The agent went away (process exited, desktop stopped) mid-conversation."""


UpdateHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]
PermissionHandler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class AcpConnection:
    """One agent process: request/response correlation plus the reader loop.

    ``on_update(session_id, update)`` receives every ``session/update``
    notification; ``on_permission(params)`` must return the ``outcome`` object
    for a ``session/request_permission`` request. Both are awaited on the
    reader task's behalf, so a permission that waits for the owner does not
    block updates: each incoming request is served on its own task.
    """

    def __init__(self, transport: LineTransport, *, on_update: UpdateHandler, on_permission: PermissionHandler):
        self._transport = transport
        self._on_update = on_update
        self._on_permission = on_permission
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._served: set[asyncio.Task] = set()
        self._reader: Optional[asyncio.Task] = None
        self._closed_reason: Optional[str] = None
        self.agent_info: Dict[str, Any] = {}
        self.auth_methods: list = []

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed_reason is not None

    @property
    def closed_reason(self) -> str:
        return self._closed_reason or ""

    async def close(self, reason: str = "closed by client") -> None:
        self._fail_all(reason)
        if self._reader is not None:
            self._reader.cancel()
        for task in list(self._served):
            task.cancel()
        await self._transport.close()

    def _fail_all(self, reason: str) -> None:
        if self._closed_reason is None:
            self._closed_reason = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AcpClosed(reason))
        self._pending.clear()

    async def _read_loop(self) -> None:
        reason = "agent closed the connection"
        try:
            while True:
                line = await self._transport.receive()
                if line is None:
                    break
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"agent connection failed: {type(exc).__name__}"
            logger.warning("ACP reader stopped: %s", exc)
        finally:
            closer = getattr(self._transport, "close_reason", "")
            self._fail_all(f"{reason} ({closer})" if closer else reason)

    # ── incoming ────────────────────────────────────────────────────────
    def _dispatch(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            # Some agents print banners or logs to stdout before switching to
            # protocol traffic; a non-JSON line is noise, not a fatal error.
            logger.debug("ACP: ignoring a non-JSON line (%d chars)", len(line))
            return
        if not isinstance(message, dict):
            return
        if "method" in message:
            if "id" in message:
                task = asyncio.create_task(self._serve(message))
                self._served.add(task)
                task.add_done_callback(self._served.discard)
            else:
                task = asyncio.create_task(self._notify(message))
                self._served.add(task)
                task.add_done_callback(self._served.discard)
            return
        future = self._pending.pop(message.get("id"), None) if "id" in message else None
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(AcpError(int(error.get("code", INTERNAL_ERROR)),
                                          str(error.get("message", "agent error")), error.get("data")))
        else:
            future.set_result(message.get("result"))

    async def _notify(self, message: Dict[str, Any]) -> None:
        if message.get("method") != "session/update":
            return
        params = message.get("params") or {}
        update = params.get("update")
        if isinstance(update, dict):
            try:
                await self._on_update(str(params.get("sessionId", "")), update)
            except Exception:
                logger.exception("ACP update handler failed")

    async def _serve(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        try:
            if method == "session/request_permission":
                outcome = await self._on_permission(message.get("params") or {})
                await self._send({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}})
                return
            await self._send({"jsonrpc": "2.0", "id": request_id,
                              "error": {"code": METHOD_NOT_FOUND, "message": f"Method not supported: {method}"}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("ACP: serving %s failed", method)
            # Fail closed: an error answer, never an implied approval.
            with _suppress_closed():
                await self._send({"jsonrpc": "2.0", "id": request_id,
                                  "error": {"code": INTERNAL_ERROR, "message": type(exc).__name__}})

    # ── outgoing ────────────────────────────────────────────────────────
    async def _send(self, message: Dict[str, Any]) -> None:
        if self.closed:
            raise AcpClosed(self.closed_reason)
        await self._transport.send(json.dumps(message, ensure_ascii=False))

    async def request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout) if timeout else await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ── protocol steps ──────────────────────────────────────────────────
    async def initialize(self) -> Dict[str, Any]:
        result = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "hermes", "version": "1"},
        }, timeout=INITIALIZE_TIMEOUT_S) or {}
        version = result.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise AcpError(INTERNAL_ERROR, f"Agent speaks ACP version {version!r}, Hermes speaks {PROTOCOL_VERSION}")
        self.agent_info = result.get("agentInfo") or {}
        self.auth_methods = result.get("authMethods") or []
        return result

    async def new_session(self, cwd: str, mcp_servers: Optional[list] = None) -> str:
        result = await self.request("session/new", {"cwd": cwd, "mcpServers": mcp_servers or []},
                                    timeout=SESSION_TIMEOUT_S) or {}
        session_id = result.get("sessionId")
        if not session_id:
            raise AcpError(INTERNAL_ERROR, "Agent returned no sessionId")
        return str(session_id)

    async def prompt(self, session_id: str, text: str) -> str:
        """Runs one turn and returns its stopReason. No timeout: a turn of a
        coding agent can legitimately take an hour; cancel() ends it."""
        result = await self.request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }) or {}
        return str(result.get("stopReason") or "end_turn")

    async def cancel(self, session_id: str) -> None:
        with _suppress_closed():
            await self.notify("session/cancel", {"sessionId": session_id})


class _suppress_closed:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, (AcpClosed, ConnectionError))
