"""Sales pipeline: leads moving toward becoming a client.

This is deliberately a *separate* model from `backend/clients.py`. A client
record means "we have an agreement" — it carries services, billing schedules
and agent connections, and the whole billing scheduler walks that table. A
deal means "we might". Folding leads into `clients` with a PROSPECT status
would mean every place that already reads that table — MRR, the billing
sweep, the agent-connection picker — would have to start filtering leads back
out, and one missed filter would let an unsigned prospect show up as revenue.

The two meet at exactly one place: `convert_to_client()`. It does not
duplicate client creation — it calls `clients.create_client()` with whatever
payload the conversion form built, so a converted deal goes through the same
contact/service/billing/agent machinery as a client added by hand. All it
adds on top is stamping the deal WON and pointing `client_id` at the result.
Everything before that stays in this module and is invisible to the rest of
the commercial model.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from backend import client_activity, currency
from backend.database import DB_PATH

logger = logging.getLogger("hermes.deals")

DEAL_STAGES: Tuple[str, ...] = (
    "NEW", "CONTACTED", "QUALIFIED", "PROPOSAL", "NEGOTIATION", "WON", "LOST",
)
OPEN_STAGES: Tuple[str, ...] = tuple(stage for stage in DEAL_STAGES if stage not in ("WON", "LOST"))
CLOSED_STAGES: Tuple[str, ...] = ("WON", "LOST")

_schema_ready = False


def _now() -> str:
    # Microsecond precision, unlike the rest of the commercial model's
    # second-precision timestamps: this module orders rows by "which one was
    # touched more recently" (the kanban board, the task list), and two edits
    # a person makes back to back land in the same second often enough that a
    # second-precision tie would visibly reorder cards on screen.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_function("unicode_lower", 1, lambda value: value.lower() if isinstance(value, str) else value)
    return conn


def _new_id() -> str:
    return f"deal-{uuid.uuid4().hex[:12]}"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _check_stage(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text not in DEAL_STAGES:
        raise ValueError(f"stage must be one of {', '.join(DEAL_STAGES)} (got {value!r})")
    return text


def _init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    currency.ensure_schema()
    client_activity.ensure_schema()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS deals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'NEW',
                client_id TEXT,
                contact_name TEXT NOT NULL DEFAULT '',
                contact_email TEXT NOT NULL DEFAULT '',
                contact_phone TEXT NOT NULL DEFAULT '',
                company_name TEXT NOT NULL DEFAULT '',
                value TEXT,
                currency TEXT NOT NULL DEFAULT 'USD',
                source TEXT NOT NULL DEFAULT '',
                responsible_user_id TEXT,
                expected_close_date TEXT,
                lost_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                stage_changed_at TEXT NOT NULL,
                won_at TEXT,
                lost_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals (stage);
            CREATE INDEX IF NOT EXISTS idx_deals_client ON deals (client_id);
            CREATE INDEX IF NOT EXISTS idx_deals_responsible ON deals (responsible_user_id);
            """
        )
    _schema_ready = True


def reset_schema_cache() -> None:
    global _schema_ready
    _schema_ready = False


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "stage": row["stage"],
        "clientId": row["client_id"],
        "contactName": row["contact_name"],
        "contactEmail": row["contact_email"],
        "contactPhone": row["contact_phone"],
        "companyName": row["company_name"],
        "value": row["value"],
        "currency": row["currency"],
        "source": row["source"],
        "responsibleUserId": row["responsible_user_id"],
        "expectedCloseDate": row["expected_close_date"],
        "lostReason": row["lost_reason"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "stageChangedAt": row["stage_changed_at"],
        "wonAt": row["won_at"],
        "lostAt": row["lost_at"],
    }


