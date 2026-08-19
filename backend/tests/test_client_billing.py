"""Commercial billing: FX snapshots, settlement, MRR, the recurring scheduler."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend import client_activity, client_billing, clients, currency, database


@pytest.fixture()
def billing_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "billing.db")
    for module in (database, currency, clients, client_billing, client_activity):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (currency, clients, client_billing, client_activity):
        module.reset_schema_cache()
    database.init_db()
    client_billing._init_schema()
    currency.update_settings(rates={"BYN": "3.27", "KZT": "515.50", "CNY": "7.24"})
    return db_path


def _client(name="Midot Project", *, amount="1000", code="USD", frequency="MONTHLY",
            service_type="RECURRING", next_date=None, status="ACTIVE"):
    payload = {
        "name": name, "status": status,
        "service": {
            "serviceName": "Разработка", "serviceType": service_type,
            "amount": amount, "currency": code, "frequency": frequency,
            "billingDay": 1, "nextBillingDate": next_date,
        },
    }
    return clients.create_client(payload)


# ── FX snapshot immutability (§84 — the mandatory test) ──────────────────────

def test_a_rate_change_does_not_restate_an_existing_invoice(billing_db):
    currency.update_settings(rates={"BYN": "3.27"}, display_currency="BYN")
    client = _client()
    old = client_billing.create_invoice(
        client_id=client["id"], amount="1000", source_currency="USD", status="ISSUED"
    )
    assert old["exchangeRateSnapshot"]["usdToDisplayRate"] == "3.27"
    assert old["displayAmount"] == "3270.00"

    currency.update_settings(rates={"BYN": "3.50"})

    reread = client_billing.get_invoice(old["id"])
    assert reread["exchangeRateSnapshot"] == old["exchangeRateSnapshot"]
    assert reread["displayAmount"] == "3270.00"
    assert reread["normalizedUsdAmount"] == old["normalizedUsdAmount"]

    fresh = client_billing.create_invoice(
        client_id=client["id"], amount="1000", source_currency="USD"
    )
    # Rates keep their canonical (normalized) form; money keeps its scale.
    assert fresh["exchangeRateSnapshot"]["usdToDisplayRate"] == "3.5"
    assert fresh["displayAmount"] == "3500.00"


def test_invoice_in_a_foreign_currency_normalizes_to_usd(billing_db):
    client = _client(amount="3270", code="BYN")
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="3270", source_currency="BYN"
    )
    assert invoice["sourceAmount"] == "3270"
    assert invoice["sourceCurrency"] == "BYN"
    assert invoice["normalizedUsdAmount"] == "1000.00"


def test_invoice_numbers_are_sequential(billing_db):
    client = _client()
    first = client_billing.create_invoice(client_id=client["id"], amount="10", source_currency="USD")
    second = client_billing.create_invoice(client_id=client["id"], amount="20", source_currency="USD")
    year = date.today().year
    assert first["invoiceNumber"] == f"INV-{year}-0001"
    assert second["invoiceNumber"] == f"INV-{year}-0002"


# ── settlement ───────────────────────────────────────────────────────────────

def test_partial_then_full_payment(billing_db):
    client = _client()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="1000", source_currency="USD", status="ISSUED"
    )
    client_billing.create_payment(
        client_id=client["id"], amount="400", payment_currency="USD", invoice_id=invoice["id"]
    )
    partial = client_billing.get_invoice(invoice["id"])
    assert partial["status"] == "PARTIALLY_PAID"
    assert partial["outstandingAmount"] == "600.00"

    client_billing.create_payment(
        client_id=client["id"], amount="600", payment_currency="USD", invoice_id=invoice["id"]
    )
    settled = client_billing.get_invoice(invoice["id"])
    assert settled["status"] == "PAID"
    assert settled["outstandingAmount"] == "0.00"
    assert settled["paidAt"]


def test_paying_the_invoiced_amount_settles_it_after_a_rate_change(billing_db):
    """3 270 BYN against a 3 270 BYN invoice settles it exactly, whatever the
    USD rate has done in between — the invoice's own currency is face value."""
    client = _client()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="3270", source_currency="BYN", status="ISSUED"
    )
    currency.update_settings(rates={"BYN": "4.10"})
    client_billing.create_payment(
        client_id=client["id"], amount="3270", payment_currency="BYN", invoice_id=invoice["id"]
    )
    assert client_billing.get_invoice(invoice["id"])["status"] == "PAID"


