"""Agent desktops: one isolated Linux container per agent, under gVisor.

Each desktop runs the agent_desktop image (agent_desktop/Dockerfile): an X
display the owner watches over VNC, a visible Chromium, a terminal, and the
pinned CLI coding agents. Its home directory is the agent's persistent
workspace — files, CLI sign-ins and the browser profile survive a stop.

Safety rests on the same layers as backend/dev_sandbox.py, plus a stricter
runtime:

* Docker is reached only through the docker-socket-proxy (DOCKER_HOST); the
  one bind-mount source this module builds is ``<host root>/<key>/home`` for a
  key that already passed ``_safe_key``.
* The container runtime must be gVisor (``runsc``). If Docker has no such
  runtime, desktops do not start — never a silent fallback to runc, where a
  kernel exploit from an agent's shell would reach the host. ``runc`` is
  accepted only when the deployment opts in explicitly
  (AGENT_DESKTOP_ALLOW_RUNC=true, for a development machine without gVisor).
* Read-only root filesystem, every capability dropped, no-new-privileges,
  memory/CPU/pid caps. Networks are the dev-runner's (internal dev-net to reach
  the backend, dev-egress-net for the internet) — never the docker-proxy's.
* Each desktop has its own bearer token, an HMAC of its key, so one desktop
  cannot drive another although they share a network.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("hermes.agent_runtimes.desktop")

LABEL = "hermes.agent-desktop"
NAME_PREFIX = "hermes-agent-desktop-"
BRIDGE_PORT = 8900
HEALTH_TIMEOUT_S = 60.0
# Matches `useradd --uid 10001 agent` in agent_desktop/Dockerfile.
DESKTOP_UID = 10001
DESKTOP_GID = 10001

_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_FORBIDDEN_RUNTIMES = {"", "io.containerd.runc.v2", "nvidia", "crun"}


class DesktopUnavailable(RuntimeError):
    """Desktops cannot run here (feature off, no gVisor, misconfigured)."""


def enabled() -> bool:
    return os.getenv("AGENT_DESKTOPS_ENABLED", "false").strip().lower() == "true"


def _safe_key(key: str) -> str:
    if not _KEY_RE.match(key or ""):
        raise ValueError(f"Invalid agent desktop key: {key!r}")
    return key


def container_name(key: str) -> str:
    return f"{NAME_PREFIX}{_safe_key(key)}"


def base_url(key: str) -> str:
    return f"http://{container_name(key)}:{BRIDGE_PORT}"


def ws_url(key: str, path: str) -> str:
    return f"ws://{container_name(key)}:{BRIDGE_PORT}{path}"


def desktop_token(key: str) -> str:
    secret = os.getenv("AGENT_DESKTOP_TOKEN_SECRET", "")
    if not secret:
        raise DesktopUnavailable("AGENT_DESKTOP_TOKEN_SECRET is not configured")
    return hmac.new(secret.encode(), _safe_key(key).encode(), hashlib.sha256).hexdigest()


def _image() -> str:
    return os.getenv("AGENT_DESKTOP_IMAGE", "hermes-agent-desktop:local")


def _runtime() -> str:
    return os.getenv("AGENT_DESKTOP_RUNTIME", "runsc").strip()


def _max_concurrent() -> int:
    return max(1, int(os.getenv("AGENT_DESKTOP_MAX_CONCURRENT", "4")))


def _local_root() -> Path:
    return Path(os.getenv("AGENT_DESKTOP_ROOT", "/app/backend/data/agent-desktops"))


def _host_root() -> str:
    return os.getenv("AGENT_DESKTOP_HOST_ROOT", "").rstrip("/")


def _docker_client():
    import docker  # local import: only touched when desktops are actually used
    return docker.from_env()  # DOCKER_HOST=tcp://docker-proxy:2375


def check_runtime(client) -> str:
    """The container runtime desktops will use, or DesktopUnavailable."""
    runtime = _runtime()
    if runtime.lower() in _FORBIDDEN_RUNTIMES:
        raise DesktopUnavailable(f"Container runtime {runtime or '(default)'!r} is not allowed for agent desktops")
    if runtime == "runc" and os.getenv("AGENT_DESKTOP_ALLOW_RUNC", "false").strip().lower() != "true":
        raise DesktopUnavailable("Agent desktops need gVisor (runsc); runc requires AGENT_DESKTOP_ALLOW_RUNC=true")
    available = sorted((client.info().get("Runtimes") or {}).keys())
    if runtime not in available:
        raise DesktopUnavailable(f"Docker has no {runtime!r} runtime (available: {', '.join(available) or 'none'})")
    return runtime


def _prepare_home(key: str) -> None:
    home = _local_root() / _safe_key(key) / "home"
    home.mkdir(parents=True, exist_ok=True)
    # The backend is root; the desktop drops to uid 10001 and must own its home,
    # or the very first write (the X authority file, the workspace) fails.
    subprocess.run(["chown", "-R", f"{DESKTOP_UID}:{DESKTOP_GID}", str(home)],
                   check=True, capture_output=True, text=True, timeout=120)


def _networks(client) -> List[str]:
    source = os.getenv("AGENT_DESKTOP_NETWORK_SOURCE", "hermes-dev-runner")
    base = client.containers.get(source)
    names = list(base.attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
    if not names:
        raise DesktopUnavailable(f"{source} is on no network to copy")
    return names


def _state(container) -> Dict[str, Any]:
    labels = container.labels or {}
    return {"key": labels.get(f"{LABEL}.key", ""), "name": container.name, "status": container.status}


def _blocking_list() -> List[Dict[str, Any]]:
    client = _docker_client()
    return [_state(c) for c in client.containers.list(all=True, filters={"label": LABEL})]


def _blocking_ensure(key: str) -> str:
    key = _safe_key(key)
    if not _host_root():
        raise DesktopUnavailable("AGENT_DESKTOP_HOST_ROOT is not configured")
    client = _docker_client()
    runtime = check_runtime(client)
    name = container_name(key)
    running = [c for c in client.containers.list(filters={"label": LABEL}) if c.name != name]
    try:
        existing = client.containers.get(name)
    except Exception:
        existing = None
    if existing is not None and existing.status == "running":
        return name
    if len(running) >= _max_concurrent():
        raise DesktopUnavailable(f"{len(running)} desktops are already running (limit {_max_concurrent()})")
    if existing is not None:
        existing.start()
        return name

    _prepare_home(key)
    networks = _networks(client)
    container = client.containers.run(
        _image(),
        name=name,
        detach=True,
        runtime=runtime,
        labels={LABEL: "1", f"{LABEL}.key": key},
        environment={"AGENT_DESKTOP_TOKEN": desktop_token(key)},
        # Built from the fixed host root and a validated key only.
        volumes={f"{_host_root()}/{key}/home": {"bind": "/home/agent", "mode": "rw"}},
        read_only=True,
        tmpfs={"/tmp": "size=1g,mode=1777"},
        shm_size="1g",
        network=networks[0],
        mem_limit=os.getenv("AGENT_DESKTOP_MEMORY", "3g"),
        nano_cpus=int(float(os.getenv("AGENT_DESKTOP_CPUS", "2")) * 1_000_000_000),
        pids_limit=1024,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        restart_policy={"Name": "no"},
    )
    for extra in networks[1:]:
        client.networks.get(extra).connect(container)
    return name


def _blocking_stop(key: str) -> bool:
    try:
        container = _docker_client().containers.get(container_name(key))
    except Exception:
        return False
    container.stop(timeout=10)
    return True


async def health(key: str) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{base_url(key)}/health")
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def ensure_desktop(key: str) -> Dict[str, Any]:
    """Starts (or reuses) the agent's desktop and returns its /health payload."""
    if not enabled():
        raise DesktopUnavailable("Agent desktops are disabled (AGENT_DESKTOPS_ENABLED)")
    await asyncio.to_thread(_blocking_ensure, key)
    deadline = asyncio.get_running_loop().time() + HEALTH_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        payload = await health(key)
        if payload is not None:
            return payload
        await asyncio.sleep(1.0)
    raise DesktopUnavailable(f"Desktop {key} did not become healthy within {HEALTH_TIMEOUT_S:.0f}s")