def create_deal(payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    _init_schema()
    title = _clean(payload.get("title"))
    if not title:
        raise ValueError("Название сделки обязательно")
    stage = _check_stage(payload.get("stage") or "NEW")
    if stage in CLOSED_STAGES:
        # A deal is born open. Someone recording a deal that is already won or
        # lost should use create + the stage endpoints, so the same validation
        # (a lost reason, a won timestamp) applies instead of being bypassed.
        raise ValueError("Новая сделка не может сразу быть в статусе «выигран» или «проигран»")

    value_text: Optional[str] = None
    value_raw = payload.get("value")
    if value_raw not in (None, ""):
        value_text = currency.decimal_to_text(currency.parse_decimal(value_raw, field="value", allow_zero=True))
    deal_currency = currency.normalize_currency(payload.get("currency") or "USD")

    now = _now()
    deal_id = _new_id()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO deals
                (id, title, stage, client_id, contact_name, contact_email, contact_phone,
                 company_name, value, currency, source, responsible_user_id, expected_close_date,
                 lost_reason, created_at, updated_at, stage_changed_at, won_at, lost_at)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, NULL, NULL)
            """,
            (
                deal_id, title, stage, _clean(payload.get("contactName")),
                _clean(payload.get("contactEmail")), _clean(payload.get("contactPhone")),
                _clean(payload.get("companyName")), value_text, deal_currency,
                _clean(payload.get("source")), _clean(payload.get("responsibleUserId")) or None,
                _clean(payload.get("expectedCloseDate")) or None, now, now, now,
            ),
        )
        client_activity.log_event(
            client_activity.DEAL_CREATED, client_id=None, entity_type="deal", entity_id=deal_id,
            summary=f"Сделка создана: {title}", payload={"stage": stage}, actor_id=actor_id, conn=conn,
        )
    return get_deal(deal_id)  # type: ignore[return-value]


def get_deal(deal_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return _row(row) if row else None


def list_deals(
    stage: Optional[str] = None,
    search: str = "",
    responsible_user_id: Optional[str] = None,
    include_closed: bool = True,
) -> List[Dict[str, Any]]:
    """Every deal the kanban board needs, unpaginated — a pipeline read in
    pages would mean a column silently missing its oldest cards. Grouping into
    columns happens in the frontend; the one thing worth doing here is the
    order cards appear in each column, so the ordering is the contract:
    most recently touched first."""
    _init_schema()
    clauses: List[str] = []
    params: List[Any] = []
    if stage:
        clauses.append("stage = ?")
        params.append(_check_stage(stage))
    elif not include_closed:
        clauses.append(f"stage NOT IN ({', '.join('?' for _ in CLOSED_STAGES)})")
        params.extend(CLOSED_STAGES)
    if responsible_user_id:
        clauses.append("responsible_user_id = ?")
        params.append(responsible_user_id)
    if search.strip():
        needle = f"%{search.strip().lower()}%"
        clauses.append(
            "(unicode_lower(title) LIKE ? OR unicode_lower(contact_name) LIKE ? "
            "OR unicode_lower(company_name) LIKE ? OR unicode_lower(source) LIKE ?)"
        )
        params.extend([needle, needle, needle, needle])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect() as conn:
        # rowid as a last-resort tiebreaker — see client_notes.list_notes.
        rows = conn.execute(
            f"SELECT * FROM deals{where} ORDER BY updated_at DESC, rowid DESC", tuple(params),
        ).fetchall()
    return [_row(row) for row in rows]


_UPDATABLE_FIELDS = (
    "title", "contactName", "contactEmail", "contactPhone", "companyName",
    "source", "responsibleUserId", "expectedCloseDate",
)
_FIELD_COLUMNS = {
    "title": "title", "contactName": "contact_name", "contactEmail": "contact_email",
    "contactPhone": "contact_phone", "companyName": "company_name", "source": "source",
    "responsibleUserId": "responsible_user_id", "expectedCloseDate": "expected_close_date",
}


def update_deal(deal_id: str, payload: Dict[str, Any], actor_id: Optional[str] = None) -> Dict[str, Any]:
    """One endpoint for every kind of edit a deal takes — a plain field change,
    dragging a kanban card to a new column, or losing a deal with a reason.
    Stage transitions get their own bookkeeping (`stage_changed_at`, the
    won/lost timestamp, the lost reason) so a drag on the board and a field
    edit in a form are both safe to send through the same call."""
    existing = get_deal(deal_id)
    if not existing:
        raise KeyError(deal_id)
    requested_stage = payload.get("stage")
    if existing["clientId"] and requested_stage is not None and requested_stage != existing["stage"]:
        # A converted deal's stage is a historical fact (it is WON, permanently)
        # — the client record is what moves forward from here.
        raise ValueError("Сделка уже конвертирована в клиента — стадия зафиксирована")

    updates: Dict[str, Any] = {}
    for field in _UPDATABLE_FIELDS:
        if field in payload:
            value = payload[field]
            updates[_FIELD_COLUMNS[field]] = _clean(value) or None if field != "title" else _clean(value)

    if "value" in payload:
        value_raw = payload["value"]
        updates["value"] = (
            None if value_raw in (None, "")
            else currency.decimal_to_text(currency.parse_decimal(value_raw, field="value", allow_zero=True))
        )
    if "currency" in payload and payload["currency"]:
        updates["currency"] = currency.normalize_currency(payload["currency"])

    stage_changed = False
    new_stage = existing["stage"]
    if "stage" in payload:
        new_stage = _check_stage(payload["stage"])
        if new_stage != existing["stage"]:
            stage_changed = True
            updates["stage"] = new_stage
            updates["stage_changed_at"] = _now()
            if new_stage == "LOST":
                reason = _clean(payload.get("lostReason") or existing["lostReason"])
                if not reason:
                    raise ValueError("Укажите причину, по которой сделка проиграна")
                updates["lost_reason"] = reason
                updates["lost_at"] = _now()
                updates["won_at"] = None
            elif new_stage == "WON":
                updates["won_at"] = _now()
                updates["lost_at"] = None
                updates["lost_reason"] = ""
            else:
                # Reopening a deal that was previously closed.
                updates["lost_at"] = None
                updates["won_at"] = None
                if existing["stage"] in CLOSED_STAGES:
                    updates["lost_reason"] = ""
    elif "lostReason" in payload and existing["stage"] == "LOST":
        updates["lost_reason"] = _clean(payload["lostReason"])

    if not updates:
        return existing
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with _connect() as conn:
        conn.execute(f"UPDATE deals SET {assignments} WHERE id = ?", (*updates.values(), deal_id))
        if stage_changed:
            event = (
                client_activity.DEAL_WON if new_stage == "WON"
                else client_activity.DEAL_LOST if new_stage == "LOST"
                else client_activity.DEAL_STAGE_CHANGED
            )
            client_activity.log_event(
                event, client_id=existing["clientId"], entity_type="deal", entity_id=deal_id,
                summary=f"Сделка «{existing['title']}»: {existing['stage']} → {new_stage}",
                payload={"from": existing["stage"], "to": new_stage}, actor_id=actor_id, conn=conn,
            )
    return get_deal(deal_id)  # type: ignore[return-value]


def delete_deal(deal_id: str) -> None:
    existing = get_deal(deal_id)
    if not existing:
        raise KeyError(deal_id)
    if existing["clientId"]:
        raise ValueError("Сделка уже связана с клиентом — удалите или архивируйте клиента вместо сделки")
    with _connect() as conn:
        conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))


def convert_to_client(
    deal_id: str, client_payload: Dict[str, Any], actor_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Turns a deal into a real client via `clients.create_client` — the same
    path the "Добавить клиента" drawer uses, so a converted lead ends up with
    exactly the same shape (contact, service, billing, agent) any other client
    can have. Returns `(deal, client)`."""
    from backend import clients  # local: avoids a hard import cycle at module load

    deal = get_deal(deal_id)
    if not deal:
        raise KeyError(deal_id)
    if deal["clientId"]:
        raise ValueError("Сделка уже конвертирована в клиента")

    client = clients.create_client(client_payload, actor_id=actor_id)
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE deals SET client_id = ?, stage = 'WON', won_at = COALESCE(won_at, ?), "
            "lost_at = NULL, lost_reason = '', stage_changed_at = ?, updated_at = ? WHERE id = ?",
            (client["id"], now, now, now, deal_id),
        )
        client_activity.log_event(
            client_activity.DEAL_CONVERTED, client_id=client["id"], entity_type="deal", entity_id=deal_id,
            summary=f"Сделка «{deal['title']}» стала клиентом «{client['name']}»",
            payload={"dealId": deal_id}, actor_id=actor_id, conn=conn,
        )
    return get_deal(deal_id), client  # type: ignore[return-value]


