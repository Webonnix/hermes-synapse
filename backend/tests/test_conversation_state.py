import pytest

from backend import conversation_state, database


@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "conversation_state.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(conversation_state, "DB_PATH", db_path)
    database.init_db()
    return db_path


def test_get_state_returns_none_when_unset(state_db):
    assert conversation_state.get_state("bind-1", "telegram", "100") is None


def test_set_handling_mode_creates_then_updates_in_place(state_db):
    created = conversation_state.set_handling_mode("bind-1", "telegram", "100", "pending_choice", "Ann")
    assert created["handling_mode"] == "pending_choice"
    assert created["display_name"] == "Ann"

    updated = conversation_state.set_handling_mode("bind-1", "telegram", "100", "ai")
    assert updated["id"] == created["id"]
    assert updated["handling_mode"] == "ai"
    assert updated["display_name"] == "Ann"  # kept — no new name was given

    fetched = conversation_state.get_state("bind-1", "telegram", "100")
    assert fetched["handling_mode"] == "ai"


def test_set_handling_mode_rejects_unknown_mode(state_db):
    with pytest.raises(ValueError):
        conversation_state.set_handling_mode("bind-1", "telegram", "100", "nonsense")


def test_states_are_scoped_by_binding_platform_and_chat(state_db):
    conversation_state.set_handling_mode("bind-1", "telegram", "100", "ai")
    conversation_state.set_handling_mode("bind-1", "discord", "100", "waiting")
    conversation_state.set_handling_mode("bind-2", "telegram", "100", "owner_takeover")

    assert conversation_state.get_state("bind-1", "telegram", "100")["handling_mode"] == "ai"
    assert conversation_state.get_state("bind-1", "discord", "100")["handling_mode"] == "waiting"
    assert conversation_state.get_state("bind-2", "telegram", "100")["handling_mode"] == "owner_takeover"


def test_touch_state_updates_timestamp_and_name_without_changing_mode(state_db):
    conversation_state.set_handling_mode("bind-1", "telegram", "100", "waiting")
    conversation_state.touch_state("bind-1", "telegram", "100", "Bob")

    state = conversation_state.get_state("bind-1", "telegram", "100")
    assert state["handling_mode"] == "waiting"
    assert state["display_name"] == "Bob"


def test_touch_state_on_missing_row_does_not_raise(state_db):
    conversation_state.touch_state("bind-missing", "telegram", "999")  # should be a no-op, not an error


def test_history_reads_the_same_session_the_agent_answers_from(state_db):
    """The dashboard must show what the agent actually remembers, so both read
    the one session id the channel managers already use."""
    from backend import bot_access, database

    session_id = conversation_state.session_id_for_conversation("bind-1", "telegram", "100")
    assert session_id == bot_access.session_id_for("telegram", "bind-1", "100")

    database.save_message(session_id, "user", "Привет")
    database.save_message(session_id, "assistant", "Здравствуйте!")

    result = conversation_state.history("bind-1", "telegram", "100")
    assert result["session_id"] == session_id
    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    assert result["messages"][0]["content"] == "Привет"
    assert result["messages"][0]["timestamp"]


def test_clear_history_forgets_the_person_but_keeps_the_handling_mode(state_db):
    from backend import database

    conversation_state.set_handling_mode("bind-1", "telegram", "100", "waiting", "Ann")
    session_id = conversation_state.session_id_for_conversation("bind-1", "telegram", "100")
    database.save_message(session_id, "user", "Привет")

    conversation_state.clear_history("bind-1", "telegram", "100")

    assert conversation_state.history("bind-1", "telegram", "100")["messages"] == []
    state = conversation_state.get_state("bind-1", "telegram", "100")
    assert state["handling_mode"] == "waiting"
    assert state["display_name"] == "Ann"


def test_list_states_carries_history_stats_for_the_whole_list(state_db):
    from backend import database

    conversation_state.set_handling_mode("bind-1", "telegram", "100", "ai", "Ann")
    conversation_state.set_handling_mode("bind-1", "telegram", "200", "waiting", "Bob")
    busy = conversation_state.session_id_for_conversation("bind-1", "telegram", "100")
    database.save_message(busy, "user", "Первый вопрос")
    database.save_message(busy, "assistant", "Последнее слово")

    by_chat = {s["chat_id"]: s for s in conversation_state.list_states("bind-1")}
    assert by_chat["100"]["messages_count"] == 2
    assert by_chat["100"]["last_message_preview"] == "Последнее слово"
    assert by_chat["100"]["last_message_role"] == "assistant"
    assert by_chat["100"]["last_message_at"]
    # A conversation nobody has written in yet reports zeroes, not a missing key.
    assert by_chat["200"]["messages_count"] == 0
    assert by_chat["200"]["last_message_preview"] == ""


def test_list_states_scopes_to_binding_and_orders_by_recency(state_db):
    conversation_state.set_handling_mode("bind-1", "telegram", "100", "ai")
    conversation_state.set_handling_mode("bind-1", "telegram", "200", "waiting")
    conversation_state.set_handling_mode("bind-2", "telegram", "300", "owner_takeover")

    rows = conversation_state.list_states("bind-1")
    assert {row["chat_id"] for row in rows} == {"100", "200"}
    assert all(row["binding_id"] == "bind-1" for row in rows)
