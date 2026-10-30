"""Daily budgets: reserved atomically *before* the work, settled after.

A reservation is a single conditional UPDATE — ``... WHERE used + n <= limit``
— so two concurrent stages cannot both squeeze under the same remaining
headroom, and a crash after the reservation leaves the budget spent rather
than silently refunded. Days are UTC.

Token accounting is conservative on purpose: a call reserves its prompt
estimate (chars / 3, an over-estimate for code and Russian alike) plus the
whole ``max_tokens``; once llama.cpp reports real usage the reservation is
settled down to it. Unknown usage keeps the full reservation.

The money figure is an *estimate* of local electricity, never a charge: the
local model has no per-token price.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.earner import store


class BudgetExceeded(RuntimeError):
    def __init__(self, what: str, used: int, limit: int):
        self.what = what
        self.used = used
        self.limit = limit
        super().__init__(f"Дневной лимит «{what}» исчерпан: {used} из {limit}")


_COUNTERS = {
    "tasks": "tasks_per_day",
    "llm_calls": "llm_calls_per_day",
    "fetches": "fetches_per_day",
    "sandbox_runs": "sandbox_runs_per_day",
    "submissions": "submissions_per_day",
}
_LABELS = {
    "tasks": "задачи", "llm_calls": "вызовы модели", "fetches": "запросы к источникам",
    "sandbox_runs": "запуски sandbox", "tokens": "токены", "submissions": "отправки",
}


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_day(connection: sqlite3.Connection, day: str) -> None:
    connection.execute("INSERT OR IGNORE INTO earner_budget_days (day) VALUES (?)", (day,))


def reserve(connection: sqlite3.Connection, counter: str, amount: int = 1) -> None:
    """Reserve ``amount`` of a counted resource inside the caller's transaction."""
    limit = store.get_settings(connection)["limits"][_COUNTERS[counter]]
    day = today()
    _ensure_day(connection, day)
    cursor = connection.execute(
        f"UPDATE earner_budget_days SET {counter} = {counter} + ? WHERE day = ? AND {counter} + ? <= ?",
        (amount, day, amount, limit),
    )
    if cursor.rowcount != 1:
        used = connection.execute(f"SELECT {counter} FROM earner_budget_days WHERE day = ?", (day,)).fetchone()[0]
        raise BudgetExceeded(_LABELS[counter], int(used), int(limit))


def reserve_llm_call(entity_id: str, stage: str, reserved_tokens: int, model: str) -> str:
    """One call and its token reservation, together or not at all."""
    call_id = store.new_id("llm")
    with store.tx() as connection:
        limits = store.get_settings(connection)["limits"]
        day = today()
        _ensure_day(connection, day)
        row = connection.execute(
            "SELECT llm_calls, tokens_reserved FROM earner_budget_days WHERE day = ?", (day,)).fetchone()
        if row["llm_calls"] + 1 > limits["llm_calls_per_day"]:
            raise BudgetExceeded(_LABELS["llm_calls"], row["llm_calls"], limits["llm_calls_per_day"])
        if row["tokens_reserved"] + reserved_tokens > limits["tokens_per_day"]:
            raise BudgetExceeded(_LABELS["tokens"], row["tokens_reserved"], limits["tokens_per_day"])
        connection.execute(
            "UPDATE earner_budget_days SET llm_calls = llm_calls + 1, tokens_reserved = tokens_reserved + ? "
            "WHERE day = ?", (reserved_tokens, day))
        connection.execute(
            "INSERT INTO earner_llm_calls (id, day, entity_id, stage, model, status, reserved_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)",
            (call_id, day, entity_id, stage, model, reserved_tokens, store.iso()))
    return call_id


def month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def reserve_external(call_id: str, usd: float) -> None:
    """Money for one external call, against the monthly cap, before the call.
    A cap of 0 (or external models switched off) refuses everything."""
    with store.tx() as connection:
        settings = store.get_settings(connection)
        cap = float(settings["external_month_usd"]) if settings["external_enabled"] else 0.0
        current = month()
        connection.execute("INSERT OR IGNORE INTO earner_budget_months (month) VALUES (?)", (current,))
        cursor = connection.execute(
            "UPDATE earner_budget_months SET usd_reserved = usd_reserved + ? WHERE month = ? AND usd_reserved + ? <= ?",
            (usd, current, usd, cap))
        if cursor.rowcount != 1:
            used = connection.execute("SELECT usd_reserved FROM earner_budget_months WHERE month = ?",
                                      (current,)).fetchone()[0]
            raise BudgetExceeded("внешние модели, $ в месяц", round(float(used), 2), round(cap, 2))
        connection.execute("UPDATE earner_llm_calls SET provider = 'external', cost_usd = ? WHERE id = ?",
                           (usd, call_id))


