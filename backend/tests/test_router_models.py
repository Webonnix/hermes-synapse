"""Unit tests for GET /api/router/models (backend/main.py) — the full 9Router
model catalog powering the AI Router tab's "Добавить тир" model picker. Same
module-level call style as test_router_stats.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend import main


def _resp(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


@pytest.mark.asyncio
async def test_not_configured_without_a_key(monkeypatch):
    monkeypatch.delenv("ROUTER_API_KEY", raising=False)
    with patch("backend.database.get_api_key", return_value=None):
        result = await main.router_models_api()
    assert result == {"available": False, "models": [], "error": "ROUTER_API_KEY not configured (Settings -> API Keys -> AI Router)."}


@pytest.mark.asyncio
async def test_unreachable_on_connection_error():
    with patch("backend.database.get_api_key", return_value="rtr_key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        result = await main.router_models_api()
    assert result["available"] is True
    assert result["models"] == []
    assert "not reachable" in result["error"]


@pytest.mark.asyncio
async def test_invalid_key_reports_error_without_raising():
    with patch("backend.database.get_api_key", return_value="wrong_key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_resp(401))):
        result = await main.router_models_api()
    assert result["models"] == []
    assert "invalid ROUTER_API_KEY" in result["error"]


@pytest.mark.asyncio
async def test_returns_sorted_deduplicated_models_with_owner():
    models = {
        "data": [
            {"id": "ds/deepseek-chat", "owned_by": "ds"},
            {"id": "kimi/kimi-k3", "owned_by": "kimi"},
            {"id": "ds/deepseek-chat", "owned_by": "ds"},  # duplicate — must be collapsed
            {"id": "kimi/k3", "owned_by": "kimi"},
        ],
    }
    with patch("backend.database.get_api_key", return_value="rtr_key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_resp(200, models))):
        result = await main.router_models_api()
    assert result["available"] is True
    assert result["error"] is None
    assert result["models"] == [
        {"id": "ds/deepseek-chat", "owned_by": "ds", "capabilities": {}},
        {"id": "kimi/k3", "owned_by": "kimi", "capabilities": {}},
        {"id": "kimi/kimi-k3", "owned_by": "kimi", "capabilities": {}},
    ]


@pytest.mark.asyncio
async def test_capabilities_pass_through_for_the_agent_settings_panel():
    """The agent form builds its per-model controls (output ceiling, reasoning
    switch) from these, so dropping them would leave the panel guessing."""
    caps = {"contextWindow": 1048576, "maxOutput": 131072, "tools": True, "thinkingCanDisable": False}
    models = {"data": [{"id": "kimi/kimi-k3", "owned_by": "kimi", "capabilities": caps},
                       {"id": "ds/x", "owned_by": "ds", "capabilities": "not-a-dict"}]}
    with patch("backend.database.get_api_key", return_value="rtr_key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_resp(200, models))):
        result = await main.router_models_api()
    by_id = {m["id"]: m for m in result["models"]}
    assert by_id["kimi/kimi-k3"]["capabilities"] == caps
    assert by_id["ds/x"]["capabilities"] == {}  # malformed entries must not reach the UI


@pytest.mark.asyncio
async def test_router_api_key_never_appears_in_the_response():
    async def fake_get(url, headers=None, params=None):
        assert headers["Authorization"] == "Bearer super-secret-router-key"
        return _resp(200, {"data": [{"id": "kimi/kimi-k3", "owned_by": "kimi"}]})

    with patch("backend.database.get_api_key", return_value="super-secret-router-key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result = await main.router_models_api()
    import json
    assert "super-secret-router-key" not in json.dumps(result)
