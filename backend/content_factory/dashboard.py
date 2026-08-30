"""Read-only aggregations for the control centre (§5–7, §40, §56, §62).

Everything the dashboard shows is derived here from rows that already exist —
runs, node runs, usage records, assets, publications, snapshots. No counter is
kept in a second place and no number is estimated: if there is nothing to show,
these functions return empty structures and the UI says so, because a dashboard
that invents plausible figures is worse than one that admits it has no data.

Kept apart from `api.py` so the same aggregations can feed a Telegram digest or
a scheduled report without going through HTTP.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.content_factory import db
from backend.content_factory.domain import NodeStatus, RunStatus
from backend.content_factory.schema import WORKSPACE_DEFAULT, ensure_schema

logger = logging.getLogger("hermes.content_factory.dashboard")


def _day_start(offset_days: int = 0) -> str:
    day = datetime.now(timezone.utc).date() - timedelta(days=offset_days)
    return f"{day.isoformat()}T00:00:00+00:00"


# ── Pipeline queue (§7 right rail) ───────────────────────────────────────────

def queue(*, limit: int = 10, workspace_id: str = WORKSPACE_DEFAULT) -> dict[str, Any]:
    """What is lined up: active runs first, then schedules that have not fired.

    `capacity` is the worker's real concurrency limit, so "3/10" on screen means
    three of ten slots are in use rather than a decorative number.
    """
    ensure_schema()
    from backend.content_factory.execution import MAX_CONCURRENT_RUNS

    live = db.query(
        """
        SELECT r.id, r.pipeline_id, r.status, r.progress, r.created_at, r.started_at, p.name
        FROM cf_pipeline_runs r
        LEFT JOIN cf_pipelines p ON p.id = r.pipeline_id
        WHERE r.workspace_id = ? AND r.status IN (?, ?, ?, ?)
        ORDER BY r.created_at
        LIMIT ?
        """,
        (workspace_id, RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING,
         RunStatus.WAITING_APPROVAL, limit),
    )
    scheduled = db.query(
        """
        SELECT s.id, s.pipeline_id, s.kind, s.expression, s.timezone, s.next_run_at, p.name
        FROM cf_schedules s
        LEFT JOIN cf_pipelines p ON p.id = s.pipeline_id
        WHERE s.workspace_id = ? AND s.enabled = 1
        ORDER BY COALESCE(s.next_run_at, '9999') ASC
        LIMIT ?
        """,
        (workspace_id, limit),
    )
    items = [
        {
            "kind": "RUN",
            "id": row["id"],
            "pipelineId": row["pipeline_id"],
            "name": row["name"] or row["pipeline_id"],
            "status": row["status"],
            "progress": row["progress"],
            "at": row["started_at"] or row["created_at"],
        }
        for row in live
    ] + [
        {
            "kind": "SCHEDULE",
            "id": row["id"],
            "pipelineId": row["pipeline_id"],
            "name": row["name"] or row["pipeline_id"],
            "status": "SCHEDULED",
            "expression": row["expression"],
            "timezone": row["timezone"],
            "at": row["next_run_at"],
        }
        for row in scheduled
    ]
    return {
        "items": items[:limit],
        "active": len(live),
        "capacity": MAX_CONCURRENT_RUNS,
    }


# ── Activity feed (§7) ───────────────────────────────────────────────────────

def activity(*, limit: int = 12, workspace_id: str = WORKSPACE_DEFAULT) -> list[dict[str, Any]]:
    """Recent notable events, newest first.

    Assembled from state that is already durable rather than from a log stream:
    a restart must not empty this list.
    """
    ensure_schema()
    events: list[dict[str, Any]] = []

    for row in db.query(
        """
        SELECT r.id, r.status, r.created_at, r.completed_at, r.error_code, p.name
        FROM cf_pipeline_runs r
        LEFT JOIN cf_pipelines p ON p.id = r.pipeline_id
        WHERE r.workspace_id = ?
        ORDER BY r.updated_at DESC
        LIMIT ?
        """,
        (workspace_id, limit),
    ):
        events.append(
            {
                "type": "run",
                "status": row["status"],
                "title": row["name"] or row["id"],
                "detail": row["error_code"] or row["status"],
                "at": row["completed_at"] or row["created_at"],
                "refId": row["id"],
            }
        )

    for row in db.query(
        """
        SELECT id, platform, status, title, published_at, updated_at, external_url
        FROM cf_publications
        WHERE workspace_id = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (workspace_id, limit),
    ):
        events.append(
            {
                "type": "publication",
                "status": row["status"],
                "title": row["title"] or row["id"],
                "detail": row["platform"],
                "at": row["published_at"] or row["updated_at"],
                "refId": row["id"],
                "url": row["external_url"],
            }
        )

    events.sort(key=lambda item: item["at"] or "", reverse=True)
    return events[:limit]


