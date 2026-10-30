"""Public conversation shares: what may leave the system, and what may not."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import database, shares


@pytest.fixture()
def shares_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "shares.db"))
    monkeypatch.setattr(database, "_backend", None, raising=False)
    database.init_db()
    yield
    monkeypatch.setattr(database, "_backend", None, raising=False)


def _messages():
    return [
        {"role": "system", "content": "You are Vexa. Internal instructions.", "timestamp": "t0"},
        {"role": "user", "content": "открой /home/albert/secrets/plan.md", "timestamp": "t1"},
        {"role": "tool", "content": json.dumps({"path": "/app/backend/data/x", "key": "sk-abc"}), "timestamp": "t2"},
        {"role": "assistant", "content": "Готово, Альберт.", "timestamp": "t3"},
    ]


def test_only_user_and_assistant_turns_are_published(shares_db):
    snapshot = shares.build_snapshot("chat", _messages())
    assert [m["role"] for m in snapshot["messages"]] == ["user", "assistant"]
    body = json.dumps(snapshot)
    # Tool payloads carry arguments, raw provider output and file paths.
    assert "sk-abc" not in body
    assert "Internal instructions" not in body


def test_local_filesystem_paths_never_leave(shares_db):
    snapshot = shares.build_snapshot("chat", _messages())
    body = json.dumps(snapshot)
    assert "/home/albert" not in body
    assert "[path]" in body


def test_an_unknown_role_is_dropped_rather_than_published(shares_db):
    """The publishable set is an allowlist, so a role added later stays private
    until someone decides it is safe."""
    snapshot = shares.build_snapshot("chat", [
        {"role": "developer", "content": "hidden", "timestamp": "t"},
        {"role": "assistant", "content": "visible", "timestamp": "t"},
    ])
    assert [m["content"] for m in snapshot["messages"]] == ["visible"]


def test_published_messages_carry_only_the_allowlisted_fields(shares_db):
    """Built by allowlist, so a new column on `messages` cannot be published by
    accident."""
    item = shares.sanitize_message(
        {"role": "user", "content": "hi", "timestamp": "t", "id": 7,
         "cost_usd": 1.5, "run_meta": {"provider": "llamacpp", "request_id": "abc"}}
    )
    assert set(item.keys()) == {"role", "content", "ts"}


def test_credentials_in_a_visible_turn_are_redacted(shares_db):
    snapshot = shares.build_snapshot("chat", [
        {"role": "user", "content": "ключ sk-proj-abcdefghijklmnopqrstuvwxyz1234", "timestamp": "t"},
    ])
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz1234" not in json.dumps(snapshot)


def test_create_load_and_revoke(shares_db):
    created = shares.create_share("chat_a", _messages(), title="Мой диалог")
    assert created["url"] == f"/share/{created['token']}"
    assert created["message_count"] == 2

    loaded = shares.load_share(created["token"])
    assert loaded["title"] == "Мой диалог"

    assert shares.revoke_share("chat_a") is True
    # A revoked link stops working, it is not merely unlisted.
    assert shares.load_share(created["token"]) is None
    assert shares.revoke_share("chat_a") is False


def test_resharing_supersedes_the_previous_link(shares_db):
    """One live share per chat: an older link the owner has forgotten about must
    not keep working."""
    first = shares.create_share("chat_a", _messages())
    second = shares.create_share("chat_a", _messages())
    assert first["token"] != second["token"]
    assert shares.load_share(first["token"]) is None
    assert shares.load_share(second["token"]) is not None


def test_a_snapshot_does_not_follow_the_conversation(shares_db):
    created = shares.create_share("chat_a", [
        {"role": "user", "content": "первое", "timestamp": "t"},
    ])
    # The conversation continues; the published page must not grow with it.
    loaded = shares.load_share(created["token"])
    assert [m["content"] for m in loaded["messages"]] == ["первое"]


def test_unknown_and_malformed_tokens_are_simply_absent(shares_db):
    assert shares.load_share("nope") is None
    assert shares.load_share("") is None
    assert shares.load_share("x" * 500) is None


def test_rendered_page_escapes_content_and_ships_no_script(shares_db):
    snapshot = shares.build_snapshot("chat", [
        {"role": "user", "content": "<img src=x onerror=alert(1)>", "timestamp": "t"},
    ])
    html = shares.render_html(snapshot)
    assert "<img src=x" not in html
    assert "&lt;img" in html
    assert "<script" not in html.lower()


def test_public_endpoint_serves_the_snapshot_without_a_session(shares_db):
    from backend.main import app

    # No lifespan context on purpose: this asserts routing and the auth
    # allow-list, not startup. Same construction as the rest of the API tests.
    client = TestClient(app)
    created = shares.create_share("chat_a", _messages(), title="Публичный")
    response = client.get(created["url"])
    assert response.status_code == 200
    assert "Публичный" in response.text
    assert response.headers["x-robots-tag"].startswith("noindex")
    assert "/home/albert" not in response.text

    shares.revoke_share("chat_a")
    # Revoked and never-existed answer identically, so the endpoint cannot be
    # used to probe which tokens were once valid.
    assert client.get(created["url"]).status_code == 404
    assert client.get("/share/never-existed").status_code == 404


def test_old_share_is_hidden_but_can_still_be_revoked(shares_db):
    created = shares.create_share('legacy', _messages())
    database._execute('UPDATE conversation_shares SET snapshot = ? WHERE token = ?',
                      (json.dumps({'version': 1, 'messages': []}), created['token']))
    assert shares.load_share(created['token']) is None
    assert shares.get_share_for_chat('legacy') is None
    assert shares.revoke_share('legacy') is True


def test_privacy_placeholders_are_never_published(shares_db):
    assert '[SECRET_' not in shares.sanitize_text('mail [SECRET_abcdef1234]')
