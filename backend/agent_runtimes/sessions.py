"""Turns of CLI coding agents running in agent desktops.

One ``DesktopAgentSession`` per (desktop, runtime): an open ACP connection to
the agent process inside the desktop and the ACP session it works in. A
session runs one turn at a time; a turn's progress lands in the desktop's
event log, which the dashboard polls (``events_since``).

Permissions are where Hermes's governance enters. An ACP agent asks
``session/request_permission`` before a sensitive tool call; the request is
classified by its ACP tool kind (``agent_desktop_<kind>`` in
control_plane.TOOL_RISKS) and becomes a Control Plane task, so the owner's
autonomy mode, approvals, kill switch and audit trail apply to CLI agents
exactly as to Hermes's own tools. Every path that is not a clear approval —
rejection, kill switch, timeout, cancelled turn, an agent offering no
``allow_once`` option — answers the agent with a refusal.

The agent's own tools run inside its gVisor desktop either way; what an agent
does without asking is bounded by that sandbox, not by this module.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from backend import control_plane
from backend.agent_runtimes import desktop
from backend.agent_runtimes.acp import AUTH_REQUIRED, AcpClosed, AcpConnection, AcpError

logger = logging.getLogger("hermes.agent_runtimes.sessions")

WORKSPACE = "/home/agent/workspace"

# Runtimes a desktop can run (agent_desktop/server.py RUNTIMES) and how the
# owner signs each in from the desktop's terminal when it asks for it.
RUNTIMES: Dict[str, Dict[str, str]] = {
    "grok": {"title": "Grok Build", "login": "grok login"},
    "opencode": {"title": "OpenCode", "login": "opencode auth login"},
}

# ACP ToolKind -> Control Plane tool name. A kind not listed here (a newer
# protocol revision, a misbehaving agent) maps to agent_desktop_unknown, which
# control_plane classifies as R4 like any unknown tool.
KIND_TOOLS = {
    kind: f"agent_desktop_{kind}"
    for kind in ("read", "search", "think", "fetch", "edit", "move", "delete", "execute", "switch_mode", "other")
}
UNKNOWN_KIND_TOOL = "agent_desktop_unknown"

PERMISSION_POLL_S = 2.0
PERMISSION_TIMEOUT_S = 15 * 60
EVENT_LOG_SIZE = 1000
TEXT_PREVIEW = 300


class SessionBusy(RuntimeError):
    pass


class EventLog:
    """Bounded, monotonically numbered log of one desktop's agent events."""

    def __init__(self, size: int = EVENT_LOG_SIZE):
        self._events: Deque[Dict[str, Any]] = deque(maxlen=size)
        self._seq = itertools.count(1)

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        stamped = {**event, "seq": next(self._seq), "at": time.time()}
        self._events.append(stamped)
        return stamped

    def since(self, after: int) -> List[Dict[str, Any]]:
        return [event for event in self._events if event["seq"] > after]


def _text_of(content: Any) -> str:
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    return ""


