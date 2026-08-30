"""Execution engine (§15–21, §41, §94, §95).

Durable workflow execution without Temporal, built on the pattern `dev_runs.py`
already proves on this deployment: all state in the database, a polling worker
that claims work under a lease, and recovery of anything whose lease expired
while the backend was restarting.

The shape of one run:

    worker_loop → claim(run) → drive(run) ─┬→ ready nodes → execute concurrently
                                           ├→ WAITING_EXTERNAL → poll later
                                           ├→ WAITING_APPROVAL → suspend, return
                                           └→ nothing left → finish

Why the database rather than in-memory state: a run can span hours of video
generation. Every node transition is committed before the next one starts, so
`kill -9` on the backend loses at most the node that was in flight, and that
node is re-attempted with its inputs intact. The one thing that must never be
re-attempted blindly — publishing — carries its own idempotency claim in
`content.py`.

Three deliberate limits:

* progress percentages are broadcast, not persisted (see `events.py`);
* one run is driven by one worker at a time, enforced by the lease;
* a node's fallback rung and attempt number are stored, so a trace can answer
  "which provider actually produced this" months later (§40).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import Any, Optional

from backend.content_factory import db, events, pipelines
from backend.content_factory.domain import (
    ContentFactoryError,
    ErrorCode,
    ErrorStrategy,
    NodeResult,
    NodeStatus,
    PipelineGraph,
    PipelineNode,
    RunStatus,
    TriggerType,
    render_template,
)
from backend.content_factory.nodes import NodeContext, get_node_handler
from backend.content_factory.schema import WORKSPACE_DEFAULT, ensure_schema

logger = logging.getLogger("hermes.content_factory.execution")

POLL_SECONDS = float(os.environ.get("CONTENT_FACTORY_POLL_SECONDS", "5"))
LEASE_SECONDS = float(os.environ.get("CONTENT_FACTORY_LEASE_SECONDS", "120"))
MAX_CONCURRENT_RUNS = int(os.environ.get("CONTENT_FACTORY_MAX_CONCURRENT_RUNS", "5"))
MAX_PARALLEL_NODES = int(os.environ.get("CONTENT_FACTORY_MAX_PARALLEL_NODES", "4"))

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


def runtime_limits() -> dict[str, float | int]:
    """Return live worker limits. Environment values are startup fallbacks;
    saved owner settings take effect on the next worker iteration."""
    try:
        from backend.content_factory import settings as cf_settings

        advanced = cf_settings.get_settings()["advanced"]
        return {
            "pollSeconds": float(advanced["pollSeconds"]),
            "maxConcurrentRuns": int(advanced["maxConcurrentRuns"]),
            "maxParallelNodes": int(advanced["maxParallelNodes"]),
            "leaseSeconds": float(advanced["leaseSeconds"]),
        }
    except Exception:
        return {
            "pollSeconds": POLL_SECONDS,
            "maxConcurrentRuns": MAX_CONCURRENT_RUNS,
            "maxParallelNodes": MAX_PARALLEL_NODES,
            "leaseSeconds": LEASE_SECONDS,
        }


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── Run lifecycle ────────────────────────────────────────────────────────────

def create_run(
    pipeline_id: str,
    *,
    trigger_type: str = TriggerType.MANUAL,
    trigger_data: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    project_id: Optional[str] = None,
    cost_budget: Optional[float] = None,
    created_by: str = "owner",
) -> dict[str, Any]:
    """Queues a run against the pipeline's *active* version — or its draft when
    this is a test run (§107), which is the only case where unpublished changes
    may execute."""
    ensure_schema()
    pipeline = pipelines.require_pipeline(pipeline_id)
    version = pipelines.runnable_version(pipeline_id, allow_draft=dry_run)

    if pipeline["status"] == "ARCHIVED":
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Pipeline is archived", retryable=False
        )
    if not dry_run and pipeline["status"] != "ACTIVE":
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "Pipeline is not active — activate it, or start a test run",
            retryable=False,
        )

    from backend.content_factory import settings as cf_settings

    configured = cf_settings.get_settings()
    budgets = configured["budgets"]
    effective_cost_budget = cost_budget
    if effective_cost_budget is None:
        effective_cost_budget = pipeline.get("max_cost_per_run")
    if effective_cost_budget is None:
        effective_cost_budget = budgets["maxCostPerRun"]
    if not dry_run:
        _enforce_period_budget(str(pipeline["workspace_id"]), budgets)

    running = db.query_one(
        "SELECT COUNT(*) AS live FROM cf_pipeline_runs WHERE pipeline_id = ? AND status IN (?, ?, ?)",
        (pipeline_id, RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING),
    )
    if int((running or {}).get("live") or 0) >= int(pipeline.get("max_concurrent_runs") or 1):
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "This pipeline is already running at its concurrency limit",
            retryable=False,
            details={"limit": pipeline.get("max_concurrent_runs")},
        )

    run_id = _new_id("run")
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_pipeline_runs
            (id, workspace_id, pipeline_id, pipeline_version_id, project_id, trigger_type,
             trigger_data, status, dry_run, publishing_mode, cost_budget, trace_id, created_by,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, pipeline["workspace_id"], pipeline_id, version["id"],
            project_id or pipeline.get("project_id"), trigger_type, db.dumps(trigger_data or {}),
            RunStatus.QUEUED, 1 if dry_run else 0, pipeline.get("publishing_mode") or "REVIEW_REQUIRED",
            effective_cost_budget,
            uuid.uuid4().hex, created_by, now, now,
        ),
    )
    _seed_node_runs(run_id, pipeline["workspace_id"], version)
    events.emit_soon(
        events.PIPELINE_STARTED,
        pipeline_run_id=run_id,
        data={"pipelineId": pipeline_id, "dryRun": dry_run},
    )
    return get_run(run_id)  # type: ignore[return-value]


def _enforce_period_budget(workspace_id: str, budgets: dict[str, Any]) -> None:
    """Refuse a paid run once its daily or monthly ceiling is already spent.
    Test runs remain available so configuration can still be diagnosed."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    periods = (
        ("dailyBudget", now.strftime("%Y-%m-%dT00:00:00+00:00"), "daily"),
        ("monthlyBudget", now.strftime("%Y-%m-01T00:00:00+00:00"), "monthly"),
    )
    for key, since, label in periods:
        limit = budgets.get(key)
        if limit is None:
            continue
        row = db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS spent FROM cf_usage_records WHERE workspace_id = ? AND created_at >= ?",
            (workspace_id, since),
        )
        spent = float((row or {}).get("spent") or 0)
        if spent < float(limit):
            continue
        action = str(budgets.get("onExceeded") or "stop")
        if action == "cheaper":
            return
        message = f"Content Factory {label} budget reached: ${spent:.2f} / ${float(limit):.2f}"
        if action == "approval":
            message += "; owner approval is required before increasing the limit"
        raise ContentFactoryError(
            ErrorCode.BUDGET_LIMIT_REACHED,
            message,
            retryable=False,
            details={"period": label, "spent": spent, "limit": float(limit), "action": action},
        )


