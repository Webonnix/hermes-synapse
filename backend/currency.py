"""CurrencyService — the single place that knows what money is worth.

USD is the base currency of the whole commercial module: every stored rate is
`1 USD = <rate> <target>`, and every financial aggregate is normalized through
USD before it is shown in whatever the operator picked as the display currency.
Cross rates (BYN→KZT) are never stored, only derived — one rate per currency
means two rates can never disagree with each other.

Three rules this module exists to enforce:

1. **Decimal end to end.** Rates and money are `decimal.Decimal`, stored as
   exact decimal *strings*. SQLite has no real DECIMAL type — a column declared
   NUMERIC gives "3.27" REAL affinity and silently rounds it to binary float —
   so the columns are TEXT holding canonical decimal text, which round-trips
   exactly. Rounding happens at the edges (display, or fixing a document),
   never inside a calculation.
2. **Rates are history, not a value.** Saving a rate appends a row to
   `exchange_rates` with its own `valid_from`; the previous row stays. The
   "current" rate is simply the newest one for that currency.
3. **Documents freeze their rates.** `snapshot()` captures the rates used at
   the moment an invoice or payment is recorded. Nothing here ever rewrites a
   snapshot, so tomorrow's rate change cannot restate yesterday's invoice.

Everything is registry-driven (`CURRENCIES` below) rather than branching on the
four codes that ship today: adding a currency is one entry plus one rate.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Dict, List, Optional

from backend import client_activity
from backend.database import DB_PATH

logger = logging.getLogger("hermes.currency")


@dataclass(frozen=True)
class CurrencyDef:
    code: str
    name_ru: str
    name_en: str
    symbol: str
    # How many fraction digits this currency shows. Calculations keep full
    # precision regardless; this only governs presentation and the amounts
    # frozen onto a financial document.
    decimals: int


# The supported set. Adding a currency here (plus a rate) is the whole change —
# no conversion, formatting or validation code branches on specific codes.
CURRENCIES: Dict[str, CurrencyDef] = {
    "USD": CurrencyDef("USD", "Доллар США", "US Dollar", "$", 2),
    "BYN": CurrencyDef("BYN", "Белорусский рубль", "Belarusian Ruble", "BYN", 2),
    "KZT": CurrencyDef("KZT", "Казахстанский тенге", "Kazakhstani Tenge", "₸", 2),
    "CNY": CurrencyDef("CNY", "Китайский юань", "Chinese Yuan", "¥", 2),
}

# Fixed for now, by design: USD is the unit of account for every aggregate, and
# a settings screen that let it move would invalidate every stored
# normalized_usd_amount. Kept as a named constant (and a stored column) so a
# future re-basing has one place to change.
BASE_CURRENCY = "USD"

DEFAULT_DISPLAY_CURRENCY = "USD"

# Seeded once, on first use, so a fresh install has a working (if approximate)
# rate table instead of failing every conversion.
DEFAULT_RATES: Dict[str, str] = {
    "BYN": "3.27",
    "KZT": "515.50",
    "CNY": "7.24",
}

# Where a rate came from. MANUAL is the only one the MVP writes; the column
# exists so an automatic feed (NBRB, a forex API) can be added later without a
# migration that touches historical rows.
RATE_SOURCES = ("MANUAL", "API", "NBRB", "EXTERNAL_PROVIDER")

# Working precision for chained conversion (BYN → USD → KZT). Well above any
# realistic rate magnitude, so the intermediate USD value never loses digits
# that would show up in the final rounded result.
_PRECISION = 34

# See format_amount.
THOUSANDS_SEPARATOR = "\u00a0"

_schema_ready = False


class CurrencyError(ValueError):
    """Invalid currency input — an unsupported code, or a rate that is not a
    finite positive decimal. Surfaced to the API as 400, never 500."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    client_activity.ensure_schema()
    with _connect() as conn:
        # rate / amount columns are TEXT on purpose — see the module docstring.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exchange_rates (
                id TEXT PRIMARY KEY,
                base_currency TEXT NOT NULL DEFAULT 'USD',
                target_currency TEXT NOT NULL,
                rate TEXT NOT NULL,
                rate_source TEXT NOT NULL DEFAULT 'MANUAL',
                valid_from TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exchange_rates_target "
            "ON exchange_rates (target_currency, valid_from DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exchange_rates_valid_from "
            "ON exchange_rates (valid_from DESC)"
        )
        # Single-row settings table (id is pinned to 1). One display currency
        # for the whole dashboard — it is an operator preference, not per-user
        # state, and it is stored server-side so a reload keeps it.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS currency_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_currency TEXT NOT NULL DEFAULT 'USD',
                display_currency TEXT NOT NULL DEFAULT 'USD',
                updated_at TEXT NOT NULL,
                updated_by TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO currency_settings (id, base_currency, display_currency, updated_at) "
            "VALUES (1, ?, ?, ?)",
            (BASE_CURRENCY, DEFAULT_DISPLAY_CURRENCY, _now()),
        )
        # Seed the starting rates once. `INSERT ... WHERE NOT EXISTS` rather
        # than a blanket insert: re-running init must never append a duplicate
        # "today's rate" row on top of a rate the operator has since edited.
        for code, rate in DEFAULT_RATES.items():
            existing = conn.execute(
                "SELECT 1 FROM exchange_rates WHERE target_currency = ? LIMIT 1", (code,)
            ).fetchone()
            if existing:
                continue
            now = _now()
            conn.execute(
                "INSERT INTO exchange_rates "
                "(id, base_currency, target_currency, rate, rate_source, valid_from, created_at, created_by) "
                "VALUES (?, ?, ?, ?, 'MANUAL', ?, ?, NULL)",
                (f"fx-{uuid.uuid4().hex[:12]}", BASE_CURRENCY, code, rate, now, now),
            )
    _schema_ready = True


