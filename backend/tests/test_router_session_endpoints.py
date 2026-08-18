"""Tests for the /api/router/session-status, /api/router/session-credential,
/api/router/combos and /api/router/connections routes in backend/main.py —
called directly as plain async functions, same style as test_router_stats.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import main


@pytest.mark.asyncio
async def test_session_status_reports_not_configured():
    with patch("backend.router_session.is_configured", return_value=False), \
         patch("backend.router_session.has_pending_proposal", return_value=None):
        result = await main.router_session_status_api()
    assert result == {"configured": False, "pending_task_id": None}


@pytest.mark.asyncio
async def test_session_status_reports_pending():
    with patch("backend.router_session.is_configured", return_value=False), \
         patch("backend.router_session.has_pending_proposal", return_value="T-abc123"):
        result = await main.router_session_status_api()
    assert result == {"configured": False, "pending_task_id": "T-abc123"}


@pytest.mark.asyncio
async def test_propose_credential_returns_the_proposal():
    with patch("backend.router_session.propose_router_password",
               return_value={"status": "awaiting_approval", "task_id": "T-1", "risk_class": "R3"}) as propose:
        result = await main.router_session_credential_propose_api(
            main.RouterSessionCredentialRequest(password="hunter2")
        )
    assert result["status"] == "awaiting_approval"
    propose.assert_called_once_with("hunter2")


@pytest.mark.asyncio
async def test_propose_credential_rejects_invalid_input():
    from fastapi import HTTPException

    with patch("backend.router_session.propose_router_password", side_effect=ValueError("password is required")):
        with pytest.raises(HTTPException) as exc_info:
            await main.router_session_credential_propose_api(main.RouterSessionCredentialRequest(password=""))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_revoke_credential_calls_through():
    with patch("backend.router_session.revoke_router_password") as revoke:
        result = await main.router_session_credential_revoke_api()
    assert result == {"status": "success"}
    revoke.assert_called_once()


@pytest.mark.asyncio
async def test_combos_not_configured():
    with patch("backend.router_session.is_configured", return_value=False):
        result = await main.router_combos_api()
    assert result["available"] is False
    assert result["combos"] is None


@pytest.mark.asyncio
async def test_combos_session_unavailable():
    with patch("backend.router_session.is_configured", return_value=True), \
         patch("backend.router_session.session_request", new=AsyncMock(return_value=None)):
        result = await main.router_combos_api()
    assert result["available"] is True
    assert result["combos"] is None
    assert "session unavailable" in result["error"]


@pytest.mark.asyncio
async def test_combos_success():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"combos": [{"name": "hermes-primary", "tiers": []}]}
    with patch("backend.router_session.is_configured", return_value=True), \
         patch("backend.router_session.session_request", new=AsyncMock(return_value=resp)):
        result = await main.router_combos_api()
    assert result["available"] is True
    assert result["combos"] == [{"name": "hermes-primary", "tiers": []}]
    assert result["error"] is None


@pytest.mark.asyncio
async def test_connections_success():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"connections": [{"name": "claude-subscription", "type": "oauth"}]}
    with patch("backend.router_session.is_configured", return_value=True), \
         patch("backend.router_session.session_request", new=AsyncMock(return_value=resp)):
        result = await main.router_connections_api()
    assert result["available"] is True
    assert result["connections"] == [{"name": "claude-subscription", "type": "oauth"}]


@pytest.mark.asyncio
async def test_connections_non_200_reports_error_without_raising():
    resp = MagicMock(status_code=500)
    with patch("backend.router_session.is_configured", return_value=True), \
         patch("backend.router_session.session_request", new=AsyncMock(return_value=resp)):
        result = await main.router_connections_api()
    assert result["available"] is True
    assert result["connections"] is None
    assert "500" in result["error"]


@pytest.mark.asyncio
async def test_bind_self_rejects_when_no_router_api_key():
    from fastapi import HTTPException

    with patch("backend.database.get_api_key", return_value=None), \
         patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("ROUTER_API_KEY", None)
        with pytest.raises(HTTPException) as exc_info:
            await main.router_bind_self_api()
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_bind_self_rejects_duplicate_binding():
    from fastapi import HTTPException

    with patch("backend.database.get_api_key", return_value="router-key"), \
         patch("backend.provider_governance.list_bindings",
               return_value=[{"id": "b1", "name": "9Router", "api_base": "http://9router:20128/v1"}]):
        with pytest.raises(HTTPException) as exc_info:
            await main.router_bind_self_api()
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_bind_self_creates_proposal_with_stored_key():
    with patch("backend.database.get_api_key", return_value="router-key"), \
         patch("backend.provider_governance.list_bindings", return_value=[]), \
         patch("backend.provider_governance.create_binding_proposal",
               return_value={"status": "awaiting_approval", "id": "b2", "control_task_id": "T-99"}) as create:
        result = await main.router_bind_self_api()
    assert result["status"] == "awaiting_approval"
    assert result["task_id"] == "T-99"
    create.assert_called_once_with("9Router", "openai_compatible", "http://9router:20128/v1", "router-key")


def test_validate_binding_config_accepts_the_9router_sidecar_hostname():
    """Regression test: the SSRF/HTTPS guard shared with MCP server validation
    (backend/mcp_governance.py's _validate_url) didn't recognize the compose
    service name '9router' as private, so both the manual 'Add provider' form
    and POST /api/router/bind-self failed with 'must use HTTPS outside the
    private network' for the exact http://9router:20128/v1 URL the AI Router
    tab's own copy tells the user to use."""
    from backend.provider_governance import validate_binding_config

    result = validate_binding_config("9router", "openai_compatible", "http://9router:20128/v1")
    assert result["api_base"] == "http://9router:20128/v1"


def test_validate_binding_config_still_rejects_plain_http_for_other_hosts():
    from backend.provider_governance import validate_binding_config

    with pytest.raises(ValueError, match="HTTPS"):
        validate_binding_config("evil", "openai_compatible", "http://example.com/v1")
