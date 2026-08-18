import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

from backend import database, local_crypto

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    # Override database path to use temporary test file
    original_db_path = database.DB_PATH
    original_db_dir = database.DB_DIR
    
    test_db = tmp_path / "test_hermes.db"
    database.DB_PATH = str(test_db)
    database.DB_DIR = str(tmp_path)
    
    database.init_db()
    
    yield
    
    # Restore original paths
    database.DB_PATH = original_db_path
    database.DB_DIR = original_db_dir

def test_database_init():
    assert os.path.exists(database.DB_PATH)

def test_save_and_retrieve_message():
    session_id = "user_test_123"
    
    # Verify initially empty
    assert len(database.get_chat_history(session_id)) == 0
    
    # Save a user message
    database.save_message(session_id, "user", "Привет, Jarvis")
    # Save an assistant reply
    database.save_message(session_id, "assistant", "Здравствуйте, Сэр")
    
    # Retrieve history
    history = database.get_chat_history(session_id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Привет, Jarvis"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Здравствуйте, Сэр"

def test_chronological_ordering():
    session_id = "user_order_999"
    
    database.save_message(session_id, "user", "Msg 1")
    database.save_message(session_id, "assistant", "Msg 2")
    database.save_message(session_id, "user", "Msg 3")
    
    history = database.get_chat_history(session_id, limit=2)
    # limit=2 should return the last two messages in chronological order (Msg 2, Msg 3)
    assert len(history) == 2
    assert history[0]["role"] == "assistant"
    assert history[0]["content"] == "Msg 2"
    assert history[1]["role"] == "user"
    assert history[1]["content"] == "Msg 3"

def test_clear_chat_history():
    session_id = "user_clear_abc"
    
    database.save_message(session_id, "user", "Hello")
    assert len(database.get_chat_history(session_id)) == 1
    
    database.clear_chat_history(session_id)
    assert len(database.get_chat_history(session_id)) == 0

def test_session_metadata_titles():
    session_id = "chat_test_session_title"
    
    # Verify title is initially None
    assert database.get_session_title(session_id) is None
    
    # Save a custom title
    database.save_session_title(session_id, "Interesting Chat About AI")
    assert database.get_session_title(session_id) == "Interesting Chat About AI"
    
    # Update the title
    database.save_session_title(session_id, "Updated Chat Title")
    assert database.get_session_title(session_id) == "Updated Chat Title"
    
    # Delete the title
    assert database.delete_session_title(session_id) is True
    assert database.get_session_title(session_id) is None
    
    # Deleting again should return False (not found)
    assert database.delete_session_title(session_id) is False


@pytest.fixture(autouse=True)
def _memory_encryption_key(monkeypatch):
    """Every test gets a fixed key so encryption doesn't depend on Valkey."""
    monkeypatch.setenv("MEMORY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(local_crypto, "_fernet", None)
    yield
    monkeypatch.setattr(local_crypto, "_fernet", None)


def test_user_memory_is_encrypted_at_rest_and_round_trips():
    database.save_user_memory("preferred_address", "Альберт", session_id="global")

    # The raw SQLite row must not contain the plaintext — that's the whole point.
    conn = sqlite3.connect(database.DB_PATH)
    raw_value = conn.execute(
        "SELECT value FROM user_memory WHERE key = 'preferred_address'"
    ).fetchone()[0]
    conn.close()
    assert raw_value != "Альберт"
    assert raw_value.startswith("enc:v1:")

    # Every read path decrypts transparently.
    assert database.get_preferred_address() == "Альберт"
    results = database.search_user_memory("Альберт", session_id="global")
    assert any(r["value"] == "Альберт" for r in results)
    listed = database.list_user_memory(session_id="global")
    assert any(item["value"] == "Альберт" for item in listed)


def test_subagent_memory_is_encrypted_at_rest_and_round_trips():
    database.db_save_subagent_memory("vexa", "api_token", "sk-super-secret")

    conn = sqlite3.connect(database.DB_PATH)
    raw_value = conn.execute(
        "SELECT value FROM subagent_memory WHERE subagent_id = 'vexa' AND key = 'api_token'"
    ).fetchone()[0]
    conn.close()
    assert raw_value != "sk-super-secret"
    assert raw_value.startswith("enc:v1:")

    assert database.db_get_subagent_memory("vexa", "api_token") == {"api_token": "sk-super-secret"}
    assert database.db_get_subagent_memory("vexa") == {"api_token": "sk-super-secret"}


def test_agent_budget_status_includes_provider_breakdown():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    # budget_period defaults to 'monthly' (see get_agent_budget_status), which
    # only sums decision_logs from the start of the *current* calendar month —
    # use "now" rather than a fixed date so this doesn't silently stop
    # exercising the sum whenever the wall clock crosses a month boundary.
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    ts1 = now.strftime("%Y-%m-%d %H:%M:%S")
    ts2 = (now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

    database.save_subagent("researcher", "Researcher", "sp", "qwen3:8b", budget_usd_limit=10.0)
    database.save_decision_log({
        "timestamp": ts1, "session_id": "researcher", "model": "gpt-4o",
        "latency_ms": 100, "success": True, "error": None, "prompt_tokens_estimate": 10,
        "user_message": "hi", "assistant_response": "ok", "traces": [],
        "agent_id": "researcher", "completion_tokens_estimate": 10, "cost_usd": 1.5,
        "provider_id": "prov-openai",
    })
    database.save_decision_log({
        "timestamp": ts2, "session_id": "researcher", "model": "qwen3:8b",
        "latency_ms": 50, "success": True, "error": None, "prompt_tokens_estimate": 5,
        "user_message": "hi again", "assistant_response": "ok", "traces": [],
        "agent_id": "researcher", "completion_tokens_estimate": 5, "cost_usd": 0.0,
        "provider_id": "ollama",
    })

    status = database.get_agent_budget_status("researcher")
    assert status["used_usd"] == pytest.approx(1.5)
    by_provider = {row["provider_id"]: row for row in status["by_provider"]}
    assert by_provider["prov-openai"]["used_usd"] == pytest.approx(1.5)
    assert by_provider["prov-openai"]["calls"] == 1
    assert by_provider["ollama"]["used_usd"] == pytest.approx(0.0)
    assert by_provider["ollama"]["calls"] == 1


def test_decision_log_without_provider_id_defaults_to_ollama():
    database.save_decision_log({
        "timestamp": "2026-01-01 00:00:00", "session_id": "s", "model": "qwen3:8b",
        "latency_ms": 10, "success": True, "error": None, "prompt_tokens_estimate": 1,
        "user_message": "hi", "assistant_response": "ok", "traces": [],
        "agent_id": "jarvis", "completion_tokens_estimate": 1, "cost_usd": 0.0,
    })
    breakdown = database.get_agent_provider_breakdown("jarvis")
    assert breakdown == [{"provider_id": "ollama", "calls": 1, "used_usd": 0.0}]


def test_subagent_project_id_round_trip():
    database.save_subagent("designer", "Designer", "sp", "qwen3:8b", project_id="proj-abc123")
    row = database.get_subagent("designer")
    assert row["project_id"] == "proj-abc123"
    listed = next(a for a in database.get_all_subagents() if a["id"] == "designer")
    assert listed["project_id"] == "proj-abc123"


def test_subagent_project_id_defaults_to_none():
    database.save_subagent("plain", "Plain", "sp", "qwen3:8b")
    assert database.get_subagent("plain")["project_id"] is None


def test_session_project_id_set_and_preserved_on_rename():
    database.save_session_metadata("s1", "Chat 1", agent_id="jarvis", project_id="proj-1")
    assert database.get_session_project_id("s1") == "proj-1"

    # A title-only rename (project_id=None) must not clobber the project —
    # same selective-update contract agent_id already had.
    database.save_session_metadata("s1", "Renamed chat")
    assert database.get_session_project_id("s1") == "proj-1"
    assert database.get_session_agent_id("s1") == "jarvis"


def test_session_project_id_can_be_cleared_with_empty_string():
    database.save_session_metadata("s1", "Chat 1", project_id="proj-1")
    database.save_session_metadata("s1", "Chat 1", project_id="")
    assert database.get_session_project_id("s1") == ""


def test_session_project_id_defaults_to_none_for_new_session():
    database.save_session_metadata("fresh", "Fresh chat")
    assert database.get_session_project_id("fresh") is None


def test_run_meta_survives_a_reload():
    """Generation stats live on the message row, so a refreshed page still shows
    the speed of a reply instead of losing it with the websocket event."""
    msg_id = database.save_message("s-meta", "assistant", "Готово.", cost_usd=0.0)
    database.update_message_meta(msg_id, {"output_tokens": 200, "decode_ms": 6439, "model": "qwen-quality:latest"})

    history = database.get_chat_history("s-meta")
    assert history[-1]["meta"] == {
        "output_tokens": 200,
        "decode_ms": 6439,
        "model": "qwen-quality:latest",
    }


def test_run_meta_is_absent_for_messages_that_never_had_it():
    database.save_message("s-plain", "user", "Привет")
    assert "meta" not in database.get_chat_history("s-plain")[-1]


def test_update_message_meta_ignores_missing_id_or_empty_meta():
    # Called on every turn, including ones where the save failed — must not raise.
    database.update_message_meta(None, {"output_tokens": 1})
    msg_id = database.save_message("s-noop", "assistant", "x")
    database.update_message_meta(msg_id, {})
    assert "meta" not in database.get_chat_history("s-noop")[-1]