def ensure_schema() -> None:
    """Creates this module's tables now, outside any caller transaction.

    Modules that write inside a `BEGIN IMMEDIATE` call this first: lazily
    running CREATE TABLE on a second connection while the first holds the
    write lock deadlocks, so the DDL has to happen before the transaction
    opens, not during it."""
    _init_schema()


def reset_schema_cache() -> None:
    """See client_activity.reset_schema_cache — same reason."""
    global _schema_ready
    _schema_ready = False


# ── validation / parsing ─────────────────────────────────────────────────────

def is_supported(currency: Optional[str]) -> bool:
    return bool(currency) and str(currency).upper() in CURRENCIES


def normalize_currency(currency: Optional[str], *, field: str = "currency") -> str:
    code = str(currency or "").strip().upper()
    if code not in CURRENCIES:
        raise CurrencyError(
            f"Unsupported {field}: {currency!r}. Supported: {', '.join(sorted(CURRENCIES))}"
        )
    return code


def parse_decimal(value: Any, *, field: str = "value", allow_zero: bool = False) -> Decimal:
    """Any caller input → Decimal, or CurrencyError.

    Floats are accepted (JSON numbers exist) but routed through `repr` so that
    1.1 becomes Decimal("1.1") and not the 1.100000000000000088... that
    `Decimal(1.1)` would produce. String input is the preferred form and is
    exact by construction.
    """
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        raise CurrencyError(f"{field} must be a number, got a boolean")
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        parsed = Decimal(repr(value))
    else:
        # Strip every space a formatted amount can carry back in — including
        # the non-breaking ones our own formatter and Intl.NumberFormat emit as
        # thousands separators — and accept a comma decimal mark, which is what
        # a Russian-locale keyboard produces.
        text = str(value or "").strip()
        for space in (" ", "\u00a0", "\u202f", "\u2009"):
            text = text.replace(space, "")
        text = text.replace(",", ".")
        if not text:
            raise CurrencyError(f"{field} is required")
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            raise CurrencyError(f"{field} is not a valid decimal number: {value!r}") from None
    if not parsed.is_finite():
        raise CurrencyError(f"{field} must be a finite number (got {value!r})")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise CurrencyError(f"{field} must be greater than 0 (got {value!r})")
    return parsed


def parse_rate(value: Any, *, currency: str = "") -> Decimal:
    label = f"rate for {currency}" if currency else "rate"
    return parse_decimal(value, field=label)


def decimal_to_text(value: Decimal) -> str:
    """Canonical storage form: plain decimal notation, no exponent, no trailing
    zero noise from arithmetic. `normalize()` alone would render 1E+3, hence the
    quantize-back step."""
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


# ── rates ────────────────────────────────────────────────────────────────────

