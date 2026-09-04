"""Regression test for the main Vexa Telegram bot: a reply that embeds a
/api/generated-images/... markdown link (e.g. from the generate_image tool)
must be resolved to the actual file and sent as a Telegram photo instead of
being shown as a dead-looking raw markdown link in the chat -- the bug a
screenshot from prod caught (bot.py only recognized /api/plots/plot_*.png
links, generate_image's own /api/generated-images/ links were never
converted at all).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import bot
from backend.agent import agent_instance


def _fake_update(chat_id: int = 555):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _fake_context():
    return MagicMock()


@pytest.mark.asyncio
async def test_generated_image_link_is_sent_as_photo(tmp_path, monkeypatch):
    images_dir = tmp_path / "generated_images"
    images_dir.mkdir()
    image_filename = "d9b139a72ecb41b6a30e640c5617a25c.png"
    (images_dir / image_filename).write_bytes(b"fake-png-bytes")

    from backend import agent_bot
    monkeypatch.setattr(agent_bot, "_GENERATED_IMAGES_DIR", str(images_dir))

    chat_id = 555
    response_text = (
        "Готово! Вот фото робота:\n\n"
        f"![Робот](/api/generated-images/{image_filename})\n\n"
        "Фотореалистичный гуманоид в минималистичной студии."
    )
    monkeypatch.setattr(agent_instance, "respond", AsyncMock(return_value=response_text))
    monkeypatch.setattr(agent_instance, "last_saved_ids", {str(chat_id): {}})
    monkeypatch.setattr(agent_instance, "last_run_metadata", {str(chat_id): {"is_complex": False}})
    monkeypatch.setattr(agent_instance, "last_costs", {str(chat_id): 0.0})
    monkeypatch.setattr(agent_instance, "telegram_reply_mode", "text")

    update = _fake_update(chat_id)
    with patch("backend.websocket_manager.manager.broadcast", new_callable=AsyncMock):
        await bot._process_user_text(update, _fake_context(), "Сгенерируй фото робота")

    update.message.reply_photo.assert_awaited_once()
    _, photo_kwargs = update.message.reply_photo.await_args
    assert photo_kwargs["photo"].name.endswith(image_filename)

    update.message.reply_text.assert_awaited_once()
    (sent_text,), _ = update.message.reply_text.await_args
    assert "/api/generated-images/" not in sent_text
    assert "Готово! Вот фото робота:" in sent_text
    assert "Фотореалистичный гуманоид" in sent_text