def _seed_node_runs(run_id: str, workspace_id: str, version: dict[str, Any]) -> None:
    """One PENDING row per node up front, so the UI can show the whole pipeline
    from the first second and a node's status is never "missing"."""
    graph = PipelineGraph.from_version(version)
    now = db.now_iso()
    rows = []
    for node in graph.nodes.values():
        rows.append(
            (
                _new_id("nrun"), workspace_id, run_id, node.id, node.type, node.name,
                NodeStatus.PENDING if node.enabled else NodeStatus.SKIPPED, now, now,
            )
        )
    db.execute_many(
        """
        INSERT INTO cf_node_runs
            (id, workspace_id, pipeline_run_id, node_id, node_type, name, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def get_run(run_id: str, *, with_nodes: bool = False) -> Optional[dict[str, Any]]:
    ensure_schema()
    run = db.query_one("SELECT * FROM cf_pipeline_runs WHERE id = ?", (run_id,))
    if run and with_nodes:
        run["nodes"] = get_node_runs(run_id)
    return run


def get_node_runs(run_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM cf_node_runs WHERE pipeline_run_id = ? ORDER BY created_at, node_id", (run_id,)
    )


def list_runs(
    *, pipeline_id: Optional[str] = None, status: Optional[str] = None,
    workspace_id: str = WORKSPACE_DEFAULT, limit: int = 50, cursor: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema()
    clauses = ["workspace_id = ?"]
    params: list[Any] = [workspace_id]
    if pipeline_id:
        clauses.append("pipeline_id = ?")
        params.append(pipeline_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if cursor:
        clauses.append("created_at < ?")
        params.append(cursor)
    page_size = max(1, min(int(limit), 200))
    params.append(page_size + 1)
    rows = db.query(
        f"SELECT * FROM cf_pipeline_runs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > page_size
    items = rows[:page_size]
    return {"items": items, "nextCursor": items[-1]["created_at"] if has_more and items else None}


def cancel_run(run_id: str, *, reason: str = "Cancelled by owner") -> dict[str, Any]:
    """§94. Marks the run cancelled first, so no further node starts, then asks
    providers to cancel what is already in flight. Assets already produced are
    kept — they were paid for."""
    run = get_run(run_id)
    if not run:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such run: {run_id}", retryable=False)
    if run["status"] in RunStatus.TERMINAL:
        return run

    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_pipeline_runs
        SET status = ?, error_code = ?, error_message = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (RunStatus.CANCELLED, ErrorCode.CANCELLED, reason, now, now, run_id),
    )
    db.execute(
        f"""
        UPDATE cf_node_runs SET status = ?, updated_at = ?
        WHERE pipeline_run_id = ? AND status IN ({','.join('?' * len(NodeStatus.PENDING_WORK))})
        """,
        (NodeStatus.CANCELLED, now, run_id, *sorted(NodeStatus.PENDING_WORK)),
    )
    events.emit_soon(events.PIPELINE_CANCELLED, pipeline_run_id=run_id, data={"reason": reason})
    return get_run(run_id)  # type: ignore[return-value]


def approve_run(run_id: str, *, node_id: str = "", decision: str = "APPROVED", note: str = "",
                actor: str = "owner") -> dict[str, Any]:
    """§36. Records the owner's decision on the waiting approval node and puts
    the run back in the queue; the worker picks it up on its next tick."""
    run = get_run(run_id)
    if not run:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such run: {run_id}", retryable=False)
    if run["status"] != RunStatus.WAITING_APPROVAL:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "This run is not waiting for approval", retryable=False
        )

    waiting = db.query_one(
        "SELECT * FROM cf_node_runs WHERE pipeline_run_id = ? AND status = ?"
        + (" AND node_id = ?" if node_id else ""),
        (run_id, NodeStatus.WAITING_APPROVAL, *( (node_id,) if node_id else () )),
    )
    if not waiting:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "No node is waiting for approval", retryable=False
        )

    now = db.now_iso()
    decision = decision.upper()
    output = db.loads(waiting.get("output"), {}) or {}
    output.update({"decision": decision, "note": note, "actor": actor})
    db.execute(
        "UPDATE cf_node_runs SET status = ?, output = ?, updated_at = ? WHERE id = ?",
        (NodeStatus.PENDING, db.dumps(output), now, waiting["id"]),
    )
    db.execute(
        "UPDATE cf_pipeline_runs SET status = ?, updated_at = ? WHERE id = ?",
        (RunStatus.QUEUED, now, run_id),
    )
    _log_activity(run_id, f"approval {decision.lower()} on {waiting['node_id']} by {actor}")
    return get_run(run_id)  # type: ignore[return-value]


def retry_node(run_id: str, node_id: str, *, regenerate: bool = False) -> dict[str, Any]:
    """§41/§42. Resets one node and everything downstream of it, leaving
    completed upstream work alone — the expensive half of a failed run does not
    have to be paid for twice.

    `regenerate` differs from `retry` only in that it discards the node's stored
    output first, so the node produces a genuinely new result rather than
    resuming from what it already had.
    """
    run = get_run(run_id)
    if not run:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such run: {run_id}", retryable=False)
    version = pipelines.get_version(str(run["pipeline_version_id"]))
    if not version:
        raise ContentFactoryError(ErrorCode.INTERNAL, "Run's pipeline version is missing", retryable=False)

    graph = PipelineGraph.from_version(version)
    if node_id not in graph.nodes:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"No node '{node_id}' in this pipeline", retryable=False
        )

    target_row = db.query_one(
        "SELECT * FROM cf_node_runs WHERE pipeline_run_id = ? AND node_id = ?", (run_id, node_id)
    )
    if target_row and target_row["status"] == NodeStatus.COMPLETED and not regenerate:
        # §42: retry is for something that failed. Re-running a node that
        # succeeded is a *regenerate* — a new AI execution and a new charge —
        # and the caller has to say so.
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            f"Node '{node_id}' completed successfully — use regenerate to run it again",
            retryable=False,
        )

    invalidated = _descendants(graph, node_id) | {node_id}
    now = db.now_iso()
    for target in invalidated:
        db.execute(
            """
            UPDATE cf_node_runs
            SET status = ?, attempt = 0, error_code = '', error_message = '', fallback_index = 0,
                external_job_id = '', next_poll_at = NULL, output = '{}', progress = 0,
                started_at = NULL, finished_at = NULL, updated_at = ?
            WHERE pipeline_run_id = ? AND node_id = ?
            """,
            (NodeStatus.PENDING, now, run_id, target),
        )
    db.execute(
        """
        UPDATE cf_pipeline_runs
        SET status = ?, error_code = '', error_message = '', completed_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (RunStatus.QUEUED, now, run_id),
    )
    _log_activity(run_id, f"{'regenerate' if regenerate else 'retry'} from node {node_id}")
    return get_run(run_id, with_nodes=True)  # type: ignore[return-value]


