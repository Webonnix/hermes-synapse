"""Follow-ups: due dates, the overdue flag, and status transitions."""

from datetime import datetime, timedelta, timezone

import pytest

from backend import client_activity, client_tasks, database


@pytest.fixture()
def tasks_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tasks.db")
    for module in (database, client_activity, client_tasks):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (client_activity, client_tasks):
        module.reset_schema_cache()
    database.init_db()
    return db_path


def _iso_date(days_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).date().isoformat()


def test_create_requires_a_title(tasks_db):
    with pytest.raises(ValueError):
        client_tasks.create_task({"title": ""})


def test_create_defaults_to_open_and_normal_priority(tasks_db):
    task = client_tasks.create_task({"title": "Позвонить"})
    assert task["status"] == "OPEN"
    assert task["priority"] == "NORMAL"
    assert task["completedAt"] is None


def test_a_past_due_date_marks_the_task_overdue(tasks_db):
    task = client_tasks.create_task({"title": "Позвонить", "dueAt": _iso_date(-3)})
    assert task["overdue"] is True


def test_a_future_due_date_is_not_overdue(tasks_db):
    task = client_tasks.create_task({"title": "Позвонить", "dueAt": _iso_date(3)})
    assert task["overdue"] is False


def test_no_due_date_is_never_overdue(tasks_db):
    task = client_tasks.create_task({"title": "Когда-нибудь"})
    assert task["overdue"] is False


def test_a_done_task_is_never_overdue_even_if_its_due_date_passed(tasks_db):
    task = client_tasks.create_task({"title": "Позвонить", "dueAt": _iso_date(-3)})
    done = client_tasks.complete_task(task["id"])
    assert done["overdue"] is False
    assert done["status"] == "DONE"
    assert done["completedAt"] is not None


def test_reopen_clears_completed_at(tasks_db):
    task = client_tasks.create_task({"title": "x"})
    client_tasks.complete_task(task["id"])
    reopened = client_tasks.reopen_task(task["id"])
    assert reopened["status"] == "OPEN"
    assert reopened["completedAt"] is None


def test_cancel_is_distinct_from_done(tasks_db):
    task = client_tasks.create_task({"title": "x"})
    cancelled = client_tasks.cancel_task(task["id"])
    assert cancelled["status"] == "CANCELLED"


def test_missing_task_raises_keyerror(tasks_db):
    with pytest.raises(KeyError):
        client_tasks.update_task("task-nope", {"title": "x"})
    with pytest.raises(KeyError):
        client_tasks.delete_task("task-nope")


def test_editing_title_cannot_blank_it_out(tasks_db):
    task = client_tasks.create_task({"title": "x"})
    with pytest.raises(ValueError):
        client_tasks.update_task(task["id"], {"title": "  "})


def test_tasks_can_be_scoped_to_a_client_a_deal_both_or_neither(tasks_db):
    client_tasks.create_task({"title": "For client", "clientId": "cli-1"})
    client_tasks.create_task({"title": "For deal", "dealId": "deal-1"})
    client_tasks.create_task({"title": "For both", "clientId": "cli-1", "dealId": "deal-1"})
    client_tasks.create_task({"title": "Freestanding"})

    assert {t["title"] for t in client_tasks.list_tasks(client_id="cli-1")} == {"For client", "For both"}
    assert {t["title"] for t in client_tasks.list_tasks(deal_id="deal-1")} == {"For deal", "For both"}
    assert len(client_tasks.list_tasks()) == 4


def test_listing_puts_open_tasks_with_the_soonest_due_date_first(tasks_db):
    later = client_tasks.create_task({"title": "Later", "dueAt": _iso_date(10)})
    sooner = client_tasks.create_task({"title": "Sooner", "dueAt": _iso_date(1)})
    no_date = client_tasks.create_task({"title": "No date"})
    done = client_tasks.create_task({"title": "Done", "dueAt": _iso_date(-1)})
    client_tasks.complete_task(done["id"])

    ordered = [t["title"] for t in client_tasks.list_tasks()]
    assert ordered == ["Sooner", "Later", "No date", "Done"]


