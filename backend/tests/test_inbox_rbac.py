"""RBAC on the inbox and voice API — the backend refuses, whatever the UI shows.

Routes are called directly as async functions (same style as test_clients_rbac.py).
"""

import pytest
from fastapi import HTTPException

from backend import auth, bot_access_gate, inbox, inbox_api, inbox_rules
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture


class _Request:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def sessions(inbox_env, monkeypatch):
    monkeypatch.setattr(auth, "active_sessions", set())
    monkeypatch.setattr(auth, "session_roles", {})
    return {role: _Request(auth.create_session(role)) for role in ("owner", "manager", "operator", "viewer", "nobody")}


@pytest.fixture()
def conversation(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Вопрос")
    bot_access_gate.record_turn(decision, {"model": "gpt-4o", "input_tokens": 1000, "output_tokens": 100}, "Ответ")
    return decision.conversation_id


async def _denied(coro):
    with pytest.raises(HTTPException) as exc:
        await coro
    return exc.value.status_code


@pytest.mark.asyncio
async def test_a_role_without_inbox_view_sees_nothing(sessions, conversation):
    assert await _denied(inbox_api.list_inbox_conversations_api(sessions["nobody"])) == 403
    assert await _denied(inbox_api.get_inbox_conversation_api(conversation, sessions["nobody"])) == 403
    assert await _denied(inbox_api.list_inbox_messages_api(conversation, sessions["nobody"])) == 403


@pytest.mark.asyncio
async def test_a_viewer_reads_but_cannot_act_in_a_conversation(sessions, conversation):
    listed = await inbox_api.list_inbox_conversations_api(sessions["viewer"])
    assert [c["id"] for c in listed["items"]] == [conversation]
    assert await _denied(inbox_api.takeover_inbox_conversation_api(conversation, sessions["viewer"])) == 403
    assert await _denied(inbox_api.whisper_inbox_conversation_api(
        conversation, inbox_api.TextRequest(text="x"), sessions["viewer"])) == 403
    assert await _denied(inbox_api.pin_inbox_route_api(
        conversation, inbox_api.RouteRequest(target_type="subagent", target_id="agent-2"), sessions["viewer"])) == 403


@pytest.mark.asyncio
async def test_an_operator_works_conversations_but_does_not_decide_who_is_blocked(sessions, conversation):
    taken = await inbox_api.takeover_inbox_conversation_api(conversation, sessions["operator"])
    assert taken["handling"] == "human"
    assert await _denied(inbox_api.create_inbox_rule_api(
        inbox_api.RuleRequest(pattern="555", action="block"), sessions["operator"])) == 403
    assert await _denied(inbox_api.block_inbox_sender_api(conversation, inbox_api.BlockRequest(), sessions["operator"])) == 403
    assert await _denied(inbox_api.set_inbox_binding_settings_api(
        "bind-1", inbox_api.BindingSettingsRequest(business_hours="mo-fr 09:00-18:00"), sessions["operator"])) == 403


@pytest.mark.asyncio
async def test_money_is_stripped_for_roles_without_financial_access(sessions, conversation):
    operator_view = await inbox_api.get_inbox_conversation_api(conversation, sessions["operator"])
    manager_view = await inbox_api.get_inbox_conversation_api(conversation, sessions["manager"])
    assert "cost_usd" not in operator_view
    assert manager_view["cost_usd"] > 0


@pytest.mark.asyncio
async def test_a_manager_edits_rules_but_voice_providers_stay_with_the_owner(sessions, conversation):
    rule = await inbox_api.create_inbox_rule_api(inbox_api.RuleRequest(pattern="555", action="block"), sessions["manager"])
    assert inbox_rules.get_rule(rule["id"])
    payload = inbox_api.VoiceProviderRequest(name="EL", kind="elevenlabs", base_url="https://api.elevenlabs.io", api_key="k")
    assert await _denied(inbox_api.create_voice_provider_api(payload, sessions["manager"])) == 403
    created = await inbox_api.create_voice_provider_api(payload, sessions["owner"])
    assert created["has_secret"] is True and "api_key" not in created


@pytest.mark.asyncio
async def test_creating_a_client_from_a_conversation_needs_client_create(sessions, conversation):
    create = inbox_api.LinkClientRequest(create={"name": "Иван"})
    assert await _denied(inbox_api.link_inbox_client_api(conversation, create, sessions["operator"])) == 403
    linked = await inbox_api.link_inbox_client_api(conversation, create, sessions["manager"])
    assert linked["client_id"]


@pytest.mark.asyncio
async def test_unknown_ids_are_404_and_bad_input_is_400(sessions):
    assert await _denied(inbox_api.get_inbox_conversation_api("ibx-nope", sessions["owner"])) == 404
    assert await _denied(inbox_api.takeover_inbox_conversation_api("ibx-nope", sessions["owner"])) == 404
    assert await _denied(inbox_api.list_inbox_conversations_api(sessions["owner"], handling="robot")) == 400
    assert await _denied(inbox_api.create_inbox_rule_api(
        inbox_api.RuleRequest(pattern="", action="block"), sessions["owner"])) == 400


@pytest.mark.asyncio
async def test_a_human_reply_outside_takeover_is_a_conflict(sessions, conversation):
    status = await _denied(inbox_api.reply_inbox_conversation_api(
        conversation, inbox_api.TextRequest(text="Здравствуйте"), sessions["owner"]))
    assert status == 409
    assert inbox.get_conversation(conversation)["handling"] == "ai"