def _descendants(graph: PipelineGraph, node_id: str) -> set[str]:
    seen: set[str] = set()
    frontier = list(graph.children(node_id))
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(graph.children(current))
    return seen


# ── Driving a run ────────────────────────────────────────────────────────────

async def drive_run(run_id: str) -> dict[str, Any]:
    """Runs one pipeline to a stopping point: completion, failure, cancellation
    or a suspension (approval / external wait with nothing else to do)."""
    ensure_schema()
    run = get_run(run_id)
    if not run:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such run: {run_id}", retryable=False)

    version = pipelines.get_version(str(run["pipeline_version_id"]))
    if not version:
        return _fail_run(run_id, ErrorCode.INTERNAL, "Pipeline version is missing")
    graph = PipelineGraph.from_version(version)

    _mark_running(run_id)
    while True:
        run = get_run(run_id)
        if not run or run["status"] in RunStatus.TERMINAL:
            return run or {}
        if _kill_switch_engaged():
            return _fail_run(run_id, ErrorCode.INTERNAL, "Control plane kill switch is engaged")

        _promote_retries(run_id)
        node_rows = {row["node_id"]: row for row in get_node_runs(run_id)}
        _propagate_skips(run_id, graph, node_rows)
        node_rows = {row["node_id"]: row for row in get_node_runs(run_id)}

        ready = _ready_nodes(graph, node_rows)
        polling = [row for row in node_rows.values() if row["status"] == NodeStatus.WAITING_EXTERNAL]
        retrying = [row for row in node_rows.values() if row["status"] == NodeStatus.RETRYING]

        if ready:
            await _execute_batch(run, graph, ready)
            _renew_lease(run_id)
            continue

        if polling:
            await _poll_external(run, graph, polling)
            _renew_lease(run_id)
            await asyncio.sleep(min(POLL_SECONDS, 5.0))
            continue

        if retrying:
            delay = min(
                (max(0.0, _seconds_until(row.get("next_poll_at"))) for row in retrying), default=1.0
            )
            _renew_lease(run_id)
            await asyncio.sleep(min(max(delay, 0.5), 30.0))
            continue

        if any(row["status"] == NodeStatus.WAITING_APPROVAL for row in node_rows.values()):
            _set_run_status(run_id, RunStatus.WAITING_APPROVAL)
            return get_run(run_id)  # type: ignore[return-value]

        return _finish_run(run_id, node_rows)


