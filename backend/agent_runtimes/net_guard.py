"""Keeps sandboxes on the dev network away from the backend's API.

The backend sits on the dev network (it drives the dev-runner and the agent
desktops there), and listens on every interface — so everything in a sandbox
could reach /api: session-protected routes answer 401, but the public ones
(``/api/auth/request-code`` sends the owner a login code in Telegram, webhooks)
were open to an agent's ``curl``.

This ASGI middleware decides by the backend's *own* address the connection
arrived on (``scope["server"]``), not by the client's: a sandbox reaches the
backend only through its dev-network interface and cannot pick another, while
a client address could in principle be anyone on the bridge.

* Arriving on a sandbox interface: only ``/agent-llm/`` (the desktops' door
  to the local model, llm_proxy.py) passes. Everything else, HTTP or
  WebSocket, is refused. The network's gateway is the host itself (a
  published port may be mapped onto this network) and passes.
* ``/agent-llm/`` arriving anywhere else is refused: it is meant for desktops
  only.

The sandbox interfaces are this container's addresses on the networks the
dev-runner joins, looked up through the docker-proxy. Until that lookup has
succeeded, ``/agent-llm/`` is refused everywhere (fail closed) and the lookup
is retried; the rest of the API keeps working rather than taking the dashboard
down with the Docker API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("hermes.agent_runtimes.net_guard")

SANDBOX_PATH_PREFIX = "/agent-llm/"
RETRY_SECONDS = 30.0


class SandboxInterfaces:
    def __init__(self):
        self.addresses: Optional[Set[str]] = None  # None: not known yet
        self.gateways: Set[str] = set()
        self._next_try = 0.0
        self._lock = asyncio.Lock()

    def _lookup(self) -> tuple[Set[str], Set[str]]:
        from backend.agent_runtimes import desktop

        client = desktop._docker_client()
        networks = desktop._networks(client)
        me = client.containers.get(socket.gethostname())
        mine: Dict[str, Any] = me.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
        addresses, gateways = set(), set()
        for name in networks:
            settings = mine.get(name) or {}
            if settings.get("IPAddress"):
                addresses.add(settings["IPAddress"])
            if settings.get("Gateway"):
                gateways.add(settings["Gateway"])
        return addresses, gateways

    async def known(self) -> Optional[Set[str]]:
        if self.addresses is not None or time.monotonic() < self._next_try:
            return self.addresses
        async with self._lock:
            if self.addresses is None and time.monotonic() >= self._next_try:
                try:
                    addresses, gateways = await asyncio.to_thread(self._lookup)
                except Exception as exc:
                    self._next_try = time.monotonic() + RETRY_SECONDS
                    logger.error("Sandbox network guard: cannot look up the sandbox interfaces (%s); "
                                 "/agent-llm stays closed, retrying in %.0fs", type(exc).__name__, RETRY_SECONDS)
                else:
                    self.gateways = gateways
                    self.addresses = addresses
                    logger.info("Sandbox network guard: sandbox interfaces %s", sorted(addresses) or "none")
        return self.addresses


class SandboxNetworkGuard:
    def __init__(self, app, interfaces: Optional[SandboxInterfaces] = None):
        self.app = app
        self.interfaces = interfaces or SandboxInterfaces()

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        local = (scope.get("server") or ("", 0))[0]
        client = (scope.get("client") or ("", 0))[0]
        sandbox_ips = await self.interfaces.known()
        from_sandbox = bool(sandbox_ips) and local in sandbox_ips and client not in self.interfaces.gateways
        if path.startswith(SANDBOX_PATH_PREFIX):
            allowed = from_sandbox
        else:
            allowed = not from_sandbox
        if allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "Not available from this network"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
