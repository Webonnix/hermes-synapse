"""RBAC and financial privacy on the clients API (§62, §63, §88).

The routes are called directly as async functions, same style as
test_agent_admin_endpoints.py. What is under test is that the *backend*
refuses — a frontend that hides a button proves nothing.
"""

import pytest
from fastapi import HTTPException

from backend import auth, client_activity, client_billing, clients, currency, database, main, permissions


class _Request:
    """Minimal stand-in for a FastAPI Request: the permission layer only ever
    reads the Authorization header off it."""

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def api_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rbac.db")
    for module in (database, currency, clients, client_billing, client_activity):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (currency, clients, client_billing, client_activity):
        module.reset_schema_cache()
    monkeypatch.setattr(auth, "active_sessions", set())
    monkeypatch.setattr(auth, "session_roles", {})
    database.init_db()
    client_billing._init_schema()
    return db_path


def _session(role: str) -> _Request:
    return _Request(auth.create_session(role))


@pytest.fixture()
def seeded(api_db):
    return clients.create_client({
        "name": "Midot Project",
        "contact": {"name": "Иван", "email": "ivan@midot.com", "isPrimary": True},
        "service": {"serviceName": "Разработка", "serviceType": "RECURRING",
                    "amount": "1000", "currency": "USD", "frequency": "MONTHLY"},
    })


# ── the role model ───────────────────────────────────────────────────────────

def test_owner_holds_every_permission():
    assert permissions.permissions_for("owner") == set(permissions.ALL_PERMISSIONS)


def test_a_session_without_a_role_is_the_owner():
    """Existing logins must keep working exactly as before roles existed."""
    token = auth.create_session()
    assert auth.role_for_session(token) == "owner"
    assert permissions.has_permission(auth.role_for_session(token), permissions.CLIENTS_CREATE)


def test_an_unknown_role_holds_nothing():
    assert permissions.permissions_for("some-role-that-was-deleted") == set()


def test_operator_sees_clients_but_no_money():
    granted = permissions.permissions_for("operator")
    assert permissions.CLIENTS_VIEW in granted
    assert permissions.CLIENTS_FINANCIALS_VIEW not in granted
    assert permissions.CLIENTS_INVOICES_VIEW not in granted


# ── currency settings ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_viewer_cannot_change_the_exchange_rate(api_db):
    payload = main.CurrencySettingsRequest(rates={"BYN": "9.99"})
    with pytest.raises(HTTPException) as exc:
        await main.update_currency_settings_api(payload, _session("viewer"))
    assert exc.value.status_code == 403
    assert currency.get_rates()["BYN"] != 9.99


@pytest.mark.asyncio
async def test_the_owner_can_change_the_exchange_rate(api_db):
    payload = main.CurrencySettingsRequest(rates={"BYN": "3.31"}, displayCurrency="BYN")
    result = await main.update_currency_settings_api(payload, _session("owner"))
    assert result["rates"]["BYN"] == "3.31"
    assert result["displayCurrency"] == "BYN"


