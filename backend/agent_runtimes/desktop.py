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
import json
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


@dataclass(frozen=True)
class DesktopKind:
    """What differs between kinds of desktop. Isolation never does: every kind
    gets the same runtime check, read-only root, dropped capabilities, networks,
    DNS, hosts entry and bridge token in _blocking_ensure."""

    name: str
    image: Callable[[], str]
    home_mount: str
    uid: int
    memory: Callable[[], str]
    cpus: Callable[[], float]
    pids: int
    screens: int
    stop_timeout: int
    tmpfs: Dict[str, str] = field(default_factory=dict)
    enabled: Callable[[], bool] = lambda: True


AGENT = "agent"
OPENBOT = "openbot"
KINDS: Dict[str, DesktopKind] = {
    # CLI agents (Grok Build, OpenCode) driven over ACP: agent_desktop/.
    AGENT: DesktopKind(
        name=AGENT,
        image=lambda: os.getenv("AGENT_DESKTOP_IMAGE", "hermes-agent-desktop:local"),
        home_mount="/home/agent", uid=10001,
        memory=lambda: os.getenv("AGENT_DESKTOP_MEMORY", "3g"),
        cpus=lambda: float(os.getenv("AGENT_DESKTOP_CPUS", "2")),
        pids=1024, screens=1, stop_timeout=10,
    ),
    # OpenBot (openbot.run) as a self-hosted server, driven from OpenBot's own
    # apps through its account server: openbot_desktop/. Its agents run with
    # full access inside the desktop and never ask, so for them the desktop
    # is the whole boundary. Separate switch: PolyForm Noncommercial license.
    OPENBOT: DesktopKind(
        name=OPENBOT,
        image=lambda: os.getenv("OPENBOT_DESKTOP_IMAGE", "hermes-openbot-desktop:local"),
        home_mount="/data", uid=1000,
        memory=lambda: os.getenv("OPENBOT_DESKTOP_MEMORY", "6g"),
        cpus=lambda: float(os.getenv("OPENBOT_DESKTOP_CPUS", "4")),
        pids=2048, screens=2,
        # OpenBot stops its agents and closes its database before it exits.
        stop_timeout=60,
        # Its control socket directory; the upstream entrypoint wants it 0700.
        tmpfs={"/run/openbot": "size=16m,uid=1000,gid=1000,mode=0700"},
        enabled=lambda: _flag("OPENBOT_DESKTOPS_ENABLED"),
    ),
}


def _kind(name: str) -> DesktopKind:
    kind = KINDS.get(name)
    if kind is None:
        raise ValueError(f"Unknown desktop kind: {name!r}")
    return kind


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


def llm_token(key: str) -> str:
    """The desktop's credential for /agent-llm (llm_proxy.py): ``<key>.<hmac>``.

    Domain-separated from desktop_token, so the token an agent can read in its
    own home never opens the bridge, and vice versa."""
    secret = os.getenv("AGENT_DESKTOP_TOKEN_SECRET", "")
    if not secret:
        raise DesktopUnavailable("AGENT_DESKTOP_TOKEN_SECRET is not configured")
    key = _safe_key(key)
    return f"{key}.{hmac.new(secret.encode(), f'llm:{key}'.encode(), hashlib.sha256).hexdigest()}"


def verify_llm_token(token: str) -> Optional[str]:
    """The desktop key a /agent-llm token belongs to, or None."""
    key, _, _mac = (token or "").partition(".")
    if not _KEY_RE.match(key) or not os.getenv("AGENT_DESKTOP_TOKEN_SECRET", ""):
        return None
    return key if hmac.compare_digest(token, llm_token(key)) else None


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


# Seeded into a new desktop's home when absent (the owner may edit it later).
# OpenCode allows edits and shell commands without asking by default, which
# would keep every one of its actions away from the Control Plane; "ask" turns
# each into a session/request_permission that sessions.py routes there.
OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {"edit": "ask", "bash": "ask", "webfetch": "ask"},
}


OPENCODE_PERMISSIONS = {
    "*": "ask",
    "read": "allow", "glob": "allow", "grep": "allow", "lsp": "allow",
    "todowrite": "allow", "question": "allow", "skill": "allow",
}


