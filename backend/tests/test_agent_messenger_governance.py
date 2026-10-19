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


def test_the_main_agent_cannot_be_bound_to_a_messenger(messenger_db):
    """Vexa answers to the owner only — a binding would hand a channel anyone
    can message straight to the agent that still holds the server tools."""
    database.save_subagent(id="vexa", name="Vexa (Main)", system_prompt="", model="llama3")
    with pytest.raises(ValueError, match="main agent"):
        agent_messenger_governance.create_telegram_binding_proposal(
            "vexa", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )


@pytest.mark.asyncio
async def test_switching_to_token_mode_forces_a_disclosed_auto_reply(messenger_db, monkeypatch):
    monkeypatch.setattr(agent_messenger_governance, "_notify_owner_access_mode_changed", lambda *a: None)
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    binding_id = proposal["id"]
    assert proposal["access_mode"] == "owner_only"
    assert proposal["response_mode"] == "draft"

    updated = await agent_messenger_governance.update_binding_settings(binding_id, access_mode="token")
    # A draft-mode public bot would need the owner to hand-approve every answer,
    # so the switch also moves it to the disclosed auto-reply mode.
    assert updated["access_mode"] == "token"
    assert updated["response_mode"] == "auto_labeled"
    assert updated["welcome_message"]


@pytest.mark.asyncio
async def test_an_unknown_plan_is_rejected_rather_than_silently_stored(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    with pytest.raises(KeyError):
        await agent_messenger_governance.update_binding_settings(
            proposal["id"], default_plan_id="plan-does-not-exist"
        )


@pytest.mark.asyncio
async def test_default_disclosure_is_used_when_none_is_set(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    assert agent_messenger_governance.auto_reply_disclosure(proposal["id"]) == agent_messenger_governance.AUTO_REPLY_DISCLOSURE


@pytest.mark.asyncio
async def test_custom_disclosure_wording_is_saved_and_used(messenger_db):
    """The owner asked to reword the auto-reply signature ('личный AI-ассистент',
    not 'ассистент Vexa, не Альберта лично') — this is what makes that a saved
    per-channel setting instead of a hardcoded string."""
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    binding_id = proposal["id"]
    updated = await agent_messenger_governance.update_binding_settings(
        binding_id, auto_reply_disclosure_text="личный AI-ассистент Альберта"
    )
    assert updated["auto_reply_disclosure"] == "личный AI-ассистент Альберта"
    assert agent_messenger_governance.auto_reply_disclosure(binding_id) == "\n\n— личный AI-ассистент Альберта"


@pytest.mark.asyncio
async def test_disclosure_cannot_be_blanked_to_nothing(messenger_db):
    """Auto-labeled mode was approved on the promise every reply identifies
    itself as automated (see create_*_binding_proposal's acceptance list) — an
    empty string must fall back to the default, not disable the signature."""
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    binding_id = proposal["id"]
    await agent_messenger_governance.update_binding_settings(binding_id, auto_reply_disclosure_text="   ")
    assert agent_messenger_governance.auto_reply_disclosure(binding_id) == agent_messenger_governance.AUTO_REPLY_DISCLOSURE


@pytest.mark.asyncio
async def test_human_takeover_pause_defaults_and_is_clamped(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    binding_id = proposal["id"]
    assert proposal.get("human_takeover_pause_minutes") is None  # unset -> callers apply the default themselves

    updated = await agent_messenger_governance.update_binding_settings(binding_id, human_takeover_pause_minutes=30)
    assert updated["human_takeover_pause_minutes"] == 30

    with pytest.raises(ValueError):
        await agent_messenger_governance.update_binding_settings(binding_id, human_takeover_pause_minutes=-1)
    with pytest.raises(ValueError):
        await agent_messenger_governance.update_binding_settings(binding_id, human_takeover_pause_minutes=99999)


@pytest.mark.asyncio
async def test_escalation_enabled_defaults_off_and_is_settable(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    assert proposal["escalation_enabled"] is False

    updated = await agent_messenger_governance.update_binding_settings(proposal["id"], escalation_enabled=True)
    assert updated["escalation_enabled"] is True

    updated = await agent_messenger_governance.update_binding_settings(proposal["id"], escalation_enabled=False)
    assert updated["escalation_enabled"] is False


# ── Per-channel model ────────────────────────────────────────────────────────
# Every bot runtime in this module has always read model_override /
# model_provider_override when it starts a binding, but only creation could set
# them and the connect form never did — so a channel was pinned to its agent's
# model for life. These cover the edit path that closes that gap, and the one
# rule it has to enforce: a channel must not be able to point at a provider the
# owner has not approved (or has revoked).

@pytest.mark.asyncio
async def test_channel_model_can_be_changed_after_creation(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    updated = await agent_messenger_governance.update_binding_settings(
        proposal["id"], model_override="ag/gemini-3.7-flash-high", model_provider_override="local",
    )
    assert updated["model_override"] == "ag/gemini-3.7-flash-high"
    assert updated["model_provider_override"] == "local"


@pytest.mark.asyncio
async def test_clearing_the_channel_model_returns_it_to_the_agent(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    await agent_messenger_governance.update_binding_settings(
        proposal["id"], model_override="m", model_provider_override="local",
    )
    cleared = await agent_messenger_governance.update_binding_settings(
        proposal["id"], model_override="", model_provider_override="",
    )
    assert cleared["model_override"] is None
    assert cleared["model_provider_override"] is None


@pytest.mark.asyncio
async def test_an_unknown_provider_binding_is_refused(messenger_db):
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    with pytest.raises(ValueError):
        await agent_messenger_governance.update_binding_settings(
            proposal["id"], model_provider_override="prov-does-not-exist",
        )


@pytest.mark.asyncio
async def test_an_inactive_provider_binding_is_refused(messenger_db, monkeypatch):
    monkeypatch.setattr(
        "backend.provider_governance.get_binding",
        lambda binding_id: {"id": binding_id, "status": "revoked"},
    )
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    with pytest.raises(ValueError):
        await agent_messenger_governance.update_binding_settings(
            proposal["id"], model_provider_override="prov-revoked",
        )


@pytest.mark.asyncio
async def test_an_active_provider_binding_is_accepted(messenger_db, monkeypatch):
    monkeypatch.setattr(
        "backend.provider_governance.get_binding",
        lambda binding_id: {"id": binding_id, "status": "active"},
    )
    proposal = agent_messenger_governance.create_telegram_binding_proposal(
        "agent-1", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    updated = await agent_messenger_governance.update_binding_settings(
        proposal["id"], model_provider_override="prov-omniroute", model_override="ag/gemini-pro-agent",
    )
    assert updated["model_provider_override"] == "prov-omniroute"
    assert updated["model_override"] == "ag/gemini-pro-agent"


# ── Matrix health check: outages must not fail a binding ──────────────────────

def _matrix_binding_row(status="active"):
    return {
        "id": "mxbind-1", "platform": "matrix", "status": status, "subagent_id": "devops",
        "allowed_chat_ids": [], "response_mode": "draft", "human_takeover_pause_minutes": 15,
    }


class _FakeMatrixManager:
    def __init__(self, running, health=None, start_exc=None):
        self.running = running
        self.health = health
        self.start_exc = start_exc
        self.stopped = []
        self.started = []

    def is_running(self, binding_id):
        return self.running

    async def check_health(self, binding_id):
        return self.health

    async def stop(self, binding_id):
        self.stopped.append(binding_id)

    async def start(self, binding_id, *args, **kwargs):
        if self.start_exc:
            raise self.start_exc
        self.started.append(binding_id)

    def set_human_takeover_pause(self, binding_id, minutes):
        pass


def _run_matrix_health_check(monkeypatch, fake_manager):
    import asyncio

    from backend import agent_matrix_bot, agent_messenger_governance as gov

    failed = []
    monkeypatch.setattr(agent_matrix_bot, "manager", fake_manager)
    monkeypatch.setattr(gov, "mark_binding_failed", lambda binding_id, reason: failed.append(binding_id))
    monkeypatch.setattr(gov, "resolve_matrix_binding_credentials", lambda binding_id: {"access_token": "mat_x"})
    monkeypatch.setattr("backend.channel_activity.record", lambda *a, **k: None)
    asyncio.run(gov._health_check_matrix_binding(_matrix_binding_row()))
    return failed


def test_matrix_health_check_ignores_a_transient_outage(monkeypatch):
    from backend import agent_matrix_bot

    fake = _FakeMatrixManager(running=True, health=agent_matrix_bot.RENEW_TRANSIENT)
    assert _run_matrix_health_check(monkeypatch, fake) == []
    assert fake.stopped == []


def test_matrix_health_check_fails_a_revoked_session(monkeypatch):
    from backend import agent_matrix_bot

    fake = _FakeMatrixManager(running=True, health=agent_matrix_bot.RENEW_REVOKED)
    assert _run_matrix_health_check(monkeypatch, fake) == ["mxbind-1"]
    assert fake.stopped == ["mxbind-1"]


def test_matrix_health_check_restarts_an_active_binding_that_is_not_running(monkeypatch):
    fake = _FakeMatrixManager(running=False)
    assert _run_matrix_health_check(monkeypatch, fake) == []
    assert fake.started == ["mxbind-1"]


def test_matrix_health_check_postponed_start_is_not_a_failure(monkeypatch):
    from backend.matrix_oauth import MatrixTransientError

    fake = _FakeMatrixManager(running=False, start_exc=MatrixTransientError("down"))
    assert _run_matrix_health_check(monkeypatch, fake) == []
