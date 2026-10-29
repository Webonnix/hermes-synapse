"""Bridge served inside one agent's desktop container.

Every agent gets its own Linux desktop (backend/agent_runtimes/desktop.py
creates the container): an X display with a window manager, a visible Chromium
and a terminal, so the owner can watch what the agent does and take over. This
process is the container's only door, and it exposes exactly two things:

``/acp``
    Spawns one CLI coding agent from the fixed ``RUNTIMES`` table in ACP mode
    (Agent Client Protocol: JSON-RPC 2.0, one message per line on stdio) and
    pipes its stdio over a WebSocket. The backend speaks the protocol; this
    side never parses it, so a new ACP agent is one table row.
``/vnc``
    Relays the desktop's VNC server (x11vnc, bound to loopback only) over a
    WebSocket, so the dashboard's noVNC viewer can show it through the backend.

Like dev_runner_api.py this file imports nothing from the Hermes backend, and
every request must carry the per-desktop bearer token from AGENT_DESKTOP_TOKEN:
a compromised desktop exposes its own home directory and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import shutil
import signal
import socket
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

HOME = Path(os.getenv("HOME", "/home/agent"))
WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", str(HOME / "workspace")))
DISPLAY = os.getenv("DISPLAY", ":1")
VNC_PORT = int(os.getenv("AGENT_DESKTOP_VNC_PORT", "5900"))

# Name -> argv of the agent's ACP mode. Fixed here, never taken from a request:
# the backend picks a *name*, and only these programs can ever be started.
# Versions are pinned in agent_desktop/Dockerfile.
RUNTIMES: Dict[str, List[str]] = {
    "grok": ["grok", "agent", "stdio"],
    "opencode": ["opencode", "acp"],
}

# One ACP message can carry a whole file (a diff, a read result), far beyond
# asyncio's 64 KiB default line limit, which would kill the stream mid-turn.
MAX_LINE_BYTES = 16 * 1024 * 1024
STDERR_TAIL_LINES = 20
# A WebSocket close reason is capped at 123 bytes by the protocol.
CLOSE_REASON_BYTES = 120

app = FastAPI(title="Hermes Agent Desktop", docs_url=None, redoc_url=None, openapi_url=None)


def _token_ok(websocket: WebSocket) -> bool:
    expected = os.getenv("AGENT_DESKTOP_TOKEN", "")
    if not expected:
        # No token configured means nobody may drive this desktop, not everybody.
        return False
    header = websocket.headers.get("authorization", "")
    return hmac.compare_digest(header, f"Bearer {expected}")


def _display_up() -> bool:
    number = DISPLAY.lstrip(":").split(".")[0]
    return Path(f"/tmp/.X11-unix/X{number}").exists()


def _vnc_up() -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", VNC_PORT), timeout=1.0):
        return True
    return False


def runtime_available(name: str) -> bool:
    argv = RUNTIMES.get(name)
    return bool(argv) and shutil.which(argv[0]) is not None


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "display": _display_up(),
        "vnc": _vnc_up(),
        "runtimes": {name: runtime_available(name) for name in RUNTIMES},
    }


def _close_reason(text: str) -> str:
    return text.encode("utf-8", errors="replace")[:CLOSE_REASON_BYTES].decode("utf-8", errors="ignore")


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Stops the agent and everything it started (shells, dev servers, browsers).

    The agent runs in its own session, so its process group is exactly the tree
    to kill; killing only the pid would leave its children running in the
    desktop after the backend has gone."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


@app.websocket("/acp")
async def acp(websocket: WebSocket):
    if not _token_ok(websocket):
        await websocket.close(code=1008)
        return
    name = websocket.query_params.get("runtime", "")
    if name not in RUNTIMES:
        await websocket.close(code=1008, reason="unknown runtime")
        return
    if not runtime_available(name):
        await websocket.close(code=1011, reason=_close_reason(f"{name} is not installed in this desktop"))
        return
    await websocket.accept()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DISPLAY": DISPLAY}
    # The bridge's own credential is for the backend, not for the agent.
    env.pop("AGENT_DESKTOP_TOKEN", None)
    proc = await asyncio.create_subprocess_exec(
        *RUNTIMES[name],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(WORKSPACE),
        env=env,
        limit=MAX_LINE_BYTES,
        start_new_session=True,
    )
    stderr_tail: Deque[str] = deque(maxlen=STDERR_TAIL_LINES)

    async def pump_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                await websocket.send_text(text)

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    async def pump_stdin() -> None:
        assert proc.stdin is not None
        while True:
            message = await websocket.receive_text()
            proc.stdin.write(message.rstrip("\n").encode("utf-8") + b"\n")
            await proc.stdin.drain()

    tasks = [asyncio.create_task(pump_stdout()), asyncio.create_task(pump_stdin())]
    stderr_task = asyncio.create_task(pump_stderr())
    try:
        # Whichever side ends first ends the session: the agent exiting, or the
        # backend going away (which must not leave an orphaned agent running).
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await _kill_group(proc)
        stderr_task.cancel()
        last = next((line for line in reversed(stderr_tail) if line.strip()), "")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011 if proc.returncode else 1000,
                                  reason=_close_reason(f"exit {proc.returncode}: {last}"))


@app.websocket("/vnc")
async def vnc(websocket: WebSocket):
    if not _token_ok(websocket):
        await websocket.close(code=1008)
        return
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
    except OSError:
        await websocket.close(code=1011, reason="desktop VNC server is not running")
        return
    await websocket.accept()

    async def to_viewer() -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await websocket.send_bytes(data)

    async def to_server() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data: Optional[bytes] = message.get("bytes")
            if data is None and message.get("text") is not None:
                data = message["text"].encode("latin-1", errors="ignore")
            if data:
                writer.write(data)
                await writer.drain()

    tasks = [asyncio.create_task(to_viewer()), asyncio.create_task(to_server())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()
