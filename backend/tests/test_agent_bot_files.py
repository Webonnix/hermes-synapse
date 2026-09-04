"""Coverage for agent_bot.py's file handling: receiving a photo (fed to the
model as vision input) or a document (text extracted and injected as
context), and sending a generated image back -- both from the immediate
auto_labeled path and from a draft approved later via channel_replies.py.

Same respx-at-the-transport-boundary approach as test_agent_bot_live_flow.py:
only the incoming Telegram update is faked (driving python-telegram-bot's
real polling loop from a test is racy), everything downstream runs for real.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx

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

    stored_secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: stored_secrets.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: stored_secrets.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: stored_secrets.pop(key, None))

    database.save_subagent(id="agent-1", name="Agent One", system_prompt="", model="llama3", skills="git_dev")
    return db_path


def _fake_context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    return context


async def _activate_binding(proposal: dict) -> dict:
    control_plane.approve_task(proposal["control_task_id"])
    return await agent_messenger_governance.execute_approved_telegram_binding(proposal["control_task_id"])


def _fake_photo_update(chat_id: int = 555, username: str = "alice", caption: str = ""):
    update = MagicMock()
    update.message.text = None
    update.message.caption = caption
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    photo_size = MagicMock()
    photo_size.get_file = AsyncMock(return_value=MagicMock(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\xff\xd8\xff\xfake-jpeg-bytes"))
    ))
    update.message.photo = [photo_size]
    update.message.document = None
    update.effective_chat.id = chat_id
    update.effective_user.id = 777
    update.effective_user.username = username
    return update


def _fake_document_update(file_name: str, content: bytes, chat_id: int = 555, username: str = "alice", caption: str = ""):
    update = MagicMock()
    update.message.text = None
    update.message.caption = caption
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    doc = MagicMock()
    doc.file_name = file_name
    doc.mime_type = "text/plain"
    doc.file_size = len(content)
    doc.get_file = AsyncMock(return_value=MagicMock(
        download_as_bytearray=AsyncMock(return_value=bytearray(content))
    ))
    update.message.document = doc
    update.message.photo = None
    update.effective_chat.id = chat_id
    update.effective_user.id = 777
    update.effective_user.username = username
    return update


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_photo_is_forwarded_as_vision_input(messenger_db_live, monkeypatch, respx_mock):
    fake = FakeTelegramAPI()
    fake.install(respx_mock, FAKE_TOKEN)

    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", FAKE_TOKEN, response_mode="auto_labeled"
    )
    try:
        await _activate_binding(proposal)

        respond_mock = AsyncMock(return_value="На фото кот.")
        monkeypatch.setattr(agent_instance, "_respond_as_subagent", respond_mock)

        handler = agent_bot.manager._make_photo_handler(proposal["id"], "agent-1")
        update = _fake_photo_update(caption="Что на фото?")
        await handler(update, _fake_context())

        respond_mock.assert_awaited_once()
        args, kwargs = respond_mock.await_args
        assert args[0] == "Что на фото?"
        assert kwargs["images"] and len(kwargs["images"]) == 1
        assert update.message.reply_text.await_count == 1
    finally:
        await agent_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_text_document_content_is_injected_into_prompt(messenger_db_live, monkeypatch, respx_mock):
    fake = FakeTelegramAPI()
    fake.install(respx_mock, FAKE_TOKEN)

    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", FAKE_TOKEN, response_mode="auto_labeled"
    )
    try:
        await _activate_binding(proposal)

        respond_mock = AsyncMock(return_value="Файл прочитан.")
        monkeypatch.setattr(agent_instance, "_respond_as_subagent", respond_mock)

        handler = agent_bot.manager._make_document_handler(proposal["id"], "agent-1")
        update = _fake_document_update("notes.txt", b"Vazhnaya informacia dlya agenta.")
        await handler(update, _fake_context())

        respond_mock.assert_awaited_once()
        args, kwargs = respond_mock.await_args
        assert "Vazhnaya informacia dlya agenta." in args[0]
        assert "notes.txt" in args[0]
        assert kwargs.get("images") is None
    finally:
        await agent_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_auto_labeled_reply_with_generated_image_sends_photo(messenger_db_live, monkeypatch, respx_mock, tmp_path):
    fake = FakeTelegramAPI()
    fake.install(respx_mock, FAKE_TOKEN)

    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", FAKE_TOKEN, response_mode="auto_labeled"
    )

    images_dir = tmp_path / "generated_images"
    images_dir.mkdir()
    image_filename = f"test_{uuid.uuid4().hex}.png"
    image_path = images_dir / image_filename
    image_path.write_bytes(b"fake-png-bytes")
    monkeypatch.setattr(agent_bot, "_GENERATED_IMAGES_DIR", str(images_dir))

    try:
        await _activate_binding(proposal)

        response_text = f"Вот картинка: ![result](/api/generated-images/{image_filename})\n\nГотово."
        monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value=response_text))

        handler = agent_bot.manager._make_handler(proposal["id"], "agent-1")
        update = MagicMock()
        update.message.text = "Нарисуй кота"
        update.message.reply_text = AsyncMock()
        update.message.reply_photo = AsyncMock()
        update.effective_chat.id = 555
        update.effective_user.id = 777
        update.effective_user.username = "alice"
        await handler(update, _fake_context())

        update.message.reply_photo.assert_awaited_once()
        assert update.message.reply_text.await_count == 1
        (sent_text,), _ = update.message.reply_text.await_args
        assert "/api/generated-images/" not in sent_text
        assert "Готово." in sent_text
    finally:
        await agent_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_draft_reply_with_generated_image_sends_photo_on_approval(messenger_db_live, monkeypatch, respx_mock, tmp_path):
    fake = FakeTelegramAPI()
    fake.install(respx_mock, FAKE_TOKEN)

    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", FAKE_TOKEN, response_mode="draft"
    )

    images_dir = tmp_path / "generated_images"
    images_dir.mkdir()
    image_filename = f"test_{uuid.uuid4().hex}.png"
    image_path = images_dir / image_filename
    image_path.write_bytes(b"fake-png-bytes")
    monkeypatch.setattr(agent_bot, "_GENERATED_IMAGES_DIR", str(images_dir))

    try:
        await _activate_binding(proposal)

        response_text = f"![chart](/api/generated-images/{image_filename})"
        monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value=response_text))

        handler = agent_bot.manager._make_handler(proposal["id"], "agent-1")
        update = MagicMock()
        update.message.text = "Покажи график"
        update.message.reply_text = AsyncMock()
        update.effective_chat.id = 555
        update.effective_user.id = 777
        update.effective_user.username = "alice"
        await handler(update, _fake_context())

        pending = channel_replies.list_pending_replies("pending")
        assert len(pending) == 1

        await channel_replies.send_pending_reply(pending[0]["id"])

        assert len(fake.sent_photos) == 1
        assert fake.sent_photos[0]["chat_id"] == "555"
        assert fake.sent_photos[0]["filename"] == image_filename
        # The image markdown link was fully consumed by the photo -- nothing
        # left to send as a text message afterwards.
        assert fake.sent_messages == []
    finally:
        await agent_bot.manager.stop_all()
        if os.path.exists(image_path):
            os.remove(image_path)
