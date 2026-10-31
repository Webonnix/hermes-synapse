"""Telegram approval cards and their buttons."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend import approval_notify, control_plane, database


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cards.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    monkeypatch.setattr(approval_notify, "_sent", approval_notify.deque())
    monkeypatch.setattr(approval_notify, "_held", 0)
    monkeypatch.setattr(approval_notify, "_summary_scheduled", False)


def test_card_names_the_action_not_its_raw_arguments():
    text, rows = approval_notify.card({"id": "T-0123456789ab", "risk_class": "R2", "tool_name": "git_push",
                                       "tool_arguments": {"repo": "hermes", "message": "secret plan"},
                                       "approval_count": 0, "approvals_required": 1})
    assert "git_push" in text and "repo: hermes" in text and "secret plan" not in text
    assert [data for _, data in rows[0]] == ["cp:approve:T-0123456789ab", "cp:reject:T-0123456789ab"]


def test_r4_card_has_no_approve_button():
    _, rows = approval_notify.card({"id": "T-0123456789ab", "risk_class": "R4", "tool_name": "execute_command",
                                    "tool_arguments": {}, "approval_count": 0, "approvals_required": 2})
    assert [data for _, data in rows[0]] == ["cp:reject:T-0123456789ab"]


@pytest.mark.asyncio
async def test_new_pending_task_sends_one_card_and_a_flood_becomes_a_summary(control_db, monkeypatch):
    sent = []
    async def fake_send(text, rows=None):
        sent.append((text, rows))
    monkeypatch.setattr(approval_notify, "_send", fake_send)
    async def no_wait():
        await fake_send(f"⏳ Ещё {approval_notify._held} действ.")
    monkeypatch.setattr(approval_notify, "_send_summary_later", no_wait)
    approval_notify.bind(asyncio.get_running_loop())
    for i in range(approval_notify.CARDS_PER_MINUTE + 3):
        control_plane.create_tool_task("git_push", {"repo": "hermes", "n": i}, "chat")
    await asyncio.sleep(0.05)
    cards = [item for item in sent if item[1]]
    assert len(cards) == approval_notify.CARDS_PER_MINUTE
    assert any("Ещё" in text for text, rows in sent if not rows)


@pytest.mark.asyncio
async def test_tasks_that_need_no_approval_send_nothing(control_db, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(approval_notify, "_send", send)
    approval_notify.bind(asyncio.get_running_loop())
    control_plane.create_tool_task("get_weather", {"city": "x"}, "chat")
    await asyncio.sleep(0.05)
    send.assert_not_called()


def _press(data, user_id="42"):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            edit_message_reply_markup=AsyncMock(), message=SimpleNamespace(text="card"))
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=int(user_id), username="o"),
                             effective_chat=SimpleNamespace(id=42), message=None)
    return update, query


@pytest.mark.asyncio
async def test_buttons_approve_and_reject_for_the_owner_only(control_db, monkeypatch):
    import backend.bot as bot
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(approval_notify, "_loop", None)
    approve = control_plane.create_tool_task("git_push", {"repo": "a"}, "chat")
    reject = control_plane.create_tool_task("git_push", {"repo": "b"}, "chat")
    with patch("backend.approval_dispatch.execute_if_ready", AsyncMock(return_value=None)):
        update, query = _press(f"cp:approve:{approve['id']}", user_id="7")
        await bot.approval_button(update, None)
        assert control_plane.get_task(approve["id"])["status"] == "awaiting_approval"  # stranger ignored
        update, query = _press(f"cp:approve:{approve['id']}")
        await bot.approval_button(update, None)
        assert control_plane.get_task(approve["id"])["status"] == "approved"
        update, query = _press(f"cp:reject:{reject['id']}")
        await bot.approval_button(update, None)
        assert control_plane.get_task(reject["id"])["status"] == "rejected"
        update, query = _press(f"cp:reject:{reject['id']}")
        await bot.approval_button(update, None)
        query.answer.assert_awaited_with("Уже решено")


@pytest.mark.asyncio
async def test_r4_button_cannot_approve(control_db, monkeypatch):
    import backend.bot as bot
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(approval_notify, "_loop", None)
    task = control_plane.create_tool_task("execute_command", {"command": "ls"}, "chat")
    update, query = _press(f"cp:approve:{task['id']}")
    await bot.approval_button(update, None)
    assert control_plane.get_task(task["id"])["approval_count"] == 0