def map_update(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An ACP session/update as a dashboard event; None for ones not shown."""
    kind = update.get("sessionUpdate")
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        text = _text_of(update.get("content"))
        if not text:
            return None
        return {"type": "message" if kind == "agent_message_chunk" else "thought", "text": text}
    if kind in ("tool_call", "tool_call_update"):
        event = {"type": "tool", "id": str(update.get("toolCallId") or "")}
        for field in ("title", "kind", "status"):
            if update.get(field) is not None:
                event[field] = str(update[field])[:TEXT_PREVIEW]
        return event
    if kind == "plan":
        entries = update.get("entries") or []
        return {"type": "plan", "entries": [
            {"content": str(entry.get("content", ""))[:TEXT_PREVIEW], "status": str(entry.get("status", ""))}
            for entry in entries if isinstance(entry, dict)
        ]}
    return None


def pick_option(options: List[Dict[str, Any]], allow: bool) -> Optional[str]:
    """The optionId to answer with. Never ``allow_always``: that would let the
    agent skip asking next time, i.e. skip the Control Plane."""
    wanted = ("allow_once",) if allow else ("reject_once", "reject_always")
    for kind in wanted:
        for option in options:
            if isinstance(option, dict) and option.get("kind") == kind and option.get("optionId"):
                return str(option["optionId"])
    return None


class DesktopAgentSession:
    def __init__(self, key: str, runtime: str, emit: Callable[[Dict[str, Any]], None]):
        self.key = key
        self.runtime = runtime
        self._emit = emit
        self.conn: Optional[AcpConnection] = None
        self.session_id = ""
        # Set by start_turn itself, before the turn's task gets to run: two
        # requests arriving back to back must not both start a turn.
        self.active = False
        self.run_id = ""
        self.cancelled = False

    @property
    def chat_id(self) -> str:
        return f"agent-desktop:{self.key}"

    async def connect(self) -> None:
        if self.conn is not None and not self.conn.closed and self.session_id:
            return
        await self.disconnect()
        await desktop.ensure_desktop(self.key)
        transport = await desktop.DesktopTransport.open(self.key, f"/acp?runtime={self.runtime}")
        conn = AcpConnection(transport, on_update=self._on_update, on_permission=self._on_permission)
        conn.start()
        try:
            await conn.initialize()
            self.session_id = await conn.new_session(WORKSPACE)
        except BaseException:
            await conn.close("setup failed")
            raise
        self.conn = conn

    async def disconnect(self) -> None:
        conn, self.conn, self.session_id = self.conn, None, ""
        if conn is not None:
            await conn.close()

    # ── ACP callbacks ───────────────────────────────────────────────────
    async def _on_update(self, session_id: str, update: Dict[str, Any]) -> None:
        if session_id != self.session_id:
            return
        event = map_update(update)
        if event:
            self._emit({**event, "runtime": self.runtime, "run_id": self.run_id})

    async def _on_permission(self, params: Dict[str, Any]) -> Dict[str, Any]:
        tool_call = params.get("toolCall") or {}
        options = params.get("options") or []
        tool_name = KIND_TOOLS.get(str(tool_call.get("kind") or ""), UNKNOWN_KIND_TOOL)
        title = str(tool_call.get("title") or tool_name)[:TEXT_PREVIEW]
        allowed, task_id, reason = await self._decide(tool_name, title, str(tool_call.get("toolCallId") or ""))
        self._emit({"type": "permission", "runtime": self.runtime, "run_id": self.run_id, "tool": tool_name,
                    "title": title, "task_id": task_id, "granted": allowed, "reason": reason})
        if self.cancelled:
            return {"outcome": "cancelled"}
        option_id = pick_option(options, allowed)
        if option_id is None:
            return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": option_id}

    async def _decide(self, tool_name: str, title: str, tool_call_id: str) -> Tuple[bool, str, str]:
        if control_plane.get_control_state()["kill_switch"]:
            return False, "", "Control Plane stopped"
        arguments = {"agent": self.key, "runtime": self.runtime, "title": title,
                     "tool_call_id": tool_call_id or uuid.uuid4().hex}
        task = await asyncio.to_thread(control_plane.create_tool_task, tool_name, arguments, self.chat_id)
        task_id = task["id"]
        if task["approval_count"] < task["approvals_required"]:
            self._emit({"type": "permission_pending", "runtime": self.runtime, "run_id": self.run_id,
                        "tool": tool_name, "title": title, "task_id": task_id,
                        "risk_class": task["risk_class"]})
            deadline = time.monotonic() + PERMISSION_TIMEOUT_S
            while True:
                if self.cancelled:
                    await asyncio.to_thread(_reject_quietly, task_id, "Turn cancelled")
                    return False, task_id, "turn cancelled"
                current = await asyncio.to_thread(control_plane.get_task, task_id) or {}
                status = current.get("status")
                if status == "approved":
                    break
                if status != "awaiting_approval":
                    return False, task_id, f"task {status}"
                if time.monotonic() >= deadline:
                    await asyncio.to_thread(_reject_quietly, task_id, "No owner decision in time")
                    return False, task_id, "timed out waiting for approval"
                await asyncio.sleep(PERMISSION_POLL_S)
        try:
            # start_task re-checks the kill switch and the command budget.
            await asyncio.to_thread(control_plane.start_task, task_id)
        except Exception as exc:
            return False, task_id, str(exc)[:200]
        await asyncio.to_thread(control_plane.finish_task, task_id, json.dumps({"permission": "granted"}))
        return True, task_id, ""


def _reject_quietly(task_id: str, reason: str) -> None:
    try:
        control_plane.reject_task(task_id, reason, actor="agent-desktop")
    except (KeyError, ValueError):
        pass


class AgentDesktopManager:
    def __init__(self):
        self._sessions: Dict[Tuple[str, str], DesktopAgentSession] = {}
        self._logs: Dict[str, EventLog] = {}
        self._tasks: set[asyncio.Task] = set()

    def log(self, key: str) -> EventLog:
        return self._logs.setdefault(key, EventLog())

    def events_since(self, key: str, after: int) -> List[Dict[str, Any]]:
        return self.log(key).since(after)

    def _session(self, key: str, runtime: str) -> DesktopAgentSession:
        session = self._sessions.get((key, runtime))
        if session is None:
            log = self.log(key)
            session = DesktopAgentSession(key, runtime, log.append)
            self._sessions[(key, runtime)] = session
        return session

    def busy(self, key: str) -> List[str]:
        return [runtime for (k, runtime), s in self._sessions.items() if k == key and s.active]

    def start_turn(self, key: str, runtime: str, text: str) -> str:
        if runtime not in RUNTIMES:
            raise ValueError(f"Unknown runtime: {runtime}")
        session = self._session(key, runtime)
        if session.active:
            raise SessionBusy(f"{RUNTIMES[runtime]['title']} is still working on the previous task")
        run_id = f"adr-{uuid.uuid4().hex[:12]}"
        session.active, session.run_id, session.cancelled = True, run_id, False
        task = asyncio.create_task(self._run_turn(session, run_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _run_turn(self, session: DesktopAgentSession, run_id: str, text: str) -> None:
        emit = self.log(session.key).append
        emit({"type": "turn_start", "runtime": session.runtime, "run_id": run_id,
              "text": text[:TEXT_PREVIEW]})
        stop_reason, error, error_code = "", "", ""
        try:
            await session.connect()
            assert session.conn is not None
            # Cancelled while the desktop was still starting: there is no
            # turn on the agent's side to cancel, so just don't begin one.
            if not session.cancelled:
                stop_reason = await session.conn.prompt(session.session_id, text)
        except AcpError as exc:
            if exc.code == AUTH_REQUIRED:
                error_code = "auth_required"
                error = (f"{RUNTIMES[session.runtime]['title']} is not signed in. Open the desktop and run "
                         f"`{RUNTIMES[session.runtime]['login']}` in its terminal, then try again.")
            else:
                error = f"agent error {exc.code}: {exc}"[:500]
            await session.disconnect()
        except AcpClosed as exc:
            error = str(exc)[:500]
            await session.disconnect()
        except desktop.DesktopUnavailable as exc:
            error = str(exc)[:500]
        except Exception as exc:
            logger.exception("Agent desktop turn %s failed", run_id)
            error = f"{type(exc).__name__}: {exc}"[:500]
            await session.disconnect()
        finally:
            emit({"type": "turn_end", "runtime": session.runtime, "run_id": run_id,
                  "stop_reason": stop_reason or ("cancelled" if session.cancelled else ""), "error": error,
                  "error_code": error_code})
            session.run_id, session.active = "", False

    async def cancel(self, key: str, runtime: str) -> bool:
        session = self._sessions.get((key, runtime))
        if session is None or not session.active:
            return False
        session.cancelled = True
        if session.conn is not None and session.session_id:
            await session.conn.cancel(session.session_id)
        return True

    async def stop_desktop(self, key: str) -> bool:
        for (k, runtime), session in list(self._sessions.items()):
            if k == key:
                session.cancelled = True
                await session.disconnect()
                self._sessions.pop((k, runtime), None)
        return await desktop.stop_desktop(key)


manager = AgentDesktopManager()
