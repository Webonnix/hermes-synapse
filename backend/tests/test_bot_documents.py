"""Coverage for the main Vexa Telegram bot's document handling: before this,
bot.py had no filters.Document handler registered at all (voice_handler read
update.message.document but was only ever wired to filters.VOICE | AUDIO, so
that branch was dead) -- a plain document upload was silently ignored."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import bot

ADMIN_ID = "216199859"


def _fake_document_update(file_name: str, content: bytes, mime_type: str = "text/plain", caption: str = ""):
    update = MagicMock()
    update.effective_user.id = int(ADMIN_ID)
    update.effective_chat.id = 555
    update.message.caption = caption
    update.message.reply_text = AsyncMock()
    doc = MagicMock()
    doc.file_name = file_name
    doc.mime_type = mime_type
    doc.file_size = len(content)
    doc.get_file = AsyncMock(return_value=MagicMock(
        download_as_bytearray=AsyncMock(return_value=bytearray(content))
    ))
    update.message.document = doc
    return update


def _fake_context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_document_handler_extracts_text_and_runs_agent_turn():
    update = _fake_document_update("notes.txt", "Важная информация для агента.".encode("utf-8"))

    with patch.dict(os.environ, {"TELEGRAM_ADMIN_ID": ADMIN_ID, "TELEGRAM_CHAT_ID": ""}, clear=False), \
         patch("backend.bot._run_user_request", new_callable=AsyncMock) as mock_run:
        await bot.document_handler(update, _fake_context())

    mock_run.assert_awaited_once()
    (_, _, prompt), kwargs = mock_run.await_args
    assert "Важная информация для агента." in prompt
    assert "notes.txt" in prompt
    assert not kwargs.get("images")


@pytest.mark.asyncio
async def test_document_handler_oversized_file_skips_download():
    update = _fake_document_update("huge.bin", b"x" * 10, mime_type="application/octet-stream")
    update.message.document.file_size = 999_999_999  # bigger than MAX_DOWNLOAD_BYTES

    with patch.dict(os.environ, {"TELEGRAM_ADMIN_ID": ADMIN_ID, "TELEGRAM_CHAT_ID": ""}, clear=False), \
         patch("backend.bot._run_user_request", new_callable=AsyncMock) as mock_run:
        await bot.document_handler(update, _fake_context())

    update.message.document.get_file.assert_not_awaited()
    mock_run.assert_awaited_once()
    (_, _, prompt), _ = mock_run.await_args
    assert "huge.bin" in prompt
    assert "слишком" in prompt


@pytest.mark.asyncio
async def test_document_handler_unauthorized_user_is_ignored():
    update = _fake_document_update("notes.txt", b"secret")
    update.effective_user.id = 999999999  # not in TELEGRAM_ADMIN_ID

    with patch.dict(os.environ, {"TELEGRAM_ADMIN_ID": ADMIN_ID, "TELEGRAM_CHAT_ID": ""}, clear=False), \
         patch("backend.bot._run_user_request", new_callable=AsyncMock) as mock_run:
        await bot.document_handler(update, _fake_context())

    mock_run.assert_not_awaited()
    update.message.reply_text.assert_awaited_once_with("Доступ запрещён.")
