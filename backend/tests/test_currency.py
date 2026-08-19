"""CurrencyService: conversion, precision, validation, history, snapshots."""

from decimal import Decimal, localcontext

import pytest

from backend import client_activity, currency, database


@pytest.fixture()
def fx_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "currency.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(currency, "DB_PATH", db_path)
    monkeypatch.setattr(client_activity, "DB_PATH", db_path)
    currency.reset_schema_cache()
    client_activity.reset_schema_cache()
    database.init_db()
    currency.ensure_schema()
    return db_path


# ── the base currency ────────────────────────────────────────────────────────

def test_usd_is_the_base_and_is_always_one(fx_db):
    settings = currency.get_settings()
    assert settings["baseCurrency"] == "USD"
    assert settings["rates"]["USD"] == "1"
    assert currency.get_rate("USD") == Decimal(1)


def test_the_usd_rate_cannot_be_edited(fx_db):
    with pytest.raises(currency.CurrencyError, match="always 1"):
        currency.update_settings(rates={"USD": "1.05"})
    # Echoing the identity back is fine — the settings screen sends the whole table.
    currency.update_settings(rates={"USD": "1", "BYN": "3.30"})
    assert currency.get_rates()["BYN"] == Decimal("3.30")


# ── conversion in every direction ────────────────────────────────────────────

def test_usd_to_byn_multiplies(fx_db):
    currency.update_settings(rates={"BYN": "3.27"})
    assert currency.convert(100, "USD", "BYN") == Decimal("327.00")


def test_byn_to_usd_divides(fx_db):
    currency.update_settings(rates={"BYN": "3.27"})
    assert currency.convert("327", "BYN", "USD") == Decimal("100")


def test_usd_to_kzt_and_back_round_trips(fx_db):
    currency.update_settings(rates={"KZT": "515.50"})
    kzt = currency.convert("10", "USD", "KZT")
    assert kzt == Decimal("5155.00")
    assert currency.convert(kzt, "KZT", "USD") == Decimal("10")


def test_cross_rate_goes_through_usd(fx_db):
    """BYN → KZT is BYN → USD → KZT, never a stored BYN/KZT pair."""
    currency.update_settings(rates={"BYN": "3.27", "KZT": "515.50"})
    # 327 BYN = 100 USD = 51 550 KZT
    assert currency.convert("327", "BYN", "KZT") == Decimal("51550.00")


def test_cny_to_byn_goes_through_usd(fx_db):
    currency.update_settings(rates={"BYN": "3.27", "CNY": "7.24"})
    # 724 CNY = 100 USD = 327 BYN
    assert currency.convert("724", "CNY", "BYN") == Decimal("327.00")


def test_same_currency_is_a_no_op(fx_db):
    assert currency.convert("1234.5678", "BYN", "BYN") == Decimal("1234.5678")


def test_conversion_keeps_decimal_precision(fx_db):
    """The intermediate USD value is not rounded — rounding happens only at
    the edges, so a chained conversion does not lose cents."""
    currency.update_settings(rates={"BYN": "3.27", "KZT": "515.50"})
    result = currency.convert("1000", "BYN", "KZT")
    with localcontext() as ctx:
        ctx.prec = 34
        expected = Decimal("1000") / Decimal("3.27") * Decimal("515.50")
    assert result == expected
    assert result != result.quantize(Decimal("0.01"))  # genuinely unrounded


def test_arithmetic_never_uses_float(fx_db):
    """0.1 + 0.2 style drift must be impossible: 3 payments of 0.1 USD are
    exactly 0.3, not 0.30000000000000004."""
    total = sum((currency.parse_decimal("0.1") for _ in range(3)), Decimal(0))
    assert total == Decimal("0.3")
    assert isinstance(currency.convert("0.1", "USD", "BYN"), Decimal)


# ── validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "NaN", "Infinity", None])
def test_invalid_rates_are_refused(fx_db, bad):
    with pytest.raises(currency.CurrencyError):
        currency.update_settings(rates={"BYN": bad})


def test_a_rejected_rate_leaves_the_others_untouched(fx_db):
    """Validation happens before any write, so one bad value cannot half-apply
    a payload."""
    before = currency.get_rates()
    with pytest.raises(currency.CurrencyError):
        currency.update_settings(rates={"BYN": "3.40", "KZT": "-5"})
    assert currency.get_rates() == before