def get_rates(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Decimal]:
    """Current rate per currency, `1 USD = rate <currency>`.

    "Current" = the newest `valid_from` row per target, tie-broken by insertion
    order. The tie-break is load-bearing, not defensive: `valid_from` has second
    precision, so correcting a rate seconds after saving it produces two rows
    the timestamp alone cannot rank, and picking the wrong one would quietly
    re-apply the rate the operator just replaced. USD is not stored; it is
    the base and is always exactly 1, so nobody can accidentally edit it into
    something else.
    """
    _init_schema()
    owned = conn is None
    conn = conn or _connect()
    try:
        rows = conn.execute(
            """
            SELECT e.target_currency, e.rate FROM exchange_rates e
            WHERE e.rowid = (
                SELECT rowid FROM exchange_rates
                WHERE target_currency = e.target_currency
                ORDER BY valid_from DESC, rowid DESC
                LIMIT 1
            )
            """
        ).fetchall()
    finally:
        if owned:
            conn.close()
    rates: Dict[str, Decimal] = {BASE_CURRENCY: Decimal(1)}
    for row in rows:
        code = str(row["target_currency"]).upper()
        if code == BASE_CURRENCY or code not in CURRENCIES:
            continue
        try:
            rates[code] = Decimal(str(row["rate"]))
        except InvalidOperation:
            logger.warning("Ignoring unparseable stored rate for %s: %r", code, row["rate"])
    return rates


def get_rate(currency: str, rates: Optional[Dict[str, Decimal]] = None) -> Decimal:
    code = normalize_currency(currency)
    if code == BASE_CURRENCY:
        return Decimal(1)
    table = rates if rates is not None else get_rates()
    rate = table.get(code)
    if rate is None:
        raise CurrencyError(f"No exchange rate configured for {code}")
    if not rate.is_finite() or rate <= 0:
        raise CurrencyError(f"Stored exchange rate for {code} is invalid: {rate}")
    return rate


def get_base_currency() -> str:
    return BASE_CURRENCY


def get_display_currency() -> str:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT display_currency FROM currency_settings WHERE id = 1").fetchone()
    code = str(row["display_currency"]).upper() if row else DEFAULT_DISPLAY_CURRENCY
    return code if code in CURRENCIES else DEFAULT_DISPLAY_CURRENCY


# ── conversion ───────────────────────────────────────────────────────────────

def convert(
    amount: Any,
    from_currency: str,
    to_currency: str,
    rates: Optional[Dict[str, Decimal]] = None,
) -> Decimal:
    """`amount` in `from_currency` → the same value in `to_currency`.

        same currency   → amount
        USD → target    → amount × rate(target)
        source → USD    → amount ÷ rate(source)
        source → target → amount ÷ rate(source) × rate(target)   (via USD)

    The result is *not* rounded: quantize it with `quantize_money` when showing
    it or writing it onto a document. Zero is allowed here (an invoice line can
    legitimately be 0), unlike a rate.
    """
    source = normalize_currency(from_currency, field="source currency")
    target = normalize_currency(to_currency, field="target currency")
    value = parse_decimal(amount, field="amount", allow_zero=True)
    if source == target:
        return value
    table = rates if rates is not None else get_rates()
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        if source == BASE_CURRENCY:
            return value * get_rate(target, table)
        usd_value = value / get_rate(source, table)
        if target == BASE_CURRENCY:
            return usd_value
        return usd_value * get_rate(target, table)


def to_usd(amount: Any, from_currency: str, rates: Optional[Dict[str, Decimal]] = None) -> Decimal:
    return convert(amount, from_currency, BASE_CURRENCY, rates)


def money_to_text(amount: Any, currency: str) -> str:
    """A *derived* money value as text, at the currency's own scale: "1500.00",
    not "1500".

    The distinction from `decimal_to_text` is deliberate. A stored source amount
    is kept exactly as the operator typed it ("750"), because that is what they
    entered. A computed one — a USD normalization, a converted total, an
    outstanding balance — is a money figure and carries its currency's full
    precision, so an API consumer never has to guess whether "1500" means
    1500.00 or a truncation.
    """
    code = normalize_currency(currency)
    value = quantize_money(parse_decimal(amount, field="amount", allow_zero=True), code)
    return format(value, "f")