# ── Statistics (§56, §60) ────────────────────────────────────────────────────

def stats(*, days: int = 7, workspace_id: str = WORKSPACE_DEFAULT) -> dict[str, Any]:
    """Daily series for the sparkline plus today-vs-yesterday deltas.

    Views and followers come from analytics snapshots, which only exist for
    published content; on a system that has not published anything the series is
    all zeros and the UI shows it as such instead of hiding the panel.
    """
    ensure_schema()
    series: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        start, end = _day_start(offset), _day_start(offset - 1)
        created = db.query_one(
            "SELECT COUNT(*) AS n FROM cf_content_items WHERE workspace_id = ? AND created_at >= ? AND created_at < ?",
            (workspace_id, start, end),
        )
        published = db.query_one(
            "SELECT COUNT(*) AS n FROM cf_publications WHERE workspace_id = ? AND status = 'PUBLISHED'"
            " AND published_at >= ? AND published_at < ?",
            (workspace_id, start, end),
        )
        cost = db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM cf_usage_records"
            " WHERE workspace_id = ? AND created_at >= ? AND created_at < ?",
            (workspace_id, start, end),
        )
        views = db.query_one(
            """
            SELECT COALESCE(SUM(views), 0) AS total FROM (
                SELECT publication_id, MAX(views) AS views
                FROM cf_analytics_snapshots
                WHERE workspace_id = ? AND timestamp >= ? AND timestamp < ?
                GROUP BY publication_id
            )
            """,
            (workspace_id, start, end),
        )
        series.append(
            {
                "date": start[:10],
                "created": int((created or {}).get("n") or 0),
                "published": int((published or {}).get("n") or 0),
                "cost": round(float((cost or {}).get("total") or 0), 4),
                "views": int((views or {}).get("total") or 0),
            }
        )

    latest = _latest_engagement(workspace_id)
    today = series[-1] if series else {"created": 0, "published": 0, "cost": 0, "views": 0}
    yesterday = series[-2] if len(series) > 1 else {"created": 0, "published": 0, "cost": 0, "views": 0}

    return {
        "series": series,
        "today": today,
        "delta": {
            "created": _delta(today["created"], yesterday["created"]),
            "published": _delta(today["published"], yesterday["published"]),
            "cost": _delta(today["cost"], yesterday["cost"]),
            "views": _delta(today["views"], yesterday["views"]),
        },
        "totals": latest,
    }


