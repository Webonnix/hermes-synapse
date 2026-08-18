"""Tests for the /api/router/tiers and /api/router/overview routes in
backend/main.py — called directly as plain async functions, same style as
test_router_session_endpoints.py."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend import main


@pytest.mark.asyncio
async def test_list_tiers_annotates_quota():
    with patch("backend.router_tiers.list_tiers", return_value=[{"id": "t1", "quota_limit": 10, "quota_window_hours": 24}]), \
         patch("backend.router_usage.tier_quota_status", return_value={"used": 3, "limit": 10, "pct": 30.0}) as quota:
        result = await main.list_router_tiers_api()
    assert result[0]["quota"] == {"used": 3, "limit": 10, "pct": 30.0}
    quota.assert_called_once_with("t1", 10, 24)


@pytest.mark.asyncio
async def test_create_tier_calls_through():
    payload = main.RouterTierRequest(label="premium", tier_rank=1, kind="binding", provider_binding_id="prov-1")
    with patch("backend.router_tiers.create_tier", return_value={"id": "rtier-1"}) as create:
        result = await main.create_router_tier_api(payload)
    assert result == {"id": "rtier-1"}
    create.assert_called_once_with("premium", 1, "binding", "prov-1", "", None, 24, True)


@pytest.mark.asyncio
async def test_create_tier_rejects_invalid_input():
    payload = main.RouterTierRequest(label="x", tier_rank=1, kind="binding", provider_binding_id=None)
    with patch("backend.router_tiers.create_tier", side_effect=ValueError("provider_binding_id is required")):
        with pytest.raises(HTTPException) as exc_info:
            await main.create_router_tier_api(payload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_tier_not_found():
    payload = main.RouterTierRequest(label="x", tier_rank=1, kind="local")
    with patch("backend.router_tiers.update_tier", side_effect=KeyError("rtier-missing")):
        with pytest.raises(HTTPException) as exc_info:
            await main.update_router_tier_api("rtier-missing", payload)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_tier_calls_through():
    with patch("backend.router_tiers.delete_tier") as delete:
        result = await main.delete_router_tier_api("rtier-1")
    assert result == {"status": "success", "id": "rtier-1"}
    delete.assert_called_once_with("rtier-1")


@pytest.mark.asyncio
async def test_overview_calls_through():
    stats = {"active_combo_label": "premium", "requests_24h": 42, "token_savings_pct": 61.0}
    with patch("backend.router_usage.overview_stats", return_value=stats):
        result = await main.router_overview_api()
    assert result == stats
