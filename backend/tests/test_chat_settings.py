"""Per-chat settings, @mention routing, branching and incognito chats."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import agent as agent_module
from backend import chat_settings, database
from backend.auth import active_sessions
from backend.main import _resolve_turn_agent, app

client = TestClient(app)
client.headers = {"Authorization": "Bearer test-token"}
active_sessions.add("test-token")


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "chat.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database._set_backend_for_tests(database.SQLiteBackend())
    database.init_db()
    yield db_path
    database._set_backend_for_tests(None)


def _messages(db_path, session_id, *contents):
    ids = []
    with sqlite3.connect(db_path) as connection:
        for index, content in enumerate(contents):
            role = "user" if index % 2 == 0 else "assistant"
            cursor = connection.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            ids.append(cursor.lastrowid)
    return ids


# --- validation and storage -------------------------------------------------

def test_unsaved_chat_has_the_defaults(chat_db):
    assert chat_settings.get_settings("chat_new") == chat_settings.DEFAULTS


def test_settings_round_trip(chat_db):
    saved = chat_settings.save_settings("chat_a", {
        "system_prompt": "  Отвечай по-английски.  ", "temperature": "0.2",
        "top_p": 0.9, "max_tokens": "512", "think": "off",
    })
    assert saved["system_prompt"] == "Отвечай по-английски."
    assert chat_settings.get_settings("chat_a") == {
        "system_prompt": "Отвечай по-английски.", "temperature": 0.2, "top_p": 0.9,
        "max_tokens": 512, "think": "off", "ephemeral": False,
    }


@pytest.mark.parametrize("payload", [
    {"temperature": 2.5},
    {"top_p": -0.1},
    {"max_tokens": 0},
    {"max_tokens": "lots"},
    {"think": "maybe"},
    {"system_prompt": "x" * (chat_settings.MAX_SYSTEM_PROMPT_CHARS + 1)},
])
def test_out_of_range_settings_are_refused(chat_db, payload):
    with pytest.raises(ValueError):
        chat_settings.save_settings("chat_a", payload)


def test_settings_api_rejects_bad_values_with_400(chat_db):
    response = client.put("/api/history/chat_a/settings", json={"temperature": 9})
    assert response.status_code == 400
    assert client.put("/api/history/chat_a/settings", json={"temperature": 1.1}).status_code == 200
    assert client.get("/api/history/chat_a/settings").json()["temperature"] == 1.1


# --- applying them to a turn ------------------------------------------------

def test_chat_settings_win_only_inside_their_turn():
    vexa = agent_module.VexaAgent.__new__(agent_module.VexaAgent)
    vexa.temperature, vexa.max_tokens, vexa.local_think = 0.7, 4096, "false"
    token = agent_module._CHAT_SETTINGS.set({"temperature": 0.1, "max_tokens": 300, "think": "on"})
    try:
        assert (vexa.temperature, vexa.max_tokens, vexa.local_think) == (0.1, 300, True)
    finally:
        agent_module._CHAT_SETTINGS.reset(token)
    assert (vexa.temperature, vexa.max_tokens, vexa.local_think) == (0.7, 4096, "false")


def test_unset_fields_fall_through_to_the_configuration():
    vexa = agent_module.VexaAgent.__new__(agent_module.VexaAgent)
    vexa.temperature, vexa.max_tokens, vexa.local_think = 0.7, 4096, "false"
    token = agent_module._CHAT_SETTINGS.set(dict(chat_settings.DEFAULTS))
    try:
        assert (vexa.temperature, vexa.max_tokens, vexa.local_think) == (0.7, 4096, "false")
        assert agent_module._chat_system_addendum() == ""
        assert agent_module._chat_extra_payload() is None
    finally:
        agent_module._CHAT_SETTINGS.reset(token)


def test_the_chat_prompt_is_appended_not_substituted():
    token = agent_module._CHAT_SETTINGS.set({"system_prompt": "Только JSON.", "top_p": 0.5})
    try:
        addendum = agent_module._chat_system_addendum()
        assert addendum.startswith("\n\n") and addendum.endswith("Только JSON.")
        assert agent_module._chat_extra_payload() == {"top_p": 0.5}
    finally:
        agent_module._CHAT_SETTINGS.reset(token)


# --- @mention ---------------------------------------------------------------

def test_mention_resolves_only_known_agents(chat_db, monkeypatch):
    monkeypatch.setattr(database, "get_subagent", lambda agent_id: {"id": agent_id} if agent_id == "coder" else None)
    assert _resolve_turn_agent("coder") == "coder"
    assert _resolve_turn_agent("vexa") == "vexa"
    assert _resolve_turn_agent("ghost") is None
    assert _resolve_turn_agent("") is None
    assert _resolve_turn_agent(None) is None


# --- branching --------------------------------------------------------------

def test_branch_copies_up_to_the_chosen_message_and_keeps_the_setup(chat_db):
    first, _, third, _ = _messages(chat_db, "chat_src", "q1", "a1", "q2", "a2")
    database.save_session_metadata("chat_src", "Исходный", agent_id="coder", project_id="p1")
    chat_settings.save_settings("chat_src", {"system_prompt": "Будь краток.", "temperature": 0.3})

    body = client.post("/api/history/chat_src/fork", json={"up_to_message_id": third, "title": "Ветка"}).json()
    new_id = body["new_session_id"]

    with sqlite3.connect(chat_db) as connection:
        copied = [row[0] for row in connection.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY id", (new_id,)
        )]
    assert copied == ["q1", "a1", "q2"]
    assert database.get_session_title(new_id) == "Ветка"
    assert database.get_session_agent_id(new_id) == "coder"
    assert database.get_session_project_id(new_id) == "p1"
    assert chat_settings.get_settings(new_id)["system_prompt"] == "Будь краток."


def test_branch_from_a_foreign_message_is_a_404(chat_db):
    _messages(chat_db, "chat_src", "q1")
    (other,) = _messages(chat_db, "chat_other", "elsewhere")
    response = client.post("/api/history/chat_src/fork", json={"up_to_message_id": other})
    assert response.status_code == 404


def test_fork_without_a_body_still_copies_everything(chat_db):
    _messages(chat_db, "chat_src", "q1", "a1")
    new_id = client.post("/api/history/chat_src/fork").json()["new_session_id"]
    with sqlite3.connect(chat_db) as connection:
        count = connection.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (new_id,)).fetchone()[0]
    assert count == 2


# --- incognito --------------------------------------------------------------

def test_incognito_chat_is_hidden_from_the_list(chat_db):
    _messages(chat_db, "chat_visible", "hi")
    _messages(chat_db, "chat_secret", "hi")
    chat_settings.save_settings("chat_secret", {"ephemeral": True})
    ids = [row["id"] for row in client.get("/api/history/sessions").json()]
    assert "chat_visible" in ids
    assert "chat_secret" not in ids


def test_incognito_chats_are_purged(chat_db):
    _messages(chat_db, "chat_secret", "hi", "hello")
    chat_settings.save_settings("chat_secret", {"ephemeral": True})
    _messages(chat_db, "chat_kept", "hi")

    assert chat_settings.purge_ephemeral_sessions() == 1

    with sqlite3.connect(chat_db) as connection:
        remaining = {row[0] for row in connection.execute("SELECT DISTINCT session_id FROM messages")}
    assert remaining == {"chat_kept"}
    assert chat_settings.ephemeral_session_ids() == []


def test_deleting_a_chat_drops_its_settings(chat_db):
    chat_settings.save_settings("chat_a", {"system_prompt": "x"})
    client.delete("/api/history/chat_a")
    assert chat_settings.get_settings("chat_a") == chat_settings.DEFAULTS


# --- arena ------------------------------------------------------------------

def test_arena_needs_exactly_two_contenders():
    response = client.post("/api/chat/arena", json={"prompt": "hi", "contenders": [{"model": "a"}]})
    assert response.status_code == 400


def test_arena_answers_each_contender_and_survives_a_failure(monkeypatch):
    from backend import chat_arena, chat_models, llm_client

    async def fake_resolve(source, model_id, **_):
        return {"api_base": "http://x", "api_key": "", "model": model_id, "provider": "local"}

    calls = []

    async def fake_call(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "broken":
            raise RuntimeError("boom")
        return llm_client.NormalizedLLMResponse(
            status=llm_client.STATUS_SUCCESS, provider="local", model=kwargs["model"],
            content=f"answer from {kwargs['model']}",
        )

    monkeypatch.setattr(chat_models, "resolve_chat_model", fake_resolve)
    monkeypatch.setattr(llm_client, "call_llm_normalized", fake_call)

    body = client.post("/api/chat/arena", json={
        "prompt": "2+2?", "contenders": [{"model": "good"}, {"model": "broken"}],
    }).json()
    first, second = body["results"]
    assert first["content"] == "answer from good" and first["error"] is None
    assert second["status"] == "error" and second["content"] == ""
    # A plain completion: no tools offered, so nothing can act twice.
    assert all("tools" not in call for call in calls)
    assert calls[0]["messages"][-1] == {"role": "user", "content": "2+2?"}