def test_mark_paid_writes_a_real_payment(billing_db):
    """The button is a payment record, not a status flip — the ledger and the
    invoice can never disagree."""
    client = _client()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="500", source_currency="USD", status="ISSUED"
    )
    client_billing.mark_invoice_paid(invoice["id"], payment_method="bank")
    assert client_billing.get_invoice(invoice["id"])["status"] == "PAID"
    payments = client_billing.list_payments(invoice_id=invoice["id"])
    assert len(payments) == 1
    assert Decimal(payments[0]["amount"]) == Decimal("500")


def test_mark_paid_is_idempotent(billing_db):
    client = _client()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="500", source_currency="USD", status="ISSUED"
    )
    client_billing.mark_invoice_paid(invoice["id"])
    client_billing.mark_invoice_paid(invoice["id"])
    assert len(client_billing.list_payments(invoice_id=invoice["id"])) == 1


def test_a_payment_for_another_clients_invoice_is_refused(billing_db):
    first = _client("A")
    second = _client("B")
    invoice = client_billing.create_invoice(
        client_id=first["id"], amount="100", source_currency="USD"
    )
    with pytest.raises(ValueError, match="другому клиенту"):
        client_billing.create_payment(
            client_id=second["id"], amount="100", payment_currency="USD", invoice_id=invoice["id"]
        )


def test_cancelling_and_its_limits(billing_db):
    client = _client()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", status="ISSUED"
    )
    assert client_billing.cancel_invoice(invoice["id"])["status"] == "CANCELLED"

    paid = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", status="ISSUED"
    )
    client_billing.mark_invoice_paid(paid["id"])
    with pytest.raises(ValueError, match="Оплаченный счёт"):
        client_billing.cancel_invoice(paid["id"])


# ── overdue (§56) ────────────────────────────────────────────────────────────

def test_an_unpaid_invoice_past_its_due_date_becomes_overdue(billing_db):
    client = _client()
    past = (date.today() - timedelta(days=5)).isoformat()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD",
        due_date=past, status="ISSUED",
    )
    assert client_billing.sweep_overdue_invoices() >= 1
    assert client_billing.get_invoice(invoice["id"])["status"] == "OVERDUE"


def test_a_paid_invoice_never_becomes_overdue(billing_db):
    client = _client()
    past = (date.today() - timedelta(days=5)).isoformat()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", due_date=past, status="ISSUED"
    )
    client_billing.mark_invoice_paid(invoice["id"])
    client_billing.sweep_overdue_invoices()
    assert client_billing.get_invoice(invoice["id"])["status"] == "PAID"


def test_a_cancelled_invoice_never_becomes_overdue(billing_db):
    client = _client()
    past = (date.today() - timedelta(days=5)).isoformat()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", due_date=past, status="ISSUED"
    )
    client_billing.cancel_invoice(invoice["id"])
    client_billing.sweep_overdue_invoices()
    assert client_billing.get_invoice(invoice["id"])["status"] == "CANCELLED"


# ── payment status per client (§80) ──────────────────────────────────────────

def test_payment_status_takes_the_worst_invoice(billing_db):
    client = _client()
    paid = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", status="ISSUED"
    )
    client_billing.mark_invoice_paid(paid["id"])
    client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", status="ISSUED",
        due_date=(date.today() - timedelta(days=3)).isoformat(),
    )
    assert client_billing.payment_status_map([client["id"]])[client["id"]] == "OVERDUE"


def test_no_invoices_means_no_invoice(billing_db):
    client = _client()
    assert client_billing.payment_status_map([client["id"]])[client["id"]] == "NO_INVOICE"


