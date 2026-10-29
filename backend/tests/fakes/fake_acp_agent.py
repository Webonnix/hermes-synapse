"""An in-memory ACP agent for tests: the agent side of the Agent Client Protocol.

``FakeAcpAgent`` plays a CLI coding agent behind a ``LineTransport``: it answers
initialize / session/new / session/prompt, streams session/update
notifications, and — when its script says so — asks the client for permission
and records the answer. ``transport`` is what the client under test talks to.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional


class PipeTransport:
    """Client end of an in-memory line pipe."""

    def __init__(self, to_agent: asyncio.Queue, from_agent: asyncio.Queue):
        self._to_agent = to_agent
        self._from_agent = from_agent
        self.closed = False
        self.close_reason = ""

    async def send(self, line: str) -> None:
        if self.closed:
            raise ConnectionError("pipe closed")
        await self._to_agent.put(line)

    async def receive(self) -> Optional[str]:
        line = await self._from_agent.get()
        return line

    async def close(self) -> None:
        self.closed = True
        await self._to_agent.put(None)


class FakeAcpAgent:
    def __init__(self, *, protocol_version: int = 1, permission_kind: Optional[str] = None,
                 auth_required: bool = False, reply: str = "done", noise: bool = False):
        self.protocol_version = protocol_version
        self.permission_kind = permission_kind
        self.auth_required = auth_required
        self.reply = reply
        self.noise = noise
        self.to_agent: asyncio.Queue = asyncio.Queue()
        self.from_agent: asyncio.Queue = asyncio.Queue()
        self.transport = PipeTransport(self.to_agent, self.from_agent)
        self.received: List[Dict[str, Any]] = []
        self.permission_answers: List[Dict[str, Any]] = []
        self.client_errors: List[Dict[str, Any]] = []
        self.cancelled = asyncio.Event()
        self.hold_prompt: Optional[asyncio.Event] = None
        self._next_id = 1000
        self._waiting: Dict[int, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> "FakeAcpAgent":
        self._task = asyncio.create_task(self._run())
        return self

    async def hang_up(self) -> None:
        """The agent process exits."""
        await self.from_agent.put(None)

    async def _send(self, message: Dict[str, Any]) -> None:
        await self.from_agent.put(json.dumps(message))

    async def _ask(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._waiting[self._next_id] = future
        await self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return await future

    async def _run(self) -> None:
        if self.noise:
            await self.from_agent.put("Grok Build v1 starting…")
        while True:
            line = await self.to_agent.get()
            if line is None:
                return
            message = json.loads(line)
            self.received.append(message)
            if "method" not in message:
                future = self._waiting.pop(message.get("id"), None)
                if future and not future.done():
                    future.set_result(message)
                continue
            asyncio.create_task(self._handle(message))

    async def _handle(self, message: Dict[str, Any]) -> None:
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if method == "initialize":
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": self.protocol_version, "agentCapabilities": {},
                "agentInfo": {"name": "fake", "version": "0"}, "authMethods": [],
            }})
        elif method == "session/new":
            if self.auth_required:
                await self._send({"jsonrpc": "2.0", "id": request_id,
                                  "error": {"code": -32000, "message": "Authentication required"}})
            else:
                await self._send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": "sess-1"}})
        elif method == "session/cancel":
            self.cancelled.set()
        elif method == "session/prompt":
            await self._prompt(request_id, params)

    async def _prompt(self, request_id: Any, params: Dict[str, Any]) -> None:
        session_id = params["sessionId"]

        async def update(payload: Dict[str, Any]) -> None:
            await self._send({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": session_id, "update": payload}})

        await update({"sessionUpdate": "plan", "entries": [{"content": "do it", "status": "pending"}]})
        if self.permission_kind:
            await update({"sessionUpdate": "tool_call", "toolCallId": "call-1", "title": "rm -rf build",
                          "kind": self.permission_kind, "status": "pending"})
            answer = await self._ask("session/request_permission", {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "call-1", "title": "rm -rf build", "kind": self.permission_kind},
                "options": [
                    {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                ],
            })
            self.permission_answers.append(answer)
        # An agent→client method Hermes does not offer must get an error back.
        error_answer = await self._ask("fs/read_text_file", {"sessionId": session_id, "path": "/etc/passwd"})
        self.client_errors.append(error_answer)
        if self.hold_prompt is not None:
            await self.hold_prompt.wait()
        if self.cancelled.is_set():
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}})
            return
        await update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": self.reply}})
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