def _delta(current: float, previous: float) -> Optional[float]:
    """Percent change, or None when there is no previous value to compare with —
    "+100%" against a zero baseline is not information."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _latest_engagement(workspace_id: str) -> dict[str, Any]:
    rows = db.query(
        """
        SELECT publication_id, MAX(timestamp) AS ts, views, likes, comments, shares,
               followers_delta, engagement_rate
        FROM cf_analytics_snapshots
        WHERE workspace_id = ?
        GROUP BY publication_id
        """,
        (workspace_id,),
    )
    views = sum(int(row["views"] or 0) for row in rows)
    engagements = sum(
        int(row["likes"] or 0) + int(row["comments"] or 0) + int(row["shares"] or 0) for row in rows
    )
    return {
        "publications": len(rows),
        "views": views,
        "followersDelta": sum(int(row["followers_delta"] or 0) for row in rows),
        "engagementRate": round(engagements / views * 100, 2) if views else 0.0,
    }


# ── Storage (§43, §44) ───────────────────────────────────────────────────────

def storage_usage(*, workspace_id: str = WORKSPACE_DEFAULT) -> dict[str, Any]:
    from backend.content_factory import storage as storage_module

    ensure_schema()
    by_type = db.query(
        """
        SELECT type, COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes
        FROM cf_assets WHERE workspace_id = ?
        GROUP BY type ORDER BY bytes DESC
        """,
        (workspace_id,),
    )
    total = db.query_one(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes FROM cf_assets WHERE workspace_id = ?",
        (workspace_id,),
    )
    try:
        health = storage_module.get_storage().health()
    except Exception as exc:
        health = {"storage": "unknown", "status": "error", "detail": type(exc).__name__}

    return {
        "backend": health,
        "totalFiles": int((total or {}).get("files") or 0),
        "totalBytes": int((total or {}).get("bytes") or 0),
        "byType": [
            {"type": row["type"], "files": int(row["files"]), "bytes": int(row["bytes"])}
            for row in by_type
        ],
    }


# ── Integrations (§62, §49) ──────────────────────────────────────────────────

def integrations() -> dict[str, Any]:
    """One page answering "what is this factory actually connected to".

    Deliberately includes what is *not* connected: a category with no provider
    is the reason a pipeline will fail later, and the owner should see that here
    rather than discover it at 09:00 when the schedule fires.
    """
    from backend.content_factory import content as content_store
    from backend.content_factory import storage as storage_module
    from backend.content_factory.providers.base import ProviderCategory
    from backend.content_factory.providers.registry import get_registry, mock_providers_enabled

    registry = get_registry()
    categories = []
    for category in (
        ProviderCategory.LLM, ProviderCategory.IMAGE, ProviderCategory.VIDEO,
        ProviderCategory.VOICE, ProviderCategory.PUBLISHING,
    ):
        providers = registry.for_category(category)
        default = registry.default_for(category)
        categories.append(
            {
                "category": category,
                "providers": [
                    {
                        "id": provider.id,
                        "model": provider.model,
                        "free": provider.is_free,
                        "asyncJob": provider.is_async_job,
                        "concurrency": registry.limit_for(provider),
                        "isDefault": default is provider,
                    }
                    for provider in providers
                ],
                "connected": bool(providers),
                # A category with only free/local providers still works, but the
                # owner should know they will get local quality, not vendor.
                "paidAvailable": any(not provider.is_free for provider in providers),
            }
        )

    channels = content_store.list_channels()
    try:
        storage_health = storage_module.get_storage().health()
    except Exception as exc:
        storage_health = {"storage": "unknown", "status": "error", "detail": type(exc).__name__}

    # Publishing needs a URL a platform can fetch. Local disk cannot serve one,
    # so this is the single most useful warning on the page.
    publish_ready = storage_health.get("storage") in {"nextcloud", "s3"} and any(
        channel.get("status") == "CONNECTED" for channel in channels
    )

    try:
        from backend.content_factory.integrations import get_storage_config

        storage_choice = get_storage_config()["backend"]
    except Exception:
        storage_choice = "env"

    return {
        "categories": categories,
        "channels": channels,
        "storage": storage_health,
        "storageBackend": storage_choice,
        "mockProviders": mock_providers_enabled(),
        "publishReady": publish_ready,
        "warnings": _integration_warnings(categories, channels, storage_health),
    }


def _integration_warnings(
    categories: list[dict[str, Any]], channels: list[dict[str, Any]], storage_health: dict[str, Any]
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    missing = [item["category"] for item in categories if not item["connected"]]
    for category in missing:
        warnings.append(
            {
                "code": "NO_PROVIDER",
                "message": f"Нет ни одного провайдера категории {category} — узлы этого типа упадут. "
                           "Подключите модель в разделе «Интеграции»",
            }
        )
    if storage_health.get("storage") == "local":
        warnings.append(
            {
                "code": "LOCAL_STORAGE",
                "message": "Ассеты пишутся на локальный диск: площадки не смогут скачать файл, "
                           "публикация работать не будет. Подключите Nextcloud или S3 "
                           "во вкладке «Хранилище»",
            }
        )
    if not channels:
        warnings.append({"code": "NO_CHANNEL", "message": "Не подключён ни один канал публикации"})
    return warnings


# ── Agent metrics (§111 inspector) ───────────────────────────────────────────

def agent_detail(agent_id: str, *, days: int = 7) -> dict[str, Any]:
    """Everything the agent inspector shows, measured from real node runs.

    Success rate and average duration are computed over finished runs only —
    counting an in-flight node as a failure would make a busy agent look broken.
    """
    ensure_schema()
    from backend.content_factory.agents import AGENT_SPECS, resolve_agent_llm
    from backend.database import get_subagent

    agent = get_subagent(agent_id)
    if not agent:
        return {}

    spec = next((item for item in AGENT_SPECS if item.id == agent_id), None)
    since = _day_start(days - 1)
    node_types = _node_types_for(agent_id)

    metrics = {"completed": 0, "failed": 0, "avgDurationMs": 0, "cost": 0.0, "successRate": None}
    if node_types:
        placeholders = ", ".join("?" for _ in node_types)
        row = db.query_one(
            f"""
            SELECT
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS failed,
                AVG(CASE WHEN status = ? THEN duration_ms END) AS avg_ms,
                COALESCE(SUM(cost), 0) AS cost
            FROM cf_node_runs
            WHERE node_type IN ({placeholders}) AND created_at >= ?
            """,
            (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.COMPLETED, *node_types, since),
        ) or {}
        completed = int(row.get("completed") or 0)
        failed = int(row.get("failed") or 0)
        finished = completed + failed
        metrics = {
            "completed": completed,
            "failed": failed,
            "avgDurationMs": int(row.get("avg_ms") or 0),
            "cost": round(float(row.get("cost") or 0), 4),
            "successRate": round(completed / finished * 100, 1) if finished else None,
        }

    resolved = resolve_agent_llm(agent_id)
    return {
        "id": agent_id,
        "name": agent["name"],
        "role": agent.get("role") or (spec.role if spec else ""),
        "description": agent.get("system_prompt", "")[:400],
        "enabled": bool(agent.get("is_enabled", True)),
        "model": agent.get("model"),
        "modelProvider": agent.get("model_provider"),
        "modelType": agent.get("model_type"),
        "temperature": agent.get("temperature"),
        "providerChain": resolved["provider_ids"],
        "budgetUsdLimit": agent.get("budget_usd_limit"),
        "budgetFallbackToLocal": agent.get("budget_fallback_to_local"),
        "skills": [skill for skill in (agent.get("skills") or "").split(",") if skill],
        "nodeTypes": node_types,
        "windowDays": days,
        "metrics": metrics,
        "recentRuns": _recent_node_runs(node_types),
    }


def _node_types_for(agent_id: str) -> list[str]:
    """Which node types this agent is the default for — that is what its metrics
    are measured over."""
    from backend.content_factory.nodes import get_node_handler, registered_types

    matches = []
    for node_type in registered_types():
        handler = get_node_handler(node_type)
        if getattr(handler, "default_agent_id", "") == agent_id:
            matches.append(node_type)
    return matches


def _recent_node_runs(node_types: list[str], limit: int = 8) -> list[dict[str, Any]]:
    if not node_types:
        return []
    placeholders = ", ".join("?" for _ in node_types)
    return db.query(
        f"""
        SELECT node_id, node_type, status, provider, model, cost, duration_ms, attempt,
               error_code, created_at, pipeline_run_id
        FROM cf_node_runs
        WHERE node_type IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (*node_types, limit),
    )