# ── MRR (§44, §85) ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("frequency,expected", [
    ("MONTHLY", "1200"),
    ("QUARTERLY", "400"),
    ("SEMI_ANNUAL", "200"),
    ("YEARLY", "100"),
])
def test_mrr_normalizes_every_frequency_to_a_month(billing_db, frequency, expected):
    _client(amount="1200", frequency=frequency)
    assert client_billing.calculate_mrr() == Decimal(expected)


def test_mrr_excludes_one_time_services(billing_db):
    _client("Recurring", amount="1000")
    _client("One-off", amount="9999", service_type="ONE_TIME", frequency="ONE_TIME")
    assert client_billing.calculate_mrr() == Decimal("1000")


def test_mrr_excludes_paused_services_and_inactive_clients(billing_db):
    active = _client("Active", amount="1000")
    paused_service = _client("Paused service", amount="500")
    clients.update_client_service(paused_service["services"][0]["id"], {"status": "PAUSED"})
    inactive = _client("Archived", amount="700")
    clients.archive_client(inactive["id"])
    assert client_billing.calculate_mrr() == Decimal("1000")
    assert active["status"] == "ACTIVE"


def test_mrr_sums_mixed_currencies_through_usd(billing_db):
    _client("USD", amount="1000", code="USD")
    _client("BYN", amount="327", code="BYN")          # = 100 USD
    _client("KZT", amount="5155", code="KZT")         # = 10 USD
    _client("CNY", amount="724", code="CNY")          # = 100 USD
    assert client_billing.calculate_mrr() == Decimal("1210")


def test_mrr_follows_the_rate(billing_db):
    _client("BYN client", amount="327", code="BYN")
    assert client_billing.calculate_mrr() == Decimal("100")
    currency.update_settings(rates={"BYN": "3.27" if False else "6.54"})
    assert client_billing.calculate_mrr() == Decimal("50")


def test_custom_frequency_scales_to_an_average_month(billing_db):
    clients.create_client({
        "name": "Custom cycle",
        "service": {
            "serviceName": "Спринты", "serviceType": "RECURRING", "amount": "100",
            "currency": "USD", "frequency": "CUSTOM", "customIntervalDays": 30,
            "nextBillingDate": "2026-09-01",
        },
    })
    assert client_billing.calculate_mrr() == Decimal("100") * (Decimal("30.4375") / Decimal(30))


def test_a_custom_frequency_needs_an_interval(billing_db):
    with pytest.raises(ValueError, match="customIntervalDays"):
        clients.create_client({
            "name": "Broken", "service": {
                "serviceName": "X", "serviceType": "RECURRING", "amount": "100",
                "currency": "USD", "frequency": "CUSTOM",
            },
        })


# ── dashboard ────────────────────────────────────────────────────────────────

def test_dashboard_reports_both_currencies(billing_db):
    currency.update_settings(rates={"BYN": "3.27"})
    _client(amount="1000")
    data = client_billing.dashboard("BYN")
    assert data["currency"] == "BYN"
    assert data["baseCurrency"] == "USD"
    assert data["mrr"] == {"usd": "1000.00", "display": "3270.00"}


def test_dashboard_counts_overdue_debt(billing_db):
    client = _client()
    client_billing.create_invoice(
        client_id=client["id"], amount="1200", source_currency="USD", status="ISSUED",
        due_date=(date.today() - timedelta(days=2)).isoformat(),
    )
    data = client_billing.dashboard("USD")
    assert data["overdue"]["usd"] == "1200.00"
    assert data["overdue"]["count"] == 1


def test_dashboard_counts_upcoming_billings(billing_db):
    _client(amount="1500", next_date=(date.today() + timedelta(days=5)).isoformat())
    _client("Later", amount="9999", next_date=(date.today() + timedelta(days=90)).isoformat())
    data = client_billing.dashboard("USD")
    assert data["toInvoice"]["usd"] == "1500.00"
    assert data["toInvoice"]["count"] == 1


# ── the recurring scheduler (§53, §54, §87) ──────────────────────────────────