def _write_resolv_conf() -> str:
    """Writes the desktops' resolv.conf and returns its host path.

    Docker's embedded DNS (127.0.0.11) works through iptables rules in the
    container's network namespace, which gVisor's own network stack never
    applies — every lookup in a desktop failed while plain IP traffic worked.
    Public resolvers instead; as a side effect a desktop cannot resolve
    internal service names (backend, llama, qdrant) at all."""
    servers = [s.strip() for s in os.getenv("AGENT_DESKTOP_DNS", "1.1.1.1,8.8.8.8").split(",") if s.strip()]
    local = _local_root() / "resolv.conf"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("".join(f"nameserver {server}\n" for server in servers) + "options edns0\n")
    local.chmod(0o644)
    return f"{_host_root()}/resolv.conf"


# Where a desktop finds the backend: an /etc/hosts entry, because a desktop
# resolves nothing internal (see _write_resolv_conf).
BACKEND_HOST = "hermes-backend"
# Hermes-managed OpenCode config, re-written at every desktop creation and
# pointed to by OPENCODE_CONFIG, which OpenCode merges over the owner's own
# ~/.config/opencode/opencode.json: the local-model provider stays current
# (address, model, token) without overwriting what the owner edited there.
MANAGED_OPENCODE_CONFIG = ".config/hermes/opencode.json"


def managed_opencode_config(key: str, external_models: Optional[List[str]] = None) -> Dict[str, Any]:
    """``external_models``: OmniRoute ids the owner allows (llm_proxy.external_models),
    offered beside the local model as ``omniroute/<id>``."""
    model = os.getenv("AGENT_LLM_MODEL") or os.getenv("LLM_MODEL") or "local"
    port = os.getenv("AGENT_LLM_BACKEND_PORT", "8000")
    models: Dict[str, Any] = {model: {
        "name": f"{model} (local)",
        "limit": {"context": int(os.getenv("AGENT_LLM_CONTEXT", "131072")),
                  "output": int(os.getenv("AGENT_LLM_MAX_TOKENS", "8192"))},
    }}
    for external in external_models or []:
        models[f"omniroute/{external}"] = {"name": f"{external} (OmniRoute)"}
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "hermes": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Hermes (local model)",
                "options": {"baseURL": f"http://{BACKEND_HOST}:{port}/agent-llm/v1", "apiKey": llm_token(key)},
                "models": models,
            },
        },
        "model": f"hermes/{model}",
        # Hermes-enforced, so it lives here rather than in the owner's file:
        # "*" also covers MCP tools (the browser), which OpenCode otherwise runs
        # without asking — every such call would have bypassed the Control
        # Plane. Pure lookups inside the workspace stay unasked.
        "permission": OPENCODE_PERMISSIONS,
    }


def recorded_kind(key: str) -> Optional[str]:
    """The kind a desktop was created as, kept beside its home so it outlives
    the container: a desktop never silently changes kind."""
    try:
        value = (_local_root() / _safe_key(key) / "kind").read_text().strip()
    except OSError:
        return None
    return value if value in KINDS else None


def _record_kind(key: str, kind: str) -> None:
    path = _local_root() / _safe_key(key) / "kind"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kind + "\n")


def model_access(key: str) -> Dict[str, Any]:
    """How a desktop reaches the local model (llm_proxy.py), as seen from inside it."""
    model = os.getenv("AGENT_LLM_MODEL") or os.getenv("LLM_MODEL") or "local"
    port = os.getenv("AGENT_LLM_BACKEND_PORT", "8000")
    return {
        "base_url": f"http://{BACKEND_HOST}:{port}/agent-llm/v1",
        "model": model,
        "api_key": llm_token(key),
        "context": int(os.getenv("AGENT_LLM_CONTEXT", "131072")),
        "max_tokens": int(os.getenv("AGENT_LLM_MAX_TOKENS", "8192")),
    }


def _prepare_openbot_home(key: str, with_local_model: bool) -> None:
    home = _local_root() / _safe_key(key) / "home"
    home.mkdir(parents=True, exist_ok=True)
    # The broker owns the uid-1000 home. Backend writes only a separate
    # uid-10001 bootstrap directory; the broker installs this one managed file.
    home = home.parent / "bootstrap"
    home.mkdir(exist_ok=True)
    note = home / "HERMES-MODEL.json"
    if with_local_model:
        # OpenBot's providers are set up in its app; this is the same data the
        # dashboard shows, for the owner's terminal inside the desktop.
        note.write_text(json.dumps(model_access(key), indent=2) + "\n")
        note.chmod(0o600)
    elif note.exists():
        note.unlink()