def _ready_nodes(graph: PipelineGraph, node_rows: dict[str, dict[str, Any]]) -> list[PipelineNode]:
    """Nodes whose parents are all finished and which have not run yet (§19 —
    everything ready runs together)."""
    ready: list[PipelineNode] = []
    for node_id, node in graph.nodes.items():
        row = node_rows.get(node_id)
        if not row or row["status"] != NodeStatus.PENDING or not node.enabled:
            continue
        parents = graph.parents(node_id)
        if all(
            (node_rows.get(parent) or {}).get("status") in NodeStatus.TERMINAL for parent in parents
        ):
            # A node all of whose parents were skipped is itself unreachable.
            if parents and all(
                (node_rows.get(parent) or {}).get("status") == NodeStatus.SKIPPED for parent in parents
            ):
                continue
            ready.append(node)
    return ready


def _promote_retries(run_id: str) -> None:
    """RETRYING → PENDING once the backoff has elapsed (§20)."""
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_node_runs
        SET status = ?, updated_at = ?
        WHERE pipeline_run_id = ? AND status = ?
          AND (next_poll_at IS NULL OR next_poll_at <= ?)
        """,
        (NodeStatus.PENDING, now, run_id, NodeStatus.RETRYING, now),
    )


def _propagate_skips(
    run_id: str, graph: PipelineGraph, node_rows: dict[str, dict[str, Any]]
) -> None:
    """Marks branches that were not taken as SKIPPED (§18).

    A node is skipped when every edge that could reach it comes from a
    completed branching node whose chosen branch was something else — so a QA
    FAIL path does not sit PENDING forever after QA passed.
    """
    now = db.now_iso()
    for node_id in graph.nodes:
        row = node_rows.get(node_id)
        if not row or row["status"] != NodeStatus.PENDING:
            continue
        incoming = [edge for edge in graph.edges if edge.target == node_id]
        if not incoming:
            continue
        decided, viable = True, False
        for edge in incoming:
            parent = node_rows.get(edge.source)
            if not parent or parent["status"] not in NodeStatus.TERMINAL:
                decided = False
                break
            if parent["status"] == NodeStatus.SKIPPED:
                continue
            branch = str((db.loads(parent.get("output"), {}) or {}).get("_branch") or "")
            if not edge.branch or not branch or edge.branch == branch:
                viable = True
        if decided and not viable:
            db.execute(
                "UPDATE cf_node_runs SET status = ?, updated_at = ? WHERE pipeline_run_id = ? AND node_id = ?",
                (NodeStatus.SKIPPED, now, run_id, node_id),
            )


async def _execute_batch(
    run: dict[str, Any], graph: PipelineGraph, ready: list[PipelineNode]
) -> None:
    semaphore = asyncio.Semaphore(int(runtime_limits()["maxParallelNodes"]))

    async def guarded(node: PipelineNode) -> None:
        async with semaphore:
            await _execute_node(run, graph, node)

    await asyncio.gather(*(guarded(node) for node in ready), return_exceptions=True)


async def _execute_node(run: dict[str, Any], graph: PipelineGraph, node: PipelineNode) -> None:
    run_id = str(run["id"])
    row = db.query_one(
        "SELECT * FROM cf_node_runs WHERE pipeline_run_id = ? AND node_id = ?", (run_id, node.id)
    )
    if not row:
        return

    attempt = int(row.get("attempt") or 0) + 1
    fallback_index = int(row.get("fallback_index") or 0)
    started = time.monotonic()
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_node_runs
        SET status = ?, attempt = ?, started_at = COALESCE(started_at, ?), updated_at = ?
        WHERE id = ?
        """,
        (NodeStatus.RUNNING, attempt, now, now, row["id"]),
    )
    await events.emit(
        events.NODE_STARTED,
        pipeline_run_id=run_id,
        node_run_id=row["id"],
        data={"nodeId": node.id, "type": node.type, "attempt": attempt},
    )

    context = _build_context(run, graph, node, row, attempt, fallback_index)
    handler = get_node_handler(node.type)

    try:
        result = await asyncio.wait_for(handler.execute(context), timeout=max(10, node.timeout))
    except asyncio.TimeoutError:
        await _handle_node_error(
            run, node, row, attempt, fallback_index,
            ContentFactoryError(ErrorCode.NODE_TIMEOUT, f"Node timed out after {node.timeout}s"),
            started,
        )
        return
    except ContentFactoryError as error:
        await _handle_node_error(run, node, row, attempt, fallback_index, error, started)
        return
    except Exception as exc:  # a handler bug must fail the node, not the worker
        logger.exception("Node %s raised", node.id)
        await _handle_node_error(
            run, node, row, attempt, fallback_index,
            ContentFactoryError(ErrorCode.INTERNAL, f"{type(exc).__name__}", retryable=False),
            started,
        )
        return

    await _store_success(run, node, row, result, started, attempt, fallback_index)


