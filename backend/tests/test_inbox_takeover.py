"""Takeover, a person's reply and the operator whisper — the dashboard's half of live
intervention, driven through the same gate and turn code the channels run."""

from unittest.mock import AsyncMock

import pytest

from backend import bot_access_gate, channel_replies, conversation_state, database, inbox, inbox_routing, social_turn
from backend.agent import agent_instance
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture


def _open(inbox_env, text="Здравствуйте"):
    return bot_access_gate.authorize("bind-1", "telegram", "555", text, "555", "@ivan", event_id="m1")


@pytest.mark.asyncio
async def test_after_takeover_the_agent_stays_silent_but_the_message_is_kept(inbox_env, monkeypatch):
    decision = _open(inbox_env)
    inbox.takeover(decision.conversation_id)
    responder = AsyncMock(return_value="не должно уйти")
    monkeypatch.setattr(agent_instance, "_respond_as_subagent", responder)
    send = AsyncMock()

    await social_turn.handle_incoming(
        binding_id="bind-1", platform="telegram", subagent_id="agent-1", chat_id="555",
        text="Вы тут?", send=send, external_user_id="555", display_name="@ivan", event_id="m2",
    )

    responder.assert_not_awaited()
    send.assert_not_awaited()
    lines = inbox.list_messages(decision.conversation_id)["items"]
    assert lines[-1]["text"] == "Вы тут?"
    assert inbox.get_conversation(decision.conversation_id)["handling"] == "human"
    assert len(inbox_env.notifications) == 1


def test_takeover_is_the_same_switch_the_smm_dialogs_panel_flips(inbox_env):
    decision = _open(inbox_env)
    inbox.takeover(decision.conversation_id)
    assert conversation_state.get_state("bind-1", "telegram", "555")["handling_mode"] == "owner_takeover"

    conversation_state.set_handling_mode("bind-1", "telegram", "555", "ai")
    assert inbox.get_conversation(decision.conversation_id)["handling"] == "ai"


@pytest.mark.asyncio
async def test_a_person_can_only_reply_after_taking_the_conversation_over(inbox_env, monkeypatch):
    decision = _open(inbox_env)
    sender = AsyncMock()
    monkeypatch.setattr(channel_replies, "send_to_channel", sender)

    with pytest.raises(PermissionError):
        await inbox.send_human_reply(decision.conversation_id, "Добрый день, это менеджер")
    sender.assert_not_awaited()

    inbox.takeover(decision.conversation_id)
    await inbox.send_human_reply(decision.conversation_id, "Добрый день, это менеджер")

    sender.assert_awaited_once_with("bind-1", "telegram", "555", "Добрый день, это менеджер")
    last = inbox.list_messages(decision.conversation_id)["items"][-1]
    assert (last["speaker"], last["delivery"]) == ("human", "sent")
    # The agent must know what the business already told this person when it resumes.
    assert database.get_chat_history(decision.session_id)[-1] == {
        **database.get_chat_history(decision.session_id)[-1], "role": "assistant", "content": "Добрый день, это менеджер",
    }


@pytest.mark.asyncio
async def test_a_reply_the_channel_refused_is_archived_as_failed(inbox_env, monkeypatch):
    decision = _open(inbox_env)
    inbox.takeover(decision.conversation_id)
    monkeypatch.setattr(channel_replies, "send_to_channel", AsyncMock(side_effect=RuntimeError("blocked by user")))

    with pytest.raises(RuntimeError):
        await inbox.send_human_reply(decision.conversation_id, "Ответ")
    last = inbox.list_messages(decision.conversation_id)["items"][-1]
    assert (last["speaker"], last["delivery"]) == ("human", "failed")


@pytest.mark.asyncio
async def test_a_discord_dm_matched_by_author_is_answered_in_the_dm_channel(inbox_env, monkeypatch):
    inbox_env.bind(platform="discord")
    decision = bot_access_gate.authorize(
        "bind-1", "discord", "user-77", "hi", "user-77", "user#7", event_id="d1",
        channel_meta={"reply_chat_id": "dm-channel-9"},
    )
    inbox.takeover(decision.conversation_id)
    sender = AsyncMock()
    monkeypatch.setattr(channel_replies, "send_to_channel", sender)

    await inbox.send_human_reply(decision.conversation_id, "Hello")
    sender.assert_awaited_once_with("bind-1", "discord", "dm-channel-9", "Hello")


def test_resume_hands_the_next_message_back_to_the_agent(inbox_env):
    decision = _open(inbox_env)
    inbox.takeover(decision.conversation_id)
    assert bot_access_gate.authorize("bind-1", "telegram", "555", "раз").action == bot_access_gate.IGNORE

    inbox.resume(decision.conversation_id)

    assert bot_access_gate.authorize("bind-1", "telegram", "555", "два").action == bot_access_gate.PROCEED
    notes = [m["text"] for m in inbox.list_messages(decision.conversation_id)["items"] if m["speaker"] == "system"]
    assert any("забрал" in n for n in notes) and any("возвращён" in n for n in notes)


def test_a_whisper_is_delivered_to_the_next_turn_once_and_only_as_a_system_block(inbox_env):
    decision = _open(inbox_env)
    inbox.add_whisper(decision.conversation_id, "предложи бесплатную доставку")

    block = inbox.consume_whisper_block(decision.session_id)
    assert "предложи бесплатную доставку" in block and "никогда не цитируй" in block
    assert inbox.consume_whisper_block(decision.session_id) == ""
    assert database.get_chat_history(decision.session_id) == []


@pytest.mark.parametrize("text", ["", "x" * (inbox.WHISPER_MAX_CHARS + 1)])
def test_empty_or_oversized_whispers_are_refused(inbox_env, text):
    decision = _open(inbox_env)
    with pytest.raises(ValueError):
        inbox.add_whisper(decision.conversation_id, text)


@pytest.mark.asyncio
async def test_a_reply_that_echoes_the_whisper_verbatim_does_not_deliver_it(inbox_env, monkeypatch):
    decision = _open(inbox_env)
    whisper = "у этого клиента долг 5000 рублей, будь вежлив"
    inbox.add_whisper(decision.conversation_id, whisper)
    inbox.consume_whisper_block(decision.session_id)
    monkeypatch.setattr(
        agent_instance, "_respond_as_subagent", AsyncMock(return_value=f"Здравствуйте! {whisper}"),
    )

    reply = await inbox_routing.respond_turn(decision, "Привет", {"id": "agent-1"}, decision.session_id)

    assert whisper not in reply
    assert reply == "Здравствуйте!"
