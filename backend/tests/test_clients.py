"""Client book: CRUD, services, agent connection status, search and filters."""

from datetime import datetime, timedelta, timezone

import pytest

from backend import client_activity, client_billing, clients, currency, database


@pytest.fixture()
def clients_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "clients.db")
    for module in (database, currency, clients, client_billing, client_activity):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (currency, clients, client_billing, client_activity):
        module.reset_schema_cache()
    database.init_db()
    clients._init_schema()
    client_billing._init_schema()
    return db_path


def _agent(db, agent_id="agent-1", name="Агент #1", enabled=True, status="idle", last_error=""):
    """Inserts a subagent the way the rest of VEXA would, so connections point
    at a real row rather than a fixture stand-in."""
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subagents "
            "(id, name, system_prompt, model, is_enabled, status, last_error, updated_at) "
            "VALUES (?, ?, '', 'test', ?, ?, ?, ?)",
            (agent_id, name, 1 if enabled else 0, status, last_error,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return agent_id


def _client(**overrides):
    payload = {
        "name": "Midot Project", "projectName": "Веб-платформа",
        "contact": {"name": "Иван Петров", "email": "ivan@midot.com", "isPrimary": True},
        "service": {"serviceName": "Разработка", "serviceType": "RECURRING",
                    "amount": "750", "currency": "USD", "frequency": "MONTHLY", "billingDay": 1},
    }
    payload.update(overrides)
    return payload


# ── creation ─────────────────────────────────────────────────────────────────

def test_create_client_with_contact_and_service(clients_db):
    client = clients.create_client(_client())
    assert client["name"] == "Midot Project"
    assert client["status"] == "ACTIVE"
    assert client["primaryContact"]["email"] == "ivan@midot.com"
    assert client["primaryContactId"] == client["primaryContact"]["id"]
    service = client["services"][0]
    assert service["serviceType"] == "RECURRING"
    assert (service["amount"], service["currency"]) == ("750", "USD")
    assert service["billing"]["frequency"] == "MONTHLY"
    assert service["billing"]["nextBillingDate"]


def test_amount_and_currency_are_separate_fields(clients_db):
    """Never "750 USD" in one column — an amount that cannot be summed or
    converted is not a price."""
    client = clients.create_client(_client())
    service = client["services"][0]
    assert service["amount"] == "750"
    assert service["currency"] == "USD"


def test_client_name_is_required(clients_db):
    with pytest.raises(ValueError, match="Название клиента"):
        clients.create_client({"name": "  "})


def test_invalid_email_is_refused(clients_db):
    with pytest.raises(ValueError, match="email"):
        clients.create_client(_client(contact={"name": "X", "email": "not-an-email"}))


def test_billing_day_is_bounded(clients_db):
    with pytest.raises(ValueError, match="1..31"):
        clients.create_client(_client(service={
            "serviceName": "X", "serviceType": "RECURRING", "amount": "10",
            "currency": "USD", "frequency": "MONTHLY", "billingDay": 45,
        }))


def test_a_client_can_hold_several_services(clients_db):
    client = clients.create_client(_client())
    clients.add_client_service(client["id"], {
        "serviceName": "Поддержка", "serviceType": "RECURRING", "amount": "300",
        "currency": "BYN", "frequency": "QUARTERLY",
    })
    services = clients.list_client_services(client["id"])
    assert len(services) == 2
    assert {item["currency"] for item in services} == {"USD", "BYN"}


def test_one_time_service_has_no_next_billing_date(clients_db):
    """A one-off has no "next" — a date here would make the scheduler bill it
    again forever."""
    client = clients.create_client(_client(service={
        "serviceName": "Настройка", "serviceType": "ONE_TIME", "amount": "450", "currency": "USD",
    }))
    billing = client["services"][0]["billing"]
    assert billing["frequency"] == "ONE_TIME"
    assert billing["nextBillingDate"] is None


# ── update / archive ─────────────────────────────────────────────────────────

def test_update_client(clients_db):
    client = clients.create_client(_client())
    updated = clients.update_client(client["id"], {"name": "Midot", "status": "PAUSED"})
    assert (updated["name"], updated["status"]) == ("Midot", "PAUSED")


def test_archive_is_a_soft_delete(clients_db):
    client = clients.create_client(_client())
    archived = clients.archive_client(client["id"])
    assert archived["status"] == "ARCHIVED"
    assert archived["archivedAt"]
    # The row survives, because invoices point at it.
    assert clients.get_client(client["id"]) is not None
    assert client["id"] not in [item["id"] for item in clients.list_clients()["items"]]


def test_archiving_stops_the_agent_connections(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    clients.archive_client(client["id"])
    connection = clients.list_connections(client_id=client["id"])[0]
    assert connection["status"] == "DISCONNECTED"


# ── agent connections (§21, §29) ─────────────────────────────────────────────

def test_no_connection_reads_as_not_connected(clients_db):
    client = clients.create_client(_client())
    assert clients.list_connections(client_id=client["id"]) == []
    row = clients.list_clients()["items"][0]
    assert row["primaryConnection"] is None


def test_a_live_agent_is_connected(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    connection = clients.touch_connection(client["agentConnections"][0]["id"])
    assert connection["status"] == "CONNECTED"


def test_a_silent_agent_goes_offline(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    stale = (datetime.now(timezone.utc) - timedelta(minutes=clients.OFFLINE_THRESHOLD_MINUTES + 5))
    import sqlite3
    with sqlite3.connect(clients_db) as conn:
        conn.execute("UPDATE subagents SET updated_at = ? WHERE id = ?",
                     (stale.isoformat(timespec="seconds"), agent))
        conn.execute("DELETE FROM agent_events WHERE agent_id = ?", (agent,))
    connection = clients.touch_connection(
        client["agentConnections"][0]["id"], seen_at=stale.isoformat(timespec="seconds")
    )
    assert connection["status"] == "OFFLINE"


def test_an_error_beats_a_stale_heartbeat(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    connection = clients.update_connection(
        client["agentConnections"][0]["id"],
        {"errorCode": "TOKEN_INVALID", "errorMessage": "Токен отозван"},
    )
    assert connection["status"] == "ERROR"
    assert connection["errorCode"] == "TOKEN_INVALID"


def test_pausing_a_connection(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    connection = clients.update_connection(
        client["agentConnections"][0]["id"], {"desiredState": "PAUSED"}
    )
    assert connection["status"] == "PAUSED"


def test_a_disabled_agent_reads_as_paused(clients_db):
    agent = _agent(clients_db, enabled=False)
    client = clients.create_client(_client(agent={"agentId": agent}))
    assert clients.list_connections(client_id=client["id"])[0]["status"] == "PAUSED"


def test_disconnect_then_reconnect(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    connection_id = client["agentConnections"][0]["id"]

    disconnected = clients.disconnect_agent(connection_id)
    assert disconnected["status"] == "DISCONNECTED"
    assert disconnected["disconnectedAt"]

    clients.update_connection(connection_id, {"errorCode": "X", "errorMessage": "boom"})
    reconnected = clients.reconnect_agent(connection_id)
    # Reconnect clears the operator's off switch *and* the stale error.
    assert reconnected["errorCode"] is None
    assert reconnected["status"] in ("CONNECTED", "OFFLINE")


def test_health_check_clears_a_fixed_error(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    connection_id = client["agentConnections"][0]["id"]
    clients.update_connection(connection_id, {"errorCode": "OLD", "errorMessage": "прошлое"})
    checked = clients.health_check(connection_id)
    assert checked["errorCode"] is None
    assert checked["lastHealthCheckAt"]


def test_health_check_reports_the_agents_own_error(clients_db):
    agent = _agent(clients_db, last_error="rate limit exceeded")
    client = clients.create_client(_client(agent={"agentId": agent}))
    checked = clients.health_check(client["agentConnections"][0]["id"])
    assert checked["status"] == "ERROR"
    assert "rate limit" in checked["errorMessage"]


def test_connecting_an_unknown_agent_is_refused(clients_db):
    client = clients.create_client(_client())
    with pytest.raises(ValueError, match="Агент не найден"):
        clients.connect_agent(client["id"], "no-such-agent")


def test_a_client_can_have_several_agents(clients_db):
    first = _agent(clients_db, "agent-1", "Агент #1")
    second = _agent(clients_db, "agent-2", "Агент #2")
    client = clients.create_client(_client(agent={"agentId": first}))
    clients.connect_agent(client["id"], second)
    assert len(clients.list_connections(client_id=client["id"])) == 2


def test_connection_events_are_audited(clients_db):
    agent = _agent(clients_db)
    client = clients.create_client(_client(agent={"agentId": agent}))
    clients.disconnect_agent(client["agentConnections"][0]["id"])
    types = [event["event_type"] for event in client_activity.list_events(client_id=client["id"])]
    assert client_activity.AGENT_CONNECTED in types
    assert client_activity.AGENT_DISCONNECTED in types


# ── billing dates ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("frequency,expected", [
    ("MONTHLY", "2026-09-01"),
    ("QUARTERLY", "2026-11-01"),
    ("SEMI_ANNUAL", "2027-02-01"),
    ("YEARLY", "2027-08-01"),
])
def test_advance_billing_date(clients_db, frequency, expected):
    assert clients.advance_billing_date("2026-08-01", frequency, 1) == expected


def test_advance_clamps_to_the_shortest_month(clients_db):
    """Billed on the 31st, February pays on the 28th — and March goes back to
    the 31st instead of drifting earlier forever."""
    assert clients.advance_billing_date("2027-01-31", "MONTHLY", 31) == "2027-02-28"
    assert clients.advance_billing_date("2027-02-28", "MONTHLY", 31) == "2027-03-31"


def test_custom_interval(clients_db):
    assert clients.advance_billing_date("2026-08-01", "CUSTOM", None, 10) == "2026-08-11"
    with pytest.raises(ValueError):
        clients.advance_billing_date("2026-08-01", "CUSTOM", None, 0)


def test_one_time_has_no_next_date(clients_db):
    assert clients.advance_billing_date("2026-08-01", "ONE_TIME") is None


# ── search, filters, sorting, pagination ─────────────────────────────────────

def _book(db):
    _agent(db, "agent-1", "Телеграм-бот")
    _agent(db, "agent-2", "Аналитик")
    first = clients.create_client(_client(agent={"agentId": "agent-1"}))
    clients.touch_connection(first["agentConnections"][0]["id"])
    clients.create_client({
        "name": "ShopEasy", "projectName": "Интернет-магазин",
        "contact": {"name": "Анна Смирнова", "email": "anna@shopeasy.com", "isPrimary": True},
        "service": {"serviceName": "Поддержка", "serviceType": "RECURRING",
                    "amount": "800", "currency": "BYN", "frequency": "MONTHLY"},
    })
    clients.create_client({
        "name": "Landing Pro",
        "service": {"serviceName": "Настройка", "serviceType": "ONE_TIME",
                    "amount": "450", "currency": "USD"},
    })
    return first


@pytest.mark.parametrize("needle,expected", [
    ("midot", "Midot Project"),
    ("Интернет-магазин", "ShopEasy"),      # project name
    ("anna@shopeasy", "ShopEasy"),         # contact email
    ("Анна", "ShopEasy"),                  # contact name
    ("Поддержка", "ShopEasy"),             # service name
    ("Телеграм-бот", "Midot Project"),     # agent name
])
def test_search_covers_every_field(clients_db, needle, expected):
    _book(clients_db)
    found = clients.list_clients(search=needle)["items"]
    assert [item["name"] for item in found] == [expected]


def test_filter_by_service_type(clients_db):
    _book(clients_db)
    found = clients.list_clients(service_type="ONE_TIME")["items"]
    assert [item["name"] for item in found] == ["Landing Pro"]


def test_filter_by_agent_status(clients_db):
    _book(clients_db)
    connected = clients.list_clients(agent_status="CONNECTED")["items"]
    assert [item["name"] for item in connected] == ["Midot Project"]
    unlinked = clients.list_clients(agent_status="NOT_CONNECTED")["items"]
    assert {item["name"] for item in unlinked} == {"ShopEasy", "Landing Pro"}


def test_filter_by_client_status(clients_db):
    first = _book(clients_db)
    clients.update_client(first["id"], {"status": "PAUSED"})
    assert [item["name"] for item in clients.list_clients(status="PAUSED")["items"]] == ["Midot Project"]


def test_sort_by_amount_normalizes_through_usd(clients_db):
    """800 BYN must sort below $450, not above it on digits alone."""
    _book(clients_db)
    ordered = clients.list_clients(sort="amount", order="desc")["items"]
    assert [item["name"] for item in ordered] == ["Midot Project", "Landing Pro", "ShopEasy"]


def test_pagination_reports_the_full_total(clients_db):
    _book(clients_db)
    page = clients.list_clients(page=1, limit=2)
    assert len(page["items"]) == 2
    assert (page["total"], page["pages"], page["page"]) == (3, 2, 1)
    assert len(clients.list_clients(page=2, limit=2)["items"]) == 1


def test_row_carries_both_display_and_source_amount(clients_db):
    """The table shows the display currency but must never lose the original."""
    _book(clients_db)
    currency.update_settings(rates={"BYN": "3.27"})
    row = next(item for item in clients.list_clients(display_currency="BYN")["items"]
               if item["name"] == "Midot Project")
    assert row["primaryService"]["amount"] == "750"
    assert row["primaryService"]["currency"] == "USD"
    assert row["primaryAmountUsd"] == "750.00"
    assert row["primaryAmountDisplay"] == "2452.50"
