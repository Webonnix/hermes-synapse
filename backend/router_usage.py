"""Usage log for the local-orchestrated fallback chain (backend/local_orchestrator.py).

9Router genuinely has no quota/usage API in this version (see backend/main.py's
router_stats_api docstring — every path 404s regardless of auth). So "quota"
and "economy" numbers shown in the AI Router tab are not scraped from 9Router
at all — they are Hermes's own honest count of which tier handled each
request, tracked here. Quota is measured in requests-per-window, not tokens:
that's the one thing every call site can report for free without needing a
tokenizer or provider-reported usage (which many OpenAI-compatible providers
omit or lie about on the free/cheap tiers this chain exists to protect).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.database import DB_PATH

_PRUNE_AFTER_DAYS = 30
_prune_counter = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS router_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tier_id TEXT,
                tier_label TEXT NOT NULL,
                tier_rank INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                ts TEXT NOT NULL,
                success INTEGER NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                latency_ms INTEGER,
                error TEXT
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_router_usage_ts ON router_usage_log(ts)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_router_usage_tier ON router_usage_log(tier_id, ts)")


def log_usage(
    tier_id: Optional[str],
    tier_label: str,
    tier_rank: int,
    kind: str,
    success: bool,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    _init_schema()
    global _prune_counter
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO router_usage_log
                (tier_id, tier_label, tier_rank, kind, ts, success, input_tokens, output_tokens, cost_usd, latency_ms, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tier_id, tier_label, tier_rank, kind, _now_iso(), 1 if success else 0,
             input_tokens, output_tokens, cost_usd, latency_ms, (error or "")[:500]),
        )
    _prune_counter += 1
    if _prune_counter >= 200:
        _prune_counter = 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_PRUNE_AFTER_DAYS)).isoformat(timespec="seconds")
        with _connect() as connection:
            connection.execute("DELETE FROM router_usage_log WHERE ts < ?", (cutoff,))


def tier_quota_status(tier_id: Optional[str], quota_limit: Optional[int], quota_window_hours: float) -> dict[str, Any]:
    """Requests used vs. quota_limit within the trailing quota_window_hours,
    counting successful attempts only (a failed/errored call shouldn't count
    against the tier's own budget)."""
    _init_schema()
    window_start = (datetime.now(timezone.utc) - timedelta(hours=quota_window_hours)).isoformat(timespec="seconds")
    with _connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS c, MIN(ts) AS first_ts FROM router_usage_log "
            "WHERE tier_id = ? AND ts >= ? AND success = 1",
            (tier_id, window_start),
        ).fetchone()
    used = row["c"] or 0
    resets_at = None
    if row["first_ts"]:
        try:
            first = datetime.fromisoformat(row["first_ts"])
            resets_at = (first + timedelta(hours=quota_window_hours)).isoformat(timespec="seconds")
        except ValueError:
            resets_at = None
    pct = None if not quota_limit else round(min(used / quota_limit, 1.0) * 100, 1)
    return {
        "used": used,
        "limit": quota_limit,
        "window_hours": quota_window_hours,
        "resets_at": resets_at,
        "pct": pct,
        "exhausted": bool(quota_limit) and used >= quota_limit,
    }


def overview_stats() -> dict[str, Any]:
    """Powers the AI Router tab's stat cards: which tier is currently primary,
    how much traffic stayed off the top (most expensive) tier in the last 24h,
    and total request volume."""
    _init_schema()
    from backend import router_tiers
    from backend.provider_governance import list_bindings

    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with _connect() as connection:
        rows = connection.execute(
            "SELECT tier_rank, success FROM router_usage_log WHERE ts >= ?",
            (day_ago,),
        ).fetchall()

    requests_24h = len(rows)
    successful = [r for r in rows if r["success"]]
    top_rank = max((r["tier_rank"] for r in successful), default=0) if successful else 0
    off_top_tier = sum(1 for r in successful if r["tier_rank"] < top_rank) if top_rank > 0 else len(successful)
    token_savings_pct = round((off_top_tier / len(successful)) * 100, 1) if successful else None

    tiers = router_tiers.list_tiers()
    active_tiers = [t for t in tiers if t["is_active"]]

    highest = max(active_tiers, key=lambda t: t["tier_rank"], default=None)
    active_combo_label = highest["label"] if highest else "Локальная модель"
    tier_count = len(active_tiers) + 1  # +1 for the implicit local tier

    # "Health" here means status == active (a validated, usable credential) —
    # deliberately not a live reachability ping, which would re-introduce the
    # dashboard-stall problem GET /api/router/stats already avoids on purpose.
    bindings = list_bindings()
    return {
        "active_combo_label": active_combo_label,
        "active_combo_tier_count": tier_count,
        "token_savings_pct": token_savings_pct,
        "requests_24h": requests_24h,
        "providers_healthy": len([b for b in bindings if b.get("status") == "active"]),
        "providers_total": len(bindings),
    }


_init_schema()