def _build_context(
    run: dict[str, Any],
    graph: PipelineGraph,
    node: PipelineNode,
    row: dict[str, Any],
    attempt: int,
    fallback_index: int,
) -> NodeContext:
    outputs = _node_outputs(str(run["id"]))
    template_context = {
        "nodes": {node_id: {"output": output} for node_id, output in outputs.items()},
        "run": {"id": run["id"], "projectId": run.get("project_id"), "dryRun": bool(run.get("dry_run"))},
        "vars": graph.variables,
        "trigger": db.loads(run.get("trigger_data"), {}) or {},
    }
    inputs = render_template(node.input_mapping, template_context) if node.input_mapping else {}
    if not isinstance(inputs, dict):
        inputs = {"value": inputs}

    stored_output = db.loads(row.get("output"), {}) or {}
    # An approval decision recorded by `approve_run` is handed back to the node
    # as config, which is how a suspended node resumes without special-casing it
    # in the engine.
    if stored_output.get("decision"):
        inputs.setdefault("_decision", stored_output["decision"])
        inputs.setdefault("_note", stored_output.get("note", ""))

    return NodeContext(
        node=node,
        run=run,
        inputs=inputs,
        node_outputs=outputs,
        node_run_id=str(row["id"]),
        external_job_id=str(row.get("external_job_id") or ""),
        attempt=attempt,
        provider_index=fallback_index,
        dry_run=bool(run.get("dry_run")),
        progress=_progress_reporter(str(run["id"]), str(row["id"]), node.id),
    )


def _progress_reporter(run_id: str, node_run_id: str, node_id: str):
    async def report(percent: int, message: str = "") -> None:
        await events.emit(
            events.NODE_PROGRESS,
            pipeline_run_id=run_id,
            node_run_id=node_run_id,
            data={"nodeId": node_id, "progress": percent, "message": message},
        )

    return report


def _node_outputs(run_id: str) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for row in get_node_runs(run_id):
        if row["status"] == NodeStatus.COMPLETED:
            outputs[row["node_id"]] = db.loads(row.get("output"), {}) or {}
    return outputs


