"""Unit tests for GET /api/router/stats (backend/main.py), the read-only
reachability/catalog check for the optional 9Router sidecar. Called directly
as a plain async function — the route carries no parameters, so no FastAPI
test harness is needed, matching this codebase's module-level testing style.

Note: this checks GET /v1/models (documented, Bearer-key auth, confirmed
working against a real running instance) rather than /api/quota or
/api/usage — those are advertised in 9Router's own docs but 404 against the
actual shipped API, and /api/providers / /api/combos exist but only accept a
dashboard session cookie, not an API key. See router_stats_api's docstring.
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
        result = await main.router_stats_api()
    assert result["available"] is False
    assert result["reachable"] is None
    assert result["model_count"] is None


@pytest.mark.asyncio
async def test_unreachable_on_connection_error():
    get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("backend.database.get_api_key", return_value="rtr_key"), \
         patch("httpx.AsyncClient.get", new=get):
        result = await main.router_stats_api()
    assert result["available"] is True
    assert result["reachable"] is False
    assert result["model_count"] is None


@pytest.mark.asyncio
async def test_unreachable_on_invalid_key():
    with patch("backend.database.get_api_key", return_value="wrong_key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_resp(401, {"error": "Unauthorized"}))):
        result = await main.router_stats_api()
    assert result["reachable"] is False
    assert "invalid" in result["error"].lower()


@pytest.mark.asyncio
async def test_full_success_reports_model_and_provider_counts():
    models = {
        "object": "list",
        "data": [
            {"id": "anthropic/claude-opus-4", "object": "model", "owned_by": "anthropic"},
            {"id": "anthropic/claude-sonnet-4", "object": "model", "owned_by": "anthropic"},
            {"id": "glm/glm-5", "object": "model", "owned_by": "glm"},
        ],
    }
    fake_binding = {"id": "prov-1", "name": "9router-main", "provider_type": "openai_compatible",
                    "api_base": "http://9router:20128/v1", "status": "active"}
    with patch("backend.database.get_api_key", return_value="rtr_key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_resp(200, models))), \
         patch("backend.provider_governance.list_bindings", return_value=[fake_binding]):
        result = await main.router_stats_api()
    assert result["available"] is True
    assert result["reachable"] is True
    assert result["error"] is None
    assert result["model_count"] == 3
    assert result["provider_count"] == 2  # anthropic, glm
    assert result["sample_models"] == ["anthropic/claude-opus-4", "anthropic/claude-sonnet-4", "glm/glm-5"]
    assert result["providers"] == [fake_binding]


@pytest.mark.asyncio
async def test_router_api_key_never_appears_in_the_response():
    async def fake_get(url, headers=None, params=None):
        assert headers["Authorization"] == "Bearer super-secret-router-key"
        return _resp(200, {"data": []})

    with patch("backend.database.get_api_key", return_value="super-secret-router-key"), \
         patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
         patch("backend.provider_governance.list_bindings", return_value=[]):
        result = await main.router_stats_api()
    import json
    assert "super-secret-router-key" not in json.dumps(result)
