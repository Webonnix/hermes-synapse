import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Dict, Any, List

logger = logging.getLogger("hermes.mcp_client")


def _base_process_environment() -> Dict[str, str]:
    """Do not leak the backend's API keys and tokens into third-party MCP processes."""
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


class MCPServerClient:
    def __init__(self, name: str, config: Dict[str, Any]):
        from backend.mcp_governance import expand_environment, validate_server_config

        validated = validate_server_config(name, config)
        self.name = name
        self.command = validated["command"]
        self.args = validated["args"]
        self.env = {**_base_process_environment(), **expand_environment(validated["env"])}
        self.process = None
        self.reader = None
        self.writer = None
        self.req_id = 0
        self.pending_requests = {}
        self.tools = []

    async def start(self):
        try:
            logger.info(f"Starting MCP server '{self.name}': {self.command} {' '.join(self.args)}")
            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env
            )
            self.reader = self.process.stdout
            self.writer = self.process.stdin
            
            # Start background reader task for messages
            asyncio.create_task(self._read_loop())
            
            # Initialize connection
            await self._initialize()
            
            # List tools
            await self._list_tools()
            logger.info(f"MCP server '{self.name}' successfully initialized with {len(self.tools)} tools.")
        except Exception as e:
            logger.error(f"Failed to start MCP server '{self.name}': {e}")
            if self.process and self.process.returncode is None:
                self.process.terminate()
                await self.process.wait()
            raise

    async def _read_loop(self):
        # Background task reading stderr to log it
        async def read_stderr():
            while True:
                if not self.process or self.process.returncode is not None:
                    break
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.warning(f"[{self.name} stderr] {line.decode('utf-8').strip()}")
        asyncio.create_task(read_stderr())

        while True:
            if not self.process or self.process.returncode is not None:
                break
            line_bytes = await self.reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg_id = msg.get("id")
                if msg_id in self.pending_requests:
                    fut = self.pending_requests[msg_id]
                    if not fut.done():
                        fut.set_result(msg)
            except Exception as e:
                logger.error(f"Error parsing line from '{self.name}': {e}")

    async def send_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.req_id += 1
        curr_id = self.req_id
        fut = asyncio.get_event_loop().create_future()
        self.pending_requests[curr_id] = fut
        
        req = {
            "jsonrpc": "2.0",
            "id": curr_id,
            "method": method,
            "params": params
        }
        
        self.writer.write(json.dumps(req).encode("utf-8") + b"\n")
        await self.writer.drain()
        
        try:
            res = await asyncio.wait_for(fut, timeout=60.0)
            return res
        except asyncio.TimeoutError:
            logger.error(f"Request {method} to '{self.name}' timed out")
            raise
        finally:
            self.pending_requests.pop(curr_id, None)

    async def _initialize(self):
        res = await self.send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "hermes-mcp-client",
                "version": "1.0.0"
            }
        })
        # The spec requires this before any other request; servers stricter
        # than the bundled OpenAPI bridge refuse tools/list without it.
        self.writer.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8") + b"\n")
        await self.writer.drain()
        return res

    async def _list_tools(self):
        res = await self.send_request("tools/list", {})
        self.tools = res.get("result", {}).get("tools", [])

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        res = await self.send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        return format_tool_result(res)

    async def shutdown(self):
        if self.process:
            logger.info(f"Stopping MCP server '{self.name}'")
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error terminating MCP server '{self.name}': {e}")


def format_tool_result(res: Dict[str, Any]) -> str:
    """Flatten a JSON-RPC tools/call response into the string handed to the model."""
    if "error" in res:
        return json.dumps({"error": res["error"]}, ensure_ascii=False)
    result = res.get("result", {}) or {}
    texts = [item.get("text", "") for item in result.get("content", []) or [] if item.get("type") == "text"]
    if texts:
        text = "\n".join(texts)
        return json.dumps({"error": text}, ensure_ascii=False) if result.get("isError") else text
    return json.dumps(result, ensure_ascii=False)


