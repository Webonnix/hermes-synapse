"""GET /api/history/sessions — which chats the sidebar shows.

The list was built from the `messages` table alone, so a chat the user had just
created — named, assigned an agent, and open on screen — did not appear until
its first reply arrived. It read as "my new chat vanished", and the workaround
(send something and hope) is not one a user can guess.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import database
from backend.auth import active_sessions
from backend.main import app

client = TestClient(app)
client.headers = {"Authorization": "Bearer test-token"}
active_sessions.add("test-token")


@pytest.fixture()
def sessions_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sessions.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database._set_backend_for_tests(database.SQLiteBackend())
    database.init_db()
    yield db_path
    database._set_backend_for_tests(None)


def _message(db_path: str, session_id: str, timestamp: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', ?)",
            (session_id, timestamp),
        )


def _ids(response) -> list[str]:
    return [row["id"] for row in response.json()]


def test_a_created_but_unused_chat_is_listed(sessions_db):
    database.save_session_metadata("chat_new_1234", "Antigravitytest", agent_id="vexa")

    body = client.get("/api/history/sessions").json()
    ids = [row["id"] for row in body]
    assert "chat_new_1234" in ids
    row = next(r for r in body if r["id"] == "chat_new_1234")
    assert row["title"] == "Antigravitytest"
    assert row["agent_id"] == "vexa"


def test_a_brand_new_chat_sorts_above_older_conversations(sessions_db):
    _message(sessions_db, "chat_old_1", "2020-01-01 00:00:00")
    database.save_session_metadata("chat_new_2", "Fresh")

    ids = [i for i in _ids(client.get("/api/history/sessions")) if i != "dashboard"]
    assert ids.index("chat_new_2") < ids.index("chat_old_1")


def test_conversations_still_sort_by_last_message(sessions_db):
    _message(sessions_db, "chat_older", "2026-01-01 00:00:00")
    _message(sessions_db, "chat_newer", "2026-06-01 00:00:00")

    ids = [i for i in _ids(client.get("/api/history/sessions")) if i != "dashboard"]
    assert ids.index("chat_newer") < ids.index("chat_older")


def test_a_chat_is_listed_once_even_with_both_metadata_and_messages(sessions_db):
    database.save_session_metadata("chat_both", "Both")
    _message(sessions_db, "chat_both", "2026-06-01 00:00:00")

    assert _ids(client.get("/api/history/sessions")).count("chat_both") == 1


def test_the_main_terminal_is_always_first(sessions_db):
    database.save_session_metadata("chat_new_2", "Fresh")
    assert _ids(client.get("/api/history/sessions"))[0] == "dashboard"


def test_agent_sessions_are_still_excluded(sessions_db):
    """A subagent's own scratch history is not a user chat — metadata rows must
    not be a second way for one to reach the sidebar."""
    database.save_subagent(id="devops", name="DevOps", system_prompt="", model="m", skills="")
    database.save_session_metadata("devops", "DevOps")

    assert "devops" not in _ids(client.get("/api/history/sessions"))
