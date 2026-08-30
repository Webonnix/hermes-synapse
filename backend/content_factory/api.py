"""REST API (§74, §75).

Mounted at `/api/v1/content-factory`. Conventions the spec asks for and this
router enforces in one place rather than per-endpoint:

* every route checks a permission before doing anything (§83);
* every error is `{"error": {code, message, retryable, details, traceId}}`
  (§76), so the frontend switches on `code` and never parses prose;
* list endpoints are cursor-paginated (§75);
* nothing that touches a secret ever returns one (§89) — channels come back
  without their credential reference, providers without keys.

The router is deliberately thin. Business rules live in `pipelines.py`,
`execution.py` and `content.py`; this file translates HTTP to those calls and
back, which is what keeps the same logic usable from the scheduler and from a
future CLI without going through HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response

from backend import permissions
from backend.content_factory import (
    agents,
    content as content_store,
    dashboard,
    db,
    events,
    execution,
    integrations as integrations_store,
    pipelines,
    settings as cf_settings,
    storage as storage_module,
    templates,
)
from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.nodes import registered_types
from backend.content_factory.providers.registry import describe_providers, get_registry
from backend.content_factory.schema import ensure_schema

logger = logging.getLogger("hermes.content_factory.api")

router = APIRouter(prefix="/api/v1/content-factory", tags=["content-factory"])


def _error(exc: ContentFactoryError, status: int = 400) -> HTTPException:
    """§76 error envelope. `traceId` is what a user can quote in a bug report."""
    return HTTPException(
        status_code=status,
        detail={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details,
                "traceId": uuid.uuid4().hex,
            }
        },
    )


def _handle(exc: ContentFactoryError) -> HTTPException:
    status = {
        ErrorCode.VALIDATION_FAILED: 400,
        ErrorCode.PROVIDER_AUTH: 502,
        ErrorCode.PROVIDER_UNAVAILABLE: 503,
        ErrorCode.BUDGET_LIMIT_REACHED: 409,
    }.get(exc.code, 400)
    return _error(exc, status)


# ── Overview (§5–7) ──────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    ensure_schema()

    counts = {
        row["status"]: row["total"]
        for row in db.query("SELECT status, COUNT(*) AS total FROM cf_pipeline_runs GROUP BY status")
    }
    content_counts = {
        row["status"]: row["total"]
        for row in db.query("SELECT status, COUNT(*) AS total FROM cf_content_items GROUP BY status")
    }
    today = db.now_iso()[:10]
    cost_today = db.query_one(
        "SELECT COALESCE(SUM(cost), 0) AS total FROM cf_usage_records WHERE created_at >= ?",
        (f"{today}T00:00:00+00:00",),
    )
    cost_month = db.query_one(
        "SELECT COALESCE(SUM(cost), 0) AS total FROM cf_usage_records WHERE created_at >= ?",
        (f"{today[:7]}-01T00:00:00+00:00",),
    )
    published_today = db.query_one(
        "SELECT COUNT(*) AS total FROM cf_publications WHERE status = 'PUBLISHED' AND published_at >= ?",
        (f"{today}T00:00:00+00:00",),
    )
    scheduled = db.query_one(
        "SELECT COUNT(*) AS total FROM cf_publications WHERE status = 'SCHEDULED'"
    )
    active_pipelines = db.query_one(
        "SELECT COUNT(*) AS total FROM cf_pipelines WHERE status = 'ACTIVE'"
    )

    return {
        "activePipelines": int((active_pipelines or {}).get("total") or 0),
        "processing": int(counts.get("RUNNING", 0)) + int(counts.get("WAITING", 0)),
        "reviewRequired": int(counts.get("WAITING_APPROVAL", 0)) + int(content_counts.get("REVIEW", 0)),
        "scheduled": int((scheduled or {}).get("total") or 0),
        "publishedToday": int((published_today or {}).get("total") or 0),
        "failed": int(counts.get("FAILED", 0)),
        "costToday": round(float((cost_today or {}).get("total") or 0), 4),
        "costMonth": round(float((cost_month or {}).get("total") or 0), 4),
        "liveRuns": execution.list_runs(status="RUNNING", limit=10)["items"],
    }


# ── Pipelines (§74 PIPELINES) ────────────────────────────────────────────────

@router.get("/pipelines")
async def list_pipelines(
    request: Request,
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": pipelines.list_pipelines(project_id=project_id, status=status, limit=limit)}


@router.post("/pipelines")
async def create_pipeline(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    actor = permissions.require(request, permissions.CF_PIPELINE_EDIT)
    try:
        return pipelines.create_pipeline(
            str(payload.get("name", "")),
            project_id=payload.get("projectId"),
            description=str(payload.get("description", "")),
            publishing_mode=str(payload.get("publishingMode", "REVIEW_REQUIRED")),
            error_strategy=str(payload.get("errorStrategy", "FAIL_FAST")),
            max_cost_per_run=payload.get("maxCostPerRun"),
            max_concurrent_runs=int(payload.get("maxConcurrentRuns", 1)),
            nodes=payload.get("nodes") or [],
            edges=payload.get("edges") or [],
            variables=payload.get("variables") or {},
            created_by=actor,
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.get("/pipelines/{pipeline_id}")
async def get_pipeline(request: Request, pipeline_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    pipeline = pipelines.get_pipeline(pipeline_id)
    if not pipeline:
        raise _error(ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Pipeline not found"), 404)
    version_id = pipeline.get("draft_version_id") or pipeline.get("active_version_id")
    pipeline["version"] = pipelines.get_version(version_id) if version_id else None
    pipeline["versions"] = pipelines.list_versions(pipeline_id)
    return pipeline


@router.patch("/pipelines/{pipeline_id}")
async def patch_pipeline(
    request: Request, pipeline_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_PIPELINE_EDIT)
    mapping = {
        "name": "name", "description": "description", "projectId": "project_id",
        "publishingMode": "publishing_mode", "errorStrategy": "error_strategy",
        "maxCostPerRun": "max_cost_per_run", "maxConcurrentRuns": "max_concurrent_runs",
    }
    fields = {mapping[key]: value for key, value in payload.items() if key in mapping}
    try:
        return pipelines.update_pipeline(pipeline_id, **fields)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.put("/pipelines/{pipeline_id}/draft")
async def save_draft(
    request: Request, pipeline_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Autosave for the builder (§105). Saving never activates."""
    actor = permissions.require(request, permissions.CF_PIPELINE_EDIT)
    try:
        version = pipelines.save_draft(
            pipeline_id,
            nodes=payload.get("nodes") or [],
            edges=payload.get("edges") or [],
            variables=payload.get("variables") or {},
            config=payload.get("config") or {},
            notes=str(payload.get("notes", "")),
            created_by=actor,
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {"version": version, "validation": pipelines.validate_version(version["id"])}


@router.get("/pipelines/{pipeline_id}/versions")
async def list_versions(request: Request, pipeline_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": pipelines.list_versions(pipeline_id)}


@router.post("/pipelines/{pipeline_id}/validate")
async def validate(request: Request, pipeline_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    pipeline = pipelines.get_pipeline(pipeline_id)
    if not pipeline:
        raise _error(ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Pipeline not found"), 404)
    version_id = pipeline.get("draft_version_id") or pipeline.get("active_version_id")
    if not version_id:
        return {"errors": [{"code": "NO_VERSION", "message": "Pipeline has no version"}], "warnings": []}
    return pipelines.validate_version(version_id)


@router.post("/pipelines/{pipeline_id}/activate")
async def activate(request: Request, pipeline_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_PIPELINE_EDIT)
    try:
        return pipelines.set_status(pipeline_id, "ACTIVE")
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.post("/pipelines/{pipeline_id}/pause")
async def pause(request: Request, pipeline_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_PIPELINE_EDIT)
    try:
        return pipelines.set_status(pipeline_id, "PAUSED")
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


# ── Runs (§74 RUNS) ──────────────────────────────────────────────────────────

@router.post("/pipelines/{pipeline_id}/runs")
async def start_run(
    request: Request, pipeline_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = permissions.require(request, permissions.CF_RUN_START)
    try:
        return execution.create_run(
            pipeline_id,
            trigger_data=payload.get("triggerData") or {},
            dry_run=bool(payload.get("dryRun", False)),
            cost_budget=payload.get("costBudget"),
            created_by=actor,
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.get("/runs")
async def list_runs(
    request: Request,
    pipeline_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return execution.list_runs(pipeline_id=pipeline_id, status=status, limit=limit, cursor=cursor)


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    run = execution.get_run(run_id, with_nodes=True)
    if not run:
        raise _error(ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Run not found"), 404)
    run["usage"] = db.query(
        "SELECT provider, model, operation, cost, created_at FROM cf_usage_records "
        "WHERE pipeline_run_id = ? ORDER BY created_at",
        (run_id,),
    )
    return run


@router.get("/runs/{run_id}/trace")
async def run_trace(request: Request, run_id: str) -> dict[str, Any]:
    """§40. Timeline of what happened, with user-safe fields only (§89): no
    prompts, no provider bodies, no credentials."""
    permissions.require(request, permissions.CF_VIEW)
    rows = execution.get_node_runs(run_id)
    entries = []
    for row in rows:
        entries.append(
            {
                "nodeId": row["node_id"],
                "name": row["name"],
                "type": row["node_type"],
                "status": row["status"],
                "attempt": row["attempt"],
                "fallbackIndex": row["fallback_index"],
                "provider": row["provider"],
                "model": row["model"],
                "cost": row["cost"],
                "startedAt": row["started_at"],
                "finishedAt": row["finished_at"],
                "durationMs": row["duration_ms"],
                "errorCode": row["error_code"],
                "errorMessage": row["error_message"],
                "logs": db.loads(row.get("logs"), []),
            }
        )
    return {"runId": run_id, "entries": entries}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    request: Request, run_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_RUN_CANCEL)
    try:
        return execution.cancel_run(run_id, reason=str(payload.get("reason", "Cancelled by owner")))
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.post("/runs/{run_id}/nodes/{node_id}/retry")
async def retry_node(request: Request, run_id: str, node_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_RUN_START)
    try:
        return execution.retry_node(run_id, node_id)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.post("/runs/{run_id}/nodes/{node_id}/regenerate")
async def regenerate_node(request: Request, run_id: str, node_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_RUN_START)
    try:
        return execution.retry_node(run_id, node_id, regenerate=True)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.post("/runs/{run_id}/approve")
async def approve(
    request: Request, run_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = permissions.require(request, permissions.CF_CONTENT_REVIEW)
    try:
        return execution.approve_run(
            run_id,
            node_id=str(payload.get("nodeId", "")),
            decision=str(payload.get("decision", "APPROVED")),
            note=str(payload.get("note", "")),
            actor=actor,
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


# ── Content (§74 CONTENT) ────────────────────────────────────────────────────

@router.get("/content")
async def list_content(
    request: Request,
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return content_store.list_content(project_id=project_id, status=status, limit=limit, cursor=cursor)


@router.get("/content/{content_id}")
async def get_content(request: Request, content_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    item = content_store.get_content_item(content_id)
    if not item:
        raise _error(ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Content not found"), 404)
    item["publications"] = content_store.list_publications(content_id=content_id)
    item["assets"] = db.query(
        "SELECT id, type, mime_type, size_bytes, duration, width, height, provider, created_at "
        "FROM cf_assets WHERE content_id = ? OR pipeline_run_id = ? ORDER BY created_at",
        (content_id, item.get("pipeline_run_id")),
    )
    return item


@router.post("/content/{content_id}/approve")
async def approve_content(
    request: Request, content_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = permissions.require(request, permissions.CF_CONTENT_REVIEW)
    item = content_store.update_content_item(
        content_id,
        status="APPROVED",
        review_note=str(payload.get("note", "")),
        reviewed_by=actor,
        reviewed_at=db.now_iso(),
    )
    await events.emit(events.CONTENT_READY_FOR_REVIEW, data={"contentId": content_id, "status": "APPROVED"})
    return item


@router.post("/content/{content_id}/reject")
async def reject_content(
    request: Request, content_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = permissions.require(request, permissions.CF_CONTENT_REVIEW)
    return content_store.update_content_item(
        content_id,
        status="DRAFT",
        review_note=str(payload.get("note", "")),
        reviewed_by=actor,
        reviewed_at=db.now_iso(),
    )


# ── Assets (§43, §46) ────────────────────────────────────────────────────────

@router.get("/assets")
async def list_assets(
    request: Request,
    project_id: Optional[str] = Query(None),
    asset_type: Optional[str] = Query(None, alias="type"),
    run_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    ensure_schema()
    clauses, params = ["1 = 1"], []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if asset_type:
        clauses.append("type = ?")
        params.append(asset_type)
    if run_id:
        clauses.append("pipeline_run_id = ?")
        params.append(run_id)
    params.append(limit)
    return {
        "items": db.query(
            f"SELECT * FROM cf_assets WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
    }


@router.get("/assets/{asset_id}/url")
async def asset_url(
    request: Request,
    asset_id: str,
    expires_in: Optional[int] = Query(None, ge=60, le=604800),
):
    """A time-limited link to the bytes (§46). Generated on demand rather than
    stored, so revoking access is a matter of not issuing another one."""
    permissions.require(request, permissions.CF_VIEW)
    asset = db.query_one("SELECT * FROM cf_assets WHERE id = ?", (asset_id,))
    if not asset:
        raise _error(ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Asset not found"), 404)
    if expires_in is None:
        expires_in = int(cf_settings.get_settings()["storage"]["signedUrlMinutes"]) * 60
        expires_in = max(60, min(604800, expires_in))
    try:
        url = storage_module.get_storage().presigned_get(str(asset["storage_key"]), expires_in=expires_in)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {"url": url, "expiresIn": expires_in, "mimeType": asset["mime_type"]}


@router.get("/assets/raw")
async def asset_raw(key: str = Query(...), expires: int = Query(...), signature: str = Query(...)):
    """Signed read for the local-storage backend. The signature is the whole
    authorization here — it is checked before the file is touched."""
    storage = storage_module.get_storage()
    if not isinstance(storage, storage_module.LocalStorage) or not storage.verify(key, expires, signature):
        raise HTTPException(status_code=403, detail="Invalid or expired asset link")
    return Response(content=storage.get(key), media_type="application/octet-stream")


# ── Channels (§49) ───────────────────────────────────────────────────────────

@router.get("/channels")
async def list_channels(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": content_store.list_channels()}


@router.post("/channels")
async def create_channel(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Registers an account and moves credentials/proxy into the secret store.
    The response only contains safe connection metadata."""
    permissions.require(request, permissions.CF_CHANNELS_MANAGE)
    try:
        channel = content_store.create_channel(
            platform=str(payload.get("platform", "")),
            account_name=str(payload.get("accountName", "")),
            account_external_id=str(payload.get("accountExternalId", "")),
            project_id=payload.get("projectId"),
            credentials=payload.get("credentials") or None,
            proxy_url=str(payload.get("proxyUrl") or ""),
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {key: value for key, value in channel.items() if key != "credentials_ref"}


@router.patch("/channels/{channel_id}/connection")
async def update_channel_connection(
    request: Request, channel_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_CHANNELS_MANAGE)
    try:
        return content_store.update_channel_connection(
            channel_id,
            credentials=payload.get("credentials") if "credentials" in payload else None,
            proxy_url=str(payload.get("proxyUrl") or "") if "proxyUrl" in payload else None,
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.delete("/channels/{channel_id}")
async def delete_channel(request: Request, channel_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_CHANNELS_MANAGE)
    return {"deleted": content_store.delete_channel(channel_id)}


# ── Publications (§74 PUBLICATIONS) ──────────────────────────────────────────

@router.get("/publications")
async def list_publications(
    request: Request,
    content_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": content_store.list_publications(content_id=content_id, status=status, limit=limit)}


# ── Analytics (§56) ──────────────────────────────────────────────────────────

@router.get("/analytics/overview")
async def analytics_overview(request: Request, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    ensure_schema()
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    snapshots = db.query(
        """
        SELECT publication_id, timestamp, views, likes, comments, shares, engagement_rate
        FROM cf_analytics_snapshots WHERE timestamp >= ? ORDER BY timestamp
        """,
        (since,),
    )
    latest_by_publication = {row["publication_id"]: row for row in snapshots}
    latest = list(latest_by_publication.values())
    return {
        "windowDays": days,
        "publications": len(latest),
        "views": sum(int(row["views"] or 0) for row in latest),
        "likes": sum(int(row["likes"] or 0) for row in latest),
        "comments": sum(int(row["comments"] or 0) for row in latest),
        "shares": sum(int(row["shares"] or 0) for row in latest),
        "engagementRate": round(
            sum(float(row["engagement_rate"] or 0) for row in latest) / len(latest), 2
        ) if latest else 0,
        "items": latest,
        "series": snapshots,
    }


@router.post("/analytics/refresh")
async def refresh_analytics(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Fetches real platform counters and records immutable snapshots."""
    permissions.require(request, permissions.CF_CHANNELS_MANAGE)
    ensure_schema()
    channel_id = str(payload.get("channelId") or "")
    clauses, params = ["p.status = 'PUBLISHED'", "p.external_id <> ''"], []
    if channel_id:
        clauses.append("p.channel_id = ?")
        params.append(channel_id)
    rows = db.query(
        "SELECT p.id, p.external_id, p.channel_id, c.platform, c.credentials_ref "
        "FROM cf_publications p JOIN cf_channels c ON c.id = p.channel_id WHERE "
        + " AND ".join(clauses), tuple(params),
    )
    refreshed, failed = 0, []
    provider_ids = {"YOUTUBE": "youtube", "TIKTOK": "tiktok"}
    for row in rows:
        provider = get_registry().get(provider_ids.get(str(row["platform"]).upper(), ""))
        if provider is None:
            failed.append({"publicationId": row["id"], "error": "provider unavailable"})
            continue
        try:
            metrics = await provider.get_analytics(row["external_id"], row["credentials_ref"])
            now = db.now_iso()
            views = int(metrics.get("views") or 0)
            interactions = sum(int(metrics.get(key) or 0) for key in ("likes", "comments", "shares"))
            engagement = round(interactions * 100 / views, 3) if views else 0
            db.execute(
                """INSERT INTO cf_analytics_snapshots
                (id, workspace_id, publication_id, timestamp, views, likes, comments, shares,
                 engagement_rate, metadata, created_at) VALUES (?, 'default', ?, ?, ?, ?, ?, ?, ?, '{}', ?)""",
                (f"stat-{uuid.uuid4().hex[:12]}", row["id"], now, views,
                 int(metrics.get("likes") or 0), int(metrics.get("comments") or 0),
                 int(metrics.get("shares") or 0), engagement, now),
            )
            db.execute(
                "UPDATE cf_channels SET analytics_updated_at = ?, last_error = '', updated_at = ? WHERE id = ?",
                (now, now, row["channel_id"]),
            )
            refreshed += 1
        except Exception as exc:
            message = getattr(exc, "message", type(exc).__name__)
            failed.append({"publicationId": row["id"], "error": str(message)})
            db.execute(
                "UPDATE cf_channels SET last_error = ?, updated_at = ? WHERE id = ?",
                (str(message)[:500], db.now_iso(), row["channel_id"]),
            )
    return {"refreshed": refreshed, "failed": failed}


# ── Providers, agents, node library (§62, §13, §22) ──────────────────────────

@router.get("/providers")
async def providers(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": describe_providers(), "storage": storage_module.get_storage().health()}


@router.get("/providers/health")
async def providers_health(request: Request) -> dict[str, Any]:
    """§62/§90: credential and reachability checks only — a health probe must
    never run a billable generation."""
    permissions.require(request, permissions.CF_VIEW)
    results = []
    for provider in get_registry().all():
        try:
            results.append(await provider.health())
        except Exception as exc:
            results.append({"provider": provider.id, "status": "error", "detail": type(exc).__name__})
    return {"items": results}


@router.get("/agents")
async def cf_agents(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": agents.describe_agents()}


@router.post("/agents/seed")
async def seed_agents(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    return {"created": agents.seed_agents()}


@router.get("/node-types")
async def node_types(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": registered_types()}


@router.get("/templates")
async def list_templates(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": templates.list_templates()}


@router.post("/templates/{template_id}/instantiate")
async def instantiate_template(
    request: Request, template_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = permissions.require(request, permissions.CF_PIPELINE_EDIT)
    try:
        result = templates.instantiate(
            template_id,
            name=str(payload.get("name", "")),
            project_id=payload.get("projectId"),
            options=payload.get("options") or {},
            created_by=actor,
        )
        if bool(payload.get("activate", False)):
            try:
                result["pipeline"] = pipelines.set_status(str(result["pipeline"]["id"]), "ACTIVE")
                result["activation"] = {"status": "ACTIVE"}
            except ContentFactoryError as activation_error:
                # Keep the valid draft and return its exact activation problem.
                # Otherwise a retry from the wizard would create duplicates while
                # hiding the already-created pipeline from the operator.
                result["activation"] = {
                    "status": "BLOCKED",
                    "error": activation_error.to_dict(),
                }
        return result
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


# ── Control centre panels (§5–7) ─────────────────────────────────────────────

@router.get("/queue")
async def pipeline_queue(request: Request, limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """What is running and what is scheduled next, with the worker's real
    capacity so "3/10" on screen means something."""
    permissions.require(request, permissions.CF_VIEW)
    return dashboard.queue(limit=limit)


@router.get("/activity")
async def recent_activity(request: Request, limit: int = Query(12, ge=1, le=50)) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return {"items": dashboard.activity(limit=limit)}


@router.get("/stats")
async def statistics(request: Request, days: int = Query(7, ge=2, le=90)) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return dashboard.stats(days=days)


@router.get("/live")
async def live_run(request: Request, run_id: Optional[str] = Query(None)) -> dict[str, Any]:
    """The run the stage rail displays: the requested one, else the newest
    active one, else the newest finished one. `{}` when nothing has ever run."""
    permissions.require(request, permissions.CF_VIEW)
    return dashboard.live_run(run_id)


@router.get("/storage/usage")
async def storage_usage(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return dashboard.storage_usage()


@router.get("/agents/{agent_id}")
async def agent_detail(request: Request, agent_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    detail = dashboard.agent_detail(agent_id)
    if not detail:
        raise _error(ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Agent not found"), 404)
    return detail


# ── Integrations & settings (§62, §101) ──────────────────────────────────────

@router.get("/integrations")
async def integrations(request: Request) -> dict[str, Any]:
    """Everything the factory is (and is not) connected to, plus the warnings
    that explain why a pipeline would fail later. No keys, ever (§89)."""
    permissions.require(request, permissions.CF_VIEW)
    return dashboard.integrations()


@router.get("/storage/config")
async def storage_config(request: Request) -> dict[str, Any]:
    """The asset store the owner picked, what the environment would give on its
    own, and which credentials exist — never their values (§89)."""
    permissions.require(request, permissions.CF_VIEW)
    config = integrations_store.storage_config_public()
    try:
        config["health"] = storage_module.get_storage().health()
    except Exception as exc:
        config["health"] = {"storage": "unknown", "status": "error", "detail": type(exc).__name__}
    return config


@router.put("/storage/config")
async def save_storage_config(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Switch the asset store. Takes effect on the next asset write — the
    storage singleton is dropped here rather than at the next restart."""
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        return await asyncio.to_thread(integrations_store.save_storage_config, payload)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.post("/storage/test")
async def test_storage_config(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Reach the store with a candidate configuration without saving it, so a
    wrong password is found here instead of in the middle of a run."""
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        return await asyncio.to_thread(integrations_store.test_storage_config, payload)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.get("/integrations/providers")
async def integration_providers(request: Request) -> dict[str, Any]:
    """The catalog of connectable media providers — connected or not — with the
    per-category default and where each key came from."""
    permissions.require(request, permissions.CF_VIEW)
    return {
        "items": await asyncio.to_thread(integrations_store.list_media_providers),
        "defaults": integrations_store.category_defaults(),
    }


@router.put("/integrations/providers/{provider_id}")
async def save_integration_provider(
    request: Request, provider_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Connect or reconfigure one provider. A secret field is write-only: it is
    stored when non-empty, deleted when explicitly null, and left alone when
    absent — the UI never receives it back and so cannot echo it."""
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        return await asyncio.to_thread(integrations_store.save_media_provider, provider_id, payload)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


@router.delete("/integrations/providers/{provider_id}")
async def delete_integration_provider(request: Request, provider_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        await asyncio.to_thread(integrations_store.delete_media_provider, provider_id)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {"deleted": provider_id}


@router.post("/integrations/providers/{provider_id}/test")
async def test_integration_provider(request: Request, provider_id: str) -> dict[str, Any]:
    """§90: credential resolution only, never a billable generation."""
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    return await integrations_store.test_media_provider(provider_id)


@router.put("/integrations/defaults/{category}")
async def set_category_default(
    request: Request, category: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Which provider a node of this category gets when it names none."""
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        defaults = await asyncio.to_thread(
            integrations_store.set_category_default, category.upper(), str(payload.get("providerId") or "")
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {"defaults": defaults}


@router.get("/integrations/llm")
async def llm_endpoints(request: Request) -> dict[str, Any]:
    """The local model plus every external OpenAI-compatible endpoint and its
    approval state."""
    permissions.require(request, permissions.CF_VIEW)
    return await asyncio.to_thread(integrations_store.list_llm_endpoints)


@router.post("/integrations/llm")
async def create_llm_endpoint(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Propose an external model endpoint.

    This does *not* activate it: the key goes to Valkey under a TTL and an R3
    control-plane task is opened, which an owner has to approve before anything
    can call the endpoint. That governance is the reason this route delegates to
    `provider_governance` instead of writing a binding itself.
    """
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        proposal = await asyncio.to_thread(
            integrations_store.create_llm_endpoint,
            str(payload.get("name") or ""),
            str(payload.get("apiBase") or ""),
            str(payload.get("apiKey") or ""),
            default_model=str(payload.get("defaultModel") or ""),
            cost_per_1m_input=payload.get("costPer1mInput"),
            cost_per_1m_output=payload.get("costPer1mOutput"),
        )
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {
        "binding": proposal,
        "approvalRequired": proposal.get("status") != "active",
        "taskId": proposal.get("control_task_id"),
    }


@router.delete("/integrations/llm/{binding_id}")
async def delete_llm_endpoint(request: Request, binding_id: str) -> dict[str, Any]:
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        await asyncio.to_thread(integrations_store.delete_llm_endpoint, binding_id)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc
    return {"deleted": binding_id}


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    return cf_settings.get_settings()


@router.put("/settings")
async def update_settings(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    permissions.require(request, permissions.CF_SETTINGS_EDIT)
    try:
        return cf_settings.update_settings(payload)
    except ContentFactoryError as exc:
        raise _handle(exc) from exc


# ── Health (§90) ─────────────────────────────────────────────────────────────

@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    permissions.require(request, permissions.CF_VIEW)
    ensure_schema()
    return {
        "schema": "ok",
        "storage": storage_module.get_storage().health(),
        "providers": len(get_registry().all()),
        "worker": {"id": execution.WORKER_ID, **execution.runtime_limits()},
    }