async def _store_success(
    run: dict[str, Any],
    node: PipelineNode,
    row: dict[str, Any],
    result: NodeResult,
    started: float,
    attempt: int,
    fallback_index: int,
) -> None:
    run_id = str(run["id"])
    now = db.now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)

    if result.waiting_approval:
        db.execute(
            """
            UPDATE cf_node_runs
            SET status = ?, output = ?, updated_at = ?, duration_ms = ?
            WHERE id = ?
            """,
            (NodeStatus.WAITING_APPROVAL, db.dumps(result.output), now, duration_ms, row["id"]),
        )
        await events.emit(
            events.CONTENT_READY_FOR_REVIEW,
            pipeline_run_id=run_id,
            node_run_id=row["id"],
            data={"nodeId": node.id, **result.output},
        )
        return

    if result.external_job_id:
        # Written before any polling begins: this row is what stops a restart
        # from submitting (and paying for) the same job twice.
        db.execute(
            """
            UPDATE cf_node_runs
            SET status = ?, output = ?, external_job_id = ?, next_poll_at = ?, provider = ?,
                model = ?, fallback_index = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                NodeStatus.WAITING_EXTERNAL, db.dumps(result.output), result.external_job_id,
                _in_seconds(result.poll_after or 15), result.provider, result.model,
                fallback_index, now, row["id"],
            ),
        )
        await events.emit(
            events.NODE_WAITING,
            pipeline_run_id=run_id,
            node_run_id=row["id"],
            data={"nodeId": node.id, "externalJobId": result.external_job_id},
        )
        return

    output = dict(result.output)
    if result.branch:
        output["_branch"] = result.branch

    db.execute(
        """
        UPDATE cf_node_runs
        SET status = ?, output = ?, cost = cost + ?, provider = ?, model = ?, fallback_index = ?,
            finished_at = ?, duration_ms = ?, logs = ?, metrics = ?, progress = 100,
            error_code = '', error_message = '', updated_at = ?
        WHERE id = ?
        """,
        (
            NodeStatus.COMPLETED, db.dumps(output), round(float(result.cost or 0), 6),
            result.provider, result.model, fallback_index, now, duration_ms,
            db.dumps(result.logs), db.dumps(result.metrics), now, row["id"],
        ),
    )
    _add_run_cost(run_id, float(result.cost or 0))
    _update_progress(run_id)
    await events.emit(
        events.NODE_COMPLETED,
        pipeline_run_id=run_id,
        node_run_id=row["id"],
        data={
            "nodeId": node.id,
            "provider": result.provider,
            "model": result.model,
            "cost": result.cost,
            "durationMs": duration_ms,
            "branch": result.branch,
        },
    )


async def _handle_node_error(
    run: dict[str, Any],
    node: PipelineNode,
    row: dict[str, Any],
    attempt: int,
    fallback_index: int,
    error: ContentFactoryError,
    started: float,
) -> None:
    """Retry, then fallback, then the pipeline's failure strategy (§20, §21, §95).

    Order matters: retrying the same provider is cheap and usually enough for a
    rate limit or a transient 500; moving to the next provider is the answer
    when that provider is genuinely unavailable; failing the run is the last
    resort.
    """
    run_id = str(run["id"])
    now = db.now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)
    from backend.content_factory.nodes import util as node_util

    can_retry = error.retryable and attempt < node.retry_policy.max_attempts
    chain_length = 0
    if not can_retry and error.retryable:
        try:
            chain_length = len(node_util.provider_chain(_context_stub(run, node), _category_for(node)))
        except Exception:
            chain_length = 0
    can_fallback = (not can_retry) and error.retryable and fallback_index + 1 < chain_length

    if can_retry or can_fallback:
        next_index = fallback_index + 1 if can_fallback else fallback_index
        delay = 0.0 if can_fallback else node.retry_policy.delay_for(attempt + 1)
        # RETRYING with a due time, not a sleeping coroutine: `_promote_retries`
        # moves it back to PENDING when the delay elapses, so a backoff of a
        # minute holds no worker and survives a restart.
        db.execute(
            """
            UPDATE cf_node_runs
            SET status = ?, attempt = ?, fallback_index = ?, next_poll_at = ?, error_code = ?,
                error_message = ?, retryable = 1, duration_ms = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                NodeStatus.RETRYING, attempt, next_index, _in_seconds(delay),
                error.code, error.message[:500], duration_ms, now, row["id"],
            ),
        )
        await events.emit(
            events.NODE_RETRYING,
            pipeline_run_id=run_id,
            node_run_id=row["id"],
            data={
                "nodeId": node.id,
                "attempt": attempt,
                "maxAttempts": node.retry_policy.max_attempts,
                "code": error.code,
                "nextRetryInSeconds": delay,
                "fallbackIndex": next_index,
            },
        )
        return

    db.execute(
        """
        UPDATE cf_node_runs
        SET status = ?, error_code = ?, error_message = ?, retryable = ?, finished_at = ?,
            duration_ms = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            NodeStatus.FAILED, error.code, error.message[:500], 1 if error.retryable else 0,
            now, duration_ms, now, row["id"],
        ),
    )
    await events.emit(
        events.NODE_FAILED,
        pipeline_run_id=run_id,
        node_run_id=row["id"],
        data={"nodeId": node.id, "code": error.code, "message": error.message, "retryable": error.retryable},
    )

    strategy = _strategy_for(run, node)
    if strategy == ErrorStrategy.CONTINUE or node.continue_on_error:
        _update_progress(run_id)
        return
    if strategy == ErrorStrategy.WAIT_FOR_USER:
        _set_run_status(run_id, RunStatus.WAITING_APPROVAL)
        return
    _fail_run(run_id, error.code, f"{node.id}: {error.message}")


def _context_stub(run: dict[str, Any], node: PipelineNode) -> NodeContext:
    """Minimal context used only to ask how long this node's provider chain is."""
    return NodeContext(node=node, run=run, inputs={})


def _category_for(node: PipelineNode) -> str:
    return get_node_handler(node.type).provider_category or "LLM"


def _strategy_for(run: dict[str, Any], node: PipelineNode) -> str:
    override = node.config.get("errorStrategy")
    if override in ErrorStrategy.ALL:
        return str(override)
    pipeline = pipelines.get_pipeline(str(run["pipeline_id"])) or {}
    return str(pipeline.get("error_strategy") or ErrorStrategy.FAIL_FAST)


async def _poll_external(
    run: dict[str, Any], graph: PipelineGraph, rows: list[dict[str, Any]]
) -> None:
    """§25 polling. A node stays parked until its handler says the job finished."""
    for row in rows:
        if _seconds_until(row.get("next_poll_at")) > 0:
            continue
        node = graph.nodes.get(str(row["node_id"]))
        if not node:
            continue
        handler = get_node_handler(node.type)
        context = _build_context(
            run, graph, node, row, int(row.get("attempt") or 1), int(row.get("fallback_index") or 0)
        )
        # The submit output (job ids) is the poll's input.
        context.inputs = {**context.inputs, **(db.loads(row.get("output"), {}) or {})}
        started = time.monotonic()
        try:
            result = await handler.poll(context)
        except ContentFactoryError as error:
            await _handle_node_error(
                run, node, row, int(row.get("attempt") or 1), int(row.get("fallback_index") or 0),
                error, started,
            )
            continue
        except Exception as exc:
            logger.exception("Polling %s failed", node.id)
            await _handle_node_error(
                run, node, row, int(row.get("attempt") or 1), int(row.get("fallback_index") or 0),
                ContentFactoryError(ErrorCode.INTERNAL, type(exc).__name__, retryable=False), started,
            )
            continue

        if result is None:
            db.execute(
                "UPDATE cf_node_runs SET next_poll_at = ?, updated_at = ? WHERE id = ?",
                (_in_seconds(float(node.config.get("pollInterval", 15))), db.now_iso(), row["id"]),
            )
            continue
        await _store_success(
            run, node, row, result, started, int(row.get("attempt") or 1),
            int(row.get("fallback_index") or 0),
        )


# ── Run state helpers ────────────────────────────────────────────────────────

def _mark_running(run_id: str) -> None:
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_pipeline_runs
        SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?
        WHERE id = ? AND status IN (?, ?, ?)
        """,
        (RunStatus.RUNNING, now, now, run_id, RunStatus.QUEUED, RunStatus.WAITING, RunStatus.WAITING_APPROVAL),
    )


def _set_run_status(run_id: str, status: str) -> None:
    db.execute(
        "UPDATE cf_pipeline_runs SET status = ?, updated_at = ? WHERE id = ?",
        (status, db.now_iso(), run_id),
    )


def _fail_run(run_id: str, code: str, message: str) -> dict[str, Any]:
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_pipeline_runs
        SET status = ?, error_code = ?, error_message = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (RunStatus.FAILED, code, message[:500], now, now, run_id),
    )
    events.emit_soon(events.PIPELINE_FAILED, pipeline_run_id=run_id, data={"code": code, "message": message})
    _log_activity(run_id, f"run failed: {code}")
    return get_run(run_id)  # type: ignore[return-value]


def _finish_run(run_id: str, node_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A run with some failed-but-tolerated nodes is PARTIALLY_COMPLETED, not
    COMPLETED (§16): the difference is what tells an owner to go and look."""
    failed = [row for row in node_rows.values() if row["status"] == NodeStatus.FAILED]
    status = RunStatus.PARTIALLY_COMPLETED if failed else RunStatus.COMPLETED
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_pipeline_runs
        SET status = ?, progress = 100, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, now, now, run_id),
    )
    events.emit_soon(
        events.PIPELINE_COMPLETED,
        pipeline_run_id=run_id,
        data={"status": status, "failedNodes": [row["node_id"] for row in failed]},
    )
    _log_activity(run_id, f"run finished: {status}")
    return get_run(run_id)  # type: ignore[return-value]


