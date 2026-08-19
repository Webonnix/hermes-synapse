"""Commercial billing: what a client was invoiced, what they paid, what they owe.

Sits on top of `backend/clients.py` (who the client is, what they bought at
what price) and `backend/currency.py` (what that price is worth today). It is
the *revenue* half of the picture; `backend/bot_access.py` + `backend/payments.py`
remain the *cost* half — an agent's plan, its quota, the crypto invoice a
stranger paid to use a bot. Nothing here writes to those tables.

Two invariants carry most of the weight:

**An issued document never changes its mind about money.** Creating an invoice
freezes the FX rates it was created under (`currency.snapshot`). Editing the
USD/BYN rate tomorrow moves the dashboard and the price list; it does not
restate an invoice from last month. The frozen snapshot, not the live rate
table, is what an old invoice is re-read through.

**The scheduler may run twice.** Recurring invoices are keyed on
`client_service_id + billing_period` with a UNIQUE index behind them, so a
retried, overlapping or manually re-triggered sweep inserts nothing the second
time — the database refuses it, rather than the code hoping it counted right.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend import client_activity, clients, currency
from backend.database import DB_PATH

logger = logging.getLogger("hermes.client_billing")

INVOICE_STATUSES = ("PLANNED", "ISSUED", "PARTIALLY_PAID", "PAID", "OVERDUE", "CANCELLED")
# Statuses that still owe money — the ones overdue detection and the debt
# aggregate care about.
OPEN_INVOICE_STATUSES = ("PLANNED", "ISSUED", "PARTIALLY_PAID", "OVERDUE")

# What the clients table shows in its «Оплата» column. Derived from a client's
# invoices, never stored on the client — a status that is a stored copy of
# other rows' state is a status that goes stale.
PAYMENT_STATUSES = (
    "NO_INVOICE", "PLANNED", "AWAITING_PAYMENT", "PARTIALLY_PAID", "PAID", "OVERDUE",
)

DEFAULT_DUE_DAYS = int(__import__("os").getenv("CLIENT_INVOICE_DUE_DAYS", "10"))

# How far ahead the "К выставлению" KPI looks.
UPCOMING_WINDOW_DAYS = 30

_schema_ready = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    currency.ensure_schema()
    client_activity.ensure_schema()
    clients._init_schema()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_invoices (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                client_service_id TEXT,
                invoice_number TEXT NOT NULL,
                source_amount TEXT NOT NULL,
                source_currency TEXT NOT NULL,
                normalized_usd_amount TEXT NOT NULL,
                display_amount TEXT,
                display_currency TEXT,
                exchange_rate_snapshot TEXT NOT NULL DEFAULT '{}',
                invoice_date TEXT NOT NULL,
                due_date TEXT,
                billing_period TEXT,
                status TEXT NOT NULL DEFAULT 'PLANNED',
                origin TEXT NOT NULL DEFAULT 'manual',
                comment TEXT NOT NULL DEFAULT '',
                issued_at TEXT,
                paid_at TEXT,
                cancelled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_client_invoices_client ON client_invoices (client_id);
            CREATE INDEX IF NOT EXISTS idx_client_invoices_status ON client_invoices (status);
            CREATE INDEX IF NOT EXISTS idx_client_invoices_date ON client_invoices (invoice_date);
            CREATE INDEX IF NOT EXISTS idx_client_invoices_due ON client_invoices (due_date);
            CREATE INDEX IF NOT EXISTS idx_client_invoices_service ON client_invoices (client_service_id);

            -- The scheduler's idempotency key, enforced by the database rather
            -- than by the sweep counting carefully. SQLite treats NULLs as
            -- distinct in a UNIQUE index, so ad-hoc invoices (no billing
            -- period) are unaffected while every generated one is unique per
            -- service and period.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_client_invoices_period
                ON client_invoices (client_service_id, billing_period);

            CREATE TABLE IF NOT EXISTS client_payments (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                invoice_id TEXT,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                normalized_usd_amount TEXT NOT NULL,
                exchange_rate_snapshot TEXT NOT NULL DEFAULT '{}',
                payment_date TEXT NOT NULL,
                payment_method TEXT NOT NULL DEFAULT '',
                reference TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_client_payments_client ON client_payments (client_id);
            CREATE INDEX IF NOT EXISTS idx_client_payments_invoice ON client_payments (invoice_id);
            CREATE INDEX IF NOT EXISTS idx_client_payments_date ON client_payments (payment_date);
            """
        )
    _schema_ready = True


def reset_schema_cache() -> None:
    """See client_activity.reset_schema_cache."""
    global _schema_ready
    _schema_ready = False


# ── row mapping ──────────────────────────────────────────────────────────────

def _json_or_empty(raw: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _invoice_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = {
        "id": row["id"],
        "clientId": row["client_id"],
        "clientServiceId": row["client_service_id"],
        "invoiceNumber": row["invoice_number"],
        "sourceAmount": row["source_amount"],
        "sourceCurrency": row["source_currency"],
        "normalizedUsdAmount": row["normalized_usd_amount"],
        "displayAmount": row["display_amount"],
        "displayCurrency": row["display_currency"],
        "exchangeRateSnapshot": _json_or_empty(row["exchange_rate_snapshot"]),
        "invoiceDate": row["invoice_date"],
        "dueDate": row["due_date"],
        "billingPeriod": row["billing_period"],
        "status": row["status"],
        "origin": row["origin"],
        "comment": row["comment"],
        "issuedAt": row["issued_at"],
        "paidAt": row["paid_at"],
        "cancelledAt": row["cancelled_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    keys = row.keys()
    if "client_name" in keys:
        data["clientName"] = row["client_name"]
    if "project_name" in keys:
        data["projectName"] = row["project_name"]
    if "service_title" in keys:
        data["serviceTitle"] = row["service_title"]
    return data


def _payment_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = {
        "id": row["id"],
        "clientId": row["client_id"],
        "invoiceId": row["invoice_id"],
        "amount": row["amount"],
        "currency": row["currency"],
        "normalizedUsdAmount": row["normalized_usd_amount"],
        "exchangeRateSnapshot": _json_or_empty(row["exchange_rate_snapshot"]),
        "paymentDate": row["payment_date"],
        "paymentMethod": row["payment_method"],
        "reference": row["reference"],
        "comment": row["comment"],
        "createdAt": row["created_at"],
    }
    keys = row.keys()
    if "client_name" in keys:
        data["clientName"] = row["client_name"]
    if "invoice_number" in keys:
        data["invoiceNumber"] = row["invoice_number"]
    return data


# ── settlement arithmetic ────────────────────────────────────────────────────

def _paid_in_source_currency(invoice: sqlite3.Row, payments: Iterable[sqlite3.Row]) -> Decimal:
    """How much of this invoice has been settled, expressed in the currency the
    invoice was issued in.

    A payment in the invoice's own currency counts at face value — paying
    3 270 BYN against a 3 270 BYN invoice settles it exactly, whatever the rate
    has done since. A payment in some other currency is converted back through
    the *invoice's frozen* rate, not today's, so the invoice's own arithmetic
    stays internally consistent for its whole life.
    """
    snapshot = _json_or_empty(invoice["exchange_rate_snapshot"])
    source_to_usd = snapshot.get("sourceToUsdRate")
    total = Decimal(0)
    for payment in payments:
        amount = Decimal(str(payment["amount"]))
        if str(payment["currency"]).upper() == str(invoice["source_currency"]).upper():
            total += amount
            continue
        usd = Decimal(str(payment["normalized_usd_amount"]))
        if source_to_usd:
            rate = Decimal(str(source_to_usd))
            if rate > 0:
                total += usd / rate
                continue
        # No usable snapshot (an invoice written before snapshots, or a
        # corrupted one): fall back to today's rates rather than dropping the
        # payment on the floor.
        total += currency.convert(usd, currency.BASE_CURRENCY, invoice["source_currency"])
    return total


def _settlement(conn: sqlite3.Connection, invoice: sqlite3.Row) -> Tuple[Decimal, Decimal]:
    """(paid, outstanding) in the invoice's source currency."""
    payments = conn.execute(
        "SELECT * FROM client_payments WHERE invoice_id = ?", (invoice["id"],)
    ).fetchall()
    paid = _paid_in_source_currency(invoice, payments)
    total = Decimal(str(invoice["source_amount"]))
    return paid, total - paid


def _recalculate_invoice_status(conn: sqlite3.Connection, invoice_id: str) -> str:
    """Derives an invoice's status from its payments and its due date.

    Cancelled invoices are left alone — cancelling is a decision, and a late
    payment arriving against a cancelled invoice should not silently revive it.
    """
    row = conn.execute("SELECT * FROM client_invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise KeyError(invoice_id)
    if row["status"] == "CANCELLED":
        return "CANCELLED"

    paid, outstanding = _settlement(conn, row)
    now = _now()
    if outstanding <= 0 and paid > 0:
        status, paid_at = "PAID", row["paid_at"] or now
    elif paid > 0:
        status, paid_at = "PARTIALLY_PAID", None
    elif row["status"] == "PLANNED" and not row["issued_at"]:
        status, paid_at = "PLANNED", None
    else:
        status, paid_at = "ISSUED", None

    # §56: past due and not settled ⇒ OVERDUE. Applies to partially paid
    # invoices too — an invoice half-paid a month late is still late.
    if status in ("ISSUED", "PARTIALLY_PAID", "PLANNED") and row["due_date"]:
        if str(row["due_date"])[:10] < _today().isoformat() and row["issued_at"]:
            status = "OVERDUE"

    if status != row["status"] or paid_at != row["paid_at"]:
        conn.execute(
            "UPDATE client_invoices SET status = ?, paid_at = ?, updated_at = ? WHERE id = ?",
            (status, paid_at, now, invoice_id),
        )
    return status


def _next_invoice_number(conn: sqlite3.Connection, when: date) -> str:
    """`INV-2026-0007`, sequential within the year. Read and written inside the
    caller's transaction, so two invoices created at the same instant cannot
    take the same number."""
    prefix = f"INV-{when.year}-"
    row = conn.execute(
        "SELECT invoice_number FROM client_invoices WHERE invoice_number LIKE ? "
        "ORDER BY invoice_number DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    sequence = 1
    if row:
        try:
            sequence = int(str(row["invoice_number"]).rsplit("-", 1)[-1]) + 1
        except ValueError:
            sequence = 1
    return f"{prefix}{sequence:04d}"


# ── invoices ─────────────────────────────────────────────────────────────────

class DuplicateInvoice(Exception):
    """The (client_service_id, billing_period) key already exists. Not an
    error for the scheduler — it is the scheduler working."""


def create_invoice(
    *,
    client_id: str,
    amount: Any,
    source_currency: str,
    client_service_id: Optional[str] = None,
    invoice_date: Optional[str] = None,
    due_date: Optional[str] = None,
    billing_period: Optional[str] = None,
    status: str = "PLANNED",
    origin: str = "manual",
    comment: str = "",
    actor_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Records an invoice and freezes the FX rates it was created under.

    `conn` lets the scheduler create many invoices inside one transaction.
    """
    _init_schema()
    status = str(status or "PLANNED").upper()
    if status not in INVOICE_STATUSES:
        raise ValueError(f"Unknown invoice status: {status}")
    source = currency.normalize_currency(source_currency, field="source currency")
    amount_value = currency.parse_decimal(amount, field="amount")
    when = date.fromisoformat(str(invoice_date)[:10]) if invoice_date else _today()
    if due_date:
        due = str(due_date)[:10]
        date.fromisoformat(due)
    else:
        due = (when + timedelta(days=DEFAULT_DUE_DAYS)).isoformat()

    # The one moment rates matter for this document. After this line the
    # snapshot is history and is never recomputed.
    snapshot = currency.snapshot(amount_value, source)
    now = _now()
    invoice_id = _new_id("inv")

    def _write(active: sqlite3.Connection) -> None:
        if not active.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
            raise KeyError(client_id)
        number = _next_invoice_number(active, when)
        try:
            active.execute(
                "INSERT INTO client_invoices "
                "(id, client_id, client_service_id, invoice_number, source_amount, source_currency, "
                " normalized_usd_amount, display_amount, display_currency, exchange_rate_snapshot, "
                " invoice_date, due_date, billing_period, status, origin, comment, issued_at, paid_at, "
                " cancelled_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    invoice_id, client_id, client_service_id or None, number,
                    currency.decimal_to_text(amount_value), source,
                    snapshot["normalizedUsdAmount"], snapshot["displayAmount"],
                    snapshot["displayCurrency"], json.dumps(snapshot, ensure_ascii=False),
                    when.isoformat(), due, billing_period or None, status, origin,
                    str(comment or ""), now if status != "PLANNED" else None, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "idx_client_invoices_period" in str(exc) or "UNIQUE" in str(exc).upper():
                raise DuplicateInvoice(
                    f"Счёт за период {billing_period} по услуге {client_service_id} уже существует"
                ) from exc
            raise
        client_activity.log_event(
            client_activity.INVOICE_CREATED, client_id=client_id, entity_type="client_invoice",
            entity_id=invoice_id,
            summary=f"Счёт {number} на {currency.format_amount(amount_value, source)}",
            payload={
                "invoiceNumber": number, "amount": currency.decimal_to_text(amount_value),
                "currency": source, "billingPeriod": billing_period, "origin": origin,
            },
            actor_id=actor_id, conn=active,
        )

    if conn is not None:
        _write(conn)
    else:
        with _connect() as own:
            own.execute("BEGIN IMMEDIATE")
            try:
                _write(own)
                own.execute("COMMIT")
            except Exception:
                own.execute("ROLLBACK")
                raise
    return get_invoice(invoice_id)


def get_invoice(invoice_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT i.*, c.name AS client_name, c.project_name AS project_name, "
            "       cs.title AS service_title "
            "FROM client_invoices i "
            "LEFT JOIN clients c ON c.id = i.client_id "
            "LEFT JOIN client_services cs ON cs.id = i.client_service_id "
            "WHERE i.id = ?",
            (invoice_id,),
        ).fetchone()
        if row is None:
            return None
        paid, outstanding = _settlement(conn, row)
    data = _invoice_row(row)
    data["paidAmount"] = currency.money_to_text(paid, row["source_currency"])
    data["outstandingAmount"] = currency.money_to_text(outstanding, row["source_currency"])
    return data


def list_invoices(
    *, client_id: Optional[str] = None, client_service_id: Optional[str] = None,
    status: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    _init_schema()
    where: List[str] = []
    params: List[Any] = []
    if client_id:
        where.append("i.client_id = ?")
        params.append(client_id)
    if client_service_id:
        where.append("i.client_service_id = ?")
        params.append(client_service_id)
    if status:
        wanted = str(status).upper()
        if wanted not in INVOICE_STATUSES:
            raise ValueError(f"Unknown invoice status: {status}")
        where.append("i.status = ?")
        params.append(wanted)
    if date_from:
        where.append("i.invoice_date >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("i.invoice_date <= ?")
        params.append(str(date_to)[:10])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT i.*, c.name AS client_name, c.project_name AS project_name, "
            "       cs.title AS service_title "
            "FROM client_invoices i "
            "LEFT JOIN clients c ON c.id = i.client_id "
            "LEFT JOIN client_services cs ON cs.id = i.client_service_id "
            f"{clause} ORDER BY i.invoice_date DESC, i.invoice_number DESC LIMIT ?",
            params,
        ).fetchall()
        result = []
        for row in rows:
            paid, outstanding = _settlement(conn, row)
            data = _invoice_row(row)
            data["paidAmount"] = currency.money_to_text(paid, row["source_currency"])
            data["outstandingAmount"] = currency.money_to_text(outstanding, row["source_currency"])
            result.append(data)
    return result


def issue_invoice(invoice_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    """PLANNED → ISSUED. This is the moment the invoice becomes a document the
    client owes against; its FX snapshot has been frozen since creation."""
    _init_schema()
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM client_invoices WHERE id = ?", (invoice_id,)).fetchone()
            if row is None:
                raise KeyError(invoice_id)
            if row["status"] == "CANCELLED":
                raise ValueError("Отменённый счёт нельзя выставить")
            conn.execute(
                "UPDATE client_invoices SET status = 'ISSUED', issued_at = COALESCE(issued_at, ?), "
                "updated_at = ? WHERE id = ?",
                (now, now, invoice_id),
            )
            _recalculate_invoice_status(conn, invoice_id)
            client_activity.log_event(
                client_activity.INVOICE_ISSUED, client_id=row["client_id"],
                entity_type="client_invoice", entity_id=invoice_id,
                summary=f"Счёт {row['invoice_number']} выставлен", actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_invoice(invoice_id)


def cancel_invoice(invoice_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM client_invoices WHERE id = ?", (invoice_id,)).fetchone()
            if row is None:
                raise KeyError(invoice_id)
            if row["status"] == "PAID":
                raise ValueError("Оплаченный счёт нельзя отменить")
            conn.execute(
                "UPDATE client_invoices SET status = 'CANCELLED', cancelled_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, invoice_id),
            )
            client_activity.log_event(
                client_activity.INVOICE_CANCELLED, client_id=row["client_id"],
                entity_type="client_invoice", entity_id=invoice_id,
                summary=f"Счёт {row['invoice_number']} отменён", actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_invoice(invoice_id)


def update_invoice(invoice_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Edits the mutable parts of an invoice — dates and comment.

    The money and the FX snapshot are not editable: an invoice whose amount can
    be rewritten after issue is not a record. Correcting an issued invoice means
    cancelling it and issuing another, which is what the audit log will show.
    """
    _init_schema()
    sets: List[str] = []
    params: List[Any] = []
    if "dueDate" in payload:
        due = str(payload["dueDate"] or "")[:10]
        if due:
            date.fromisoformat(due)
        sets.append("due_date = ?")
        params.append(due or None)
    if "invoiceDate" in payload:
        when = str(payload["invoiceDate"] or "")[:10]
        date.fromisoformat(when)
        sets.append("invoice_date = ?")
        params.append(when)
    if "comment" in payload:
        sets.append("comment = ?")
        params.append(str(payload["comment"] or ""))
    if not sets:
        return get_invoice(invoice_id)
    sets.append("updated_at = ?")
    params.extend([_now(), invoice_id])
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM client_invoices WHERE id = ?", (invoice_id,)).fetchone()
            if row is None:
                raise KeyError(invoice_id)
            conn.execute(f"UPDATE client_invoices SET {', '.join(sets)} WHERE id = ?", params)
            _recalculate_invoice_status(conn, invoice_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_invoice(invoice_id)


# ── payments ─────────────────────────────────────────────────────────────────

def create_payment(
    *,
    client_id: str,
    amount: Any,
    payment_currency: str,
    invoice_id: Optional[str] = None,
    payment_date: Optional[str] = None,
    payment_method: str = "",
    reference: str = "",
    comment: str = "",
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Registers money received, and re-derives the invoice's status from it.

    The payment gets its own FX snapshot at the moment it is recorded — it is
    a separate financial event from the invoice and may well have happened at a
    different rate. Recording it and moving the invoice to PAID /
    PARTIALLY_PAID happen in one transaction (§68).
    """
    _init_schema()
    code = currency.normalize_currency(payment_currency, field="payment currency")
    value = currency.parse_decimal(amount, field="amount")
    snapshot = currency.snapshot(value, code)
    when = str(payment_date or _today().isoformat())[:10]
    date.fromisoformat(when)
    now = _now()
    payment_id = _new_id("pay")

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
                raise KeyError(client_id)
            if invoice_id:
                invoice = conn.execute(
                    "SELECT id, client_id FROM client_invoices WHERE id = ?", (invoice_id,)
                ).fetchone()
                if invoice is None:
                    raise KeyError(invoice_id)
                if invoice["client_id"] != client_id:
                    raise ValueError("Счёт принадлежит другому клиенту")
            conn.execute(
                "INSERT INTO client_payments "
                "(id, client_id, invoice_id, amount, currency, normalized_usd_amount, "
                " exchange_rate_snapshot, payment_date, payment_method, reference, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payment_id, client_id, invoice_id or None, currency.decimal_to_text(value), code,
                    snapshot["normalizedUsdAmount"], json.dumps(snapshot, ensure_ascii=False),
                    when, str(payment_method or ""), str(reference or ""), str(comment or ""), now,
                ),
            )
            new_status = None
            if invoice_id:
                new_status = _recalculate_invoice_status(conn, invoice_id)
            client_activity.log_event(
                client_activity.PAYMENT_CREATED, client_id=client_id, entity_type="client_payment",
                entity_id=payment_id,
                summary=f"Платёж {currency.format_amount(value, code)}",
                payload={
                    "amount": currency.decimal_to_text(value), "currency": code,
                    "invoiceId": invoice_id, "normalizedUsdAmount": snapshot["normalizedUsdAmount"],
                },
                actor_id=actor_id, conn=conn,
            )
            if new_status == "PAID":
                client_activity.log_event(
                    client_activity.INVOICE_PAID, client_id=client_id, entity_type="client_invoice",
                    entity_id=invoice_id, summary="Счёт полностью оплачен",
                    actor_id=actor_id, conn=conn,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_payment(payment_id)


def get_payment(payment_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT p.*, c.name AS client_name, i.invoice_number AS invoice_number "
            "FROM client_payments p "
            "LEFT JOIN clients c ON c.id = p.client_id "
            "LEFT JOIN client_invoices i ON i.id = p.invoice_id "
            "WHERE p.id = ?",
            (payment_id,),
        ).fetchone()
    return _payment_row(row) if row else None


def list_payments(
    *, client_id: Optional[str] = None, invoice_id: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, limit: int = 200,
) -> List[Dict[str, Any]]:
    _init_schema()
    where: List[str] = []
    params: List[Any] = []
    if client_id:
        where.append("p.client_id = ?")
        params.append(client_id)
    if invoice_id:
        where.append("p.invoice_id = ?")
        params.append(invoice_id)
    if date_from:
        where.append("p.payment_date >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("p.payment_date <= ?")
        params.append(str(date_to)[:10])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(int(limit), 1000)))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT p.*, c.name AS client_name, i.invoice_number AS invoice_number "
            "FROM client_payments p "
            "LEFT JOIN clients c ON c.id = p.client_id "
            "LEFT JOIN client_invoices i ON i.id = p.invoice_id "
            f"{clause} ORDER BY p.payment_date DESC, p.created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_payment_row(row) for row in rows]


def mark_invoice_paid(
    invoice_id: str, *, payment_method: str = "", reference: str = "",
    comment: str = "", actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Settles the whole outstanding balance in one move — the "отметить
    оплаченным" button. Implemented as a real payment record rather than a
    status flip, so the payments ledger and the invoice never disagree."""
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM client_invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            raise KeyError(invoice_id)
        if row["status"] == "CANCELLED":
            raise ValueError("Отменённый счёт нельзя отметить оплаченным")
        _, outstanding = _settlement(conn, row)
        client_id = row["client_id"]
        source = row["source_currency"]
    if outstanding <= 0:
        return get_invoice(invoice_id)
    create_payment(
        client_id=client_id, amount=outstanding, payment_currency=source, invoice_id=invoice_id,
        payment_method=payment_method or "manual", reference=reference,
        comment=comment or "Отмечен оплаченным вручную", actor_id=actor_id,
    )
    return get_invoice(invoice_id)


# ── derived client state ─────────────────────────────────────────────────────

def payment_status_map(client_ids: List[str]) -> Dict[str, str]:
    """Payment status per client for the table's «Оплата» column (§80).

    Worst-first: one overdue invoice makes the client overdue however many
    others are paid. Computed for the whole page in one query rather than per
    row.
    """
    _init_schema()
    if not client_ids:
        return {}
    placeholders = ", ".join("?" for _ in client_ids)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM client_invoices WHERE client_id IN ({placeholders}) "
            "AND status != 'CANCELLED'",
            client_ids,
        ).fetchall()
        by_client: Dict[str, List[str]] = {}
        for row in rows:
            status = _recalculate_invoice_status(conn, row["id"])
            by_client.setdefault(row["client_id"], []).append(status)

    result: Dict[str, str] = {}
    for client_id in client_ids:
        statuses = by_client.get(client_id, [])
        if not statuses:
            result[client_id] = "NO_INVOICE"
        elif "OVERDUE" in statuses:
            result[client_id] = "OVERDUE"
        elif "PARTIALLY_PAID" in statuses:
            result[client_id] = "PARTIALLY_PAID"
        elif any(status in ("ISSUED",) for status in statuses):
            result[client_id] = "AWAITING_PAYMENT"
        elif all(status == "PLANNED" for status in statuses):
            result[client_id] = "PLANNED"
        else:
            result[client_id] = "PAID"
    return result


# ── MRR and the dashboard ────────────────────────────────────────────────────

def _monthly_equivalent(amount: Decimal, frequency: str, custom_interval_days: Optional[int]) -> Decimal:
    """One service's contribution to monthly recurring revenue.

    Every frequency is normalized to "per month" before anything is summed —
    a yearly contract is a twelfth of itself each month, and a custom interval
    is scaled by how many times it fits into an average month. ONE_TIME
    contributes nothing: it is revenue, but it is not *recurring* revenue, and
    mixing them makes MRR meaningless.
    """
    months = clients.FREQUENCY_MONTHS.get(frequency)
    if months:
        return amount / Decimal(months)
    if frequency == "CUSTOM" and custom_interval_days:
        interval = Decimal(int(custom_interval_days))
        if interval > 0:
            # 365.25/12 — the average month, so a 90-day cycle lands on a third
            # of its amount rather than on a calendar-quarter approximation.
            return amount * (Decimal("30.4375") / interval)
    return Decimal(0)


def calculate_mrr(rates: Optional[Dict[str, Decimal]] = None) -> Decimal:
    """Total MRR in USD.

    Counts only ACTIVE recurring services belonging to ACTIVE clients (§44):
    a paused service is not billing, and an archived client is not revenue.
    Each service is normalized to a month *in its own currency*, then converted
    to USD — never the other way round, so the currency conversion happens once
    per service at full precision.
    """
    _init_schema()
    rates = rates if rates is not None else currency.get_rates()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cs.amount, cs.currency, b.frequency, b.custom_interval_days "
            "FROM client_services cs "
            "JOIN clients c ON c.id = cs.client_id "
            "LEFT JOIN billing_configurations b ON b.client_service_id = cs.id "
            "WHERE cs.status = 'ACTIVE' AND cs.service_type = 'RECURRING' AND c.status = 'ACTIVE'"
        ).fetchall()
    total = Decimal(0)
    for row in rows:
        try:
            amount = Decimal(str(row["amount"]))
        except (TypeError, ValueError):
            continue
        monthly = _monthly_equivalent(
            amount, str(row["frequency"] or "MONTHLY"), row["custom_interval_days"]
        )
        if monthly <= 0:
            continue
        total += currency.convert(monthly, row["currency"], currency.BASE_CURRENCY, rates)
    return total


def _upcoming_billings(conn: sqlite3.Connection, rates: Dict[str, Decimal], days: int) -> Tuple[Decimal, int]:
    """What is scheduled to be invoiced in the next `days` days, in USD.

    Reads the billing schedule rather than existing invoices: the card answers
    "what is about to go out", and an invoice that already exists has already
    gone out.
    """
    horizon = (_today() + timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT cs.amount, cs.currency FROM billing_configurations b "
        "JOIN client_services cs ON cs.id = b.client_service_id "
        "JOIN clients c ON c.id = cs.client_id "
        "WHERE b.next_billing_date IS NOT NULL AND b.next_billing_date <= ? "
        "AND cs.status = 'ACTIVE' AND c.status = 'ACTIVE'",
        (horizon,),
    ).fetchall()
    total = Decimal(0)
    for row in rows:
        total += currency.convert(row["amount"] or 0, row["currency"], currency.BASE_CURRENCY, rates)
    return total, len(rows)


def _overdue_total(conn: sqlite3.Connection, rates: Dict[str, Decimal]) -> Tuple[Decimal, int]:
    """Outstanding balance on overdue invoices, in USD.

    Uses each invoice's *own frozen* snapshot to get to USD — the debt on a
    January invoice is what it was worth in January, not what today's rate
    would make of it.
    """
    rows = conn.execute(
        "SELECT * FROM client_invoices WHERE status != 'CANCELLED'"
    ).fetchall()
    total = Decimal(0)
    count = 0
    for row in rows:
        if _recalculate_invoice_status(conn, row["id"]) != "OVERDUE":
            continue
        _, outstanding = _settlement(conn, row)
        if outstanding <= 0:
            continue
        snapshot = _json_or_empty(row["exchange_rate_snapshot"])
        rate = snapshot.get("sourceToUsdRate")
        if rate and Decimal(str(rate)) > 0:
            total += outstanding * Decimal(str(rate))
        else:
            total += currency.convert(outstanding, row["source_currency"], currency.BASE_CURRENCY, rates)
        count += 1
    return total, count


def _pair(usd: Decimal, display: str, rates: Dict[str, Decimal]) -> Dict[str, str]:
    """Every money figure the dashboard returns carries both its USD value and
    its display-currency value — the frontend renders, it does not convert."""
    display_value = currency.convert(usd, currency.BASE_CURRENCY, display, rates)
    return {
        "usd": currency.money_to_text(usd, currency.BASE_CURRENCY),
        "display": currency.money_to_text(display_value, display),
    }


def dashboard(display_currency: Optional[str] = None) -> Dict[str, Any]:
    """The KPI row above the clients table."""
    _init_schema()
    rates = currency.get_rates()
    display = currency.normalize_currency(display_currency or currency.get_display_currency())
    client_counts = clients.count_clients()
    connection_counts = clients.count_connections()
    mrr = calculate_mrr(rates)
    with _connect() as conn:
        to_invoice, to_invoice_count = _upcoming_billings(conn, rates, UPCOMING_WINDOW_DAYS)
        overdue, overdue_count = _overdue_total(conn, rates)
        paid_row = conn.execute(
            "SELECT COALESCE(SUM(CAST(normalized_usd_amount AS REAL)), 0) AS total FROM client_payments"
        ).fetchone()
    return {
        "currency": display,
        "baseCurrency": currency.BASE_CURRENCY,
        "totalClients": client_counts["total"],
        "activeClients": client_counts["active"],
        "connectedAgents": connection_counts["connected"],
        "agentsWithErrors": connection_counts["error"],
        "agentsOffline": connection_counts["offline"],
        "mrr": _pair(mrr, display, rates),
        "toInvoice": {**_pair(to_invoice, display, rates), "count": to_invoice_count},
        "overdue": {**_pair(overdue, display, rates), "count": overdue_count},
        # Lifetime collected. A REAL SUM is fine here and only here: it is a
        # headline figure, never an amount anyone is billed from.
        "collectedUsd": f"{float(paid_row['total'] or 0):.2f}",
    }


def client_billing_summary(client_id: str, display_currency: Optional[str] = None) -> Dict[str, Any]:
    """The Billing block on a client's card: MRR, next invoice, debt, paid."""
    _init_schema()
    rates = currency.get_rates()
    display = currency.normalize_currency(display_currency or currency.get_display_currency())
    services = clients.list_client_services(client_id)

    mrr = Decimal(0)
    next_billing: Optional[str] = None
    for service in services:
        billing = service.get("billing") or {}
        if service["status"] == "ACTIVE" and service["serviceType"] == "RECURRING":
            monthly = _monthly_equivalent(
                Decimal(str(service["amount"])), str(billing.get("frequency") or "MONTHLY"),
                billing.get("customIntervalDays"),
            )
            if monthly > 0:
                mrr += currency.convert(monthly, service["currency"], currency.BASE_CURRENCY, rates)
        candidate = billing.get("nextBillingDate")
        if candidate and (next_billing is None or candidate < next_billing):
            next_billing = candidate

    outstanding_usd = Decimal(0)
    paid_usd = Decimal(0)
    with _connect() as conn:
        for row in conn.execute(
            "SELECT * FROM client_invoices WHERE client_id = ? AND status != 'CANCELLED'", (client_id,)
        ).fetchall():
            _recalculate_invoice_status(conn, row["id"])
            _, outstanding = _settlement(conn, row)
            if outstanding > 0:
                snapshot = _json_or_empty(row["exchange_rate_snapshot"])
                rate = snapshot.get("sourceToUsdRate")
                if rate and Decimal(str(rate)) > 0:
                    outstanding_usd += outstanding * Decimal(str(rate))
                else:
                    outstanding_usd += currency.convert(
                        outstanding, row["source_currency"], currency.BASE_CURRENCY, rates
                    )
        for row in conn.execute(
            "SELECT normalized_usd_amount FROM client_payments WHERE client_id = ?", (client_id,)
        ).fetchall():
            paid_usd += Decimal(str(row["normalized_usd_amount"]))

    return {
        "currency": display,
        "baseCurrency": currency.BASE_CURRENCY,
        "mrr": _pair(mrr, display, rates),
        "outstanding": _pair(outstanding_usd, display, rates),
        "paid": _pair(paid_usd, display, rates),
        "nextBillingDate": next_billing,
    }


# ── automation ───────────────────────────────────────────────────────────────

def billing_period_key(when: date, frequency: str) -> str:
    """The period an invoice covers, and half of the scheduler's idempotency
    key. Month granularity for monthly cycles, quarter/half/year labels for
    longer ones, the exact date for CUSTOM — a 10-day cycle bills several times
    in one month and must not collide with itself."""
    if frequency == "MONTHLY":
        return f"{when.year}-{when.month:02d}"
    if frequency == "QUARTERLY":
        return f"{when.year}-Q{(when.month - 1) // 3 + 1}"
    if frequency == "SEMI_ANNUAL":
        return f"{when.year}-H{1 if when.month <= 6 else 2}"
    if frequency == "YEARLY":
        return str(when.year)
    return when.isoformat()


def generate_due_invoices(today: Optional[date] = None, actor_id: str = "scheduler") -> Dict[str, Any]:
    """Creates the invoices that fall due today, and moves each schedule on.

    Idempotent by construction (§54): the (client_service_id, billing_period)
    UNIQUE index rejects a second insert for the same period, so a re-run, an
    overlapping run or a manual trigger on the same day produces zero extra
    invoices — the duplicate is counted as "skipped", not raised.

    The schedule is advanced in the same transaction as the invoice that
    consumed it: a crash between the two would otherwise either bill twice or
    stop billing entirely.
    """
    _init_schema()
    today = today or _today()
    created: List[str] = []
    skipped = 0
    failed = 0

    with _connect() as conn:
        due = conn.execute(
            "SELECT b.*, cs.client_id, cs.amount, cs.currency, cs.title, cs.service_type "
            "FROM billing_configurations b "
            "JOIN client_services cs ON cs.id = b.client_service_id "
            "JOIN clients c ON c.id = cs.client_id "
            "WHERE b.next_billing_date IS NOT NULL AND b.next_billing_date <= ? "
            "AND b.frequency != 'ONE_TIME' AND cs.status = 'ACTIVE' AND c.status = 'ACTIVE'",
            (today.isoformat(),),
        ).fetchall()

    for row in due:
        period_date = date.fromisoformat(str(row["next_billing_date"])[:10])
        period = billing_period_key(period_date, str(row["frequency"]))
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                create_invoice(
                    client_id=row["client_id"], amount=row["amount"], source_currency=row["currency"],
                    client_service_id=row["client_service_id"],
                    invoice_date=period_date.isoformat(), billing_period=period,
                    status="ISSUED", origin="scheduler",
                    comment=f"Автоматический счёт за период {period}",
                    actor_id=actor_id, conn=conn,
                )
                if row["auto_advance_billing_date"]:
                    next_date = clients.advance_billing_date(
                        period_date.isoformat(), str(row["frequency"]),
                        row["billing_day"], row["custom_interval_days"],
                    )
                    conn.execute(
                        "UPDATE billing_configurations SET next_billing_date = ?, updated_at = ? "
                        "WHERE id = ?",
                        (next_date, _now(), row["id"]),
                    )
                conn.execute("COMMIT")
                created.append(f"{row['client_service_id']}:{period}")
            except DuplicateInvoice:
                conn.execute("ROLLBACK")
                skipped += 1
                # The invoice for this period exists, but the schedule clearly
                # did not move on (or we would not be here). Advance it in its
                # own transaction so the sweep converges instead of retrying
                # the same period forever.
                if row["auto_advance_billing_date"]:
                    next_date = clients.advance_billing_date(
                        period_date.isoformat(), str(row["frequency"]),
                        row["billing_day"], row["custom_interval_days"],
                    )
                    with _connect() as fix:
                        fix.execute(
                            "UPDATE billing_configurations SET next_billing_date = ?, updated_at = ? "
                            "WHERE id = ? AND next_billing_date = ?",
                            (next_date, _now(), row["id"], row["next_billing_date"]),
                        )
            except Exception:
                conn.execute("ROLLBACK")
                failed += 1
                logger.exception(
                    "Recurring invoice failed for service %s period %s",
                    row["client_service_id"], period,
                )

    if created or skipped or failed:
        logger.info(
            "Recurring invoice sweep: %d created, %d skipped (already billed), %d failed",
            len(created), skipped, failed,
        )
    return {"created": created, "createdCount": len(created), "skipped": skipped, "failed": failed}


def sweep_overdue_invoices() -> int:
    """Re-derives every open invoice's status so past-due ones become OVERDUE
    (§56). Enforcement does not depend on this sweep — every read path
    recalculates too — it just keeps the stored status queryable."""
    _init_schema()
    placeholders = ", ".join("?" for _ in OPEN_INVOICE_STATUSES)
    changed = 0
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id, status FROM client_invoices WHERE status IN ({placeholders})",
            OPEN_INVOICE_STATUSES,
        ).fetchall()
        for row in rows:
            if _recalculate_invoice_status(conn, row["id"]) != row["status"]:
                changed += 1
    return changed


async def scheduler_loop(interval_seconds: int = 3600) -> None:
    """Daily-ish background job (§53): generates due recurring invoices and
    ages unpaid ones into OVERDUE.

    Runs hourly rather than once at 02:00 because the backend restarts freely
    and a fixed-time job would simply be missed on a day it was down; the sweep
    is idempotent, so running it 24 times a day costs nothing extra.
    """
    import asyncio

    while True:
        try:
            result = await asyncio.to_thread(generate_due_invoices)
            aged = await asyncio.to_thread(sweep_overdue_invoices)
            if result["createdCount"] or aged:
                logger.info(
                    "Client billing sweep: %d invoices created, %d marked overdue",
                    result["createdCount"], aged,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Client billing sweep failed")
        await asyncio.sleep(interval_seconds)
