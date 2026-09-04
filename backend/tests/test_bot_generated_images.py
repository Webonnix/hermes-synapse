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


@pytest.mark.asyncio
async def test_mistyped_filename_in_text_still_delivers_the_real_photo(tmp_path, monkeypatch):
    """Reproduces the exact follow-up bug seen live in prod: the model's own
    final text echoed back a truncated/garbled UUID for the second image in a
    conversation ("853567155c424892837b1588253c13.png", 30 hex chars instead
    of the real 32), so extract_local_images alone couldn't resolve it and
    the photo silently fell back to raw markdown text. last_generated_images
    -- populated from the generate_image tool's own result during the turn,
    not re-parsed out of the model's text -- must still get the real photo
    delivered and the (unresolvable, now-useless) link stripped anyway."""
    images_dir = tmp_path / "generated_images"
    images_dir.mkdir()
    real_filename = "853567155c424892837b1588253c13aa.png"  # the actual, correctly-generated file
    typo_filename = "853567155c424892837b1588253c13.png"  # what the model echoed back in its text
    (images_dir / real_filename).write_bytes(b"fake-png-bytes")

    from backend import agent_bot
    monkeypatch.setattr(agent_bot, "_GENERATED_IMAGES_DIR", str(images_dir))

    chat_id = 555
    response_text = (
        "Готово! Вот солдат с РПГ в руках:\n\n"
        f"![Солдат с РПГ](/api/generated-images/{typo_filename})\n\n"
        "Фотореалистичный солдат в тактической экипировке держит РПГ в руках."
    )
    monkeypatch.setattr(agent_instance, "respond", AsyncMock(return_value=response_text))
    monkeypatch.setattr(agent_instance, "last_saved_ids", {str(chat_id): {}})
    monkeypatch.setattr(agent_instance, "last_run_metadata", {str(chat_id): {"is_complex": False}})
    monkeypatch.setattr(agent_instance, "last_costs", {str(chat_id): 0.0})
    monkeypatch.setattr(
        agent_instance, "last_generated_images",
        {str(chat_id): [f"/api/generated-images/{real_filename}"]},
    )
    monkeypatch.setattr(agent_instance, "telegram_reply_mode", "text")

    update = _fake_update(chat_id)
    with patch("backend.websocket_manager.manager.broadcast", new_callable=AsyncMock):
        await bot._process_user_text(update, _fake_context(), "С РПГ в руках")

    update.message.reply_photo.assert_awaited_once()
    _, photo_kwargs = update.message.reply_photo.await_args
    assert photo_kwargs["photo"].name.endswith(real_filename)

    update.message.reply_text.assert_awaited_once()
    (sent_text,), _ = update.message.reply_text.await_args
    assert "/api/generated-images/" not in sent_text
    assert "Готово! Вот солдат с РПГ в руках:" in sent_text