@pytest.mark.asyncio
async def test_an_invalid_rate_is_a_400_not_a_500(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.update_currency_settings_api(
            main.CurrencySettingsRequest(rates={"BYN": "-1"}), _session("owner")
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_a_rate_change_records_who_did_it(api_db):
    await main.update_currency_settings_api(
        main.CurrencySettingsRequest(rates={"BYN": "3.33"}), _session("owner")
    )
    event = client_activity.list_events(event_type=client_activity.CURRENCY_RATE_CHANGED)[0]
    assert event["actor_id"] == "owner"


@pytest.mark.asyncio
async def test_a_manager_reads_rates_but_cannot_set_them(api_db):
    """Deliberate: the FX table moves every client's reported revenue at once,
    so editing it stays with the owner even though a manager runs the book."""
    assert await main.get_currency_settings_api(_session("manager"))
    with pytest.raises(HTTPException) as exc:
        await main.update_currency_settings_api(
            main.CurrencySettingsRequest(rates={"BYN": "9.99"}), _session("manager")
        )
    assert exc.value.status_code == 403


# ── financial privacy (§63) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_role_without_financials_gets_no_amounts_in_the_list(api_db, seeded):
    result = await main.list_clients_api(_session("operator"))
    row = result["items"][0]
    assert row["name"] == "Midot Project"
    # Not merely hidden in the UI — absent from the payload.
    assert "primaryAmountUsd" not in row
    assert "primaryAmountDisplay" not in row
    assert "paymentStatus" not in row
    assert "amount" not in row["primaryService"]


@pytest.mark.asyncio
async def test_a_role_with_financials_does_get_amounts(api_db, seeded):
    result = await main.list_clients_api(_session("manager"))
    row = result["items"][0]
    assert row["primaryAmountUsd"] == "1000.00"
    assert row["primaryService"]["amount"] == "1000"


@pytest.mark.asyncio
async def test_the_client_card_is_redacted_too(api_db, seeded):
    card = await main.get_client_api(seeded["id"], _session("operator"))
    assert card["name"] == "Midot Project"
    assert "amount" not in card["services"][0]


@pytest.mark.asyncio
async def test_the_dashboard_is_refused_without_financials(api_db, seeded):
    with pytest.raises(HTTPException) as exc:
        await main.clients_dashboard_api(_session("operator"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_invoices_are_refused_without_financials(api_db, seeded):
    with pytest.raises(HTTPException) as exc:
        await main.list_client_invoices_api(_session("operator"))
    assert exc.value.status_code == 403


# ── mutations ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_viewer_cannot_create_a_client(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.create_client_api(main.ClientRequest(name="X"), _session("viewer"))
    assert exc.value.status_code == 403
    assert clients.count_clients()["total"] == 0


@pytest.mark.asyncio
async def test_a_viewer_cannot_create_an_invoice(api_db, seeded):
    payload = main.ClientInvoiceRequest(clientId=seeded["id"], amount="100", sourceCurrency="USD")
    with pytest.raises(HTTPException) as exc:
        await main.create_client_invoice_api(payload, _session("viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_operator_cannot_register_a_payment(api_db, seeded):
    payload = main.ClientPaymentRequest(clientId=seeded["id"], amount="100", currency="USD")
    with pytest.raises(HTTPException) as exc:
        await main.create_client_payment_api(payload, _session("operator"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_viewer_cannot_connect_an_agent(api_db, seeded):
    payload = main.AgentConnectionRequest(agentId="jarvis")
    with pytest.raises(HTTPException) as exc:
        await main.connect_client_agent_api(seeded["id"], payload, _session("viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_operator_may_connect_an_agent(api_db, seeded):
    """Support staff manage delivery; they just never see the price."""
    agents = clients.list_agents_for_picker()
    connection = await main.connect_client_agent_api(
        seeded["id"], main.AgentConnectionRequest(agentId=agents[0]["id"]), _session("operator")
    )
    assert connection["agentId"] == agents[0]["id"]


@pytest.mark.asyncio
async def test_a_viewer_cannot_archive_a_client(api_db, seeded):
    with pytest.raises(HTTPException) as exc:
        await main.archive_client_api(seeded["id"], _session("viewer"))
    assert exc.value.status_code == 403
    assert clients.get_client(seeded["id"])["status"] == "ACTIVE"


# ── error mapping ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_missing_client_is_404(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.get_client_api("cli-nope", _session("owner"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_duplicate_billing_period_is_409(api_db, seeded):
    service_id = seeded["services"][0]["id"]
    payload = main.ClientInvoiceRequest(
        clientId=seeded["id"], amount="100", sourceCurrency="USD",
        clientServiceId=service_id, billingPeriod="2026-09",
    )
    await main.create_client_invoice_api(payload, _session("owner"))
    with pytest.raises(HTTPException) as exc:
        await main.create_client_invoice_api(payload, _session("owner"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_bad_input_is_400(api_db):
    with pytest.raises(HTTPException) as exc:
        await main.create_client_api(main.ClientRequest(name="   "), _session("owner"))
    assert exc.value.status_code == 400