class MCPHttpClient:
    """MCP client over the Streamable HTTP transport (spec 2025-03-26).

    Same surface as MCPServerClient (``tools``, ``start``, ``call_tool``,
    ``shutdown``) so the registry and tool dispatch don't care which transport
    a server uses. Only built in for servers whose URL and credential the
    deployment itself owns (see backend/omniroute_mcp.py) — user-proposed MCP
    connections still go through mcp_governance's stdio-only validation.
    """

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(self, name: str, url: str, headers: Dict[str, str] | None = None, timeout: float = 60.0, transport=None):
        self.name = name
        self._transport = transport
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.session_id: str | None = None
        self.req_id = 0
        self.tools: List[Dict[str, Any]] = []
        self._client = None

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    @staticmethod
    def _parse_body(content_type: str, body: str, want_id: int | None) -> Dict[str, Any] | None:
        if "text/event-stream" not in content_type:
            return json.loads(body) if body.strip() else None
        # An SSE response may interleave server notifications with the reply;
        # keep the one that answers our request id.
        data_lines: List[str] = []
        found = None
        for line in body.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line.strip() and data_lines:
                try:
                    message = json.loads("\n".join(data_lines))
                except ValueError:
                    message = None
                data_lines = []
                if isinstance(message, dict) and message.get("id") == want_id:
                    found = message
        return found

    async def _post(self, payload: Dict[str, Any], *, retry_on_expired: bool = True) -> Dict[str, Any] | None:
        import httpx

        resp = await self._client.post(self.url, json=payload, headers=self._request_headers())
        # 404 on a request that carried a session id means the server dropped
        # the session (restart, `omniroute mcp restart`) — re-initialize once.
        if resp.status_code == 404 and self.session_id and retry_on_expired and payload.get("method") != "initialize":
            self.session_id = None
            await self._initialize()
            return await self._post(payload, retry_on_expired=False)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"MCP server '{self.name}' returned HTTP {resp.status_code}: {resp.text[:300]}",
                request=resp.request,
                response=resp,
            )
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        if "id" not in payload:
            return None
        return self._parse_body(resp.headers.get("content-type", ""), resp.text, payload["id"])

    async def send_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.req_id += 1
        res = await self._post({"jsonrpc": "2.0", "id": self.req_id, "method": method, "params": params})
        if res is None:
            raise RuntimeError(f"MCP server '{self.name}' sent no response to {method}")
        return res

    async def _initialize(self):
        res = await self.send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hermes-mcp-client", "version": "1.0.0"},
        })
        if "error" in res:
            raise RuntimeError(f"MCP initialize failed: {res['error']}")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    async def start(self):
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        try:
            await self._initialize()
            tools: List[Dict[str, Any]] = []
            cursor = None
            for _ in range(50):
                res = await self.send_request("tools/list", {"cursor": cursor} if cursor else {})
                if "error" in res:
                    raise RuntimeError(f"MCP tools/list failed: {res['error']}")
                result = res.get("result", {}) or {}
                tools.extend(result.get("tools", []) or [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self.tools = tools
            logger.info("MCP server '%s' (HTTP) initialized with %d tools.", self.name, len(self.tools))
        except Exception:
            await self.shutdown()
            raise

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        res = await self.send_request("tools/call", {"name": tool_name, "arguments": arguments})
        return format_tool_result(res)

    async def shutdown(self):
        if not self._client:
            return
        if self.session_id:
            try:
                await self._client.delete(self.url, headers=self._request_headers())
            except Exception as exc:
                logger.debug("MCP session close for '%s' failed: %s", self.name, exc)
        self.session_id = None
        await self._client.aclose()
        self._client = None


# Global registry
mcp_clients: Dict[str, Any] = {}
mcp_tool_to_server: Dict[str, str] = {}
_omniroute_startup_task = None


def unregister_client_tools(name: str) -> None:
    """Drop every tool a server registered, from both the dispatch map and the
    schema agents see, so a disconnected server's tools can't be offered."""
    from backend.tools import TOOLS_SCHEMA

    dead = {tool for tool, server in mcp_tool_to_server.items() if server == name}
    for tool in dead:
        mcp_tool_to_server.pop(tool, None)
    TOOLS_SCHEMA[:] = [item for item in TOOLS_SCHEMA if item.get("function", {}).get("name") not in dead]


def register_client_tools(name: str, client: Any) -> None:
    from backend.tools import TOOLS_SCHEMA

    for tool in client.tools:
        tool_name = tool["name"]
        mcp_tool_to_server[tool_name] = name
        if not any(item.get("function", {}).get("name") == tool_name for item in TOOLS_SCHEMA):
            TOOLS_SCHEMA.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })


async def init_mcp_servers():
    # OmniRoute's MCP server is built in rather than listed in mcp_config.json:
    # its URL and key come from this deployment's own settings. Connecting is
    # best-effort and runs in the background so a slow sidecar never delays
    # startup.
    from backend import omniroute_mcp

    global _omniroute_startup_task
    _omniroute_startup_task = asyncio.create_task(omniroute_mcp.connect())

    backend_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(backend_dir, "data", "mcp_config.json")
    
    if not os.path.exists(config_path):
        config_path = os.path.join(os.path.dirname(backend_dir), "mcp_config.json")
        if not os.path.exists(config_path):
            logger.info("No mcp_config.json found, skipping MCP client setup.")
            return

    try:
        logger.info(f"Loading MCP config from {config_path}")
        with open(config_path, "r") as f:
            config = json.load(f)
        servers = config.get("mcpServers", {})
        for name, srv_config in servers.items():
            if name == omniroute_mcp.SERVER_NAME:
                continue
            try:
                client = MCPServerClient(name, srv_config)
                await client.start()
                mcp_clients[name] = client
                register_client_tools(name, client)
            except Exception as exc:
                logger.error("Skipping unsafe or unavailable MCP server '%s': %s", name, exc)
    except Exception as e:
        logger.error(f"Error loading MCP servers: {e}")

async def handle_mcp_tool(name: str, arguments: Dict[str, Any]) -> str:
    server_name = mcp_tool_to_server.get(name)
    if not server_name or server_name not in mcp_clients:
        return json.dumps({"error": f"MCP server not found for tool {name}"})
    return await mcp_clients[server_name].call_tool(name, arguments)

async def shutdown_mcp_servers():
    for client in list(mcp_clients.values()):
        await client.shutdown()
    mcp_clients.clear()
    mcp_tool_to_server.clear()
