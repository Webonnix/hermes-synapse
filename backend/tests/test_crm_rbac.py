"""RBAC and route wiring for the CRM endpoints: the sales pipeline (deals),
follow-ups (tasks) and client notes.

Same style as test_clients_rbac.py — routes called directly as async
functions, because what matters is that the *backend* refuses, not that a
button happens to be hidden.
"""

import pytest
from fastapi import HTTPException

from backend import (
    auth, client_activity, client_notes, client_tasks, clients, currency, database, deals, main, permissions,
)


class _Request:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def api_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "crm.db")
    for module in (database, currency, clients, client_activity, deals, client_tasks, client_notes):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (currency, clients, client_activity, deals, client_tasks, client_notes):
        module.reset_schema_cache()
    monkeypatch.setattr(auth, "active_sessions", set())
    monkeypatch.setattr(auth, "session_roles", {})
    database.init_db()
    return db_path


def _session(role: str) -> _Request:
    return _Request(auth.create_session(role))


# ── permission registry ──────────────────────────────────────────────────────

def test_manager_holds_full_crm_access():
    granted = permissions.permissions_for("manager")
    for permission in (
        permissions.CLIENTS_DEALS_VIEW, permissions.CLIENTS_DEALS_EDIT,
        permissions.CLIENTS_TASKS_VIEW, permissions.CLIENTS_TASKS_EDIT,
        permissions.CLIENTS_NOTES_VIEW, permissions.CLIENTS_NOTES_EDIT,
    ):
        assert permission in granted


def test_operator_gets_tasks_and_notes_but_not_the_pipeline():
    granted = permissions.permissions_for("operator")
    assert permissions.CLIENTS_TASKS_EDIT in granted
    assert permissions.CLIENTS_NOTES_EDIT in granted
    assert permissions.CLIENTS_DEALS_VIEW not in granted
    assert permissions.CLIENTS_DEALS_EDIT not in granted


def test_viewer_is_read_only_across_the_board():
    granted = permissions.permissions_for("viewer")
    for view_permission in (
        permissions.CLIENTS_DEALS_VIEW, permissions.CLIENTS_TASKS_VIEW, permissions.CLIENTS_NOTES_VIEW,
    ):
        assert view_permission in granted
    for edit_permission in (
        permissions.CLIENTS_DEALS_EDIT, permissions.CLIENTS_TASKS_EDIT, permissions.CLIENTS_NOTES_EDIT,
    ):
        assert edit_permission not in granted


# ── deals: access control ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_cannot_even_list_deals(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.list_deals_api(_session("operator"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_viewer_can_list_but_not_create_a_deal(api_db):
    result = await main.list_deals_api(_session("viewer"))
    assert result == []
    with pytest.raises(HTTPException) as exc:
        await main.create_deal_api(main.DealRequest(title="x"), _session("viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_manager_can_create_and_list_a_deal(api_db):
    created = await main.create_deal_api(main.DealRequest(title="Сайт для Zenith"), _session("manager"))
    assert created["title"] == "Сайт для Zenith"
    listed = await main.list_deals_api(_session("manager"))
    assert len(listed) == 1


# ── deals: route wiring and error mapping ────────────────────────────────────

@pytest.mark.asyncio
async def test_bad_input_on_create_deal_is_400(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.create_deal_api(main.DealRequest(title=""), _session("manager"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_deal_is_404(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.get_deal_api("deal-nope", _session("manager"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_stage_move_without_a_lost_reason_is_400(api_db):
    created = await main.create_deal_api(main.DealRequest(title="x"), _session("manager"))
    with pytest.raises(HTTPException) as exc:
        await main.update_deal_api(created["id"], main.DealRequest(stage="LOST"), _session("manager"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_pipeline_summary_route(api_db):
    created = await main.create_deal_api(
        main.DealRequest(title="x", value="500", currency="USD"), _session("manager"),
    )
    await main.update_deal_api(created["id"], main.DealRequest(stage="WON"), _session("manager"))
    summary = await main.deals_pipeline_api(_session("manager"))
    assert summary["wonCount"] == 1


@pytest.mark.asyncio
async def test_convert_to_client_requires_both_deals_edit_and_clients_create(api_db):
    created = await main.create_deal_api(main.DealRequest(title="Acme"), _session("manager"))
    # 'viewer' has neither permission — confirms the route enforces both.
    with pytest.raises(HTTPException) as exc:
        await main.convert_deal_api(created["id"], main.ClientRequest(name="Acme"), _session("viewer"))
    assert exc.value.status_code == 403

    result = await main.convert_deal_api(created["id"], main.ClientRequest(name="Acme"), _session("manager"))
    assert result["deal"]["stage"] == "WON"
    assert result["client"]["name"] == "Acme"


@pytest.mark.asyncio
async def test_delete_a_converted_deal_is_400_not_500(api_db):
    created = await main.create_deal_api(main.DealRequest(title="Acme"), _session("manager"))
    await main.convert_deal_api(created["id"], main.ClientRequest(name="Acme"), _session("manager"))
    with pytest.raises(HTTPException) as exc:
        await main.delete_deal_api(created["id"], _session("manager"))
    assert exc.value.status_code == 400


# ── tasks ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_can_manage_tasks(api_db):
    created = await main.create_client_task_api(main.ClientTaskRequest(title="Позвонить"), _session("operator"))
    assert created["status"] == "OPEN"
    done = await main.complete_client_task_api(created["id"], _session("operator"))
    assert done["status"] == "DONE"
    stats = await main.client_task_stats_api(_session("operator"))
    assert stats["open"] == 0


@pytest.mark.asyncio
async def test_viewer_cannot_create_a_task(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.create_client_task_api(main.ClientTaskRequest(title="x"), _session("viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_task_actions_are_404(api_db):
    for coro in (
        main.complete_client_task_api("task-nope", _session("manager")),
        main.reopen_client_task_api("task-nope", _session("manager")),
        main.cancel_client_task_api("task-nope", _session("manager")),
    ):
        with pytest.raises(HTTPException) as exc:
            await coro
        assert exc.value.status_code == 404


# ── notes ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_can_write_and_read_notes(api_db):
    created = await main.create_client_note_api(
        "cli-1", main.ClientNoteRequest(body="Договорились созвониться"), _session("operator"),
    )
    assert created["body"] == "Договорились созвониться"
    listed = await main.list_client_notes_api("cli-1", _session("operator"))
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_viewer_cannot_write_a_note(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.create_client_note_api("cli-1", main.ClientNoteRequest(body="x"), _session("viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_deleting_a_missing_note_is_404(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.delete_client_note_api("note-nope", _session("manager"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_an_empty_note_body_is_400_not_500(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.create_client_note_api("cli-1", main.ClientNoteRequest(body="   "), _session("manager"))
    assert exc.value.status_code == 400
