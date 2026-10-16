"""Conversation routing (backend/inbox_routing.py): who executes a turn, in which order
the decisions win, and how every failure ends in a policy outcome instead of an error."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import Response

from backend import bot_access_gate, database, inbox, inbox_routing, inbox_rules, social_turn, voice_providers
from backend.agent import agent_instance
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture
from backend.websocket_manager import ConnectionManager

AGENT_BASE = "https://voice-agent.example.com"


def _open(chat_id="555", text="Здравствуйте", event_id=""):
    return bot_access_gate.authorize("bind-1", "telegram", chat_id, text, chat_id, "@ivan", event_id=event_id)


def _voice_target(mode="voice_agent", policy="fallback_internal", **provider_config):
    kind = "voice_agent_http" if mode == "voice_agent" else "openai_compatible"
    base = AGENT_BASE if mode == "voice_agent" else "https://tts.example.com/v1"
    provider = voice_providers.create_provider({
        "name": "Remote", "kind": kind, "base_url": base, "api_key": "sk-secret-value",
        "config": provider_config, "max_retries": 0, "timeout_seconds": 5,
    })
    target = voice_providers.create_target({
        "provider_id": provider["id"], "name": "Голос", "capability_mode": mode,
        "voice_id": "alloy" if mode == "tts" else "", "failure_policy": policy,
    })
    return provider, target


def test_without_a_pin_the_bindings_own_agent_answers(inbox_env):
    decision = _open()
    assert decision.route["source"] == "auto"
    assert decision.route["executor_subagent_id"] == "agent-1"
    assert inbox_routing.effective_subagent_id(decision, "agent-1") == "agent-1"


def test_a_manual_pin_wins_over_auto_until_it_is_removed(inbox_env):
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "subagent", "agent-2", "owner")

    pinned = _open(text="второе")
    assert (pinned.route["source"], pinned.route["executor_subagent_id"]) == ("manual", "agent-2")

    inbox_routing.unpin_route(decision.conversation_id)
    assert _open(text="третье").route["source"] == "auto"


def test_a_pin_to_something_that_does_not_exist_is_refused(inbox_env):
    decision = _open()
    with pytest.raises(ValueError):
        inbox_routing.pin_route(decision.conversation_id, "subagent", "ghost-agent")
    with pytest.raises(ValueError):
        inbox_routing.pin_route(decision.conversation_id, "subagent", "vexa")
    with pytest.raises(ValueError):
        inbox_routing.pin_route(decision.conversation_id, "webhook", "agent-2")


def test_a_pinned_agent_that_was_disabled_falls_back_with_a_recorded_reason(inbox_env):
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "subagent", "agent-2")
    agent = database.get_subagent("agent-2")
    database.save_subagent(id="agent-2", name=agent["name"], system_prompt="", model="llama3", is_enabled=False)

    fallback = _open(text="ещё")

    assert (fallback.route["source"], fallback.route["executor_subagent_id"]) == ("fallback", "agent-1")
    assert "отключён" in inbox_routing.get_route(decision.conversation_id)["last_reason"]


@pytest.mark.parametrize("silence", ["takeover", "block"])
def test_takeover_and_block_never_reach_the_router(inbox_env, monkeypatch, silence):
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "subagent", "agent-2")
    if silence == "takeover":
        inbox.takeover(decision.conversation_id)
    else:
        inbox_rules.create_rule("555", "block")

    def must_not_run(*args, **kwargs):
        raise AssertionError("router consulted for a silenced conversation")

    monkeypatch.setattr(inbox_routing, "resolve", must_not_run)
    assert _open(text="алло").action == bot_access_gate.IGNORE


def test_a_pin_set_during_takeover_applies_from_the_first_turn_after_resume(inbox_env):
    decision = _open()
    inbox.takeover(decision.conversation_id)
    inbox_routing.pin_route(decision.conversation_id, "subagent", "agent-2")
    assert _open(text="жду").action == bot_access_gate.IGNORE

    inbox.resume(decision.conversation_id)

    after = _open(text="вернулись")
    assert (after.action, after.route["executor_subagent_id"]) == (bot_access_gate.PROCEED, "agent-2")


@pytest.mark.asyncio
async def test_a_redelivered_event_runs_the_executor_exactly_once(inbox_env, monkeypatch):
    responder = AsyncMock(return_value="Ответ")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", responder)
    send = AsyncMock()
    for _ in range(2):
        await social_turn.handle_incoming(
            binding_id="bind-1", platform="telegram", subagent_id="agent-1", chat_id="555",
            text="Привет", send=send, external_user_id="555", event_id="dup-1", response_mode="auto_labeled",
        )
    assert responder.await_count == 1
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_the_pinned_agent_is_the_one_that_actually_answers(inbox_env, monkeypatch):
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "subagent", "agent-2")
    responder = AsyncMock(return_value="Ответ отдела продаж")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", responder)

    await social_turn.handle_incoming(
        binding_id="bind-1", platform="telegram", subagent_id="agent-1", chat_id="555",
        text="Хочу купить", send=AsyncMock(), external_user_id="555", event_id="e-2",
    )

    assert responder.await_args.args[1]["id"] == "agent-2"


# ── external voice targets ──────────────────────────────────────────────────

@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_voice_agent_target_answers_and_keeps_the_agents_memory_continuous(inbox_env, monkeypatch, respx_mock):
    _, target = _voice_target()
    route = respx_mock.post(f"{AGENT_BASE}/respond").mock(return_value=Response(200, json={"text": "Голосовой ответ"}))
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    turn = _open(text="Расскажите о тарифах")
    internal = AsyncMock(return_value="не должен отвечать")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", internal)

    reply = await inbox_routing.respond_turn(turn, "Расскажите о тарифах", {"id": "agent-1"}, turn.session_id)

    assert reply == "Голосовой ответ"
    internal.assert_not_awaited()
    assert json.loads(route.calls.last.request.content)["text"] == "Расскажите о тарифах"
    assert [m["content"] for m in database.get_chat_history(turn.session_id)][-2:] == ["Расскажите о тарифах", "Голосовой ответ"]


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_failing_voice_agent_falls_back_to_the_internal_agent_by_default(inbox_env, monkeypatch, respx_mock):
    _, target = _voice_target()
    respx_mock.post(f"{AGENT_BASE}/respond").mock(return_value=Response(503))
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    turn = _open(text="вопрос")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Отвечаю сам"))

    assert await inbox_routing.respond_turn(turn, "вопрос", {"id": "agent-1"}, turn.session_id) == "Отвечаю сам"
    notes = [m["text"] for m in inbox.list_messages(turn.conversation_id)["items"] if m["speaker"] == "system"]
    assert any("не ответил" in n for n in notes)


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_voice_agent_timeout_is_a_controlled_fallback(inbox_env, monkeypatch, respx_mock):
    _, target = _voice_target()
    respx_mock.post(f"{AGENT_BASE}/respond").mock(side_effect=httpx.ReadTimeout("slow"))
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    turn = _open(text="вопрос")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Резерв"))

    assert await inbox_routing.respond_turn(turn, "вопрос", {"id": "agent-1"}, turn.session_id) == "Резерв"


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_human_takeover_policy_hands_the_conversation_to_a_person(inbox_env, monkeypatch, respx_mock):
    _, target = _voice_target(policy="human_takeover")
    respx_mock.post(f"{AGENT_BASE}/respond").mock(return_value=Response(500))
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    turn = _open(text="вопрос")
    internal = AsyncMock(return_value="нет")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", internal)

    reply = await inbox_routing.respond_turn(turn, "вопрос", {"id": "agent-1"}, turn.session_id)

    assert reply == inbox_routing.HANDOFF_TEXT
    internal.assert_not_awaited()
    assert inbox.get_conversation(turn.conversation_id)["handling"] == "human"


@pytest.mark.asyncio
async def test_an_unavailable_fail_closed_target_answers_offline_without_anyone_else(inbox_env, monkeypatch):
    provider, target = _voice_target(policy="fail_closed")
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    voice_providers.update_provider(provider["id"], {"enabled": False})
    turn = _open(text="вопрос")
    internal = AsyncMock(return_value="нет")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", internal)

    assert turn.route["source"] == "fallback" and turn.route["failure_policy"] == "fail_closed"
    reply = await inbox_routing.respond_turn(turn, "вопрос", {"id": "agent-1"}, turn.session_id)

    assert reply == bot_access_gate.AGENT_OFFLINE_MESSAGE
    internal.assert_not_awaited()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_whisper_is_withheld_from_a_provider_without_an_instruction_channel(inbox_env, monkeypatch, respx_mock):
    _, target = _voice_target()
    route = respx_mock.post(f"{AGENT_BASE}/respond").mock(return_value=Response(200, json={"text": "ок"}))
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    inbox.add_whisper(decision.conversation_id, "скажи про акцию недели")
    turn = _open(text="что нового?")

    await inbox_routing.respond_turn(turn, "что нового?", {"id": "agent-1"}, turn.session_id)

    body = json.loads(route.calls.last.request.content)
    assert "instructions" not in body
    assert "акци" not in json.dumps(body, ensure_ascii=False)
    # Still queued for the next turn that can take it safely.
    assert "скажи про акцию недели" in inbox.consume_whisper_block(turn.session_id)


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_provider_with_an_instruction_channel_gets_the_whisper_there_and_only_there(inbox_env, respx_mock):
    _, target = _voice_target(supports_instructions=True)
    route = respx_mock.post(f"{AGENT_BASE}/respond").mock(return_value=Response(200, json={"text": "ок"}))
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    inbox.add_whisper(decision.conversation_id, "скажи про акцию недели")
    turn = _open(text="что нового?")

    await inbox_routing.respond_turn(turn, "что нового?", {"id": "agent-1"}, turn.session_id)

    body = json.loads(route.calls.last.request.content)
    assert "скажи про акцию недели" in body["instructions"]
    assert body["text"] == "что нового?"


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_tts_target_voices_the_agents_text_and_archives_the_audio_by_reference(inbox_env, monkeypatch, respx_mock):
    _, target = _voice_target(mode="tts")
    respx_mock.post("https://tts.example.com/v1/audio/speech").mock(
        return_value=Response(200, content=b"ID3fakeaudio", headers={"content-type": "audio/mpeg"})
    )
    decision = _open()
    inbox_routing.pin_route(decision.conversation_id, "external_voice", target["id"])
    turn = _open(text="озвучь")
    assert turn.route["executor_subagent_id"] == "agent-1"
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Текст ответа"))

    reply = await inbox_routing.respond_turn(turn, "озвучь", {"id": "agent-1"}, turn.session_id)
    bot_access_gate.record_turn(turn, {}, reply)

    last = inbox.list_messages(turn.conversation_id)["items"][-1]
    assert last["text"] == "Текст ответа"
    [attachment] = last["attachments"]
    assert attachment["kind"] == "tts" and attachment["mime"] == "audio/mpeg"
    assert inbox.get_attachment(attachment["id"])["path"].read_bytes() == b"ID3fakeaudio"


# ── live updates ────────────────────────────────────────────────────────────

class _Socket:
    def __init__(self):
        self.frames = []
        self.accept = AsyncMock()

    async def send_text(self, text):
        self.frames.append(json.loads(text))


@pytest.mark.asyncio
async def test_route_changes_reach_only_inbox_subscribers_and_carry_no_content(inbox_env, monkeypatch):
    manager = ConnectionManager()
    subscribed, other = _Socket(), _Socket()
    await manager.connect(subscribed, subprotocol="hermes-v2")
    await manager.connect(other, subprotocol="hermes-v2")
    manager.subscribe(subscribed, ["inbox"])
    manager.subscribe(other, ["dashboard"])
    monkeypatch.setattr("backend.websocket_manager.manager", manager)
    inbox.bind_event_loop(asyncio.get_running_loop())

    decision = await asyncio.to_thread(_open, "555", "секретный текст клиента")
    subscribed.frames.clear()
    await asyncio.to_thread(inbox_routing.pin_route, decision.conversation_id, "subagent", "agent-2")
    await asyncio.sleep(0.05)

    route_frames = [f for f in subscribed.frames if f.get("kind") == "route"]
    assert route_frames and route_frames[0]["conversation_id"] == decision.conversation_id
    assert all("секретный" not in json.dumps(f, ensure_ascii=False) for f in subscribed.frames)
    assert other.frames == []