def _update_progress(run_id: str) -> None:
    rows = get_node_runs(run_id)
    if not rows:
        return
    done = sum(1 for row in rows if row["status"] in NodeStatus.TERMINAL)
    db.execute(
        "UPDATE cf_pipeline_runs SET progress = ?, updated_at = ? WHERE id = ?",
        (int(done / len(rows) * 100), db.now_iso(), run_id),
    )


def _add_run_cost(run_id: str, amount: float) -> None:
    if amount <= 0:
        return
    db.execute(
        "UPDATE cf_pipeline_runs SET actual_cost = actual_cost + ?, updated_at = ? WHERE id = ?",
        (round(amount, 6), db.now_iso(), run_id),
    )


def _in_seconds(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))).isoformat(timespec="seconds")


def _seconds_until(timestamp: Any) -> float:
    if not timestamp:
        return 0.0
    from datetime import datetime, timezone

    try:
        target = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return 0.0
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - datetime.now(timezone.utc)).total_seconds()


def _kill_switch_engaged() -> bool:
    """The existing control-plane kill switch stops Content Factory too — one
    emergency stop for the whole system, not one per subsystem."""
    try:
        from backend.control_plane import get_control_state

        return bool(get_control_state().get("kill_switch"))
    except Exception:
        return False


def _log_activity(run_id: str, message: str) -> None:
    try:
        from backend.activity_logger import log_activity

        log_activity("active", "ContentFactory", f"{message} ({run_id})")
    except Exception:
        logger.debug("Activity log unavailable", exc_info=True)


