"""Durable run journal: write-ahead turns, lifecycle, and crash reconciliation."""

import pytest

from backend import database, run_journal


@pytest.fixture()
def journal_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "runs.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_backend", None, raising=False)
    database.init_db()
    yield db_path
    monkeypatch.setattr(database, "_backend", None, raising=False)


def test_submission_is_durable_before_the_agent_runs(journal_db):
    trace_id = run_journal.record_submitted(
        "run1", "dashboard", user_text="посчитай что-нибудь", model="qwen", provider="llamacpp"
    )
    run = run_journal.get_run("run1")
    assert run["status"] == run_journal.STATUS_SUBMITTED
    assert run["chat_id"] == "dashboard"
    assert run["trace_id"] == trace_id
    assert run["model"] == "qwen"
    # The turn is queryable as in-flight from the instant it is accepted.
    assert [item["run_id"] for item in run_journal.list_open_runs()] == ["run1"]


def test_lifecycle_advances_to_completed_with_cost_and_tokens(journal_db):
    run_journal.record_submitted("run1", "dashboard", user_text="hi")
    run_journal.record_started("run1", model="qwen", provider="llamacpp")
    assert run_journal.get_run("run1")["status"] == run_journal.STATUS_RUNNING

    run_journal.record_finished(
        "run1",
        run_journal.STATUS_COMPLETED,
        meta={"input_tokens": 120, "output_tokens": 40, "latency_ms": 900,
              "finish_reason": "stop", "model": "qwen", "provider": "llamacpp"},
        cost_usd=0.0042,
        last_seq=17,
    )
    run = run_journal.get_run("run1")
    assert run["status"] == run_journal.STATUS_COMPLETED
    assert run["input_tokens"] == 120 and run["output_tokens"] == 40
    assert run["cost_usd"] == pytest.approx(0.0042)
    assert run["last_seq"] == 17
    assert run_journal.list_open_runs() == []


def test_closing_a_run_drops_the_stored_user_turn(journal_db):
    """The journal keeps the submitted text only while it is at risk. Once the
    run is closed the messages table owns the transcript, and a second copy of
    user content with no owner is exactly what the audit says not to keep."""
    run_journal.record_submitted("run1", "dashboard", user_text="секретный текст")
    rows = run_journal._query("SELECT user_text FROM agent_runs WHERE run_id = ?", ("run1",))
    assert rows[0][0] == "секретный текст"

    run_journal.record_finished("run1", run_journal.STATUS_COMPLETED)
    rows = run_journal._query("SELECT user_text FROM agent_runs WHERE run_id = ?", ("run1",))
    assert rows[0][0] is None


def test_error_and_cancellation_are_distinguishable(journal_db):
    run_journal.record_submitted("run_err", "dashboard")
    run_journal.record_finished("run_err", run_journal.STATUS_ERROR, error_code="handler_exception")
    run_journal.record_submitted("run_cancel", "dashboard")
    run_journal.record_finished("run_cancel", run_journal.STATUS_CANCELLED)

    assert run_journal.get_run("run_err")["error_code"] == "handler_exception"
    assert run_journal.get_run("run_cancel")["status"] == run_journal.STATUS_CANCELLED
    assert run_journal.get_run("run_cancel")["error_code"] is None


def test_restart_closes_out_runs_that_were_in_flight(journal_db):
    """The exact failure this exists for: the process dies mid-turn. A run left
    'running' cannot still be executing, and leaving it open is what kept the UI
    waiting on an answer that would never arrive."""
    run_journal.record_submitted("run_live", "dashboard", user_text="long job")
    run_journal.record_started("run_live")
    run_journal.record_submitted("run_done", "dashboard")
    run_journal.record_finished("run_done", run_journal.STATUS_COMPLETED)

    reconciled = run_journal.reconcile_interrupted_runs()

    assert [item["run_id"] for item in reconciled] == ["run_live"]
    assert run_journal.get_run("run_live")["status"] == run_journal.STATUS_INTERRUPTED
    assert run_journal.get_run("run_live")["error_code"] == "process_restart"
    # A completed run is untouched.
    assert run_journal.get_run("run_done")["status"] == run_journal.STATUS_COMPLETED
    # Idempotent: a second clean startup is a no-op.
    assert run_journal.reconcile_interrupted_runs() == []


def test_events_are_ordered_and_carry_their_payload(journal_db):
    run_journal.record_submitted("run1", "dashboard")
    run_journal.record_started("run1", model="qwen")
    run_journal.record_finished("run1", run_journal.STATUS_COMPLETED)
    events = run_journal.get_run_events("run1")
    assert [event["kind"] for event in events] == ["submitted", "started", "completed"]
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert events[1]["payload"]["model"] == "qwen"


def test_runs_are_listable_per_chat(journal_db):
    run_journal.record_submitted("a", "chat_a")
    run_journal.record_submitted("b", "chat_b")
    assert [run["run_id"] for run in run_journal.list_runs("chat_a")] == ["a"]
    assert len(run_journal.list_runs()) == 2


def test_a_journal_outage_never_breaks_the_turn(journal_db, monkeypatch):
    """Best-effort by design: losing observability must not cost the user their
    message, so every write swallows its error after logging it."""
    def explode(*args, **kwargs):
        raise RuntimeError("disk is gone")

    monkeypatch.setattr(run_journal, "_execute", explode)
    monkeypatch.setattr(run_journal, "_query", explode)

    assert run_journal.record_submitted("run1", "dashboard") is not None
    run_journal.record_started("run1")
    run_journal.record_finished("run1", run_journal.STATUS_COMPLETED)
    run_journal.append_event("run1", "anything")
    assert run_journal.reconcile_interrupted_runs() == []


def test_prune_drops_old_runs_and_their_events(journal_db):
    run_journal.record_submitted("old", "dashboard")
    run_journal._execute(
        "UPDATE agent_runs SET submitted_at = ? WHERE run_id = ?", ("2020-01-01T00:00:00+00:00", "old")
    )
    run_journal.record_submitted("fresh", "dashboard")

    assert run_journal.prune(max_age_days=30) == 1
    assert run_journal.get_run("old") is None
    assert run_journal.get_run_events("old") == []
    assert run_journal.get_run("fresh") is not None


def test_schema_is_rendered_for_both_dialects_from_one_definition():
    """The SQLite and PostgreSQL schemas come from the same source, so they
    cannot drift the way two hand-copied CREATE TABLE blocks do."""
    sqlite_ddl = "\n".join(run_journal.schema_statements("sqlite"))
    postgres_ddl = "\n".join(run_journal.schema_statements("postgres"))
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in sqlite_ddl
    assert "SERIAL PRIMARY KEY" in postgres_ddl
    # Everything except the serial-column dialect is identical.
    assert sqlite_ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "<serial>") == \
        postgres_ddl.replace("SERIAL PRIMARY KEY", "<serial>")
