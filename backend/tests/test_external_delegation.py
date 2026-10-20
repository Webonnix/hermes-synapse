"""The local model's explicit hand-off to external AI Router tiers
(backend/external_delegation.py and the delegate_to_external_model tool)."""

import json

import pytest

from backend import external_delegation
from backend.llm_client import NormalizedLLMResponse

TIERS = [
    {"id": "t1", "label": "DeepSeek Chat", "tier_rank": 1, "kind": "binding", "provider_binding_id": "p1",
     "model_override": "ds/deepseek-flash", "api_base": "http://omniroute:20128/v1", "api_key": "k"},
    {"id": "t2", "label": "DeepSeek Reasoner", "tier_rank": 2, "kind": "binding", "provider_binding_id": "p1",
     "model_override": "ds/deepseek-flash-max", "api_base": "http://omniroute:20128/v1", "api_key": "k"},
]


@pytest.fixture()
def tiers(monkeypatch):
    current = list(TIERS)
    monkeypatch.setattr("backend.router_tiers.list_active_tiers_resolved", lambda: current)
    monkeypatch.setattr("backend.local_orchestrator._log", lambda *a, **k: None)
    monkeypatch.setattr("backend.cost.calculate_cost", lambda *a, **k: 0.0012)
    return current


def _fake_llm(monkeypatch, outcomes):
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        status, text = outcomes[kwargs["model"]]
        return NormalizedLLMResponse(status=status, provider="openai_compatible", model=kwargs["model"],
                                     latency_ms=1, content=text, error_message=None if text else "boom")

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake)
    return calls


@pytest.mark.asyncio
async def test_fast_uses_the_first_tier_and_passes_context(tiers, monkeypatch):
    calls = _fake_llm(monkeypatch, {"ds/deepseek-flash": ("success", "готово")})
    result = await external_delegation.delegate("Перепиши функцию", context="def f(): pass")
    assert result == {"status": "success", "tier": "DeepSeek Chat", "model": "ds/deepseek-flash",
                      "answer": "готово", "cost_usd": 0.0012}
    assert "def f(): pass" in calls[0]["messages"][1]["content"]
    assert calls[0]["api_base"] == "http://omniroute:20128/v1"


@pytest.mark.asyncio
async def test_strong_starts_at_the_top_and_falls_through_on_failure(tiers, monkeypatch):
    calls = _fake_llm(monkeypatch, {"ds/deepseek-flash-max": ("provider_error", ""),
                                    "ds/deepseek-flash": ("success", "ok")})
    result = await external_delegation.delegate("Докажи", level="strong")
    assert [c["model"] for c in calls] == ["ds/deepseek-flash-max", "ds/deepseek-flash"]
    assert result["tier"] == "DeepSeek Chat"


@pytest.mark.asyncio
async def test_reports_every_failed_attempt(tiers, monkeypatch):
    _fake_llm(monkeypatch, {"ds/deepseek-flash": ("timeout", ""), "ds/deepseek-flash-max": ("timeout", "")})
    result = await external_delegation.delegate("x")
    assert result["status"] == "failed" and len(result["attempts"]) == 2


@pytest.mark.asyncio
async def test_no_tiers_means_unavailable_and_no_hint(tiers):
    tiers.clear()
    assert (await external_delegation.delegate("x"))["status"] == "unavailable"
    assert external_delegation.delegation_hint() == ""


def test_hint_names_the_models_and_the_tool(tiers):
    hint = external_delegation.delegation_hint()
    assert "DeepSeek Chat (ds/deepseek-flash)" in hint and "DeepSeek Reasoner" in hint
    assert external_delegation.TOOL_NAME in hint


def test_tool_dispatch_returns_json(tiers, monkeypatch):
    from backend.tools import execute_tool

    _fake_llm(monkeypatch, {"ds/deepseek-flash": ("success", "42")})
    out = json.loads(execute_tool("delegate_to_external_model", {"task": "6*7?"}))
    assert out["answer"] == "42" and out["cost_usd"] == 0.0012


def test_policy_risk_and_budget_gate():
    from backend import tool_permissions
    from backend.agent import PAID_TOOLS
    from backend.control_plane import classify_tool_risk

    assert classify_tool_risk("delegate_to_external_model") == "R2"
    assert "delegate_to_external_model" in PAID_TOOLS
    assert tool_permissions.is_tool_allowed("subagent", "delegate_to_external_model")
    assert not tool_permissions.is_tool_allowed("public_channel", "delegate_to_external_model")


def test_explicit_deepseek_request_escalates_without_the_classifier():
    from backend.local_orchestrator import _keyword_delegate

    assert _keyword_delegate("Сделай это через DeepSeek, пожалуйста")
    assert not _keyword_delegate("Сколько осталось квоты DeepSeek?")


@pytest.mark.asyncio
async def test_small_max_tokens_is_raised_for_reasoning_tiers(tiers, monkeypatch):
    calls = _fake_llm(monkeypatch, {"ds/deepseek-flash": ("success", "ok")})
    await external_delegation.delegate("x", max_tokens=1024)
    assert calls[0]["max_tokens"] == 4096