def quantize_money(amount: Decimal, currency: str) -> Decimal:
    """Round to the currency's presentation precision, half-up (the rule a
    human accountant applies, and the one an invoice total must follow)."""
    code = normalize_currency(currency)
    exponent = Decimal(1).scaleb(-CURRENCIES[code].decimals)
    return amount.quantize(exponent, rounding=ROUND_HALF_UP)


def format_amount(amount: Any, currency: str, *, with_symbol: bool = True) -> str:
    """Server-side rendering, for log lines and audit summaries. The dashboard
    formats with the browser's locale instead (components/currency)."""
    code = normalize_currency(currency)
    definition = CURRENCIES[code]
    value = quantize_money(parse_decimal(amount, field="amount", allow_zero=True), code)
    whole, _, fraction = f"{abs(value):.{definition.decimals}f}".partition(".")
    # U+00A0 as the thousands separator, deliberately: it is what ru-RU
    # formatting uses, and what Intl.NumberFormat produces in the dashboard —
    # an ASCII space here would make server-rendered and browser-rendered
    # amounts differ by an invisible character.
    grouped = f"{int(whole):,}".replace(",", THOUSANDS_SEPARATOR)
    text = f"{grouped}.{fraction}" if fraction else grouped
    if value < 0:
        text = f"-{text}"
    if not with_symbol:
        return text
    if code == "USD":
        return f"${text}"
    if code == "CNY":
        return f"¥{text}"
    # Non-breaking space before the suffix too: "1 250.00" and "BYN" must not
    # be split across a line break in a table cell.
    if code == "KZT":
        return f"{text}{THOUSANDS_SEPARATOR}₸"
    return f"{text}{THOUSANDS_SEPARATOR}{code}"


# ── FX snapshots for financial documents ─────────────────────────────────────

def snapshot(
    amount: Any,
    source_currency: str,
    display_currency: Optional[str] = None,
    rates: Optional[Dict[str, Decimal]] = None,
) -> Dict[str, str]:
    """Freeze today's rates onto a document.

    The returned dict is stored verbatim on the invoice/payment row. Once
    written it is never recomputed: a later rate change produces a *new*
    snapshot on the *next* document and leaves this one alone. That is what
    makes an issued invoice a record rather than a live query.
    """
    source = normalize_currency(source_currency, field="source currency")
    display = normalize_currency(display_currency or get_display_currency(), field="display currency")
    table = rates if rates is not None else get_rates()
    source_amount = parse_decimal(amount, field="amount", allow_zero=True)
    usd_amount = convert(source_amount, source, BASE_CURRENCY, table)
    display_amount = convert(usd_amount, BASE_CURRENCY, display, table)
    return {
        "baseCurrency": BASE_CURRENCY,
        "sourceCurrency": source,
        "displayCurrency": display,
        "sourceAmount": decimal_to_text(source_amount),
        "normalizedUsdAmount": money_to_text(usd_amount, BASE_CURRENCY),
        "displayAmount": money_to_text(display_amount, display),
        # 1 <source> = X USD, and 1 USD = Y <display>. Stored as the two hops
        # the conversion actually took, so the arithmetic on an old invoice can
        # be re-checked years later without the rate table.
        "sourceToUsdRate": decimal_to_text(Decimal(1) / get_rate(source, table)),
        "usdToDisplayRate": decimal_to_text(get_rate(display, table)),
        "capturedAt": _now(),
    }


# ── settings ─────────────────────────────────────────────────────────────────

def get_settings() -> Dict[str, Any]:
    """The payload behind GET /api/settings/currency — settings and current
    rates in one round trip, so no screen has to fetch a rate per row."""
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM currency_settings WHERE id = 1").fetchone()
        rates = get_rates(conn)
    display = str(row["display_currency"]).upper() if row else DEFAULT_DISPLAY_CURRENCY
    return {
        "baseCurrency": BASE_CURRENCY,
        "displayCurrency": display if display in CURRENCIES else DEFAULT_DISPLAY_CURRENCY,
        "rates": {code: decimal_to_text(value) for code, value in sorted(rates.items())},
        "currencies": [
            {
                "code": item.code,
                "nameRu": item.name_ru,
                "nameEn": item.name_en,
                "symbol": item.symbol,
                "decimals": item.decimals,
                "isBase": item.code == BASE_CURRENCY,
            }
            for item in CURRENCIES.values()
        ],
        "updatedAt": row["updated_at"] if row else None,
        "updatedBy": row["updated_by"] if row else None,
    }


