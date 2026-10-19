"""OmniRoute's built-in MCP server: the Streamable HTTP client
(backend/mcp_client.py's MCPHttpClient) and the curated connection on top of
it (backend/omniroute_mcp.py)."""

import json

import httpx
import pytest

from backend import mcp_client, omniroute_mcp


class FakeOmniRoute:
    """Minimal Streamable HTTP MCP server: answers initialize with a session
    id, paginates tools/list, replies to tools/call over SSE."""

    def __init__(self, tools, *, status=200):
        self.tools = tools
        self.status = status
        self.requests = []
        self.sessions = 0
        self.expire_next_call = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, text="LOCAL_ONLY")
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        self.requests.append((body.get("method"), request.headers.get("mcp-session-id"), request.headers.get("authorization")))
        method = body.get("method")
        if method == "initialize":
            self.sessions += 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}},
                                  headers={"mcp-session-id": f"s{self.sessions}"})
        if "id" not in body:
            return httpx.Response(202)
        if method == "tools/list":
            cursor = (body.get("params") or {}).get("cursor")
            half = len(self.tools) // 2
            if cursor:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self.tools[half:]}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self.tools[:half], "nextCursor": "p2"}})
        if method == "tools/call":
            if self.expire_next_call:
                self.expire_next_call = False
                return httpx.Response(404, text="session not found")
            name = body["params"]["name"]
            events = (
                'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                f'event: message\ndata: {{"jsonrpc":"2.0","id":{body["id"]},"result":{{"content":[{{"type":"text","text":"ok:{name}"}}]}}}}\n\n'
            )
            return httpx.Response(200, text=events, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601}})


def _tools(*names):
    return [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}} for n in names]


@pytest.mark.asyncio
async def test_http_client_handshake_pagination_and_sse_reply():
    server = FakeOmniRoute(_tools("a", "b", "c", "d"))
    client = mcp_client.MCPHttpClient("x", "http://omniroute:20128/api/mcp/stream",
                                      headers={"Authorization": "Bearer k"}, transport=httpx.MockTransport(server.handler))
    await client.start()
    assert [t["name"] for t in client.tools] == ["a", "b", "c", "d"]
    assert await client.call_tool("b", {}) == "ok:b"
    methods = [m for m, _, _ in server.requests]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    # Every request after initialize carries the session id and the key.
    assert all(sid == "s1" and auth == "Bearer k" for m, sid, auth in server.requests[1:])
    await client.shutdown()


@pytest.mark.asyncio
async def test_http_client_reinitializes_an_expired_session_once():
    server = FakeOmniRoute(_tools("a", "b"))
    client = mcp_client.MCPHttpClient("x", "http://omniroute:20128/api/mcp/stream", transport=httpx.MockTransport(server.handler))
    await client.start()
    server.expire_next_call = True
    assert await client.call_tool("a", {}) == "ok:a"
    assert server.sessions == 2
    await client.shutdown()


def test_tool_result_error_flag_is_kept():
    res = {"result": {"isError": True, "content": [{"type": "text", "text": "quota exhausted"}]}}
    assert json.loads(mcp_client.format_tool_result(res)) == {"error": "quota exhausted"}


@pytest.fixture()
def omni(monkeypatch):
    from backend.tools import TOOLS_SCHEMA

    before = list(TOOLS_SCHEMA)
    monkeypatch.setattr("backend.database.get_api_key", lambda name: "mcp-key" if name == "OMNIROUTE_MCP_API_KEY" else None)
    monkeypatch.delenv("OMNIROUTE_MCP_ENABLED", raising=False)

    def install(server):
        real = mcp_client.MCPHttpClient

        def factory(*args, **kwargs):
            return real(*args, transport=httpx.MockTransport(server.handler), **kwargs)

        monkeypatch.setattr(mcp_client, "MCPHttpClient", factory)

    yield install
    mcp_client.mcp_clients.pop(omniroute_mcp.SERVER_NAME, None)
    mcp_client.unregister_client_tools(omniroute_mcp.SERVER_NAME)
    TOOLS_SCHEMA[:] = before


@pytest.mark.asyncio
async def test_connect_exposes_only_curated_tools(omni):
    from backend.tools import TOOLS_SCHEMA

    server = FakeOmniRoute(_tools("omniroute_get_health", "omniroute_route_request", "omniroute_memory_add", "omniroute_switch_combo"))
    omni(server)
    status = await omniroute_mcp.connect()
    assert status["connected"] is True
    assert status["tools"] == ["omniroute_get_health", "omniroute_switch_combo"]
    assert status["available_tools"] == 4
    names = {item["function"]["name"] for item in TOOLS_SCHEMA}
    assert "omniroute_get_health" in names and "omniroute_route_request" not in names
    assert server.requests[0][2] == "Bearer mcp-key"
    assert await mcp_client.handle_mcp_tool("omniroute_get_health", {}) == "ok:omniroute_get_health"

    await omniroute_mcp.disconnect()
    assert "omniroute_get_health" not in {item["function"]["name"] for item in TOOLS_SCHEMA}
    assert omniroute_mcp.status()["connected"] is False


@pytest.mark.asyncio
async def test_connect_explains_a_key_without_mcp_scope(omni):
    omni(FakeOmniRoute([], status=403))
    status = await omniroute_mcp.connect()
    assert status["connected"] is False
    assert "mcp:connect" in status["error"]


@pytest.mark.asyncio
async def test_connect_can_be_switched_off(omni, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_MCP_ENABLED", "false")
    status = await omniroute_mcp.connect()
    assert status == {**status, "enabled": False, "connected": False}


def test_risk_classes_and_owner_only_policy():
    from backend import tool_permissions
    from backend.control_plane import classify_tool_risk

    assert classify_tool_risk("omniroute_check_quota") == "R0"
    assert classify_tool_risk("omniroute_web_search") == "R1"
    assert classify_tool_risk("omniroute_switch_combo") == "R3"
    assert classify_tool_risk("omniroute_something_new") == "R4"
    assert tool_permissions.is_tool_allowed("subagent", "omniroute_check_quota")
    assert not tool_permissions.is_tool_allowed("subagent", "omniroute_switch_combo")
    assert tool_permissions.is_tool_allowed("owner", "omniroute_switch_combo")
    assert not tool_permissions.is_tool_allowed("public_channel", "omniroute_check_quota")


def test_omniroute_is_a_reserved_mcp_name():
    from backend.mcp_governance import build_openapi_config

    with pytest.raises(ValueError, match="reserved"):
        build_openapi_config("omniroute", "https://example.com/openapi.json")


def test_retired_router_host_bindings_are_repointed(tmp_path, monkeypatch):
    import sqlite3

    from backend import database, provider_governance

    db_path = str(tmp_path / "gov.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(provider_governance, "DB_PATH", db_path)
    provider_governance._init_schema()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO provider_bindings (id, name, provider_type, api_base, valkey_secret_key, status, config_digest, created_at, updated_at)"
            " VALUES ('b1', 'router', 'openai_compatible', ?, 'k', 'active', 'old', '', '')",
            ("http://9router:20128/v1",),
        )
    provider_governance._init_schema()
    with sqlite3.connect(db_path) as conn:
        api_base, digest = conn.execute("SELECT api_base, config_digest FROM provider_bindings WHERE id='b1'").fetchone()
    assert api_base == "http://omniroute:20128/v1"
    assert digest == provider_governance._digest(
        provider_governance.validate_binding_config("router", "openai_compatible", api_base)
    )
