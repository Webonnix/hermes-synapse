"""OmniRoute's built-in MCP server, connected as a first-class Hermes MCP server.

OmniRoute (the `omniroute` compose sidecar that fronts external LLM providers)
ships an MCP server with ~110 tools. Hermes talks to it over the Streamable
HTTP transport at ``{ROUTER_API_BASE}/api/mcp/stream`` (backend/mcp_client.py's
MCPHttpClient) instead of spawning ``omniroute --mcp`` — the sidecar is already
running and owns the state those tools read.

Requirements on the OmniRoute side, both checked by ``status()`` and surfaced
in Admin -> AI Router:

* MCP enabled with the ``streamable-http`` transport (``mcpEnabled`` /
  ``mcpTransport`` in its settings; ``enable_on_router()`` flips them through
  the governed dashboard session from backend/router_session.py).
* ``/api/mcp/*`` is LOCAL_ONLY for non-loopback callers — the backend
  container is one — unless the Bearer key carries the ``mcp:connect`` (or
  ``manage``) scope. Use a dedicated key in OMNIROUTE_MCP_API_KEY; without it
  the regular ROUTER_API_KEY is tried.

Only a curated subset of the tools is exposed to agents (``TOOL_RISKS``):
routing/quota/health/cost visibility, web search/fetch, and a few owner-only
routing controls. Deliberately left out:

* ``omniroute_route_request`` — it sends a prompt straight to an upstream LLM,
  which would skip Hermes's own privacy gateway (backend/privacy) and agent
  budget accounting. Agents reach models through provider bindings instead.
* memory / skills / Obsidian / Notion / plugins / gamification / proxy-pool /
  CCR tools — Hermes has its own memory, skills and Obsidian integration, and
  duplicating them would split state between two systems.
* ``omniroute_db_health_check`` (can auto-repair OmniRoute's DB) and
  ``omniroute_sync_pricing`` — maintenance actions for OmniRoute's own UI.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.omniroute_mcp")

SERVER_NAME = "omniroute"

# Risk classes follow backend/control_plane.py: R0/R1 run without approval,
# R2 is budget-gated, R3 needs one owner approval per call.
TOOL_RISKS: Dict[str, str] = {
    # Read-only visibility into the router itself.
    "omniroute_get_health": "R0",
    "omniroute_list_combos": "R0",
    "omniroute_get_combo_metrics": "R0",
    "omniroute_check_quota": "R0",
    "omniroute_cost_report": "R0",
    "omniroute_list_models_catalog": "R0",
    "omniroute_simulate_route": "R0",
    "omniroute_get_provider_metrics": "R0",
    "omniroute_best_combo_for_task": "R0",
    "omniroute_explain_route": "R0",
    "omniroute_get_session_snapshot": "R0",
    "omniroute_cache_stats": "R0",
    "omniroute_compression_status": "R0",
    # External lookups through OmniRoute's search/fetch providers — same tier
    # as Hermes's own web_search.
    "omniroute_web_search": "R1",
    "omniroute_web_fetch": "R1",
    # Real upstream calls on every provider of a combo: costs tokens.
    "omniroute_test_combo": "R2",
    # Change how every agent's traffic is routed.
    "omniroute_switch_combo": "R3",
    "omniroute_create_combo": "R3",
    "omniroute_set_routing_strategy": "R3",
    "omniroute_set_resilience_profile": "R3",
    "omniroute_set_budget_guard": "R3",
    "omniroute_cache_flush": "R3",
}

# Tools that change OmniRoute's configuration (or spend on test calls) act on
# the owner's infrastructure — backend/tool_permissions.py keeps them with the
# main agent, the same way it reserves shell and git.
OWNER_ONLY_TOOLS = frozenset(name for name, risk in TOOL_RISKS.items() if risk in {"R2", "R3"})

_last_error: Optional[str] = None


def enabled() -> bool:
    return os.getenv("OMNIROUTE_MCP_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def router_api_base() -> str:
    return os.getenv("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/")


def mcp_url() -> str:
    return f"{router_api_base()}/api/mcp/stream"


def _api_key() -> str:
    from backend import database as db

    for name in ("OMNIROUTE_MCP_API_KEY", "ROUTER_API_KEY"):
        value = db.get_api_key(name) or os.getenv(name, "")
        if value:
            return value
    return ""


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "HTTP 403" in text:
        return ("OmniRoute отклонил ключ для /api/mcp (LOCAL_ONLY): выдайте ключу scope "
                "mcp:connect (Dashboard → API Keys) и сохраните его как OMNIROUTE_MCP_API_KEY.")
    if "HTTP 401" in text:
        return "OmniRoute не принял ключ (HTTP 401) — проверьте OMNIROUTE_MCP_API_KEY / ROUTER_API_KEY."
    if "HTTP 404" in text or "HTTP 409" in text or "HTTP 503" in text:
        return ("MCP-сервер OmniRoute выключен или выбран не тот транспорт — включите MCP "
                "с транспортом streamable-http (кнопка «Включить MCP» или Dashboard → MCP).")
    return f"OmniRoute MCP недоступен: {text[:300]}"


async def connect() -> Dict[str, Any]:
    """(Re)connect the OmniRoute MCP server and register its curated tools.

    Never raises: OmniRoute is optional, so a failure is recorded for
    ``status()`` and the backend keeps running without these tools.
    """
    global _last_error
    from backend.mcp_client import MCPHttpClient, mcp_clients, register_client_tools, unregister_client_tools

    await disconnect()
    if not enabled():
        _last_error = "Отключено через OMNIROUTE_MCP_ENABLED."
        return status()
    key = _api_key()
    if not key:
        _last_error = "Не задан ключ OmniRoute (OMNIROUTE_MCP_API_KEY или ROUTER_API_KEY)."
        return status()

    client = MCPHttpClient(SERVER_NAME, mcp_url(), headers={"Authorization": f"Bearer {key}"})
    try:
        await client.start()
    except Exception as exc:
        _last_error = _explain(exc)
        logger.warning("OmniRoute MCP connect failed: %s", exc)
        return status()

    client.available_tools = len(client.tools)
    client.tools = [tool for tool in client.tools if tool.get("name") in TOOL_RISKS]
    unregister_client_tools(SERVER_NAME)
    mcp_clients[SERVER_NAME] = client
    register_client_tools(SERVER_NAME, client)
    _last_error = None
    logger.info("OmniRoute MCP connected: %d of %d tools exposed.", len(client.tools), client.available_tools)
    return status()


async def disconnect() -> None:
    from backend.mcp_client import mcp_clients, unregister_client_tools

    client = mcp_clients.pop(SERVER_NAME, None)
    unregister_client_tools(SERVER_NAME)
    if client:
        await client.shutdown()


def status() -> Dict[str, Any]:
    from backend.mcp_client import mcp_clients

    client = mcp_clients.get(SERVER_NAME)
    return {
        "enabled": enabled(),
        "connected": client is not None,
        "url": mcp_url(),
        "tools": sorted(tool["name"] for tool in client.tools) if client else [],
        "available_tools": getattr(client, "available_tools", 0) if client else 0,
        "error": None if client else _last_error,
    }


async def enable_on_router() -> Dict[str, Any]:
    """Turn on OmniRoute's MCP server (streamable-http) through the governed
    dashboard session, then connect. Returns status() plus ``router_error``
    when OmniRoute refused the settings change."""
    from backend.router_session import is_configured, session_request

    if not is_configured():
        result = status()
        result["router_error"] = "Сессия дашборда OmniRoute не настроена (AI Router → пароль дашборда)."
        return result
    resp = await session_request("PATCH", "/api/settings", json={"mcpEnabled": True, "mcpTransport": "streamable-http"})
    if resp is None or resp.status_code >= 400:
        result = status()
        code = "нет ответа" if resp is None else f"HTTP {resp.status_code}"
        result["router_error"] = f"OmniRoute не принял изменение настроек MCP ({code})."
        return result
    return await connect()