def test_unsupported_currency_is_refused(fx_db):
    with pytest.raises(currency.CurrencyError):
        currency.convert(10, "USD", "EUR")
    with pytest.raises(currency.CurrencyError):
        currency.update_settings(display_currency="EUR")


def test_missing_rate_is_an_error_not_a_silent_one(fx_db):
    with pytest.raises(currency.CurrencyError, match="No exchange rate"):
        currency.get_rate("BYN", rates={"USD": Decimal(1)})


# ── display currency ─────────────────────────────────────────────────────────

def test_display_currency_persists(fx_db):
    currency.update_settings(display_currency="BYN")
    currency.reset_schema_cache()  # simulate a fresh process
    assert currency.get_display_currency() == "BYN"
    assert currency.get_settings()["displayCurrency"] == "BYN"


# ── history and audit ────────────────────────────────────────────────────────

def test_changing_a_rate_appends_history_and_keeps_the_old_one(fx_db):
    currency.update_settings(rates={"BYN": "3.27"})
    currency.update_settings(rates={"BYN": "3.31"})
    history = currency.rate_history("BYN")
    assert [item["rate"] for item in history][:2] == ["3.31", "3.27"]
    assert currency.get_rates()["BYN"] == Decimal("3.31")


def test_saving_an_unchanged_rate_does_not_grow_history(fx_db):
    currency.update_settings(rates={"BYN": "3.27"})
    before = len(currency.rate_history("BYN"))
    currency.update_settings(rates={"BYN": "3.27"})
    assert len(currency.rate_history("BYN")) == before


def test_rate_changes_are_audited(fx_db):
    currency.update_settings(rates={"BYN": "3.31"}, actor_id="user_x")
    events = client_activity.list_events(event_type=client_activity.CURRENCY_RATE_CHANGED)
    assert events, "rate change must be audited"
    entry = events[0]
    assert entry["payload"]["currency"] == "BYN"
    assert entry["payload"]["newRate"] == "3.31"
    assert entry["payload"]["oldRate"] == "3.27"
    assert entry["actor_id"] == "user_x"


def test_display_currency_change_is_audited(fx_db):
    currency.update_settings(display_currency="KZT", actor_id="user_x")
    events = client_activity.list_events(event_type=client_activity.DISPLAY_CURRENCY_CHANGED)
    assert events[0]["payload"] == {"oldCurrency": "USD", "newCurrency": "KZT"}


# ── formatting and rounding ──────────────────────────────────────────────────

def test_formatting_per_currency(fx_db):
    space = currency.THOUSANDS_SEPARATOR
    assert currency.format_amount("1250", "USD") == f"$1{space}250.00"
    assert currency.format_amount("1250", "BYN") == f"1{space}250.00{space}BYN"
    assert currency.format_amount("515500", "KZT") == f"515{space}500.00{space}₸"
    assert currency.format_amount("7240", "CNY") == f"¥7{space}240.00"


def test_a_formatted_amount_parses_back(fx_db):
    """The non-breaking separator our own formatter emits must survive a
    round-trip through the parser — a user editing a displayed amount pastes
    exactly that string back."""
    formatted = currency.format_amount("1250.5", "BYN", with_symbol=False)
    assert currency.parse_decimal(formatted) == Decimal("1250.50")


def test_rounding_is_half_up_at_the_edge_only(fx_db):
    assert currency.quantize_money(Decimal("1.005"), "USD") == Decimal("1.01")
    assert currency.quantize_money(Decimal("1.004"), "USD") == Decimal("1.00")


def test_decimal_to_text_never_uses_exponent_notation(fx_db):
    assert currency.decimal_to_text(Decimal("1E+2")) == "100"
    assert currency.decimal_to_text(Decimal("0.10")) == "0.1"


# ── snapshots ────────────────────────────────────────────────────────────────

def test_snapshot_captures_both_hops(fx_db):
    currency.update_settings(rates={"BYN": "3.27"}, display_currency="BYN")
    snapshot = currency.snapshot("1000", "USD")
    assert snapshot["baseCurrency"] == "USD"
    assert snapshot["sourceCurrency"] == "USD"
    assert snapshot["displayCurrency"] == "BYN"
    # Derived money carries the currency's scale; the rate stays canonical.
    assert snapshot["normalizedUsdAmount"] == "1000.00"
    assert snapshot["displayAmount"] == "3270.00"
    assert snapshot["usdToDisplayRate"] == "3.27"