# ── Live run detail for the stage rail (§7) ──────────────────────────────────

def live_run(run_id: Optional[str] = None, *, workspace_id: str = WORKSPACE_DEFAULT) -> dict[str, Any]:
    """The run the control centre puts on screen: the requested one, else the
    newest active one, else the newest finished one. Empty dict when the factory
    has never run — the UI shows an empty state rather than a fake pipeline."""
    ensure_schema()
    from backend.content_factory import execution, pipelines

    row = None
    if run_id:
        row = execution.get_run(run_id)
    if row is None:
        row = db.query_one(
            """
            SELECT * FROM cf_pipeline_runs
            WHERE workspace_id = ? AND status IN (?, ?, ?, ?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (workspace_id, RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.WAITING,
             RunStatus.WAITING_APPROVAL),
        )
    if row is None:
        row = db.query_one(
            "SELECT * FROM cf_pipeline_runs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1",
            (workspace_id,),
        )
    if row is None:
        return {}

    nodes = execution.get_node_runs(str(row["id"]))
    pipeline = pipelines.get_pipeline(str(row["pipeline_id"])) or {}
    version = pipelines.get_version(str(row["pipeline_version_id"])) or {}
    current = next(
        (node for node in nodes if node["status"] in (NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL)),
        None,
    )
    assets = db.query(
        """
        SELECT id, type, mime_type, duration, width, height, size_bytes, provider, created_at
        FROM cf_assets WHERE pipeline_run_id = ? ORDER BY created_at DESC LIMIT 12
        """,
        (row["id"],),
    )

    return {
        "run": row,
        "pipeline": pipeline,
        "version": {"id": version.get("id"), "version": version.get("version"),
                    "nodes": version.get("nodes"), "edges": version.get("edges")},
        "nodes": nodes,
        "current": current,
        "assets": assets,
        "logs": _run_logs(str(row["id"])),
    }


def _run_logs(run_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """Node-level log lines, flattened into one timeline (§40).

    Only what nodes chose to record: no prompts, no provider bodies, no
    credentials (§89).
    """
    entries: list[dict[str, Any]] = []
    for row in db.query(
        "SELECT node_id, status, logs, error_code, error_message, started_at, finished_at,"
        " provider, attempt FROM cf_node_runs WHERE pipeline_run_id = ? ORDER BY created_at",
        (run_id,),
    ):
        for line in db.loads(row.get("logs"), []) or []:
            entries.append({"at": row["finished_at"] or row["started_at"], "nodeId": row["node_id"],
                            "text": str(line)})
        if row["error_code"]:
            entries.append(
                {
                    "at": row["finished_at"] or row["started_at"],
                    "nodeId": row["node_id"],
                    "text": f"{row['error_code']}: {row['error_message']}"
                            + (f" (попытка {row['attempt']})" if row["attempt"] else ""),
                    "level": "error",
                }
            )
    entries.sort(key=lambda item: item["at"] or "")
    return entries[-limit:]