# ── Worker (§17 durability, §67 stateless workers) ───────────────────────────

def claim_run(run_id: str) -> bool:
    """Takes the lease on a run. False means another worker holds it."""
    now = db.now_iso()
    return db.execute(
        """
        UPDATE cf_pipeline_runs
        SET worker_id = ?, lease_expires_at = ?, updated_at = ?
        WHERE id = ? AND (worker_id = '' OR worker_id = ? OR lease_expires_at IS NULL OR lease_expires_at < ?)
        """,
        (WORKER_ID, _in_seconds(float(runtime_limits()["leaseSeconds"])), now, run_id, WORKER_ID, now),
    ) > 0


def _renew_lease(run_id: str) -> None:
    db.execute(
        "UPDATE cf_pipeline_runs SET lease_expires_at = ? WHERE id = ? AND worker_id = ?",
        (_in_seconds(float(runtime_limits()["leaseSeconds"])), run_id, WORKER_ID),
    )


def release_run(run_id: str) -> None:
    db.execute(
        "UPDATE cf_pipeline_runs SET worker_id = '', lease_expires_at = NULL WHERE id = ? AND worker_id = ?",
        (run_id, WORKER_ID),
    )


def recover_crashed_runs() -> list[str]:
    """Runs whose worker died mid-flight: the lease expired while the run was
    still RUNNING. Their in-flight nodes go back to PENDING so the work resumes
    instead of hanging — except nodes parked on an external job, which are
    resumed by polling the job that is already paid for."""
    now = db.now_iso()
    stale = db.query(
        """
        SELECT id FROM cf_pipeline_runs
        WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
        """,
        (RunStatus.RUNNING, now),
    )
    recovered = []
    for row in stale:
        run_id = str(row["id"])
        db.execute(
            "UPDATE cf_node_runs SET status = ?, updated_at = ? WHERE pipeline_run_id = ? AND status = ?",
            (NodeStatus.PENDING, now, run_id, NodeStatus.RUNNING),
        )
        db.execute(
            "UPDATE cf_pipeline_runs SET status = ?, worker_id = '', lease_expires_at = NULL, updated_at = ? WHERE id = ?",
            (RunStatus.QUEUED, now, run_id),
        )
        recovered.append(run_id)
    if recovered:
        logger.info("Content Factory: recovered %d interrupted run(s)", len(recovered))
    return recovered


async def worker_loop() -> None:
    """Started from the app lifespan, mirroring `dev_runs.worker_loop`."""
    ensure_schema()
    limits = runtime_limits()
    logger.info("Content Factory worker started (%s, poll %.1fs, %d concurrent)",
                WORKER_ID, limits["pollSeconds"], limits["maxConcurrentRuns"])
    in_flight: dict[str, asyncio.Task] = {}
    while True:
        try:
            limits = runtime_limits()
            max_concurrent = int(limits["maxConcurrentRuns"])
            in_flight = {run_id: task for run_id, task in in_flight.items() if not task.done()}
            recover_crashed_runs()
            free = max_concurrent - len(in_flight)
            if free > 0:
                candidates = db.query(
                    """
                    SELECT id FROM cf_pipeline_runs
                    WHERE status IN (?, ?)
                    ORDER BY created_at
                    LIMIT ?
                    """,
                    (RunStatus.QUEUED, RunStatus.RUNNING, free + len(in_flight)),
                )
                for row in candidates:
                    run_id = str(row["id"])
                    if run_id in in_flight or len(in_flight) >= max_concurrent:
                        continue
                    if claim_run(run_id):
                        in_flight[run_id] = asyncio.create_task(_drive_and_release(run_id))
        except asyncio.CancelledError:
            logger.info("Content Factory worker stopped")
            for task in in_flight.values():
                task.cancel()
            raise
        except Exception:
            logger.exception("Content Factory worker iteration failed")
        await asyncio.sleep(float(limits["pollSeconds"]))


async def _drive_and_release(run_id: str) -> None:
    try:
        await drive_run(run_id)
    except Exception:
        logger.exception("Run %s crashed", run_id)
        _fail_run(run_id, ErrorCode.INTERNAL, "Worker error")
    finally:
        release_run(run_id)
