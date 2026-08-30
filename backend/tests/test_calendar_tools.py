"""Google Calendar integration (backend/tools.py).

The real Google API client is never touched here — `_get_calendar_service`
is monkeypatched to a small fake that records what it was called with and
plays back canned responses, the same shape `googleapiclient` returns. What
is under test is *our* logic: colour↔type mapping, timezone handling, and —
the part most likely to hide a bug — that updating an event's start time
without touching its end carries the original duration forward instead of
silently truncating it to an hour.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend import tools

TZ = ZoneInfo(tools.CALENDAR_LOCAL_TZ_NAME)


class _FakeEvents:
    """Stands in for `service.events()`. Each chained call
    (`.insert(...)`, `.get(...)`, ...) returns an object whose `.execute()`
    plays back (or records into) `store`, keyed by event id."""

    def __init__(self, store):
        self.store = store
        self._next_id = 100

    def insert(self, calendarId, body):
        event_id = f"evt-{self._next_id}"
        self._next_id += 1
        stored = {**body, "id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"}
        self.store[event_id] = stored
        return _Call(stored)

    def get(self, calendarId, eventId):
        if eventId not in self.store:
            raise Exception("404 Not Found")
        return _Call(self.store[eventId])

    def update(self, calendarId, eventId, body):
        self.store[eventId] = {**body, "id": eventId, "htmlLink": f"https://calendar.google.com/{eventId}"}
        return _Call(self.store[eventId])

    def delete(self, calendarId, eventId):
        existed = eventId in self.store
        self.store.pop(eventId, None)
        return _Call(None) if existed else _RaisingCall(Exception("404 Not Found"))

    def list(self, calendarId, timeMin, timeMax, maxResults, singleEvents, orderBy):
        items = sorted(self.store.values(), key=lambda e: e.get("start", {}).get("dateTime", ""))
        return _Call({"items": items})


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _RaisingCall:
    def __init__(self, exc):
        self.exc = exc

    def execute(self):
        raise self.exc


class _FakeService:
    def __init__(self, store):
        self._events = _FakeEvents(store)

    def events(self):
        return self._events


@pytest.fixture()
def fake_calendar(monkeypatch):
    store = {}
    monkeypatch.setattr(tools, "_get_calendar_service", lambda: (_FakeService(store), None))
    return store


def test_calendar_connection_status_reports_missing_credentials():
    # No fixture here on purpose — this exercises the real filesystem check,
    # and this test environment genuinely has no credentials file either way.
    status = tools.calendar_connection_status()
    assert status["connected"] is False
    assert status["reason"] in ("no_credentials", "no_token", "auth_error")


def test_create_event_stores_the_requested_color(fake_calendar):
    event, err = tools._create_event("Созвон с Иваном", "2026-09-01", "15:00", event_type="CALL")
    assert err is None
    assert event["eventType"] == "CALL"
    assert event["title"] == "Созвон с Иваном"


def test_create_event_defaults_to_other_and_one_hour_duration(fake_calendar):
    event, err = tools._create_event("Без типа", "2026-09-01", "10:00")
    assert err is None
    assert event["eventType"] == "OTHER"
    start = datetime.fromisoformat(event["start"])
    end = datetime.fromisoformat(event["end"])
    assert (end - start).total_seconds() == 3600


def test_create_event_respects_explicit_end_time(fake_calendar):
    event, _ = tools._create_event("Встреча", "2026-09-01", "14:00", end_time="14:30")
    start = datetime.fromisoformat(event["start"])
    end = datetime.fromisoformat(event["end"])
    assert (end - start).total_seconds() == 1800


def test_create_event_rolls_an_end_time_before_start_to_the_next_day(fake_calendar):
    """An overnight event: end_time '01:00' is earlier in the clock than
    start '23:00' on the same calendar date, so it means the next day."""
    event, _ = tools._create_event("Ночная смена", "2026-09-01", "23:00", end_time="01:00")
    start = datetime.fromisoformat(event["start"])
    end = datetime.fromisoformat(event["end"])
    assert end.date() == start.date().replace(day=start.day + 1)


def test_add_calendar_event_tool_wrapper_returns_created_status(fake_calendar):
    result = json.loads(tools.add_calendar_event("Обучение", "2026-09-01", event_type="TRAINING"))
    assert result["status"] == "created"
    assert "event_id" in result and "link" in result


def test_unknown_event_type_falls_back_to_other(fake_calendar):
    result = json.loads(tools.add_calendar_event("X", "2026-09-01", event_type="NOT_A_TYPE"))
    assert result["status"] == "created"
    stored = fake_calendar[result["event_id"]]
    assert stored["colorId"] == tools.CALENDAR_EVENT_COLORS["OTHER"]


def test_list_events_maps_color_back_to_event_type(fake_calendar):
    tools._create_event("Дедлайн проекта", "2026-09-02", "18:00", event_type="DEADLINE")
    events, err = tools._list_events(
        datetime(2026, 9, 1, tzinfo=TZ), datetime(2026, 9, 3, tzinfo=TZ),
    )
    assert err is None
    assert len(events) == 1
    assert events[0]["eventType"] == "DEADLINE"


def test_get_calendar_events_tool_wrapper_says_nothing_planned(fake_calendar):
    result = json.loads(tools.get_calendar_events(days_ahead=0))
    assert result["events"] == []
    assert "message" in result


def test_update_event_moving_only_the_start_time_preserves_duration(fake_calendar):
    """The regression this whole module exists to prevent: editing when a
    90-minute meeting starts must not quietly shrink it to the default hour."""
    created, _ = tools._create_event("Долгая встреча", "2026-09-01", "10:00", duration_minutes=90)
    updated, err = tools._update_event(created["id"], time="11:00")
    assert err is None
    start = datetime.fromisoformat(updated["start"])
    end = datetime.fromisoformat(updated["end"])
    assert start.strftime("%H:%M") == "11:00"
    assert (end - start).total_seconds() == 90 * 60


def test_update_event_can_change_just_the_title(fake_calendar):
    created, _ = tools._create_event("Старое имя", "2026-09-01", "10:00")
    updated, err = tools._update_event(created["id"], title="Новое имя")
    assert err is None
    assert updated["title"] == "Новое имя"
    # Untouched fields survive the round trip.
    assert updated["start"] == created["start"]


def test_update_event_can_change_the_type_color(fake_calendar):
    created, _ = tools._create_event("Событие", "2026-09-01", "10:00", event_type="OTHER")
    updated, err = tools._update_event(created["id"], event_type="MEETING")
    assert err is None
    assert updated["eventType"] == "MEETING"


def test_update_missing_event_is_an_error_not_a_crash(fake_calendar):
    event, err = tools._update_event("does-not-exist", title="x")
    assert event is None
    assert err is not None


def test_delete_event_removes_it(fake_calendar):
    created, _ = tools._create_event("Отменяемая встреча", "2026-09-01", "10:00")
    ok, err = tools._delete_event(created["id"])
    assert ok is True
    assert err is None
    assert created["id"] not in fake_calendar


def test_deleting_an_already_gone_event_is_treated_as_success(fake_calendar):
    """A retried chat message ('удали событие') should not surface an error
    for the second attempt just because the first one already worked."""
    ok, err = tools._delete_event("never-existed")
    assert ok is True
    assert err is None


def test_delete_calendar_event_tool_wrapper(fake_calendar):
    created, _ = tools._create_event("X", "2026-09-01", "10:00")
    result = json.loads(tools.delete_calendar_event(created["id"]))
    assert result["status"] == "deleted"
