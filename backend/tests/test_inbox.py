"""Inbox archive (backend/inbox.py): what arrives, what the agent sends, what the owner
does with a draft — all of it lands in one record, keyed by the same conversation the
access gate already knows about.

Driven through the real entry points (bot_access_gate.authorize / record_turn,
channel_replies) rather than by calling the archive directly, because the point is
that the channels' existing flow writes it without any of them knowing.
"""

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from backend import bot_access_gate, channel_replies, clients, database, inbox
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture


def _messages(conversation_id):
    return inbox.list_messages(conversation_id)["items"]


def test_an_inbound_message_is_archived_on_its_conversation(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Здравствуйте, есть доставка?", "555", "@ivan", event_id="m1")

    assert decision.action == bot_access_gate.PROCEED
    conversation = inbox.get_conversation(decision.conversation_id)
    assert conversation["handling"] == "ai"
    assert conversation["unread_count"] == 1
    assert conversation["display_name"] == "@ivan"
    assert conversation["session_id"] == decision.session_id
    [line] = _messages(decision.conversation_id)
    assert (line["speaker"], line["text"], line["external_event_id"]) == ("customer", "Здравствуйте, есть доставка?", "m1")


def test_a_redelivered_event_is_ignored_and_archived_once(inbox_env):
    first = bot_access_gate.authorize("bind-1", "telegram", "555", "Привет", event_id="m1")
    again = bot_access_gate.authorize("bind-1", "telegram", "555", "Привет", event_id="m1")

    assert first.action == bot_access_gate.PROCEED
    assert again.action == bot_access_gate.IGNORE
    assert len(_messages(first.conversation_id)) == 1


def test_the_unique_index_still_stops_a_redelivery_that_raced_past_the_precheck(inbox_env, monkeypatch):
    first = bot_access_gate.authorize("bind-1", "telegram", "555", "Привет", event_id="m1")
    monkeypatch.setattr(inbox, "is_duplicate_event", lambda *args: False)

    again = bot_access_gate.authorize("bind-1", "telegram", "555", "Привет", event_id="m1")

    assert again.action == bot_access_gate.IGNORE
    assert len(_messages(first.conversation_id)) == 1


def test_a_broken_archive_never_costs_the_customer_their_reply(inbox_env, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(inbox, "_connect", broken)

    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Привет", event_id="m1")
    assert decision.action == bot_access_gate.PROCEED
    bot_access_gate.record_turn(decision, {"model": "llama3"}, "Здравствуйте!")  # must not raise


def test_a_stranger_on_a_whitelisted_binding_is_not_archived(inbox_env):
    inbox_env.bind(allowed_chat_ids=["100"])
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "спам")
    assert decision.action == bot_access_gate.IGNORE
    assert inbox.list_conversations()["items"] == []


def test_a_direct_reply_is_archived_as_sent_with_its_cost_attributed(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Сколько стоит?")
    bot_access_gate.record_turn(decision, {"model": "gpt-4o", "input_tokens": 1000, "output_tokens": 500}, "От 100 $.")

    lines = _messages(decision.conversation_id)
    assert [(l["speaker"], l["delivery"], l["text"]) for l in lines] == [
        ("customer", "sent", "Сколько стоит?"), ("agent", "sent", "От 100 $."),
    ]
    assert inbox.get_conversation(decision.conversation_id)["cost_usd"] > 0


def test_a_failed_turn_is_archived_as_a_failure_not_as_an_empty_reply(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Вопрос")
    bot_access_gate.record_turn(decision, {"error": "timeout"}, "")

    last = _messages(decision.conversation_id)[-1]
    assert (last["speaker"], last["delivery"]) == ("system", "failed")


@pytest.mark.asyncio
async def test_a_draft_follows_its_fate_from_queued_to_sent_with_the_owners_edit(inbox_env, monkeypatch):
    inbox_env.bind(response_mode="draft")
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Когда доставка?")
    bot_access_gate.record_turn(decision, {"model": "llama3"}, "Завтра.")
    # Draft mode: record_turn must not claim anything was sent.
    assert [l["speaker"] for l in _messages(decision.conversation_id)] == ["customer"]

    draft = channel_replies.create_pending_reply(
        binding_id="bind-1", platform="telegram", subagent_id="agent-1", chat_id="555",
        incoming_text="Когда доставка?", drafted_reply="Завтра.",
    )
    queued = _messages(decision.conversation_id)[-1]
    assert (queued["speaker"], queued["delivery"], queued["pending_reply_id"]) == ("agent", "draft", draft["id"])
    # Nothing reached the customer yet, so the list must not read "Агент: Завтра."
    preview = inbox.get_conversation(decision.conversation_id)
    assert (preview["last_speaker"], preview["last_message_preview"]) == ("customer", "Когда доставка?")

    monkeypatch.setattr(channel_replies, "send_to_channel", AsyncMock())
    await channel_replies.send_pending_reply(draft["id"], "Завтра до 18:00.")

    sent = _messages(decision.conversation_id)[-1]
    assert (sent["delivery"], sent["text"], sent["meta"]) == ("sent", "Завтра до 18:00.", {"edited": True})
    assert inbox.get_conversation(decision.conversation_id)["last_message_preview"] == "Завтра до 18:00."


@pytest.mark.asyncio
async def test_a_draft_that_the_channel_refused_is_archived_as_failed_and_stays_pending(inbox_env, monkeypatch):
    inbox_env.bind(response_mode="draft")
    bot_access_gate.authorize("bind-1", "telegram", "555", "Вопрос")
    draft = channel_replies.create_pending_reply(
        binding_id="bind-1", platform="telegram", subagent_id="agent-1", chat_id="555",
        incoming_text="Вопрос", drafted_reply="Ответ",
    )
    monkeypatch.setattr(channel_replies, "send_to_channel", AsyncMock(side_effect=RuntimeError("token revoked")))

    with pytest.raises(RuntimeError):
        await channel_replies.send_pending_reply(draft["id"])

    conversation = inbox.find_conversation("bind-1", "telegram", "555")
    assert _messages(conversation["id"])[-1]["delivery"] == "failed"
    assert channel_replies.get_pending_reply(draft["id"])["status"] == "pending"


def test_a_discarded_draft_is_archived_as_discarded(inbox_env):
    inbox_env.bind(response_mode="draft")
    bot_access_gate.authorize("bind-1", "telegram", "555", "Вопрос")
    draft = channel_replies.create_pending_reply(
        binding_id="bind-1", platform="telegram", subagent_id="agent-1", chat_id="555",
        incoming_text="Вопрос", drafted_reply="Ответ",
    )
    channel_replies.discard_pending_reply(draft["id"])

    conversation = inbox.find_conversation("bind-1", "telegram", "555")
    assert _messages(conversation["id"])[-1]["delivery"] == "discarded"


def test_an_email_draft_lands_on_the_senders_conversation_despite_its_thread_descriptor(inbox_env):
    inbox_env.bind(platform="email", response_mode="draft")
    decision = bot_access_gate.authorize(
        "bind-1", "email", "anna@example.com", "Тема\nТело", "anna@example.com", "anna@example.com",
        event_id="<abc@mail>", channel_meta={"subject": "Тема", "message_id": "<abc@mail>", "references": ""},
    )
    channel_replies.create_pending_reply(
        binding_id="bind-1", platform="email", subagent_id="agent-1",
        chat_id=json.dumps({"to": "anna@example.com", "subject": "Тема", "message_id": "<abc@mail>", "references": ""}),
        incoming_text="Тело", drafted_reply="Добрый день!",
    )

    assert [l["speaker"] for l in _messages(decision.conversation_id)] == ["customer", "agent"]
    thread = json.loads(inbox._reply_chat_id(inbox.get_conversation(decision.conversation_id)))
    assert thread == {"to": "anna@example.com", "subject": "Re: Тема", "message_id": "<abc@mail>", "references": ""}


def test_a_sender_known_to_the_crm_is_linked_to_their_client_automatically(inbox_env):
    client = clients.create_client({"name": "Иван Петров", "type": "PERSON",
                                    "contact": {"name": "Иван", "telegram": "https://t.me/Ivan_P"}})

    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Добрый день", "555", "@ivan_p")

    conversation = inbox.get_conversation(decision.conversation_id)
    assert conversation["client_id"] == client["id"]
    assert conversation["client_name"] == "Иван Петров"
    assert [c["id"] for c in inbox.conversations_for_client(client["id"])] == [decision.conversation_id]


def test_creating_a_client_from_a_conversation_links_that_person_everywhere(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Хочу заказать", "555", "@olga")
    linked = inbox.link_client(decision.conversation_id, create={"name": "Ольга"})

    client = clients.get_client(linked["client_id"], expand=True)
    assert client["name"] == "Ольга"
    assert client["primaryContact"]["telegram"] == "@olga"

    inbox_env.bind(id="bind-2")
    other = bot_access_gate.authorize("bind-2", "telegram", "555", "Это снова я", "555", "@olga")
    assert inbox.get_conversation(other.conversation_id)["client_id"] == linked["client_id"]


def test_search_finds_message_text_but_never_operator_whispers(inbox_env):
    delivery = bot_access_gate.authorize("bind-1", "telegram", "1", "Где мой заказ? Доставка задерживается")
    bot_access_gate.authorize("bind-1", "telegram", "2", "Как оплатить картой?")
    inbox.add_whisper(delivery.conversation_id, "предложи скидку на оплату")

    found = inbox.list_conversations(search="доставк")["items"]
    assert [c["id"] for c in found] == [delivery.conversation_id]
    assert [c["chat_id"] for c in inbox.list_conversations(search="оплат")["items"]] == ["2"]


def test_a_whisper_never_becomes_the_preview_line(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "1", "Вопрос клиента")
    inbox.add_whisper(decision.conversation_id, "секретная подсказка оператора")
    assert inbox.get_conversation(decision.conversation_id)["last_message_preview"] == "Вопрос клиента"


def test_filters_by_handling_status_and_unread(inbox_env):
    taken = bot_access_gate.authorize("bind-1", "telegram", "1", "Первый")
    quiet = bot_access_gate.authorize("bind-1", "telegram", "2", "Второй")
    inbox.takeover(taken.conversation_id)
    inbox.mark_read(quiet.conversation_id)
    inbox.set_status(quiet.conversation_id, "closed")

    assert [c["id"] for c in inbox.list_conversations(handling="human")["items"]] == [taken.conversation_id]
    assert [c["id"] for c in inbox.list_conversations(unread=True)["items"]] == [taken.conversation_id]
    assert [c["id"] for c in inbox.list_conversations(status="closed")["items"]] == [quiet.conversation_id]
    with pytest.raises(ValueError):
        inbox.list_conversations(handling="robot")


def test_a_new_customer_message_reopens_a_closed_conversation(inbox_env):
    decision = bot_access_gate.authorize("bind-1", "telegram", "1", "Спасибо")
    inbox.set_status(decision.conversation_id, "closed")
    bot_access_gate.authorize("bind-1", "telegram", "1", "Ещё вопрос")
    assert inbox.get_conversation(decision.conversation_id)["status"] == "open"


def test_pagination_walks_every_conversation_exactly_once(inbox_env):
    ids = {bot_access_gate.authorize("bind-1", "telegram", str(n), f"сообщение {n}").conversation_id for n in range(7)}
    seen, cursor = [], None
    while True:
        page = inbox.list_conversations(limit=3, cursor=cursor)
        seen.extend(c["id"] for c in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(ids)


def test_clearing_the_agents_memory_leaves_the_archive_alone(inbox_env):
    from backend import conversation_state

    decision = bot_access_gate.authorize("bind-1", "telegram", "1", "Запомни меня")
    bot_access_gate.record_turn(decision, {}, "Запомнил.")
    conversation_state.clear_history("bind-1", "telegram", "1")

    assert database.get_chat_history(decision.session_id) == []
    assert len(_messages(decision.conversation_id)) == 2


def test_attachments_are_stored_on_disk_and_referenced_not_inlined(inbox_env, tmp_path):
    decision = bot_access_gate.authorize("bind-1", "telegram", "1", "Озвучь")
    message_id = inbox.record_agent_reply(decision.conversation_id, "Готово")
    audio = b"ID3" + b"\x00" * 2048

    attachment_id = inbox.store_attachment(message_id, kind="tts", mime="audio/mpeg", data=audio)

    stored = inbox.get_attachment(attachment_id)
    assert stored["path"].read_bytes() == audio
    assert stored["storage_ref"].endswith(".mp3") and len(stored["storage_ref"]) < 64
    with sqlite3.connect(inbox_env.db_path) as connection:
        row = connection.execute("SELECT * FROM inbox_attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert all(not isinstance(value, bytes) for value in row)
    [line] = [l for l in _messages(decision.conversation_id) if l["id"] == message_id]
    assert line["attachments"][0]["id"] == attachment_id


def test_identity_normalization_treats_handles_links_and_phone_formats_alike():
    assert inbox.normalize_identity("@Ivan") == inbox.normalize_identity("https://t.me/ivan") == "ivan"
    assert inbox.normalize_identity("+7 (900) 123-45-67") == inbox.normalize_identity("79001234567") == "+79001234567"
    assert inbox.normalize_identity("Anna@Example.com") == "anna@example.com"
    assert inbox.sender_identities("telegram", "555", "555", "Иван Петров") == ["555"]