def update_settings(
    *,
    display_currency: Optional[str] = None,
    rates: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
    rate_source: str = "MANUAL",
) -> Dict[str, Any]:
    """Applies a settings change atomically: every rate row, the display
    currency and their audit entries land in one transaction or none do.

    Rates are validated *before* anything is written, so a payload with one bad
    value cannot leave half of it applied. A rate equal to the current one is
    skipped rather than appended — the history should record changes, not
    saves.
    """
    _init_schema()
    if rate_source not in RATE_SOURCES:
        raise CurrencyError(f"Unknown rate source: {rate_source}")

    validated: Dict[str, Decimal] = {}
    if rates:
        for raw_code, raw_rate in rates.items():
            code = normalize_currency(raw_code, field="rate currency")
            if code == BASE_CURRENCY:
                # 1 USD = 1 USD is an identity, not a setting. Accept it when
                # the client echoes the whole rate table back, refuse anything
                # else rather than silently ignoring a real (wrong) edit.
                if parse_rate(raw_rate, currency=code) != Decimal(1):
                    raise CurrencyError("The USD → USD rate is always 1 and cannot be changed")
                continue
            validated[code] = parse_rate(raw_rate, currency=code)

    new_display: Optional[str] = None
    if display_currency is not None:
        new_display = normalize_currency(display_currency, field="display currency")

    now = _now()
    changes: List[Dict[str, str]] = []
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = get_rates(conn)
            for code, rate in validated.items():
                previous = current.get(code)
                if previous is not None and previous == rate:
                    continue
                conn.execute(
                    "INSERT INTO exchange_rates "
                    "(id, base_currency, target_currency, rate, rate_source, valid_from, created_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"fx-{uuid.uuid4().hex[:12]}", BASE_CURRENCY, code,
                        decimal_to_text(rate), rate_source, now, now, actor_id,
                    ),
                )
                old_text = decimal_to_text(previous) if previous is not None else None
                new_text = decimal_to_text(rate)
                changes.append({"currency": code, "oldRate": old_text or "", "newRate": new_text})
                client_activity.log_event(
                    client_activity.CURRENCY_RATE_CHANGED,
                    entity_type="exchange_rate",
                    entity_id=code,
                    summary=f"USD/{code}: {old_text or '—'} → {new_text}",
                    payload={
                        "currency": code,
                        "oldRate": old_text,
                        "newRate": new_text,
                        "rateSource": rate_source,
                    },
                    actor_id=actor_id,
                    conn=conn,
                )

            if new_display is not None:
                row = conn.execute(
                    "SELECT display_currency FROM currency_settings WHERE id = 1"
                ).fetchone()
                old_display = str(row["display_currency"]).upper() if row else DEFAULT_DISPLAY_CURRENCY
                conn.execute(
                    "UPDATE currency_settings SET display_currency = ?, updated_at = ?, updated_by = ? "
                    "WHERE id = 1",
                    (new_display, now, actor_id),
                )
                if old_display != new_display:
                    client_activity.log_event(
                        client_activity.DISPLAY_CURRENCY_CHANGED,
                        entity_type="currency_settings",
                        entity_id="display_currency",
                        summary=f"Валюта отображения: {old_display} → {new_display}",
                        payload={"oldCurrency": old_display, "newCurrency": new_display},
                        actor_id=actor_id,
                        conn=conn,
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if changes:
        logger.info("Exchange rates updated: %s", changes)
    return get_settings()


def rate_history(
    currency: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    """Newest first. Nothing here is ever updated or deleted — a rate that was
    wrong is corrected by adding the right one, and both stay visible."""
    _init_schema()
    params: List[Any] = []
    clause = ""
    if currency:
        clause = "WHERE target_currency = ?"
        params.append(normalize_currency(currency))
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM exchange_rates {clause} ORDER BY valid_from DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
    return [
        {
            "id": row["id"],
            "baseCurrency": row["base_currency"],
            "targetCurrency": row["target_currency"],
            "rate": row["rate"],
            "rateSource": row["rate_source"],
            "validFrom": row["valid_from"],
            "createdAt": row["created_at"],
            "createdBy": row["created_by"],
        }
        for row in rows
    ]