def settle_external(call_id: str, reserved_usd: float, actual_usd: Optional[float]) -> None:
    """The real cost when the provider reported usage — even above the
    reservation, so a low prompt estimate never hides real spend; unknown usage
    keeps the whole reservation (conservative)."""
    with store.tx() as connection:
        row = connection.execute("SELECT day FROM earner_llm_calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            return
        current = month()
        spent = reserved_usd if actual_usd is None else max(0.0, actual_usd)
        connection.execute(
            "UPDATE earner_budget_months SET usd_reserved = usd_reserved - ? + ?, usd_used = usd_used + ? WHERE month = ?",
            (reserved_usd, spent, spent, current))
        connection.execute("UPDATE earner_llm_calls SET cost_usd = ? WHERE id = ?", (round(spent, 6), call_id))


def external_usage() -> Dict[str, Any]:
    with store.read() as connection:
        settings = store.get_settings(connection)
        row = connection.execute("SELECT * FROM earner_budget_months WHERE month = ?", (month(),)).fetchone()
    cap = float(settings["external_month_usd"]) if settings["external_enabled"] else 0.0
    reserved = float(row["usd_reserved"]) if row else 0.0
    return {"month": month(), "enabled": settings["external_enabled"], "model": settings["external_model"],
            "limit_usd": round(cap, 2), "used_usd": round(float(row["usd_used"]) if row else 0.0, 4),
            "reserved_usd": round(reserved, 4), "remaining_usd": round(max(0.0, cap - reserved), 4)}


def estimate_cost_usd(duration_ms: int) -> float:
    """Electricity estimate for the time the GPUs spent on this call."""
    try:
        watts = float(os.getenv("EARNER_LOCAL_WATTS", "600"))
        price = float(os.getenv("EARNER_KWH_PRICE_USD", "0.10"))
    except ValueError:
        watts, price = 600.0, 0.10
    return round(watts / 1000.0 * (duration_ms / 3_600_000.0) * price, 6)


def settle_llm_call(call_id: str, *, status: str, prompt_tokens: Optional[int], completion_tokens: Optional[int],
                    duration_ms: int, error: str = "", model: str = "") -> None:
    with store.tx() as connection:
        row = connection.execute("SELECT * FROM earner_llm_calls WHERE id = ?", (call_id,)).fetchone()
        if row is None or row["status"] != "reserved":
            return
        cost = estimate_cost_usd(duration_ms)
        known = prompt_tokens is not None and completion_tokens is not None
        actual = int(prompt_tokens or 0) + int(completion_tokens or 0)
        if status == "refused":
            # Never sent (e.g. the dollar cap said no): give the day its call and tokens back.
            connection.execute(
                "UPDATE earner_budget_days SET llm_calls = MAX(0, llm_calls - 1), tokens_reserved = tokens_reserved - ? "
                "WHERE day = ?", (int(row["reserved_tokens"]), row["day"]))
            connection.execute("UPDATE earner_llm_calls SET status = 'refused', error = ? WHERE id = ?",
                               (error[:300], call_id))
            return
        if known and status == "ok":
            # Settle the reservation down to what was really used; never up —
            # max_tokens already capped the completion.
            delta = min(0, actual - int(row["reserved_tokens"]))
        else:
            delta = 0
        connection.execute(
            "UPDATE earner_budget_days SET tokens_reserved = tokens_reserved + ?, tokens_used = tokens_used + ?, "
            "est_cost_usd = est_cost_usd + ? WHERE day = ?", (delta, actual, cost, row["day"]))
        connection.execute(
            "UPDATE earner_llm_calls SET status = ?, prompt_tokens = ?, completion_tokens = ?, duration_ms = ?, "
            "est_cost_usd = ?, error = ?, model = CASE WHEN ? = '' THEN model ELSE ? END WHERE id = ?",
            (status, int(prompt_tokens or 0), int(completion_tokens or 0), duration_ms, cost, error[:300],
             model, model, call_id))


def usage(day: Optional[str] = None) -> Dict[str, Any]:
    day = day or today()
    with store.read() as connection:
        limits = store.get_settings(connection)["limits"]
        row = connection.execute("SELECT * FROM earner_budget_days WHERE day = ?", (day,)).fetchone()
        sandbox_busy = connection.execute("SELECT holder FROM earner_sandbox_slot WHERE id = 1").fetchone()
    used = dict(row) if row else {"tasks": 0, "llm_calls": 0, "tokens_reserved": 0, "tokens_used": 0,
                                  "fetches": 0, "sandbox_runs": 0, "est_cost_usd": 0.0, "submissions": 0}

    def entry(used_value: int, limit: int) -> Dict[str, int]:
        return {"used": int(used_value), "limit": int(limit), "remaining": max(0, int(limit) - int(used_value))}

    return {
        "day": day,
        "timezone": "UTC",
        "tasks": entry(used["tasks"], limits["tasks_per_day"]),
        "llm_calls": entry(used["llm_calls"], limits["llm_calls_per_day"]),
        "tokens": {**entry(used["tokens_reserved"], limits["tokens_per_day"]), "actual": int(used["tokens_used"])},
        "fetches": entry(used["fetches"], limits["fetches_per_day"]),
        "sandbox_runs": entry(used["sandbox_runs"], limits["sandbox_runs_per_day"]),
        "submissions": entry(used.get("submissions") or 0, limits["submissions_per_day"]),
        "sandbox_concurrency": {"used": 1 if sandbox_busy and sandbox_busy["holder"] else 0,
                                "limit": store.SANDBOX_CONCURRENCY},
        "est_cost_usd": round(float(used["est_cost_usd"]), 4),
        "external": external_usage(),
        "est_cost_note": "Оценка электроэнергии локальной модели, а не фактическая оплата.",
    }


def history(days: int = 14, calls: int = 100) -> Dict[str, Any]:
    with store.read() as connection:
        day_rows = connection.execute(
            "SELECT * FROM earner_budget_days ORDER BY day DESC LIMIT ?", (days,)).fetchall()
        call_rows = connection.execute(
            "SELECT * FROM earner_llm_calls ORDER BY created_at DESC LIMIT ?", (calls,)).fetchall()
        month_rows = connection.execute(
            "SELECT * FROM earner_budget_months ORDER BY month DESC LIMIT 12").fetchall()
    return {"days": [dict(row) for row in day_rows], "calls": [dict(row) for row in call_rows],
            "months": [dict(row) for row in month_rows]}