async def stop_desktop(key: str) -> bool:
    return await asyncio.to_thread(_blocking_stop, _safe_key(key))


async def list_desktops() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_blocking_list)


# ── WebSocket line transport to the desktop's /acp bridge ───────────────────

async def _ws_connect(url: str, headers: Dict[str, str]):
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
        return await connect(url, additional_headers=headers, max_size=None, ping_interval=20)
    except ImportError:
        import websockets  # older API
        return await websockets.connect(url, extra_headers=headers, max_size=None, ping_interval=20)


class DesktopTransport:
    """ACP lines over the desktop bridge's WebSocket (one frame = one message)."""

    def __init__(self, ws):
        self._ws = ws
        self.close_reason = ""

    @classmethod
    async def open(cls, key: str, path: str) -> "DesktopTransport":
        ws = await _ws_connect(ws_url(key, path), {"Authorization": f"Bearer {desktop_token(key)}"})
        return cls(ws)

    async def send(self, line: str) -> None:
        await self._ws.send(line)

    async def receive(self) -> Optional[str]:
        from websockets.exceptions import ConnectionClosed

        try:
            message = await self._ws.recv()
        except ConnectionClosed as exc:
            received = getattr(exc, "rcvd", None)
            self.close_reason = getattr(received, "reason", "") or ""
            return None
        return message if isinstance(message, str) else message.decode("utf-8", errors="replace")

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass
