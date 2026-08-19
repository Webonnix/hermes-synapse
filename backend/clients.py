"""Commercial client book: who we sell to, what we sell them, and which agent
is delivering it.

This is deliberately *not* the same model as `backend/bot_access.py` +
`backend/payments.py`. Those cover the technical side — an agent's plan, its
token, its quota, what the infrastructure costs. This module covers the
commercial side — a client, the service they bought, the price they pay, the
agent assigned to them. Two tables, two questions:

    revenue (this module)  −  cost (agent billing)  =  gross margin

Keeping them apart is what makes that subtraction possible later; merging them
into one "billing" table would make it permanently ambiguous whether a row is
money coming in or money going out.

Money is stored as `amount` + `currency`, never as a formatted string, and the
amount column is TEXT holding an exact decimal (see backend/currency.py for why
not NUMERIC). Nothing in this module rounds; `backend/currency.py` does that at
the edges.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from backend import client_activity, currency
from backend.database import DB_PATH

logger = logging.getLogger("hermes.clients")

CLIENT_TYPES = ("COMPANY", "PERSON")
CLIENT_STATUSES = ("ACTIVE", "PAUSED", "COMPLETED", "OVERDUE", "ARCHIVED")
SERVICE_TYPES = ("ONE_TIME", "RECURRING")
CLIENT_SERVICE_STATUSES = ("ACTIVE", "PAUSED", "COMPLETED")
BILLING_FREQUENCIES = ("ONE_TIME", "MONTHLY", "QUARTERLY", "SEMI_ANNUAL", "YEARLY", "CUSTOM")

# How many months one billing period covers. CUSTOM/ONE_TIME are not month
# based and are handled separately in `advance_billing_date`.
FREQUENCY_MONTHS: Dict[str, int] = {
    "MONTHLY": 1,
    "QUARTERLY": 3,
    "SEMI_ANNUAL": 6,
    "YEARLY": 12,
}

CONNECTION_STATUSES = (
    "CONNECTED", "NOT_CONNECTED", "DISCONNECTED", "OFFLINE", "ERROR", "PAUSED",
)
CONNECTION_TYPES = ("BOT", "API", "TOKEN", "CHANNEL", "INTERNAL")

# What the operator *asked* for, as opposed to what the connection currently
# *is*. Status is derived from this plus the agent's health — see
# `resolve_connection_status`, which is why there is no `connected: bool`
# anywhere in this module (§22).
DESIRED_STATES = ("ACTIVE", "DISCONNECTED", "PAUSED")

# An agent that has not been seen for this long is OFFLINE: the connection is
# configured and nobody turned it off, but nothing is answering. Configurable
# because "too long" depends on how chatty the deployment's agents are.
OFFLINE_THRESHOLD_MINUTES = int(os.getenv("CLIENT_AGENT_OFFLINE_MINUTES", "30"))

_schema_ready = False


# ── plumbing ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # SQLite's built-in LOWER() and LIKE only fold ASCII, so "Интернет" and
    # "интернет" are different strings to it and search silently misses every
    # Cyrillic client typed in another case. Python's str.lower does full
    # Unicode folding; the search query calls this instead of LOWER().
    conn.create_function("unicode_lower", 1, lambda value: value.lower() if isinstance(value, str) else value)
    return conn


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _check_enum(value: Any, allowed: Tuple[str, ...], field: str, default: Optional[str] = None) -> str:
    text = str(value or "").strip().upper()
    if not text and default is not None:
        return default
    if text not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)} (got {value!r})")
    return text


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _validate_email(email: str) -> str:
    email = _clean(email)
    if not email:
        return ""
    # Deliberately loose: one @, something on each side, no spaces. Anything
    # stricter rejects addresses that genuinely deliver.
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain or " " in email:
        raise ValueError(f"Некорректный email: {email}")
    return email


def _init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    # Dependencies first, and outside any transaction — see currency.ensure_schema.
    currency.ensure_schema()
    client_activity.ensure_schema()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'COMPANY',
                name TEXT NOT NULL,
                project_name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                primary_contact_id TEXT,
                responsible_user_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_clients_status ON clients (status);
            CREATE INDEX IF NOT EXISTS idx_clients_name ON clients (name);

            CREATE TABLE IF NOT EXISTS client_contacts (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                telegram TEXT NOT NULL DEFAULT '',
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_client_contacts_client ON client_contacts (client_id);

            CREATE TABLE IF NOT EXISTS services (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS client_services (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                service_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                service_type TEXT NOT NULL DEFAULT 'RECURRING',
                amount TEXT NOT NULL DEFAULT '0',
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_client_services_client ON client_services (client_id);
            CREATE INDEX IF NOT EXISTS idx_client_services_service ON client_services (service_id);
            CREATE INDEX IF NOT EXISTS idx_client_services_status ON client_services (status, service_type);

            CREATE TABLE IF NOT EXISTS billing_configurations (
                id TEXT PRIMARY KEY,
                client_service_id TEXT NOT NULL UNIQUE,
                frequency TEXT NOT NULL DEFAULT 'MONTHLY',
                billing_day INTEGER,
                next_billing_date TEXT,
                custom_interval_days INTEGER,
                auto_advance_billing_date INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_billing_config_next
                ON billing_configurations (next_billing_date);

            CREATE TABLE IF NOT EXISTS client_agent_connections (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                client_service_id TEXT,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'NOT_CONNECTED',
                desired_state TEXT NOT NULL DEFAULT 'ACTIVE',
                connection_type TEXT NOT NULL DEFAULT 'INTERNAL',
                channel TEXT NOT NULL DEFAULT '',
                connected_at TEXT,
                disconnected_at TEXT,
                last_seen_at TEXT,
                last_health_check_at TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cac_client ON client_agent_connections (client_id);
            CREATE INDEX IF NOT EXISTS idx_cac_agent ON client_agent_connections (agent_id);
            CREATE INDEX IF NOT EXISTS idx_cac_status ON client_agent_connections (status);
            """
        )
    _schema_ready = True


def reset_schema_cache() -> None:
    """See client_activity.reset_schema_cache."""
    global _schema_ready
    _schema_ready = False


# ── row mapping ──────────────────────────────────────────────────────────────

def _client_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "projectName": row["project_name"],
        "description": row["description"],
        "status": row["status"],
        "primaryContactId": row["primary_contact_id"],
        "responsibleUserId": row["responsible_user_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "archivedAt": row["archived_at"],
    }


