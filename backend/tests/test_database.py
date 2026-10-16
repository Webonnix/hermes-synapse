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
    database.save_message(session_id, "user", "Привет, Vexa")
    # Save an assistant reply
    database.save_message(session_id, "assistant", "Здравствуйте, Сэр")
    
    # Retrieve history
    history = database.get_chat_history(session_id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Привет, Vexa"
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
        "provider_id": "local",
    })

    status = database.get_agent_budget_status("researcher")
    assert status["used_usd"] == pytest.approx(1.5)
    by_provider = {row["provider_id"]: row for row in status["by_provider"]}
    assert by_provider["prov-openai"]["used_usd"] == pytest.approx(1.5)
    assert by_provider["prov-openai"]["calls"] == 1
    assert by_provider["local"]["used_usd"] == pytest.approx(0.0)
    assert by_provider["local"]["calls"] == 1


def test_decision_log_without_provider_id_defaults_to_local():
    database.save_decision_log({
        "timestamp": "2026-01-01 00:00:00", "session_id": "s", "model": "qwen3:8b",
        "latency_ms": 10, "success": True, "error": None, "prompt_tokens_estimate": 1,
        "user_message": "hi", "assistant_response": "ok", "traces": [],
        "agent_id": "vexa", "completion_tokens_estimate": 1, "cost_usd": 0.0,
    })
    breakdown = database.get_agent_provider_breakdown("vexa")
    assert breakdown == [{"provider_id": "local", "calls": 1, "used_usd": 0.0}]


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
    database.save_session_metadata("s1", "Chat 1", agent_id="vexa", project_id="proj-1")
    assert database.get_session_project_id("s1") == "proj-1"

    # A title-only rename (project_id=None) must not clobber the project —
    # same selective-update contract agent_id already had.
    database.save_session_metadata("s1", "Renamed chat")
    assert database.get_session_project_id("s1") == "proj-1"
    assert database.get_session_agent_id("s1") == "vexa"


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


# ── Legacy primary-agent id migration ('jarvis' -> 'vexa') ───────────────────

def _legacy_conn():
    conn = sqlite3.connect(database.DB_PATH)
    return conn, conn.cursor()


def test_legacy_jarvis_row_and_references_are_migrated_to_vexa():
    # Simulate a pre-rename install: replace the freshly seeded canonical row
    # with the legacy one and hang some references off it.
    conn, cur = _legacy_conn()
    cur.execute("DELETE FROM subagents WHERE id = 'vexa'")
    cur.execute(
        "INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id) "
        "VALUES ('jarvis', 'Vexa (Main)', 'p', 'm', 'orchestrator', NULL)"
    )
    cur.execute(
        "INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id) "
        "VALUES ('kid', 'Kid', 'p', 'm', 'agent', 'jarvis')"
    )
    cur.execute(
        "INSERT INTO decision_logs (timestamp, session_id, model, latency_ms, success, error, "
        "prompt_tokens_estimate, user_message, assistant_response, traces, agent_id) "
        "VALUES ('t', 's-mig', 'm', 1, 1, NULL, 1, 'u', 'a', '[]', 'jarvis')"
    )
    cur.execute("INSERT INTO session_metadata (session_id, title, agent_id) VALUES ('sess-mig', 'T', 'jarvis')")
    cur.execute("INSERT INTO agent_events (agent_id, timestamp, event_type, message) VALUES ('jarvis', 't', 'x', 'm')")
    cur.execute("INSERT INTO workflow_tasks (id, goal, assignee, created_at, updated_at) VALUES ('T-mig', 'g', 'jarvis', 't', 't')")
    conn.commit()

    renamed = database._rename_main_agent_row(cur)
    database._cascade_main_agent_id(cur)
    conn.commit()

    assert renamed is True
    assert cur.execute("SELECT COUNT(*) FROM subagents WHERE id = 'jarvis'").fetchone()[0] == 0
    assert cur.execute("SELECT name FROM subagents WHERE id = 'vexa'").fetchone()[0] == "Vexa (Main)"
    assert cur.execute("SELECT parent_id FROM subagents WHERE id = 'kid'").fetchone()[0] == "vexa"
    assert cur.execute("SELECT agent_id FROM decision_logs WHERE session_id = 's-mig'").fetchone()[0] == "vexa"
    assert cur.execute("SELECT agent_id FROM session_metadata WHERE session_id = 'sess-mig'").fetchone()[0] == "vexa"
    assert cur.execute("SELECT agent_id FROM agent_events WHERE message = 'm'").fetchone()[0] == "vexa"
    assert cur.execute("SELECT assignee FROM workflow_tasks WHERE id = 'T-mig'").fetchone()[0] == "vexa"
    conn.close()


def test_main_agent_migration_is_a_noop_without_a_legacy_row():
    # Fresh install already seeded 'vexa' and no 'jarvis' — both helpers must be
    # safe and quiet.
    conn, cur = _legacy_conn()
    assert database._rename_main_agent_row(cur) is False
    database._cascade_main_agent_id(cur)  # must not raise
    conn.commit()
    assert cur.execute("SELECT COUNT(*) FROM subagents WHERE id = 'vexa'").fetchone()[0] == 1
    conn.close()


