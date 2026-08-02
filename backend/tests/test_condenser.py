from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import condenser, database


@pytest.fixture()
def condenser_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "condenser.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()
    return db_path


def _agent(**overrides):
    defaults = dict(
        condenser_enabled=True,
        max_history_len=4,
        condense_trigger_extra=3,
        api_base="http://fake",
        api_key="",
        model="fake-model",
        provider="ollama",
        ollama_num_ctx=8192,
        ollama_keep_alive="5m",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_disabled_agent_never_condenses(condenser_db, monkeypatch):
    agent = _agent(condenser_enabled=False)
    for i in range(20):
        database.save_message("s1", "user", f"msg {i}")
    summarize = AsyncMock()
    monkeypatch.setattr(condenser, "_summarize_chunk", summarize)

    await condenser.maybe_condense(agent, "s1")

    summarize.assert_not_called()
    assert database.get_condensed_summary("s1") is None


@pytest.mark.asyncio
async def test_below_trigger_threshold_does_not_condense(condenser_db, monkeypatch):
    agent = _agent()  # keep=4, trigger_extra=3 -> needs >= 7 messages to fire
    for i in range(6):
        database.save_message("s1", "user", f"msg {i}")
    summarize = AsyncMock()
    monkeypatch.setattr(condenser, "_summarize_chunk", summarize)

    await condenser.maybe_condense(agent, "s1")

    summarize.assert_not_called()
    assert database.get_condensed_summary("s1") is None


@pytest.mark.asyncio
async def test_condenses_once_threshold_crossed(condenser_db, monkeypatch):
    agent = _agent()
    for i in range(7):
        database.save_message("s1", "user", f"msg {i}")
    summarize = AsyncMock(return_value="Summary v1")
    monkeypatch.setattr(condenser, "_summarize_chunk", summarize)

    await condenser.maybe_condense(agent, "s1")

    summarize.assert_awaited_once()
    _agent_arg, previous_summary_arg, new_messages_arg = summarize.await_args.args
    assert previous_summary_arg == ""
    assert len(new_messages_arg) == 3  # 7 total - keep last 4 = 3 old ones folded in

    stored = database.get_condensed_summary("s1")
    assert stored["summary"] == "Summary v1"

    summary_msg = condenser.summary_message("s1")
    assert summary_msg["role"] == "system"
    assert "Summary v1" in summary_msg["content"]


@pytest.mark.asyncio
async def test_repeated_condense_only_folds_in_whats_newly_fallen_out(condenser_db, monkeypatch):
    agent = _agent()
    for i in range(7):
        database.save_message("s1", "user", f"msg {i}")
    summarize = AsyncMock(return_value="Summary v1")
    monkeypatch.setattr(condenser, "_summarize_chunk", summarize)
    await condenser.maybe_condense(agent, "s1")

    # Only 2 more messages arrived -- not enough new fallout yet (trigger_extra=3).
    for i in range(7, 9):
        database.save_message("s1", "user", f"msg {i}")
    await condenser.maybe_condense(agent, "s1")
    assert summarize.await_count == 1

    # Enough new fallout has now accumulated.
    for i in range(9, 12):
        database.save_message("s1", "user", f"msg {i}")
    summarize.return_value = "Summary v2"
    await condenser.maybe_condense(agent, "s1")

    assert summarize.await_count == 2
    _agent_arg, previous_summary_arg, new_messages_arg = summarize.await_args.args
    assert previous_summary_arg == "Summary v1"  # folds forward, doesn't re-summarize from scratch
    assert len(new_messages_arg) > 0

    assert database.get_condensed_summary("s1")["summary"] == "Summary v2"


@pytest.mark.asyncio
async def test_get_history_prepends_summary_when_enabled(condenser_db, monkeypatch):
    from backend.agent import JarvisAgent

    for i in range(7):
        database.save_message("s2", "user", f"msg {i}")

    agent = JarvisAgent()
    agent.condenser_enabled = True
    agent.max_history_len = 4
    agent.condense_trigger_extra = 3
    monkeypatch.setattr(condenser, "_summarize_chunk", AsyncMock(return_value="Folded summary"))

    history = await agent.get_history("s2")

    assert history[0]["role"] == "system"
    assert "Folded summary" in history[0]["content"]
    assert len(history) == 1 + 4  # summary + last 4 verbatim messages