def _contact_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "clientId": row["client_id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "telegram": row["telegram"],
        "isPrimary": bool(row["is_primary"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _service_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "isActive": bool(row["is_active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _client_service_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = {
        "id": row["id"],
        "clientId": row["client_id"],
        "serviceId": row["service_id"],
        "title": row["title"],
        "description": row["description"],
        "serviceType": row["service_type"],
        "amount": row["amount"],
        "currency": row["currency"],
        "status": row["status"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    keys = row.keys()
    if "service_name" in keys:
        data["serviceName"] = row["service_name"]
    return data


def _billing_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "clientServiceId": row["client_service_id"],
        "frequency": row["frequency"],
        "billingDay": row["billing_day"],
        "nextBillingDate": row["next_billing_date"],
        "customIntervalDays": row["custom_interval_days"],
        "autoAdvanceBillingDate": bool(row["auto_advance_billing_date"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _connection_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = {
        "id": row["id"],
        "clientId": row["client_id"],
        "clientServiceId": row["client_service_id"],
        "agentId": row["agent_id"],
        "status": row["status"],
        "desiredState": row["desired_state"],
        "connectionType": row["connection_type"],
        "channel": row["channel"],
        "connectedAt": row["connected_at"],
        "disconnectedAt": row["disconnected_at"],
        "lastSeenAt": row["last_seen_at"],
        "lastHealthCheckAt": row["last_health_check_at"],
        "errorCode": row["error_code"],
        "errorMessage": row["error_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    keys = row.keys()
    if "agent_name" in keys:
        data["agentName"] = row["agent_name"]
    if "client_name" in keys:
        data["clientName"] = row["client_name"]
    if "project_name" in keys:
        data["projectName"] = row["project_name"]
    if "service_title" in keys:
        data["serviceTitle"] = row["service_title"]
    return data


# ── services catalogue ───────────────────────────────────────────────────────

def list_services(include_inactive: bool = True) -> List[Dict[str, Any]]:
    _init_schema()
    clause = "" if include_inactive else "WHERE is_active = 1"
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM services {clause} ORDER BY name COLLATE NOCASE").fetchall()
    return [_service_row(row) for row in rows]


def create_service(name: str, description: str = "", is_active: bool = True) -> Dict[str, Any]:
    _init_schema()
    name = _clean(name)
    if not name:
        raise ValueError("Название услуги обязательно")
    now = _now()
    service_id = _new_id("svc")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO services (id, name, description, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (service_id, name, _clean(description), 1 if is_active else 0, now, now),
        )
    return get_service(service_id)


def get_service(service_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    return _service_row(row) if row else None


def update_service(service_id: str, **fields: Any) -> Dict[str, Any]:
    _init_schema()
    existing = get_service(service_id)
    if not existing:
        raise KeyError(service_id)
    sets: List[str] = []
    params: List[Any] = []
    if "name" in fields:
        name = _clean(fields["name"])
        if not name:
            raise ValueError("Название услуги обязательно")
        sets.append("name = ?")
        params.append(name)
    if "description" in fields:
        sets.append("description = ?")
        params.append(_clean(fields["description"]))
    if "is_active" in fields:
        sets.append("is_active = ?")
        params.append(1 if fields["is_active"] else 0)
    if not sets:
        return existing
    sets.append("updated_at = ?")
    params.extend([_now(), service_id])
    with _connect() as conn:
        conn.execute(f"UPDATE services SET {', '.join(sets)} WHERE id = ?", params)
    return get_service(service_id)


def _ensure_service(conn: sqlite3.Connection, service_id: Optional[str], fallback_name: str) -> str:
    """Resolves the catalogue service a client service points at.

    Accepts an existing id, or a name for an ad-hoc service typed into the
    create drawer — the drawer offers a free-text field precisely so nobody has
    to leave the flow to create a catalogue entry first. Unknown ids are an
    error, not an implicit create: that would let a typo silently fork the
    catalogue.
    """
    service_id = _clean(service_id)
    if service_id:
        row = conn.execute("SELECT id FROM services WHERE id = ?", (service_id,)).fetchone()
        if row:
            return row["id"]
        raise ValueError(f"Услуга не найдена: {service_id}")
    name = _clean(fallback_name)
    if not name:
        raise ValueError("Нужно выбрать услугу или указать её название")
    row = conn.execute("SELECT id FROM services WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    now = _now()
    new_id = _new_id("svc")
    conn.execute(
        "INSERT INTO services (id, name, description, is_active, created_at, updated_at) "
        "VALUES (?, ?, '', 1, ?, ?)",
        (new_id, name, now, now),
    )
    return new_id


# ── billing configuration ────────────────────────────────────────────────────

def _clamp_billing_day(day: Optional[Any]) -> Optional[int]:
    if day in (None, ""):
        return None
    value = int(day)
    if not 1 <= value <= 31:
        raise ValueError("День выставления должен быть в диапазоне 1..31")
    return value


def _add_months(anchor: date, months: int, billing_day: Optional[int]) -> date:
    """Anchor + N months, pinned to `billing_day` and clamped to the month's
    length — a service billed on the 31st bills on the 30th in a 30-day month
    and goes back to the 31st afterwards, instead of drifting earlier forever."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    target_day = billing_day or anchor.day
    # Last day of the target month, found by stepping back from the 1st of the
    # next one — no calendar table needed.
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(target_day, last_day))


def advance_billing_date(current: str, frequency: str, billing_day: Optional[int] = None,
                         custom_interval_days: Optional[int] = None) -> Optional[str]:
    """The next date after `current` for this frequency. None for ONE_TIME —
    a one-off has no "next", and returning today's date would make the
    scheduler bill it forever."""
    frequency = _check_enum(frequency, BILLING_FREQUENCIES, "frequency")
    if frequency == "ONE_TIME":
        return None
    anchor = date.fromisoformat(current[:10])
    if frequency == "CUSTOM":
        interval = int(custom_interval_days or 0)
        if interval <= 0:
            raise ValueError("customIntervalDays должен быть больше 0")
        return (anchor + timedelta(days=interval)).isoformat()
    return _add_months(anchor, FREQUENCY_MONTHS[frequency], billing_day).isoformat()


def _upsert_billing_configuration(
    conn: sqlite3.Connection,
    client_service_id: str,
    payload: Dict[str, Any],
    service_type: str,
) -> None:
    frequency = _check_enum(
        payload.get("frequency"), BILLING_FREQUENCIES, "frequency",
        default="ONE_TIME" if service_type == "ONE_TIME" else "MONTHLY",
    )
    if service_type == "ONE_TIME":
        frequency = "ONE_TIME"
    billing_day = _clamp_billing_day(payload.get("billingDay"))
    custom_interval = payload.get("customIntervalDays")
    if frequency == "CUSTOM":
        custom_interval = int(custom_interval or 0)
        if custom_interval <= 0:
            raise ValueError("customIntervalDays должен быть больше 0")
    else:
        custom_interval = int(custom_interval) if custom_interval else None

    next_date = _clean(payload.get("nextBillingDate"))[:10] or None
    if next_date:
        date.fromisoformat(next_date)  # raises on a malformed date
    elif frequency != "ONE_TIME":
        # No date given: bill on the next occurrence of billing_day, or one
        # period from today when the caller did not pin a day either.
        today = datetime.now(timezone.utc).date()
        if billing_day:
            candidate = _add_months(today, 0, billing_day)
            if candidate <= today:
                candidate = _add_months(today, 1, billing_day)
            next_date = candidate.isoformat()
        else:
            next_date = advance_billing_date(today.isoformat(), frequency, billing_day, custom_interval)

    now = _now()
    existing = conn.execute(
        "SELECT id FROM billing_configurations WHERE client_service_id = ?", (client_service_id,)
    ).fetchone()
    auto_advance = 1 if payload.get("autoAdvanceBillingDate", True) else 0
    if existing:
        conn.execute(
            "UPDATE billing_configurations SET frequency = ?, billing_day = ?, next_billing_date = ?, "
            "custom_interval_days = ?, auto_advance_billing_date = ?, updated_at = ? WHERE id = ?",
            (frequency, billing_day, next_date, custom_interval, auto_advance, now, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO billing_configurations "
            "(id, client_service_id, frequency, billing_day, next_billing_date, custom_interval_days, "
            " auto_advance_billing_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("bcfg"), client_service_id, frequency, billing_day, next_date,
             custom_interval, auto_advance, now, now),
        )


def get_billing_configuration(client_service_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM billing_configurations WHERE client_service_id = ?", (client_service_id,)
        ).fetchone()
    return _billing_row(row) if row else None


# ── client services ──────────────────────────────────────────────────────────

def _insert_client_service(
    conn: sqlite3.Connection, client_id: str, payload: Dict[str, Any]
) -> str:
    service_id = _ensure_service(conn, payload.get("serviceId"), payload.get("serviceName", ""))
    service_type = _check_enum(payload.get("serviceType"), SERVICE_TYPES, "serviceType", default="RECURRING")
    status = _check_enum(payload.get("status"), CLIENT_SERVICE_STATUSES, "status", default="ACTIVE")
    # Amount and currency stay two columns forever: "$1,500" as one string
    # cannot be summed, converted or re-displayed in another currency.
    amount = currency.parse_decimal(payload.get("amount", 0), field="amount", allow_zero=True)
    code = currency.normalize_currency(payload.get("currency") or currency.BASE_CURRENCY)
    now = _now()
    client_service_id = _new_id("csvc")
    conn.execute(
        "INSERT INTO client_services "
        "(id, client_id, service_id, title, description, service_type, amount, currency, status, "
        " started_at, completed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            client_service_id, client_id, service_id, _clean(payload.get("title")),
            _clean(payload.get("description")), service_type,
            currency.decimal_to_text(amount), code, status,
            _clean(payload.get("startedAt")) or now, _clean(payload.get("completedAt")) or None,
            now, now,
        ),
    )
    _upsert_billing_configuration(conn, client_service_id, payload.get("billing") or payload, service_type)
    return client_service_id


def add_client_service(client_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    """A client can hold any number of services; this appends one."""
    _init_schema()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
                raise KeyError(client_id)
            client_service_id = _insert_client_service(conn, client_id, payload)
            client_activity.log_event(
                client_activity.SERVICE_CREATED,
                client_id=client_id, entity_type="client_service", entity_id=client_service_id,
                summary=f"Услуга добавлена: {_clean(payload.get('title')) or payload.get('serviceName', '')}",
                payload={"amount": str(payload.get("amount")), "currency": payload.get("currency")},
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_client_service(client_service_id)


def get_client_service(client_service_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT cs.*, s.name AS service_name FROM client_services cs "
            "LEFT JOIN services s ON s.id = cs.service_id WHERE cs.id = ?",
            (client_service_id,),
        ).fetchone()
    if not row:
        return None
    data = _client_service_row(row)
    data["billing"] = get_billing_configuration(client_service_id)
    return data


def update_client_service(
    client_service_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None
) -> Dict[str, Any]:
    _init_schema()
    existing = get_client_service(client_service_id)
    if not existing:
        raise KeyError(client_service_id)
    sets: List[str] = []
    params: List[Any] = []
    if "title" in payload:
        sets.append("title = ?")
        params.append(_clean(payload["title"]))
    if "description" in payload:
        sets.append("description = ?")
        params.append(_clean(payload["description"]))
    if "serviceType" in payload:
        sets.append("service_type = ?")
        params.append(_check_enum(payload["serviceType"], SERVICE_TYPES, "serviceType"))
    if "status" in payload:
        sets.append("status = ?")
        params.append(_check_enum(payload["status"], CLIENT_SERVICE_STATUSES, "status"))
    if "amount" in payload:
        sets.append("amount = ?")
        params.append(currency.decimal_to_text(
            currency.parse_decimal(payload["amount"], field="amount", allow_zero=True)
        ))
    if "currency" in payload:
        sets.append("currency = ?")
        params.append(currency.normalize_currency(payload["currency"]))
    if "completedAt" in payload:
        sets.append("completed_at = ?")
        params.append(_clean(payload["completedAt"]) or None)
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if sets:
                sets.append("updated_at = ?")
                params.extend([now, client_service_id])
                conn.execute(f"UPDATE client_services SET {', '.join(sets)} WHERE id = ?", params)
            if "billing" in payload or "frequency" in payload:
                service_type = _check_enum(
                    payload.get("serviceType", existing["serviceType"]), SERVICE_TYPES, "serviceType"
                )
                _upsert_billing_configuration(
                    conn, client_service_id, payload.get("billing") or payload, service_type
                )
            client_activity.log_event(
                client_activity.SERVICE_UPDATED,
                client_id=existing["clientId"], entity_type="client_service",
                entity_id=client_service_id, summary="Услуга обновлена",
                payload={k: str(v) for k, v in payload.items() if k != "billing"},
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_client_service(client_service_id)


def list_client_services(client_id: str) -> List[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cs.*, s.name AS service_name FROM client_services cs "
            "LEFT JOIN services s ON s.id = cs.service_id "
            "WHERE cs.client_id = ? ORDER BY cs.created_at",
            (client_id,),
        ).fetchall()
        configs = {
            row["client_service_id"]: _billing_row(row)
            for row in conn.execute(
                "SELECT b.* FROM billing_configurations b "
                "JOIN client_services cs ON cs.id = b.client_service_id WHERE cs.client_id = ?",
                (client_id,),
            ).fetchall()
        }
    result = []
    for row in rows:
        data = _client_service_row(row)
        data["billing"] = configs.get(row["id"])
        result.append(data)
    return result


# ── contacts ─────────────────────────────────────────────────────────────────

def _insert_contact(conn: sqlite3.Connection, client_id: str, payload: Dict[str, Any]) -> str:
    now = _now()
    contact_id = _new_id("cnt")
    conn.execute(
        "INSERT INTO client_contacts "
        "(id, client_id, name, email, phone, telegram, is_primary, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            contact_id, client_id, _clean(payload.get("name")), _validate_email(payload.get("email")),
            _clean(payload.get("phone")), _clean(payload.get("telegram")),
            1 if payload.get("isPrimary") else 0, now, now,
        ),
    )
    return contact_id


def add_contact(client_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
                raise KeyError(client_id)
            contact_id = _insert_contact(conn, client_id, payload)
            if payload.get("isPrimary"):
                conn.execute(
                    "UPDATE client_contacts SET is_primary = 0 WHERE client_id = ? AND id != ?",
                    (client_id, contact_id),
                )
                conn.execute(
                    "UPDATE clients SET primary_contact_id = ?, updated_at = ? WHERE id = ?",
                    (contact_id, _now(), client_id),
                )
            client_activity.log_event(
                client_activity.CONTACT_CREATED, client_id=client_id, entity_type="client_contact",
                entity_id=contact_id, summary=f"Контакт добавлен: {_clean(payload.get('name'))}",
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_contact(contact_id)


def get_contact(contact_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM client_contacts WHERE id = ?", (contact_id,)).fetchone()
    return _contact_row(row) if row else None


def list_contacts(client_id: str) -> List[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM client_contacts WHERE client_id = ? ORDER BY is_primary DESC, created_at",
            (client_id,),
        ).fetchall()
    return [_contact_row(row) for row in rows]


def update_contact(contact_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    existing = get_contact(contact_id)
    if not existing:
        raise KeyError(contact_id)
    mapping = {
        "name": ("name", _clean), "email": ("email", _validate_email),
        "phone": ("phone", _clean), "telegram": ("telegram", _clean),
    }
    sets: List[str] = []
    params: List[Any] = []
    for key, (column, coerce) in mapping.items():
        if key in payload:
            sets.append(f"{column} = ?")
            params.append(coerce(payload[key]))
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if sets:
                sets.append("updated_at = ?")
                params.extend([_now(), contact_id])
                conn.execute(f"UPDATE client_contacts SET {', '.join(sets)} WHERE id = ?", params)
            if payload.get("isPrimary"):
                conn.execute(
                    "UPDATE client_contacts SET is_primary = CASE WHEN id = ? THEN 1 ELSE 0 END "
                    "WHERE client_id = ?",
                    (contact_id, existing["clientId"]),
                )
                conn.execute(
                    "UPDATE clients SET primary_contact_id = ?, updated_at = ? WHERE id = ?",
                    (contact_id, _now(), existing["clientId"]),
                )
            client_activity.log_event(
                client_activity.CONTACT_UPDATED, client_id=existing["clientId"],
                entity_type="client_contact", entity_id=contact_id, summary="Контакт обновлён",
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_contact(contact_id)


def delete_contact(contact_id: str) -> bool:
    _init_schema()
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM client_contacts WHERE id = ?", (contact_id,))
        conn.execute(
            "UPDATE clients SET primary_contact_id = NULL WHERE primary_contact_id = ?", (contact_id,)
        )
    return cursor.rowcount > 0


# ── clients ──────────────────────────────────────────────────────────────────

def create_client(payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Creates the client and, in the same transaction, whatever the drawer
    filled in alongside it — contact, service, billing schedule, agent
    connection. One transaction because a client with a price but no contact,
    or a service whose billing row failed to write, is not a state anyone
    should be able to observe (§68).
    """
    _init_schema()
    name = _clean(payload.get("name"))
    if not name:
        raise ValueError("Название клиента обязательно")
    client_type = _check_enum(payload.get("type"), CLIENT_TYPES, "type", default="COMPANY")
    status = _check_enum(payload.get("status"), CLIENT_STATUSES, "status", default="ACTIVE")
    now = _now()
    client_id = _new_id("cli")

    contacts = payload.get("contacts") or []
    single_contact = payload.get("contact")
    if single_contact:
        contacts = [single_contact, *contacts]
    services = payload.get("services") or []
    single_service = payload.get("service")
    if single_service:
        services = [single_service, *services]
    agent_payload = payload.get("agent") or {}

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO clients "
                "(id, type, name, project_name, description, status, primary_contact_id, "
                " responsible_user_id, created_at, updated_at, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
                (
                    client_id, client_type, name, _clean(payload.get("projectName")),
                    _clean(payload.get("description")), status,
                    _clean(payload.get("responsibleUserId")) or None, now, now,
                ),
            )
            primary_contact_id: Optional[str] = None
            for index, contact in enumerate(contacts):
                if not any(_clean(contact.get(key)) for key in ("name", "email", "phone", "telegram")):
                    continue
                contact_id = _insert_contact(conn, client_id, contact)
                if contact.get("isPrimary") or (primary_contact_id is None and index == 0):
                    primary_contact_id = contact_id
            if primary_contact_id:
                conn.execute(
                    "UPDATE client_contacts SET is_primary = CASE WHEN id = ? THEN 1 ELSE 0 END "
                    "WHERE client_id = ?",
                    (primary_contact_id, client_id),
                )
                conn.execute(
                    "UPDATE clients SET primary_contact_id = ? WHERE id = ?",
                    (primary_contact_id, client_id),
                )

            first_service_id: Optional[str] = None
            for service in services:
                if not (_clean(service.get("serviceId")) or _clean(service.get("serviceName"))):
                    continue
                created_id = _insert_client_service(conn, client_id, service)
                first_service_id = first_service_id or created_id

            agent_id = _clean(agent_payload.get("agentId"))
            if agent_id:
                _insert_connection(
                    conn, client_id=client_id, agent_id=agent_id,
                    client_service_id=agent_payload.get("clientServiceId") or first_service_id,
                    connection_type=agent_payload.get("connectionType", "INTERNAL"),
                    channel=agent_payload.get("channel", ""),
                    actor_id=actor_id,
                )

            client_activity.log_event(
                client_activity.CLIENT_CREATED, client_id=client_id, entity_type="client",
                entity_id=client_id, summary=f"Клиент создан: {name}",
                payload={"type": client_type, "projectName": _clean(payload.get("projectName"))},
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_client(client_id)


def get_client(client_id: str, *, expand: bool = True) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row:
        return None
    data = _client_row(row)
    if expand:
        data["contacts"] = list_contacts(client_id)
        data["services"] = list_client_services(client_id)
        data["agentConnections"] = list_connections(client_id=client_id)
        data["primaryContact"] = next(
            (contact for contact in data["contacts"] if contact["isPrimary"]),
            data["contacts"][0] if data["contacts"] else None,
        )
    return data


def update_client(client_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    existing = get_client(client_id, expand=False)
    if not existing:
        raise KeyError(client_id)
    mapping = {
        "name": "name", "projectName": "project_name", "description": "description",
        "responsibleUserId": "responsible_user_id",
    }
    sets: List[str] = []
    params: List[Any] = []
    for key, column in mapping.items():
        if key in payload:
            value = _clean(payload[key])
            if key == "name" and not value:
                raise ValueError("Название клиента обязательно")
            sets.append(f"{column} = ?")
            params.append(value or (None if column == "responsible_user_id" else ""))
    if "type" in payload:
        sets.append("type = ?")
        params.append(_check_enum(payload["type"], CLIENT_TYPES, "type"))
    if "status" in payload:
        status = _check_enum(payload["status"], CLIENT_STATUSES, "status")
        sets.append("status = ?")
        params.append(status)
        sets.append("archived_at = ?")
        params.append(_now() if status == "ARCHIVED" else None)
    if "primaryContactId" in payload:
        sets.append("primary_contact_id = ?")
        params.append(_clean(payload["primaryContactId"]) or None)
    if not sets:
        return get_client(client_id)
    sets.append("updated_at = ?")
    params.extend([_now(), client_id])
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id = ?", params)
            client_activity.log_event(
                client_activity.CLIENT_UPDATED, client_id=client_id, entity_type="client",
                entity_id=client_id, summary="Клиент обновлён",
                payload={key: str(value) for key, value in payload.items()},
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_client(client_id)


def archive_client(client_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Soft delete. Invoices and payments reference this client and are
    financial records — deleting the row would orphan them, so DELETE is an
    archive here (§39)."""
    _init_schema()
    existing = get_client(client_id, expand=False)
    if not existing:
        raise KeyError(client_id)
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE clients SET status = 'ARCHIVED', archived_at = ?, updated_at = ? WHERE id = ?",
                (now, now, client_id),
            )
            # An archived client's agents keep running otherwise — the operator
            # archived the client, not the infrastructure, but the connection
            # should stop counting as an active delivery.
            conn.execute(
                "UPDATE client_agent_connections SET desired_state = 'DISCONNECTED', "
                "status = 'DISCONNECTED', disconnected_at = ?, updated_at = ? "
                "WHERE client_id = ? AND desired_state != 'DISCONNECTED'",
                (now, now, client_id),
            )
            client_activity.log_event(
                client_activity.CLIENT_ARCHIVED, client_id=client_id, entity_type="client",
                entity_id=client_id, summary=f"Клиент архивирован: {existing['name']}",
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_client(client_id)


# ── agent connections ────────────────────────────────────────────────────────

def _agent_snapshot(conn: sqlite3.Connection, agent_id: str) -> Optional[Dict[str, Any]]:
    """The existing subagent row this connection points at. The clients module
    owns no agents of its own — it links to the ones VEXA already runs, so the
    "open the agent" jump from a client lands in the existing admin screen."""
    row = conn.execute(
        "SELECT id, name, status, is_enabled, last_error, updated_at FROM subagents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    return dict(row) if row else None


def _last_seen(conn: sqlite3.Connection, agent_id: str, stored: Optional[str]) -> Optional[str]:
    """Most recent evidence the agent is alive: its newest event, or its own
    row's updated_at, whichever is later — falling back to what the connection
    itself last recorded."""
    candidates = [stored]
    row = conn.execute(
        "SELECT timestamp FROM agent_events WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,)
    ).fetchone()
    if row and row["timestamp"]:
        candidates.append(str(row["timestamp"]))
    agent = _agent_snapshot(conn, agent_id)
    if agent and agent.get("updated_at"):
        candidates.append(str(agent["updated_at"]))
    valid = [value for value in candidates if value]
    return max(valid) if valid else None


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_connection_status(
    conn: sqlite3.Connection, connection: Optional[sqlite3.Row], *, last_seen: Optional[str] = None
) -> Tuple[str, Optional[str]]:
    """The backend is the single source of truth for connection state (§29).

    Precedence, most decisive first — an operator's explicit "off" outranks
    health, and a real error outranks a stale heartbeat:

        no connection row      → NOT_CONNECTED
        turned off by operator → DISCONNECTED
        paused (agent or link) → PAUSED
        error recorded         → ERROR
        last seen too long ago → OFFLINE
        otherwise              → CONNECTED

    Returns (status, last_seen) so callers can persist both.
    """
    if connection is None:
        return "NOT_CONNECTED", None
    agent_id = connection["agent_id"]
    desired = str(connection["desired_state"] or "ACTIVE").upper()
    if desired == "DISCONNECTED":
        return "DISCONNECTED", connection["last_seen_at"]

    agent = _agent_snapshot(conn, agent_id)
    if agent is None:
        # Pointing at an agent that no longer exists is a configuration error,
        # not an outage — say so instead of showing a permanent "offline".
        return "ERROR", connection["last_seen_at"]

    seen = last_seen if last_seen is not None else _last_seen(conn, agent_id, connection["last_seen_at"])

    if desired == "PAUSED" or str(agent.get("status") or "").lower() == "paused":
        return "PAUSED", seen
    if not agent.get("is_enabled"):
        # A disabled agent is the admin's off switch on the agent side; from
        # the client's perspective the delivery is paused, not broken.
        return "PAUSED", seen
    if connection["error_code"] or connection["error_message"] or agent.get("last_error"):
        return "ERROR", seen
    threshold = datetime.now(timezone.utc) - timedelta(minutes=OFFLINE_THRESHOLD_MINUTES)
    seen_at = _parse_timestamp(seen)
    if seen_at is None or seen_at < threshold:
        return "OFFLINE", seen
    return "CONNECTED", seen


def _refresh_connection(conn: sqlite3.Connection, connection_id: str) -> Optional[sqlite3.Row]:
    """Recomputes and stores one connection's status. Stored so the list
    endpoint can filter on it in SQL; recomputed on every read so it is never
    stale."""
    row = conn.execute(
        "SELECT * FROM client_agent_connections WHERE id = ?", (connection_id,)
    ).fetchone()
    if row is None:
        return None
    status, seen = resolve_connection_status(conn, row)
    if status != row["status"] or seen != row["last_seen_at"]:
        conn.execute(
            "UPDATE client_agent_connections SET status = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
            (status, seen, _now(), connection_id),
        )
        row = conn.execute(
            "SELECT * FROM client_agent_connections WHERE id = ?", (connection_id,)
        ).fetchone()
    return row


def refresh_all_connections() -> int:
    """Recomputes every connection's status. Called before listing so a client
    list ordered or filtered by agent status reflects reality."""
    _init_schema()
    with _connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM client_agent_connections").fetchall()]
        for connection_id in ids:
            _refresh_connection(conn, connection_id)
    return len(ids)


def _insert_connection(
    conn: sqlite3.Connection, *, client_id: str, agent_id: str,
    client_service_id: Optional[str] = None, connection_type: str = "INTERNAL",
    channel: str = "", actor_id: Optional[str] = None,
) -> str:
    agent = _agent_snapshot(conn, agent_id)
    if agent is None:
        raise ValueError(f"Агент не найден: {agent_id}")
    connection_type = _check_enum(connection_type, CONNECTION_TYPES, "connectionType", default="INTERNAL")
    now = _now()
    connection_id = _new_id("cac")
    conn.execute(
        "INSERT INTO client_agent_connections "
        "(id, client_id, client_service_id, agent_id, status, desired_state, connection_type, channel, "
        " connected_at, disconnected_at, last_seen_at, last_health_check_at, error_code, error_message, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'CONNECTED', 'ACTIVE', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)",
        (connection_id, client_id, _clean(client_service_id) or None, agent_id,
         connection_type, _clean(channel), now, now, now),
    )
    _refresh_connection(conn, connection_id)
    client_activity.log_event(
        client_activity.AGENT_CONNECTED, client_id=client_id, entity_type="client_agent_connection",
        entity_id=connection_id, summary=f"Агент подключён: {agent.get('name') or agent_id}",
        payload={"agentId": agent_id, "connectionType": connection_type},
        actor_id=actor_id, conn=conn,
    )
    return connection_id


def connect_agent(
    client_id: str, agent_id: str, *, client_service_id: Optional[str] = None,
    connection_type: str = "INTERNAL", channel: str = "", actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """A client may have zero, one or several agents — nothing here enforces a
    single link (§93)."""
    _init_schema()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
                raise KeyError(client_id)
            connection_id = _insert_connection(
                conn, client_id=client_id, agent_id=agent_id, client_service_id=client_service_id,
                connection_type=connection_type, channel=channel, actor_id=actor_id,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_connection(connection_id)


def get_connection(connection_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        _refresh_connection(conn, connection_id)
        row = conn.execute(
            "SELECT c.*, s.name AS agent_name, cl.name AS client_name, cl.project_name AS project_name, "
            "       cs.title AS service_title "
            "FROM client_agent_connections c "
            "LEFT JOIN subagents s ON s.id = c.agent_id "
            "LEFT JOIN clients cl ON cl.id = c.client_id "
            "LEFT JOIN client_services cs ON cs.id = c.client_service_id "
            "WHERE c.id = ?",
            (connection_id,),
        ).fetchone()
    return _connection_row(row) if row else None


def list_connections(
    *, client_id: Optional[str] = None, agent_id: Optional[str] = None,
    status: Optional[str] = None, refresh: bool = True,
) -> List[Dict[str, Any]]:
    _init_schema()
    where: List[str] = []
    params: List[Any] = []
    if client_id:
        where.append("c.client_id = ?")
        params.append(client_id)
    if agent_id:
        where.append("c.agent_id = ?")
        params.append(agent_id)
    if status:
        where.append("c.status = ?")
        params.append(_check_enum(status, CONNECTION_STATUSES, "status"))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        if refresh:
            scope_clause = ""
            scope_params: List[Any] = []
            if client_id:
                scope_clause = "WHERE client_id = ?"
                scope_params.append(client_id)
            for row in conn.execute(
                f"SELECT id FROM client_agent_connections {scope_clause}", scope_params
            ).fetchall():
                _refresh_connection(conn, row["id"])
        rows = conn.execute(
            "SELECT c.*, s.name AS agent_name, cl.name AS client_name, cl.project_name AS project_name, "
            "       cs.title AS service_title "
            "FROM client_agent_connections c "
            "LEFT JOIN subagents s ON s.id = c.agent_id "
            "LEFT JOIN clients cl ON cl.id = c.client_id "
            "LEFT JOIN client_services cs ON cs.id = c.client_service_id "
            f"{clause} ORDER BY c.created_at DESC",
            params,
        ).fetchall()
    return [_connection_row(row) for row in rows]


def update_connection(
    connection_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None
) -> Dict[str, Any]:
    _init_schema()
    existing = get_connection(connection_id)
    if not existing:
        raise KeyError(connection_id)
    sets: List[str] = []
    params: List[Any] = []
    if "desiredState" in payload:
        sets.append("desired_state = ?")
        params.append(_check_enum(payload["desiredState"], DESIRED_STATES, "desiredState"))
    if "connectionType" in payload:
        sets.append("connection_type = ?")
        params.append(_check_enum(payload["connectionType"], CONNECTION_TYPES, "connectionType"))
    if "channel" in payload:
        sets.append("channel = ?")
        params.append(_clean(payload["channel"]))
    if "clientServiceId" in payload:
        sets.append("client_service_id = ?")
        params.append(_clean(payload["clientServiceId"]) or None)
    if "errorCode" in payload or "errorMessage" in payload:
        sets.append("error_code = ?")
        params.append(_clean(payload.get("errorCode")) or None)
        sets.append("error_message = ?")
        params.append(_clean(payload.get("errorMessage")) or None)
    if sets:
        sets.append("updated_at = ?")
        params.extend([_now(), connection_id])
        with _connect() as conn:
            conn.execute(
                f"UPDATE client_agent_connections SET {', '.join(sets)} WHERE id = ?", params
            )
            _refresh_connection(conn, connection_id)
    return get_connection(connection_id)


def touch_connection(connection_id: str, seen_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Records that this connection was active just now.

    The heartbeat side of `resolve_connection_status`: the resolver reads
    liveness from the agent's own events and row, and this lets anything that
    knows a client-facing agent responded say so directly, without waiting for
    the agent's own bookkeeping to catch up.
    """
    _init_schema()
    stamp = seen_at or _now()
    with _connect() as conn:
        updated = conn.execute(
            "UPDATE client_agent_connections SET last_seen_at = ?, updated_at = ? WHERE id = ?",
            (stamp, _now(), connection_id),
        )
        if updated.rowcount == 0:
            return None
        _refresh_connection(conn, connection_id)
    return get_connection(connection_id)


def disconnect_agent(connection_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    existing = get_connection(connection_id)
    if not existing:
        raise KeyError(connection_id)
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE client_agent_connections SET desired_state = 'DISCONNECTED', "
                "status = 'DISCONNECTED', disconnected_at = ?, updated_at = ? WHERE id = ?",
                (now, now, connection_id),
            )
            client_activity.log_event(
                client_activity.AGENT_DISCONNECTED, client_id=existing["clientId"],
                entity_type="client_agent_connection", entity_id=connection_id,
                summary=f"Агент отключён: {existing.get('agentName') or existing['agentId']}",
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_connection(connection_id)


def reconnect_agent(connection_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Clears the operator's off switch *and* any recorded error, then lets the
    status resolver decide what the connection actually is — reconnecting is a
    request, not a claim that it worked."""
    _init_schema()
    existing = get_connection(connection_id)
    if not existing:
        raise KeyError(connection_id)
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE client_agent_connections SET desired_state = 'ACTIVE', error_code = NULL, "
                "error_message = NULL, connected_at = COALESCE(connected_at, ?), disconnected_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (now, now, connection_id),
            )
            _refresh_connection(conn, connection_id)
            client_activity.log_event(
                client_activity.AGENT_RECONNECTED, client_id=existing["clientId"],
                entity_type="client_agent_connection", entity_id=connection_id,
                summary=f"Переподключение: {existing.get('agentName') or existing['agentId']}",
                actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_connection(connection_id)


def delete_connection(connection_id: str, actor_id: Optional[str] = None) -> bool:
    _init_schema()
    existing = get_connection(connection_id)
    if not existing:
        return False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM client_agent_connections WHERE id = ?", (connection_id,))
            client_activity.log_event(
                client_activity.AGENT_DISCONNECTED, client_id=existing["clientId"],
                entity_type="client_agent_connection", entity_id=connection_id,
                summary="Подключение удалено", actor_id=actor_id, conn=conn,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return True


def health_check(connection_id: str, actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Probes the linked agent and records the result.

    "Probe" here means reading the agent's own health the rest of VEXA already
    maintains — its enabled flag, its status, its last error, when it was last
    active. The clients module does not invent a second liveness mechanism; it
    reports on the one that exists.
    """
    _init_schema()
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM client_agent_connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            raise KeyError(connection_id)
        agent = _agent_snapshot(conn, row["agent_id"])
        error_code = row["error_code"]
        error_message = row["error_message"]
        if agent is None:
            error_code, error_message = "AGENT_NOT_FOUND", f"Агент {row['agent_id']} не найден"
        elif agent.get("last_error"):
            error_code, error_message = "AGENT_ERROR", str(agent["last_error"])
        else:
            # A green check clears a stale error: the point of re-checking is to
            # let a fixed connection recover without a manual reset.
            error_code, error_message = None, None
        conn.execute(
            "UPDATE client_agent_connections SET error_code = ?, error_message = ?, "
            "last_health_check_at = ?, updated_at = ? WHERE id = ?",
            (error_code, error_message, now, now, connection_id),
        )
        refreshed = _refresh_connection(conn, connection_id)
        status = refreshed["status"] if refreshed else "ERROR"
        client_activity.log_event(
            client_activity.AGENT_ERROR if status == "ERROR" else client_activity.AGENT_HEALTH_CHECKED,
            client_id=row["client_id"], entity_type="client_agent_connection",
            entity_id=connection_id, summary=f"Проверка соединения: {status}",
            payload={"status": status, "errorCode": error_code, "errorMessage": error_message},
            actor_id=actor_id, conn=conn,
        )
    return get_connection(connection_id)


# ── the main clients query ───────────────────────────────────────────────────

SORT_COLUMNS = {
    "name": "c.name COLLATE NOCASE",
    "status": "c.status",
    "createdAt": "c.created_at",
    "updatedAt": "c.updated_at",
    # Sorted by the USD-normalized amount, not the raw number: $500 and
    # 500 000 KZT are not comparable as digits, and sorting them as digits is
    # exactly the bug the base-currency rule exists to prevent.
    "amount": "primary_amount_usd",
    "nextBillingDate": "primary_next_billing",
}

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _usd_amount(amount: Any, code: str, rates: Dict[str, Decimal]) -> Decimal:
    try:
        return currency.convert(amount or 0, code or currency.BASE_CURRENCY, currency.BASE_CURRENCY, rates)
    except currency.CurrencyError:
        return Decimal(0)


def _primary_service(services: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The service the table row speaks for when a client has several: the
    first active recurring one, else the first active one, else the first."""
    if not services:
        return None
    active = [item for item in services if item["status"] == "ACTIVE"]
    recurring = [item for item in active if item["serviceType"] == "RECURRING"]
    return (recurring or active or services)[0]


def list_clients(
    *,
    search: str = "",
    status: Optional[str] = None,
    service_type: Optional[str] = None,
    service_id: Optional[str] = None,
    agent_status: Optional[str] = None,
    agent_id: Optional[str] = None,
    billing_from: Optional[str] = None,
    billing_to: Optional[str] = None,
    payment_status: Optional[str] = None,
    responsible_user_id: Optional[str] = None,
    include_archived: bool = False,
    page: int = 1,
    limit: int = DEFAULT_PAGE_SIZE,
    sort: str = "name",
    order: str = "asc",
    display_currency: Optional[str] = None,
) -> Dict[str, Any]:
    """Server-side search, filter, sort and pagination for the clients table.

    Every filter is applied in SQL against the whole book — paginating first
    and filtering the page afterwards would make "показано 1–50 из 236"
    a lie. The per-row derived values (USD amount, payment status, agent
    status) are computed in one pass over the *matched* rows, not per row via
    extra queries.
    """
    _init_schema()
    refresh_all_connections()
    from backend import client_billing

    rates = currency.get_rates()
    display = currency.normalize_currency(display_currency or currency.get_display_currency())

    where: List[str] = []
    params: List[Any] = []
    if not include_archived and status != "ARCHIVED":
        where.append("c.status != 'ARCHIVED'")
    if status:
        where.append("c.status = ?")
        params.append(_check_enum(status, CLIENT_STATUSES, "status"))
    if responsible_user_id:
        where.append("c.responsible_user_id = ?")
        params.append(responsible_user_id)
    if search:
        # §77: one box, everything a person might type — the client, the
        # project, whoever they talk to, or the agent serving them.
        needle = f"%{search.strip().lower()}%"
        where.append(
            "(unicode_lower(c.name) LIKE ? OR unicode_lower(c.project_name) LIKE ? "
            " OR unicode_lower(c.description) LIKE ? "
            " OR EXISTS (SELECT 1 FROM client_contacts ct WHERE ct.client_id = c.id AND ("
            "     unicode_lower(ct.name) LIKE ? OR unicode_lower(ct.email) LIKE ? "
            "     OR unicode_lower(ct.phone) LIKE ? OR unicode_lower(ct.telegram) LIKE ?)) "
            " OR EXISTS (SELECT 1 FROM client_services cs LEFT JOIN services s ON s.id = cs.service_id "
            "     WHERE cs.client_id = c.id AND (unicode_lower(cs.title) LIKE ? "
            "     OR unicode_lower(s.name) LIKE ?)) "
            " OR EXISTS (SELECT 1 FROM client_agent_connections ac "
            "     LEFT JOIN subagents sa ON sa.id = ac.agent_id "
            "     WHERE ac.client_id = c.id AND (unicode_lower(sa.name) LIKE ? "
            "     OR unicode_lower(ac.agent_id) LIKE ?)))"
        )
        params.extend([needle] * 11)
    if service_type:
        where.append(
            "EXISTS (SELECT 1 FROM client_services cs WHERE cs.client_id = c.id AND cs.service_type = ?)"
        )
        params.append(_check_enum(service_type, SERVICE_TYPES, "serviceType"))
    if service_id:
        where.append(
            "EXISTS (SELECT 1 FROM client_services cs WHERE cs.client_id = c.id AND cs.service_id = ?)"
        )
        params.append(service_id)
    if agent_status:
        wanted = _check_enum(agent_status, CONNECTION_STATUSES, "agentStatus")
        if wanted == "NOT_CONNECTED":
            # "Not connected" is the absence of a live link, which includes
            # having no row at all — a filter that only matched stored
            # NOT_CONNECTED rows would show almost nothing.
            where.append(
                "NOT EXISTS (SELECT 1 FROM client_agent_connections ac WHERE ac.client_id = c.id "
                "AND ac.status NOT IN ('NOT_CONNECTED', 'DISCONNECTED'))"
            )
        else:
            where.append(
                "EXISTS (SELECT 1 FROM client_agent_connections ac WHERE ac.client_id = c.id AND ac.status = ?)"
            )
            params.append(wanted)
    if agent_id:
        where.append(
            "EXISTS (SELECT 1 FROM client_agent_connections ac WHERE ac.client_id = c.id AND ac.agent_id = ?)"
        )
        params.append(agent_id)
    if billing_from or billing_to:
        conditions = ["cs.client_id = c.id", "b.next_billing_date IS NOT NULL"]
        if billing_from:
            conditions.append("b.next_billing_date >= ?")
            params.append(str(billing_from)[:10])
        if billing_to:
            conditions.append("b.next_billing_date <= ?")
            params.append(str(billing_to)[:10])
        where.append(
            "EXISTS (SELECT 1 FROM billing_configurations b JOIN client_services cs "
            f"ON cs.id = b.client_service_id WHERE {' AND '.join(conditions)})"
        )

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(f"SELECT c.* FROM clients c {clause}", params).fetchall()

    payment_states = client_billing.payment_status_map([row["id"] for row in rows])

    items: List[Dict[str, Any]] = []
    for row in rows:
        client = _client_row(row)
        client["contacts"] = list_contacts(client["id"])
        client["primaryContact"] = next(
            (contact for contact in client["contacts"] if contact["isPrimary"]),
            client["contacts"][0] if client["contacts"] else None,
        )
        client["services"] = list_client_services(client["id"])
        client["agentConnections"] = list_connections(client_id=client["id"], refresh=False)
        primary = _primary_service(client["services"])
        client["primaryService"] = primary
        client["paymentStatus"] = payment_states.get(client["id"], "NO_INVOICE")

        if primary:
            usd = _usd_amount(primary["amount"], primary["currency"], rates)
            client["primaryAmountUsd"] = currency.money_to_text(usd, currency.BASE_CURRENCY)
            client["primaryAmountDisplay"] = currency.money_to_text(
                currency.convert(usd, currency.BASE_CURRENCY, display, rates), display
            )
            billing = primary.get("billing") or {}
            client["nextBillingDate"] = billing.get("nextBillingDate")
            client["billingFrequency"] = billing.get("frequency")
            sort_amount = usd
        else:
            client["primaryAmountUsd"] = currency.money_to_text(0, currency.BASE_CURRENCY)
            client["primaryAmountDisplay"] = currency.money_to_text(0, display)
            client["nextBillingDate"] = None
            client["billingFrequency"] = None
            sort_amount = Decimal(0)

        # The agent shown on the row: the one that is actually connected if
        # there is one, otherwise the most recently created link.
        connections = client["agentConnections"]
        client["primaryConnection"] = next(
            (item for item in connections if item["status"] == "CONNECTED"),
            connections[0] if connections else None,
        )
        client["_sortAmount"] = sort_amount
        items.append(client)

    if payment_status:
        wanted_payment = str(payment_status).strip().upper()
        items = [item for item in items if item["paymentStatus"] == wanted_payment]

    reverse = str(order or "asc").lower() == "desc"
    sort_key = sort if sort in SORT_COLUMNS else "name"

    def sort_value(item: Dict[str, Any]):
        if sort_key == "amount":
            return item["_sortAmount"]
        if sort_key == "nextBillingDate":
            # Unscheduled rows sort last in both directions: a missing date is
            # "no answer", not "the earliest possible date".
            return item["nextBillingDate"] or ("0000-00-00" if reverse else "9999-12-31")
        if sort_key == "name":
            return (item["name"] or "").lower()
        return item.get({"status": "status", "createdAt": "createdAt", "updatedAt": "updatedAt"}[sort_key]) or ""

    items.sort(key=sort_value, reverse=reverse)

    total = len(items)
    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    page = max(1, int(page or 1))
    start = (page - 1) * limit
    page_items = items[start:start + limit]
    for item in page_items:
        item.pop("_sortAmount", None)
    for item in items:
        item.pop("_sortAmount", None)

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if total else 1,
        "displayCurrency": display,
    }


def count_clients() -> Dict[str, int]:
    _init_schema()
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS total FROM clients GROUP BY status").fetchall()
    counts = {row["status"]: int(row["total"]) for row in rows}
    return {
        "total": sum(value for key, value in counts.items() if key != "ARCHIVED"),
        "active": counts.get("ACTIVE", 0),
        "paused": counts.get("PAUSED", 0),
        "overdue": counts.get("OVERDUE", 0),
        "completed": counts.get("COMPLETED", 0),
        "archived": counts.get("ARCHIVED", 0),
    }


def count_connections() -> Dict[str, int]:
    _init_schema()
    refresh_all_connections()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM client_agent_connections GROUP BY status"
        ).fetchall()
    counts = {row["status"]: int(row["total"]) for row in rows}
    return {
        "connected": counts.get("CONNECTED", 0),
        "notConnected": counts.get("NOT_CONNECTED", 0),
        "disconnected": counts.get("DISCONNECTED", 0),
        "offline": counts.get("OFFLINE", 0),
        "error": counts.get("ERROR", 0),
        "paused": counts.get("PAUSED", 0),
        "total": sum(counts.values()),
    }


def list_agents_for_picker() -> List[Dict[str, Any]]:
    """Existing VEXA agents, for the "подключить агента" selector. This module
    never creates agents — it links to the ones the agent admin already
    manages."""
    _init_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, role, status, is_enabled FROM subagents ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [
        {
            "id": row["id"], "name": row["name"], "role": row["role"],
            "status": row["status"], "isEnabled": bool(row["is_enabled"]),
        }
        for row in rows
    ]