def test_main_agent_migration_leaves_data_alone_when_both_rows_exist():
    conn, cur = _legacy_conn()
    cur.execute(
        "INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id) "
        "VALUES ('jarvis', 'Legacy', 'p', 'm', 'orchestrator', NULL)"
    )
    conn.commit()
    assert database._rename_main_agent_row(cur) is False
    assert cur.execute("SELECT COUNT(*) FROM subagents WHERE id = 'jarvis'").fetchone()[0] == 1
    conn.close()


def test_full_init_db_upgrades_a_legacy_jarvis_install_end_to_end():
    # Rewind the freshly-seeded DB to what a pre-2026-08 install looks like:
    # a 'jarvis' orchestrator row with children and a chat session pointing at
    # it — then run the real init_db() and confirm the whole path converges.
    conn, cur = _legacy_conn()
    cur.execute("UPDATE subagents SET id = 'jarvis' WHERE id = 'vexa'")
    cur.execute("UPDATE subagents SET parent_id = 'jarvis' WHERE parent_id = 'vexa'")
    cur.execute("INSERT INTO session_metadata (session_id, title, agent_id) VALUES ('legacy-sess', 'Old chat', 'jarvis')")
    conn.commit()
    conn.close()

    database.init_db()

    assert database.get_subagent("jarvis") is None
    main_agent = database.get_subagent("vexa")
    assert main_agent and main_agent["agent_type"] == "orchestrator"
    assert database.get_session_agent_id("legacy-sess") == "vexa"
    # every seeded child now points at the canonical id
    kids = [a for a in database.get_all_subagents() if a["agent_type"] == "agent" and a.get("parent_id")]
    assert kids and all(k["parent_id"] != "jarvis" for k in kids)


def test_local_llm_migration_moves_ollama_state_to_llamacpp(monkeypatch):
    """Persisted Ollama-era state must follow the local model to llama.cpp —
    otherwise the dashboard keeps posting to a server that no longer exists and
    every local agent names a model id llama-server does not serve."""
    import json
    import sqlite3

    monkeypatch.setenv("LLAMACPP_BASE_URL", "http://hermes-llama:8080")
    monkeypatch.setenv("LLM_MODEL", "qwen3.8-27b")
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    cur.execute("CREATE TABLE subagents (id TEXT, model TEXT, model_provider TEXT)")
    cur.execute("CREATE TABLE decision_logs (id INTEGER, provider_id TEXT)")
    legacy = {
        "api_base": "http://hermes-ollama:11434", "model": "unsloth-mtp-q4km:latest",
        "ollama_base_url": "http://hermes-ollama:11434", "ollama_num_ctx": 196608,
        "ollama_think": False, "ollama_keep_alive": "-1",
        "openai_api_base": "http://hermes-ollama:11434/v1",
    }
    for key, value in legacy.items():
        cur.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    # Written raw by set_setting — the other of the table's two encodings.
    cur.execute("INSERT INTO app_settings (key, value) VALUES ('provider', 'ollama')")
    cur.executemany("INSERT INTO subagents VALUES (?, ?, ?)", [
        ("vexa", "unsloth-mtp-q4km", "ollama"), ("ext", "kimi/kimi-k3", "prov-x"), ("blank", "old:tag", ""),
    ])
    cur.executemany("INSERT INTO decision_logs VALUES (?, ?)", [(1, "ollama"), (2, "prov-x")])

    database._migrate_local_llm_to_llamacpp(cur)

    rows = {key: json.loads(value) for key, value in cur.execute("SELECT key, value FROM app_settings")}
    assert rows["provider"] == "llamacpp"
    assert rows["api_base"] == rows["local_base_url"] == "http://hermes-llama:8080"
    assert rows["model"] == "qwen3.8-27b"
    assert rows["local_num_ctx"] == 196608 and rows["local_think"] is False
    assert rows["openai_api_base"] == "https://openrouter.ai/api/v1"
    assert not any(key.startswith("ollama_") for key in rows)
    assert dict(cur.execute("SELECT id, model_provider FROM subagents")) == {"vexa": "local", "ext": "prov-x", "blank": ""}
    assert dict(cur.execute("SELECT id, model FROM subagents")) == {
        "vexa": "qwen3.8-27b", "ext": "kimi/kimi-k3", "blank": "qwen3.8-27b",
    }
    assert [r[0] for r in cur.execute("SELECT provider_id FROM decision_logs ORDER BY id")] == ["local", "prov-x"]

    # Next start: the marker stops the one-time part, so a model the owner picks
    # afterwards survives a restart; the sentinel rewrite still heals old writers.
    cur.execute("UPDATE subagents SET model = 'custom' WHERE id = 'vexa'")
    cur.execute("INSERT INTO decision_logs VALUES (3, 'ollama')")
    database._migrate_local_llm_to_llamacpp(cur)
    assert dict(cur.execute("SELECT id, model FROM subagents"))["vexa"] == "custom"
    assert [r[0] for r in cur.execute("SELECT provider_id FROM decision_logs ORDER BY id")] == ["local", "prov-x", "local"]