def _prepare_home(key: str, with_local_model: bool = False, external_models: Optional[List[str]] = None) -> None:
    home = _local_root() / _safe_key(key) / "home"
    home.mkdir(parents=True, exist_ok=True)
    opencode_config = home / ".config" / "opencode" / "opencode.json"
    if not opencode_config.exists():
        opencode_config.parent.mkdir(parents=True, exist_ok=True)
        opencode_config.write_text(json.dumps(OPENCODE_CONFIG, indent=2) + "\n")
    managed = home / MANAGED_OPENCODE_CONFIG
    managed.parent.mkdir(parents=True, exist_ok=True)
    if with_local_model:
        managed.write_text(json.dumps(managed_opencode_config(key, external_models), indent=2) + "\n")
    else:
        # No route to the backend: no provider that could only fail — but the
        # permission policy applies either way.
        managed.write_text(json.dumps({"$schema": "https://opencode.ai/config.json",
                                       "permission": OPENCODE_PERMISSIONS}) + "\n")
    # The backend is root; the desktop drops to uid 10001 and must own its home,
    # or the very first write (the X authority file, the workspace) fails.
    if os.geteuid() == 0:
        subprocess.run(["chown", "-R", f"{DESKTOP_UID}:{DESKTOP_GID}", str(home)],
                       check=True, capture_output=True, text=True, timeout=120)


