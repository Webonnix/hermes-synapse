"""Tests for the /api/agents (save) and /api/providers pricing routes in
backend/main.py — called directly as plain async functions, same style as
test_router_tiers_endpoints.py."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend import main


def _subagent_update(**overrides) -> "main.SubagentUpdate":
    base = dict(
        id="researcher", name="Researcher", system_prompt="sp", model="qwen3:8b",
        model_provider="prov-x", allowed_provider_ids=["prov-y"],
    )
    base.update(overrides)
    return main.SubagentUpdate(**base)


@pytest.mark.asyncio
async def test_save_agent_persists_allowed_provider_ids():
    with patch("backend.database.save_subagent") as save:
        await main.save_subagent_api(_subagent_update())
    args = save.call_args.args
    # save_subagent(clean_id, name, system_prompt, model, agent_type, parent_id,
    #   skills, x, y, temperature, role, status, is_enabled, model_provider,
    #   model_type, model_params, budget_usd_limit, budget_period, tier_id,
    #   allowed_provider_ids, budget_fallback_to_local)
    assert args[13] == "prov-x"  # model_provider unchanged, no tier ceiling
    assert args[19] == ["prov-y"]  # allowed_provider_ids


@pytest.mark.asyncio
async def test_save_agent_tier_ceiling_clears_external_provider():
    """A tier with allow_external_provider=False must win over whatever the
    form submitted — defense in depth alongside the same ceiling enforced at
    call time in agent_provider_access.py."""
    locked_tier = {"id": "tier-locked", "allow_external_provider": False}
    with patch("backend.agent_tiers.get_tier", return_value=locked_tier), \
         patch("backend.database.save_subagent") as save:
        await main.save_subagent_api(_subagent_update(tier_id="tier-locked"))
    args = save.call_args.args
    assert args[13] == "ollama"  # model_provider forced back to local
    assert args[19] == []  # allowed_provider_ids cleared


@pytest.mark.asyncio
async def test_save_agent_open_tier_does_not_clear_provider():
    open_tier = {"id": "tier-open", "allow_external_provider": True}
    with patch("backend.agent_tiers.get_tier", return_value=open_tier), \
         patch("backend.database.save_subagent") as save:
        await main.save_subagent_api(_subagent_update(tier_id="tier-open"))
    args = save.call_args.args
    assert args[13] == "prov-x"
    assert args[19] == ["prov-y"]


@pytest.mark.asyncio
async def test_update_provider_pricing_calls_through():
    payload = main.ProviderPricingRequest(cost_per_1m_input=1.5, cost_per_1m_output=3.0)
    with patch("backend.provider_governance.update_binding_pricing", return_value={"id": "prov-1"}) as update:
        result = await main.update_provider_pricing_api("prov-1", payload)
    assert result == {"id": "prov-1"}
    update.assert_called_once_with("prov-1", 1.5, 3.0)


@pytest.mark.asyncio
async def test_update_provider_pricing_not_found():
    payload = main.ProviderPricingRequest()
    with patch("backend.provider_governance.update_binding_pricing", side_effect=KeyError("prov-ghost")):
        with pytest.raises(HTTPException) as exc_info:
            await main.update_provider_pricing_api("prov-ghost", payload)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_add_provider_forwards_pricing_override():
    payload = main.ProviderBindingRequest(
        name="ds", api_base="https://api.deepseek.com/v1", api_key="sk-x",
        cost_per_1m_input=0.5, cost_per_1m_output=1.0,
    )
    with patch(
        "backend.provider_governance.create_binding_proposal",
        return_value={"status": "awaiting_approval", "id": "prov-1", "control_task_id": "task-1"},
    ) as create:
        await main.add_provider_api(payload)
    create.assert_called_once_with("ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x", 0.5, 1.0)
