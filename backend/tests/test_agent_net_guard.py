"""net_guard.SandboxNetworkGuard: sandboxes on the dev network reach only
/agent-llm, never /api — decided by the backend interface a request arrived on."""

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, WebSocket

from backend.agent_runtimes import desktop, net_guard

SANDBOX_IP = "172.23.0.4"   # backend on dev-net
WEB_IP = "172.20.0.2"       # backend on the dashboard network
GATEWAY = "172.23.0.1"


class Known(net_guard.SandboxInterfaces):
    def __init__(self, addresses, gateways=()):
        super().__init__()
        self.addresses = set(addresses) if addresses is not None else None
        self.gateways = set(gateways)

    async def known(self):
        return self.addresses


def _app(interfaces):
    app = FastAPI()

    @app.post("/api/auth/request-code")
    async def request_code():
        return {"sent": True}

    @app.get("/api/agent-desktops")
    async def listing():
        return {"ok": True}

    @app.get("/agent-llm/v1/models")
    async def models():
        return {"data": []}

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    app.add_middleware(net_guard.SandboxNetworkGuard, interfaces=interfaces)
    return app


async def _get(app, server_ip, path, *, client_ip="172.23.0.5", method="GET"):
    transport = httpx.ASGITransport(app=app, client=(client_ip, 40000))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{server_ip}:8000") as client:
        return await client.request(method, path)


async def test_sandbox_interface_reaches_only_the_model_door():
    app = _app(Known({SANDBOX_IP}, {GATEWAY}))
    assert (await _get(app, SANDBOX_IP, "/api/auth/request-code", method="POST")).status_code == 403
    assert (await _get(app, SANDBOX_IP, "/api/agent-desktops")).status_code == 403
    assert (await _get(app, SANDBOX_IP, "/agent-llm/v1/models")).status_code == 200


async def test_dashboard_interface_keeps_the_api_but_not_the_model_door():
    app = _app(Known({SANDBOX_IP}, {GATEWAY}))
    assert (await _get(app, WEB_IP, "/api/agent-desktops", client_ip="172.20.0.3")).status_code == 200
    assert (await _get(app, WEB_IP, "/agent-llm/v1/models", client_ip="172.20.0.3")).status_code == 403


async def test_the_host_through_the_sandbox_network_gateway_is_not_a_sandbox():
    app = _app(Known({SANDBOX_IP}, {GATEWAY}))
    assert (await _get(app, SANDBOX_IP, "/api/agent-desktops", client_ip=GATEWAY)).status_code == 200


async def test_until_interfaces_are_known_the_model_door_is_closed_and_the_api_works():
    app = _app(Known(None))
    assert (await _get(app, SANDBOX_IP, "/agent-llm/v1/models")).status_code == 403
    assert (await _get(app, WEB_IP, "/api/agent-desktops")).status_code == 200


async def _ws_handshake(app, server_ip):
    """Raw ASGI websocket connect (TestClient pins scope["server"] for websockets)."""
    sent = []
    incoming = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/api/ws", "raw_path": b"/api/ws", "query_string": b"",
             "headers": [], "server": (server_ip, 8000), "client": ("172.23.0.5", 40000),
             "scheme": "ws", "root_path": "", "subprotocols": [], "asgi": {"version": "3.0"}}
    await app(scope, receive, send)
    return sent


async def test_websockets_from_the_sandbox_interface_are_refused():
    app = _app(Known({SANDBOX_IP}, {GATEWAY}))
    refused = await _ws_handshake(app, SANDBOX_IP)
    assert refused[0] == {"type": "websocket.close", "code": 1008}
    accepted = await _ws_handshake(app, WEB_IP)
    assert accepted[0]["type"] == "websocket.accept"


def test_lookup_takes_the_backend_addresses_on_the_dev_runner_networks_only(monkeypatch):
    monkeypatch.setenv("DEV_RUNNER_CONTAINER_NAME", "proj-dev-runner")
    monkeypatch.delenv("AGENT_DESKTOP_NETWORK_SOURCE", raising=False)
    monkeypatch.setattr(net_guard.socket, "gethostname", lambda: "backend-ctr")
    containers = {
        "proj-dev-runner": MagicMock(attrs={"NetworkSettings": {"Networks": {"p_dev-net": {}, "p_dev-egress-net": {}}}}),
        "backend-ctr": MagicMock(attrs={"NetworkSettings": {"Networks": {
            "p_dev-net": {"IPAddress": SANDBOX_IP, "Gateway": GATEWAY},
            "p_web": {"IPAddress": WEB_IP, "Gateway": "172.20.0.1"},
        }}}),
    }
    client = MagicMock()
    client.containers.get.side_effect = lambda name: containers[name]
    monkeypatch.setattr(desktop, "_docker_client", lambda: client)
    assert net_guard.SandboxInterfaces()._lookup() == ({SANDBOX_IP}, {GATEWAY})


async def test_failed_lookup_is_retried_later_not_cached(monkeypatch):
    interfaces = net_guard.SandboxInterfaces()
    calls = []

    def lookup():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("docker-proxy down")
        return {SANDBOX_IP}, {GATEWAY}

    monkeypatch.setattr(interfaces, "_lookup", lookup)
    assert await interfaces.known() is None
    assert await interfaces.known() is None and len(calls) == 1  # waits for the retry window
    interfaces._next_try = 0.0
    assert await interfaces.known() == {SANDBOX_IP}
