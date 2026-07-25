import pytest

from backend import agent_messenger_governance, agent_tiers, control_plane, database


@pytest.fixture()
def messenger_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "messenger.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    monkeypatch.setattr(agent_tiers, "DB_PATH", db_path)
    monkeypatch.setattr(agent_messenger_governance, "DB_PATH", db_path)
    database.init_db()
    agent_messenger_governance._init_schema()

    monkeypatch.setattr(agent_messenger_governance, "_verify_bot_token", lambda token: "test_bot")

    stored_secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: stored_secrets.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: stored_secrets.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: stored_secrets.pop(key, None))

    database.save_subagent(
        id="agent-1", name="Agent One", system_prompt="", model="llama3", skills="git_dev"
    )
    return db_path


def _insert_raw_binding(db_path: str, subagent_id: str, platform: str, status: str) -> None:
    import sqlite3
    import uuid

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_messenger_bindings
                (id, subagent_id, platform, bot_username, valkey_secret_key, allowed_chat_ids,
                 status, control_task_id, created_at, updated_at)
            VALUES (?, ?, ?, 'existing', 'secret-key', '[]', ?, NULL, '2026-01-01', '2026-01-01')
            """,
            (f"bind-{uuid.uuid4().hex[:8]}", subagent_id, platform, status),
        )


def test_duplicate_check_is_scoped_to_platform(messenger_db):
    """An active binding on a different platform must not block a Telegram proposal."""
    _insert_raw_binding(messenger_db, "agent-1", "matrix", "active")

    proposal = agent_messenger_governance.create_telegram_binding_proposal("agent-1", "123:" + "a" * 35)
    assert proposal["status"] == "awaiting_approval"
    assert proposal["platform"] == "telegram"


def test_duplicate_check_still_blocks_same_platform(messenger_db):
    _insert_raw_binding(messenger_db, "agent-1", "telegram", "active")

    with pytest.raises(ValueError, match="already has a"):
        agent_messenger_governance.create_telegram_binding_proposal("agent-1", "123:" + "a" * 35)


def test_tier_with_allow_messenger_false_blocks_proposal(messenger_db):
    tier = agent_tiers.create_tier("No Messenger", allow_messenger=False)
    database.save_subagent(
        id="agent-1", name="Agent One", system_prompt="", model="llama3",
        skills="git_dev", tier_id=tier["id"],
    )

    with pytest.raises(ValueError, match="does not allow messenger bindings"):
        agent_messenger_governance.create_telegram_binding_proposal("agent-1", "123:" + "a" * 35)


def test_tier_with_allow_messenger_true_permits_proposal(messenger_db):
    tier = agent_tiers.create_tier("Messenger OK", allow_messenger=True)
    database.save_subagent(
        id="agent-1", name="Agent One", system_prompt="", model="llama3",
        skills="git_dev", tier_id=tier["id"],
    )

    proposal = agent_messenger_governance.create_telegram_binding_proposal("agent-1", "123:" + "a" * 35)
    assert proposal["status"] == "awaiting_approval"