def test_the_scheduler_creates_an_invoice_and_advances_the_date(billing_db):
    client = _client(amount="1000", next_date="2026-08-01")
    service_id = client["services"][0]["id"]

    result = client_billing.generate_due_invoices(date(2026, 8, 19))
    assert result["createdCount"] == 1

    invoice = client_billing.list_invoices(client_service_id=service_id)[0]
    assert invoice["billingPeriod"] == "2026-08"
    assert invoice["status"] == "ISSUED"
    assert invoice["origin"] == "scheduler"
    assert clients.get_billing_configuration(service_id)["nextBillingDate"] == "2026-09-01"


def test_running_the_scheduler_twice_creates_nothing_extra(billing_db):
    client = _client(amount="1000", next_date="2026-08-01")
    service_id = client["services"][0]["id"]
    client_billing.generate_due_invoices(date(2026, 8, 19))
    second = client_billing.generate_due_invoices(date(2026, 8, 19))
    assert second["createdCount"] == 0
    assert len(client_billing.list_invoices(client_service_id=service_id)) == 1


def test_a_duplicate_period_is_rejected_by_the_database(billing_db):
    """The idempotency key is a UNIQUE index, not a code-side check — a second
    writer that skipped the sweep still cannot double-bill a period."""
    client = _client(amount="1000", next_date="2026-08-01")
    service_id = client["services"][0]["id"]
    client_billing.create_invoice(
        client_id=client["id"], amount="1000", source_currency="USD",
        client_service_id=service_id, billing_period="2026-08",
    )
    with pytest.raises(client_billing.DuplicateInvoice):
        client_billing.create_invoice(
            client_id=client["id"], amount="1000", source_currency="USD",
            client_service_id=service_id, billing_period="2026-08",
        )


def test_a_skipped_period_still_advances_the_schedule(billing_db):
    """Otherwise the sweep retries the same already-billed period forever."""
    client = _client(amount="1000", next_date="2026-08-01")
    service_id = client["services"][0]["id"]
    client_billing.create_invoice(
        client_id=client["id"], amount="1000", source_currency="USD",
        client_service_id=service_id, billing_period="2026-08",
    )
    result = client_billing.generate_due_invoices(date(2026, 8, 19))
    assert result["skipped"] == 1
    assert clients.get_billing_configuration(service_id)["nextBillingDate"] == "2026-09-01"


def test_the_scheduler_skips_one_time_paused_and_archived(billing_db):
    _client("One-off", amount="450", service_type="ONE_TIME", frequency="ONE_TIME")
    paused = _client("Paused", amount="500", next_date="2026-08-01")
    clients.update_client_service(paused["services"][0]["id"], {"status": "PAUSED"})
    archived = _client("Archived", amount="600", next_date="2026-08-01")
    clients.archive_client(archived["id"])

    assert client_billing.generate_due_invoices(date(2026, 8, 19))["createdCount"] == 0


def test_the_scheduler_does_not_bill_ahead_of_schedule(billing_db):
    _client(amount="1000", next_date="2026-12-01")
    assert client_billing.generate_due_invoices(date(2026, 8, 19))["createdCount"] == 0


@pytest.mark.parametrize("frequency,when,expected", [
    ("MONTHLY", date(2026, 9, 3), "2026-09"),
    ("QUARTERLY", date(2026, 9, 3), "2026-Q3"),
    ("SEMI_ANNUAL", date(2026, 9, 3), "2026-H2"),
    ("YEARLY", date(2026, 9, 3), "2026"),
    ("CUSTOM", date(2026, 9, 3), "2026-09-03"),
])
def test_billing_period_keys(billing_db, frequency, when, expected):
    assert client_billing.billing_period_key(when, frequency) == expected


# ── audit ────────────────────────────────────────────────────────────────────

def test_billing_events_are_audited(billing_db):
    client = _client()
    invoice = client_billing.create_invoice(
        client_id=client["id"], amount="100", source_currency="USD", status="ISSUED",
        actor_id="user_x",
    )
    client_billing.mark_invoice_paid(invoice["id"], actor_id="user_x")
    types = [event["event_type"] for event in client_activity.list_events(client_id=client["id"])]
    for expected in (client_activity.INVOICE_CREATED, client_activity.PAYMENT_CREATED,
                     client_activity.INVOICE_PAID):
        assert expected in types