def test_status_filter(tasks_db):
    task = client_tasks.create_task({"title": "x"})
    client_tasks.complete_task(task["id"])
    client_tasks.create_task({"title": "y"})
    assert [t["title"] for t in client_tasks.list_tasks(status="DONE")] == ["x"]


def test_overdue_only_filter_excludes_done_and_future(tasks_db):
    overdue = client_tasks.create_task({"title": "overdue", "dueAt": _iso_date(-1)})
    client_tasks.create_task({"title": "future", "dueAt": _iso_date(1)})
    done_overdue = client_tasks.create_task({"title": "done", "dueAt": _iso_date(-1)})
    client_tasks.complete_task(done_overdue["id"])

    result = client_tasks.list_tasks(overdue_only=True)
    assert [t["id"] for t in result] == [overdue["id"]]


def test_stats_count_open_overdue_and_due_today(tasks_db):
    client_tasks.create_task({"title": "overdue", "dueAt": _iso_date(-2)})
    client_tasks.create_task({"title": "today", "dueAt": _iso_date(0)})
    client_tasks.create_task({"title": "future", "dueAt": _iso_date(5)})
    done = client_tasks.create_task({"title": "done", "dueAt": _iso_date(-2)})
    client_tasks.complete_task(done["id"])

    stats = client_tasks.task_stats()
    assert stats["open"] == 3
    assert stats["overdue"] == 1
    assert stats["dueToday"] == 1


def test_completing_a_task_logs_an_activity_event(tasks_db):
    task = client_tasks.create_task({"title": "x", "clientId": "cli-1"})
    client_tasks.complete_task(task["id"])
    events = client_activity.list_events(client_id="cli-1", entity_id=task["id"])
    assert any(event["event_type"] == client_activity.TASK_COMPLETED for event in events)


# ── daily_digest_text (the CRM's Telegram reminder) ──────────────────────────

def test_digest_is_none_when_nothing_is_due(tasks_db):
    client_tasks.create_task({"title": "future", "dueAt": _iso_date(10)})
    client_tasks.create_task({"title": "no date"})
    assert client_tasks.daily_digest_text() is None


def test_digest_is_none_for_a_done_task_even_if_its_due_date_passed(tasks_db):
    task = client_tasks.create_task({"title": "x", "dueAt": _iso_date(-1)})
    client_tasks.complete_task(task["id"])
    assert client_tasks.daily_digest_text() is None


def test_digest_separates_overdue_from_due_today(tasks_db):
    client_tasks.create_task({"title": "Позвонить Ивану", "dueAt": _iso_date(-2)})
    client_tasks.create_task({"title": "Отправить КП", "dueAt": _iso_date(0)})
    text = client_tasks.daily_digest_text()
    assert text is not None
    assert "Просрочено (1)" in text
    assert "Позвонить Ивану" in text
    assert "На сегодня (1)" in text
    assert "Отправить КП" in text


def test_digest_does_not_double_count_an_overdue_task_as_due_today(tasks_db):
    """An overdue task's due date is in the past, not today — it must only
    ever land in one section."""
    client_tasks.create_task({"title": "x", "dueAt": _iso_date(-3)})
    text = client_tasks.daily_digest_text()
    assert text.count("Просрочено") == 1
    assert "На сегодня" not in text


def test_digest_truncates_a_long_section_with_a_remainder_count(tasks_db):
    for index in range(15):
        client_tasks.create_task({"title": f"Задача {index}", "dueAt": _iso_date(0)})
    text = client_tasks.daily_digest_text(max_per_section=3)
    assert "На сегодня (15)" in text
    assert text.count("• Задача") == 3
    assert "…и ещё 12" in text