def _pair(usd: Decimal, display: str, rates: Dict[str, Decimal]) -> Dict[str, str]:
    display_value = currency.convert(usd, currency.BASE_CURRENCY, display, rates)
    return {
        "usd": currency.money_to_text(usd, currency.BASE_CURRENCY),
        "display": currency.money_to_text(display_value, display),
    }


def pipeline_summary(display_currency: Optional[str] = None) -> Dict[str, Any]:
    """The KPI row above the board: how much is open, how it is spread across
    stages, and the win rate — all in the same USD-pivot the rest of the
    commercial model uses, so a deal in EUR and one in RUB add up correctly."""
    _init_schema()
    rates = currency.get_rates()
    display = currency.normalize_currency(display_currency or currency.get_display_currency())

    with _connect() as conn:
        rows = conn.execute("SELECT stage, value, currency, won_at FROM deals").fetchall()
        closed_counts = conn.execute(
            "SELECT stage, COUNT(*) AS n FROM deals WHERE stage IN ('WON', 'LOST') GROUP BY stage"
        ).fetchall()

    by_stage: Dict[str, Dict[str, Any]] = {stage: {"count": 0, "valueUsd": Decimal("0")} for stage in DEAL_STAGES}
    open_value = Decimal("0")
    open_count = 0
    won_this_month_count = 0
    won_this_month_value = Decimal("0")
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    for row in rows:
        stage = row["stage"]
        by_stage.setdefault(stage, {"count": 0, "valueUsd": Decimal("0")})
        by_stage[stage]["count"] += 1
        usd = currency.to_usd(row["value"], row["currency"], rates) if row["value"] is not None else None
        if usd is not None:
            by_stage[stage]["valueUsd"] += usd
            if stage in OPEN_STAGES:
                open_value += usd
        if stage in OPEN_STAGES:
            open_count += 1
        # Every currency is converted through the same USD pivot before it is
        # summed — a raw SQL SUM across mixed currencies would add a EUR
        # amount to a RUB one as if they were the same unit.
        if stage == "WON" and row["won_at"] and row["won_at"][:7] == current_month:
            won_this_month_count += 1
            if usd is not None:
                won_this_month_value += usd

    closed = {row["stage"]: row["n"] for row in closed_counts}
    won = closed.get("WON", 0)
    lost = closed.get("LOST", 0)
    win_rate = round(won * 100 / (won + lost), 1) if (won + lost) else None

    return {
        "currency": display,
        "baseCurrency": currency.BASE_CURRENCY,
        "openCount": open_count,
        "openValue": _pair(open_value, display, rates),
        "wonCount": won,
        "lostCount": lost,
        "winRate": win_rate,
        "wonThisMonthCount": won_this_month_count,
        "wonThisMonthValue": _pair(won_this_month_value, display, rates),
        "byStage": {
            stage: {"count": data["count"], "valueUsd": currency.money_to_text(data["valueUsd"], currency.BASE_CURRENCY)}
            for stage, data in by_stage.items()
        },
    }
