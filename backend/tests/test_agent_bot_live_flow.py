"""End-to-end coverage for the Telegram binding's approve -> live-bot ->
send path, which prior tests never exercised: test_agent_messenger_governance.py
monkeypatches _verify_bot_token itself, and nothing drives agent_bot.py's
polling Application or channel_replies.py's outbound HTTP calls at all.

Mocking happens at the httpx transport boundary (respx), so the real code
runs end to end: token-shape regex, getMe response parsing, binding
activation, python-telegram-bot's Application/polling lifecycle, and the
outbound sendMessage payload. Only the incoming Telegram update is
short-circuited -- driving python-telegram-bot's polling loop from a test
is racy, so the message handler is invoked directly with a stand-in Update,
the same way python-telegram-bot tests itself.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from backend import agent_bot, agent_messenger_governance, agent_tiers, channel_replies, control_plane, database
from backend.agent import agent_instance
from tests.fakes.fake_telegram import FakeTelegramAPI

FAKE_TOKEN = "123:" + "a" * 35


@pytest.fixture()
def messenger_db_live(tmp_path, monkeypatch):
    db_path = str(tmp_path / "messenger_live.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    monkeypatch.setattr(agent_tiers, "DB_PATH", db_path)
    monkeypatch.setattr(agent_messenger_governance, "DB_PATH", db_path)
    monkeypatch.setattr(channel_replies, "DB_PATH", db_path)
    database.init_db()
    agent_messenger_governance._init_schema()
    channel_replies._init_schema()

    # _verify_bot_token is deliberately left un-mocked here: it runs for
    # real against the fake respx-mocked Telegram API in each test.

    stored_secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: stored_secrets.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: stored_secrets.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: stored_secrets.pop(key, None))

    database.save_subagent(id="agent-1", name="Agent One", system_prompt="", model="llama3", skills="git_dev")
    return db_path


def _fake_update(text: str, chat_id: int = 555, username: str = "alice"):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = 777
    update.effective_user.username = username
    return update


def _fake_context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    return context


async def _activate_binding(proposal: dict) -> dict:
    control_plane.approve_task(proposal["control_task_id"])
    return await agent_messenger_governance.execute_approved_telegram_binding(proposal["control_task_id"])


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_telegram_binding_draft_mode_end_to_end(messenger_db_live, monkeypatch, respx_mock):
    fake = FakeTelegramAPI()
    fake.install(respx_mock, FAKE_TOKEN)

    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", FAKE_TOKEN, response_mode="draft"
    )
    # _verify_bot_token ran for real (not monkeypatched) against the fake API.
    assert fake.get_me_calls == 1

    try:
        result = await _activate_binding(proposal)
        assert result["status"] == "active"
        assert proposal["id"] in agent_bot.manager._apps

        monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Здесь ваш ответ."))

        handler = agent_bot.manager._make_handler(proposal["id"], "agent-1")
        await handler(_fake_update("Привет!"), _fake_context())

        # Draft mode never talks to Telegram directly -- it queues for review.
        assert fake.sent_messages == []
        pending = channel_replies.list_pending_replies("pending")
        assert len(pending) == 1
        assert pending[0]["drafted_reply"] == "Здесь ваш ответ."
        assert pending[0]["incoming_from"] == "@alice"

        sent = await channel_replies.send_pending_reply(pending[0]["id"])
        assert sent["status"] == "sent"
        assert len(fake.sent_messages) == 1
        assert fake.sent_messages[0]["chat_id"] == "555"
        assert fake.sent_messages[0]["text"] == "Здесь ваш ответ."
    finally:
        await agent_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_telegram_binding_auto_labeled_mode_sends_immediately(messenger_db_live, monkeypatch, respx_mock):
    fake = FakeTelegramAPI()
    fake.install(respx_mock, FAKE_TOKEN)

    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", FAKE_TOKEN, response_mode="auto_labeled"
    )

    try:
        await _activate_binding(proposal)

        monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Готово."))

        handler = agent_bot.manager._make_handler(proposal["id"], "agent-1")
        update = _fake_update("Статус?")
        await handler(update, _fake_context())

        # Auto-labeled mode replies immediately and never queues a draft.
        assert update.message.reply_text.await_count == 1
        (sent_text,), _ = update.message.reply_text.await_args
        assert "Готово." in sent_text
        assert channel_replies.list_pending_replies("pending") == []
    finally:
        await agent_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
def test_verify_bot_token_rejects_telegram_error_response(messenger_db_live, respx_mock):
    respx_mock.get(f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe").mock(
        return_value=Response(200, json={"ok": False, "description": "Unauthorized"})
    )

    with pytest.raises(ValueError, match="Telegram rejected"):
        agent_messenger_governance._verify_bot_token(FAKE_TOKEN)


def test_verify_bot_token_rejects_malformed_shape(messenger_db_live):
    # No respx mock installed at all -- if the code tried to reach the
    # network here, the real (unmocked) httpx call would go out. It
    # shouldn't get that far: the regex check rejects the shape first.
    with pytest.raises(ValueError, match="doesn't look like a BotFather token"):
        agent_messenger_governance._verify_bot_token("not-a-real-token")
