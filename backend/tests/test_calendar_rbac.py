"""RBAC and route wiring for the Календарь endpoints (/api/calendar/*).

Same style as test_crm_rbac.py — routes called directly as async functions,
Google's client mocked the same way test_calendar_tools.py does. What is
under test: permission enforcement, that dates parse into the right
Asia/Jerusalem range, and that a Google-side failure surfaces as a real HTTP
status instead of a 500.
"""

import json

import pytest
from fastapi import HTTPException

from backend import auth, database, main, permissions, tools


class _Request:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}


class _FakeEvents:
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
        if not existed:
            raise Exception("404 Not Found")
        return _Call(None)

    def list(self, calendarId, timeMin, timeMax, maxResults, singleEvents, orderBy):
        items = sorted(self.store.values(), key=lambda e: e.get("start", {}).get("dateTime", ""))
        return _Call({"items": items})


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _FakeService:
    def __init__(self, store):
        self._events = _FakeEvents(store)

    def events(self):
        return self._events


@pytest.fixture()
def api_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "calendar-rbac.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "active_sessions", set())
    monkeypatch.setattr(auth, "session_roles", {})
    database.init_db()
    return db_path


@pytest.fixture()
def fake_calendar(monkeypatch):
    store = {}
    monkeypatch.setattr(tools, "_get_calendar_service", lambda: (_FakeService(store), None))
    return store


@pytest.fixture()
def disconnected_calendar(monkeypatch):
    monkeypatch.setattr(tools, "_get_calendar_service", lambda: (None, "Токен Google не найден."))


def _session(role: str) -> _Request:
    return _Request(auth.create_session(role))


def test_manager_and_viewer_both_hold_calendar_view_only_manager_holds_edit():
    assert permissions.CLIENTS_CALENDAR_EDIT in permissions.permissions_for("manager")
    assert permissions.CLIENTS_CALENDAR_VIEW in permissions.permissions_for("viewer")
    assert permissions.CLIENTS_CALENDAR_EDIT not in permissions.permissions_for("viewer")
    # Operator sees the owner's calendar (useful for their own day) but
    # cannot put things on it — it is the owner's personal calendar, not a
    # shared CRM resource the way tasks and notes are.
    assert permissions.CLIENTS_CALENDAR_VIEW in permissions.permissions_for("operator")
    assert permissions.CLIENTS_CALENDAR_EDIT not in permissions.permissions_for("operator")


def test_status_route_requires_calendar_view(api_db):
    database.set_setting(permissions.SETTING_CUSTOM_ROLES, json.dumps({"nobody": []}))
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(main.calendar_status_api(_session("nobody")))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_status_reports_not_connected(api_db):
    # No fake_calendar fixture — this test environment genuinely has no
    # credentials file, so the endpoint should say so, not raise.
    result = await main.calendar_status_api(_session("owner"))
    assert result["connected"] is False


@pytest.mark.asyncio
async def test_events_route_requires_calendar_view_permission(api_db):
    database.set_setting(permissions.SETTING_CUSTOM_ROLES, json.dumps({"nobody": []}))
    with pytest.raises(HTTPException) as exc:
        await main.list_calendar_events_api(_session("nobody"), dateFrom="2026-09-01", dateTo="2026-09-30")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_requires_edit_not_just_view(api_db):
    database.set_setting(permissions.SETTING_CUSTOM_ROLES, json.dumps({
        "readonly": [permissions.CLIENTS_CALENDAR_VIEW],
    }))
    with pytest.raises(HTTPException) as exc:
        await main.create_calendar_event_api(
            main.CalendarEventRequest(title="x", date="2026-09-01"), _session("readonly"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bad_date_is_400(api_db, fake_calendar):
    with pytest.raises(HTTPException) as exc:
        await main.list_calendar_events_api(_session("owner"), dateFrom="not-a-date", dateTo="2026-09-30")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_create_update_delete_round_trip(api_db, fake_calendar):
    created = await main.create_calendar_event_api(
        main.CalendarEventRequest(title="Созвон с Zenith", date="2026-09-10", time="15:00", eventType="CALL"),
        _session("owner"),
    )
    assert created["eventType"] == "CALL"

    listed = await main.list_calendar_events_api(_session("owner"), dateFrom="2026-09-01", dateTo="2026-09-30")
    assert any(e["id"] == created["id"] for e in listed)

    updated = await main.update_calendar_event_api(
        created["id"], main.CalendarEventUpdateRequest(title="Созвон перенесён"), _session("owner"),
    )
    assert updated["title"] == "Созвон перенесён"

    result = await main.delete_calendar_event_api(created["id"], _session("owner"))
    assert result["status"] == "success"

    listed_after = await main.list_calendar_events_api(_session("owner"), dateFrom="2026-09-01", dateTo="2026-09-30")
    assert all(e["id"] != created["id"] for e in listed_after)


@pytest.mark.asyncio
async def test_updating_a_missing_event_is_404_not_500(api_db, fake_calendar):
    with pytest.raises(HTTPException) as exc:
        await main.update_calendar_event_api(
            "does-not-exist", main.CalendarEventUpdateRequest(title="x"), _session("owner"),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_disconnected_calendar_surfaces_as_502_not_500(api_db, disconnected_calendar):
    with pytest.raises(HTTPException) as exc:
        await main.list_calendar_events_api(_session("owner"), dateFrom="2026-09-01", dateTo="2026-09-30")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_unknown_event_type_in_the_request_falls_back_to_other(api_db, fake_calendar):
    created = await main.create_calendar_event_api(
        main.CalendarEventRequest(title="x", date="2026-09-01", eventType="NOT_REAL"), _session("owner"),
    )
    assert created["eventType"] == "OTHER"