def _networks(client) -> List[str]:
    # The dev-runner's container name differs per deployment (compose project
    # prefix), and the backend already knows it from DEV_RUNNER_CONTAINER_NAME.
    source = (os.getenv("AGENT_DESKTOP_NETWORK_SOURCE")
              or os.getenv("DEV_RUNNER_CONTAINER_NAME", "hermes-dev-runner"))
    base = client.containers.get(source)
    names = list(base.attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
    if not names:
        raise DesktopUnavailable(f"{source} is on no network to copy")
    return names


def _backend_ip(client, networks: List[str]) -> Optional[str]:
    """This backend's address on a network the desktop joins, or None."""
    override = os.getenv("AGENT_LLM_BACKEND_IP", "").strip()
    if override:
        return override
    try:
        me = client.containers.get(socket.gethostname())
    except Exception as exc:
        logger.warning("Agent desktops: cannot find the backend's own container (%s)", type(exc).__name__)
        return None
    mine = me.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
    for name in networks:
        address = (mine.get(name) or {}).get("IPAddress")
        if address:
            return address
    logger.warning("Agent desktops: the backend shares no network with the desktops; no local model for them")
    return None


def _extra_hosts(backend_ip: Optional[str]) -> Dict[str, str]:
    return {BACKEND_HOST: backend_ip} if backend_ip else {}


def _hosts_current(container, backend_ip: Optional[str]) -> bool:
    current = container.attrs.get("HostConfig", {}).get("ExtraHosts") or []
    return sorted(current) == sorted(f"{host}:{ip}" for host, ip in _extra_hosts(backend_ip).items())


def _state(container) -> Dict[str, Any]:
    labels = container.labels or {}
    return {"key": labels.get(f"{LABEL}.key", ""), "name": container.name, "status": container.status,
            "kind": labels.get(f"{LABEL}.kind", AGENT)}


def _blocking_list() -> List[Dict[str, Any]]:
    client = _docker_client()
    return [_state(c) for c in client.containers.list(all=True, filters={"label": LABEL})]


def _resolve_kind(key: str, requested: Optional[str]) -> DesktopKind:
    recorded = recorded_kind(key)
    if requested and recorded and requested != recorded:
        raise DesktopUnavailable(f"Desktop {key} is a {recorded} desktop, not {requested}")
    kind = _kind(requested or recorded or AGENT)
    if not kind.enabled():
        raise DesktopUnavailable(f"{kind.name} desktops are disabled on this server")
    return kind


def _blocking_ensure(key: str, kind_name: Optional[str] = None,
                     external_models: Optional[List[str]] = None) -> str:
    key = _safe_key(key)
    if not _host_root():
        raise DesktopUnavailable("AGENT_DESKTOP_HOST_ROOT is not configured")
    kind = _resolve_kind(key, kind_name)
    client = _docker_client()
    runtime = check_runtime(client)
    name = container_name(key)
    running = [c for c in client.containers.list(filters={"label": LABEL}) if c.name != name]
    try:
        existing = client.containers.get(name)
    except Exception:
        existing = None
    networks = _networks(client)
    backend_ip = _backend_ip(client, networks)
    if existing is not None and existing.status == "running":
        if _hosts_current(existing, backend_ip):
            return name
        # The backend came back with another address (recreated): the
        # desktop's hosts entry is fixed at creation, so recreate it.
        logger.info("Agent desktop %s: backend address changed, recreating", key)
    elif len(running) >= _max_concurrent():
        raise DesktopUnavailable(f"{len(running)} desktops are already running (limit {_max_concurrent()})")
    if existing is not None:
        # A stopped desktop is recreated rather than restarted: everything worth
        # keeping is in its home, and a fresh container picks up the current
        # image, backend address and settings. A running one is stopped with
        # its kind's grace period first — OpenBot closes its database then.
        if existing.status == "running":
            existing.stop(timeout=kind.stop_timeout)
        existing.remove(force=True)

    _record_kind(key, kind.name)
    if kind.name == OPENBOT:
        _prepare_openbot_home(key, with_local_model=backend_ip is not None)
        environment = {"AGENT_DESKTOP_TOKEN": desktop_token(key),
                       "AGENT_DESKTOP_VNC_PORTS": "5900,5901",
                       "OPENBOT_DISPLAY_SIZE": os.getenv("OPENBOT_DESKTOP_DISPLAY_SIZE", "1440x900")}
    else:
        _prepare_home(key, with_local_model=backend_ip is not None, external_models=external_models)
        environment = {"AGENT_DESKTOP_TOKEN": desktop_token(key),
                       "OPENCODE_CONFIG": f"/home/agent/{MANAGED_OPENCODE_CONFIG}"}
    resolv_conf = _write_resolv_conf()
    # Created, wired to every network, and only then started: gVisor builds
    # the sandbox's network stack once, at start, so a network connected to a
    # running desktop never shows up inside it (the desktop came up with no
    # route to the internet, only the internal dev-net).
    container = client.containers.create(
        kind.image(),
        name=name,
        runtime=runtime,
        labels={LABEL: "1", f"{LABEL}.key": key, f"{LABEL}.kind": kind.name},
        environment=environment,
        extra_hosts=_extra_hosts(backend_ip),
        # Built from the fixed host root and a validated key only.
        volumes={
            f"{_host_root()}/{key}/home": {"bind": kind.home_mount, "mode": "rw"},
            resolv_conf: {"bind": "/etc/resolv.conf", "mode": "ro"},
        },
        read_only=True,
        tmpfs={"/tmp": "size=1g,mode=1777", **kind.tmpfs},
        shm_size="1g",
        network=networks[0],
        mem_limit=kind.memory(),
        nano_cpus=int(kind.cpus() * 1_000_000_000),
        pids_limit=kind.pids,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        restart_policy={"Name": "no"},
    )
    for extra in networks[1:]:
        client.networks.get(extra).connect(container)
    container.start()
    return name


def _blocking_stop(key: str) -> bool:
    try:
        container = _docker_client().containers.get(container_name(key))
    except Exception:
        return False
    kind = KINDS.get((container.labels or {}).get(f"{LABEL}.kind", AGENT), KINDS[AGENT])
    container.stop(timeout=kind.stop_timeout)
    return True


async def health(key: str) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{base_url(key)}/health")
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def ensure_desktop(key: str, kind: Optional[str] = None) -> Dict[str, Any]:
    """Starts (or reuses) the agent's desktop and returns its /health payload.

    ``kind`` applies to a new desktop; an existing one keeps the kind it was
    created as (a different one is refused)."""
    if not enabled():
        raise DesktopUnavailable("Agent desktops are disabled (AGENT_DESKTOPS_ENABLED)")
    from backend.agent_runtimes import llm_proxy  # imports this module

    external = await llm_proxy.external_models() or []
    await asyncio.to_thread(_blocking_ensure, key, kind, external)
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
