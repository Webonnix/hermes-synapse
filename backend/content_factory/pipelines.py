"""Pipelines and their immutable versions (§10, §11, §96).

The split that makes traceability possible: `cf_pipelines` is the mutable
header (name, status, publishing mode, limits) and `cf_pipeline_versions` holds
frozen snapshots of the graph. A run records both ids, so a video produced in
March can still be explained after the pipeline was rewritten in May — the
version it ran is exactly as it was.

Editing an active pipeline therefore never touches what is running: it writes
into the pipeline's *draft* version, and activating is a separate, explicit act
(§105) that runs validation first (§96). A pipeline with a critical validation
error cannot be activated at all — the cost of a broken schedule firing every
morning is far higher than the cost of refusing to save.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from backend.content_factory import db
from backend.content_factory.domain import (
    ContentFactoryError,
    ErrorCode,
    ErrorStrategy,
    PipelineGraph,
    PublishingMode,
)
from backend.content_factory.schema import SCHEMA_VERSION, WORKSPACE_DEFAULT, ensure_schema

logger = logging.getLogger("hermes.content_factory.pipelines")

PIPELINE_STATUSES = {"DRAFT", "ACTIVE", "PAUSED", "ARCHIVED"}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── Pipelines ────────────────────────────────────────────────────────────────

def create_pipeline(
    name: str,
    *,
    project_id: Optional[str] = None,
    description: str = "",
    publishing_mode: str = PublishingMode.REVIEW_REQUIRED,
    error_strategy: str = ErrorStrategy.FAIL_FAST,
    max_cost_per_run: Optional[float] = None,
    max_concurrent_runs: int = 1,
    nodes: Optional[list[dict[str, Any]]] = None,
    edges: Optional[list[dict[str, Any]]] = None,
    variables: Optional[dict[str, Any]] = None,
    created_by: str = "owner",
    workspace_id: str = WORKSPACE_DEFAULT,
) -> dict[str, Any]:
    ensure_schema()
    clean_name = str(name).strip()
    if not clean_name or len(clean_name) > 120:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Pipeline name must be 1-120 characters", retryable=False
        )
    if publishing_mode not in PublishingMode.ALL:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown publishing mode: {publishing_mode}", retryable=False
        )
    if error_strategy not in ErrorStrategy.ALL:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown error strategy: {error_strategy}", retryable=False
        )

    pipeline_id = _new_id("pipe")
    now = db.now_iso()
    with db.transaction() as tx:
        tx.execute(
            """
            INSERT INTO cf_pipelines
                (id, workspace_id, project_id, name, description, status, publishing_mode,
                 error_strategy, max_cost_per_run, max_concurrent_runs, created_by,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pipeline_id, workspace_id, project_id, clean_name, description or "",
                publishing_mode, error_strategy, max_cost_per_run, max(1, int(max_concurrent_runs)),
                created_by, now, now,
            ),
        )
        version_id = _insert_version(
            tx,
            pipeline_id=pipeline_id,
            workspace_id=workspace_id,
            version=1,
            nodes=nodes or [],
            edges=edges or [],
            variables=variables or {},
            created_by=created_by,
        )
        tx.execute(
            "UPDATE cf_pipelines SET draft_version_id = ?, updated_at = ? WHERE id = ?",
            (version_id, now, pipeline_id),
        )
    return get_pipeline(pipeline_id)  # type: ignore[return-value]


def get_pipeline(pipeline_id: str) -> Optional[dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM cf_pipelines WHERE id = ?", (pipeline_id,))


def require_pipeline(pipeline_id: str) -> dict[str, Any]:
    pipeline = get_pipeline(pipeline_id)
    if not pipeline:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"No such pipeline: {pipeline_id}", retryable=False
        )
    return pipeline


def list_pipelines(
    *,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    workspace_id: str = WORKSPACE_DEFAULT,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_schema()
    clauses = ["workspace_id = ?"]
    params: list[Any] = [workspace_id]
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    return db.query(
        f"SELECT * FROM cf_pipelines WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )


def update_pipeline(pipeline_id: str, **fields: Any) -> dict[str, Any]:
    """Header-only edits. The graph is edited through `save_draft`, because a
    graph change must produce a version, not mutate one."""
    ensure_schema()
    require_pipeline(pipeline_id)
    allowed = {
        "name", "description", "project_id", "publishing_mode", "error_strategy",
        "max_cost_per_run", "max_concurrent_runs",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return require_pipeline(pipeline_id)
    if "publishing_mode" in updates and updates["publishing_mode"] not in PublishingMode.ALL:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Unknown publishing mode", retryable=False
        )
    if "error_strategy" in updates and updates["error_strategy"] not in ErrorStrategy.ALL:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Unknown error strategy", retryable=False)

    assignments = ", ".join(f"{column} = ?" for column in updates)
    db.execute(
        f"UPDATE cf_pipelines SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), db.now_iso(), pipeline_id),
    )
    return require_pipeline(pipeline_id)


def set_status(pipeline_id: str, status: str) -> dict[str, Any]:
    """ACTIVE requires a clean validation (§96): activation is the moment a
    schedule starts firing unattended, so it is the right place to be strict."""
    pipeline = require_pipeline(pipeline_id)
    if status not in PIPELINE_STATUSES:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"Unknown status: {status}", retryable=False)

    if status == "ACTIVE":
        version_id = pipeline.get("active_version_id") or pipeline.get("draft_version_id")
        if not version_id:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "Pipeline has no version to activate", retryable=False
            )
        report = validate_version(version_id)
        if report["errors"]:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "Pipeline cannot be activated with validation errors",
                retryable=False,
                details={"errors": report["errors"]},
            )
        publish_version(version_id)

    db.execute(
        "UPDATE cf_pipelines SET status = ?, updated_at = ? WHERE id = ?",
        (status, db.now_iso(), pipeline_id),
    )
    return require_pipeline(pipeline_id)


# ── Versions ─────────────────────────────────────────────────────────────────

def _insert_version(
    tx: Any,
    *,
    pipeline_id: str,
    workspace_id: str,
    version: int,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    variables: dict[str, Any],
    config: Optional[dict[str, Any]] = None,
    notes: str = "",
    created_by: str = "owner",
) -> str:
    version_id = _new_id("ver")
    now = db.now_iso()
    tx.execute(
        """
        INSERT INTO cf_pipeline_versions
            (id, workspace_id, pipeline_id, version, schema_version, nodes, edges, variables,
             config, notes, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id, workspace_id, pipeline_id, version, SCHEMA_VERSION,
            db.dumps(nodes), db.dumps(edges), db.dumps(variables), db.dumps(config or {}),
            notes, created_by, now, now,
        ),
    )
    return version_id


def save_draft(
    pipeline_id: str,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    variables: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
    notes: str = "",
    created_by: str = "owner",
) -> dict[str, Any]:
    """Writes the graph into the pipeline's draft version.

    An unpublished draft is overwritten in place (autosave, §105). A published
    version is never touched — editing after activation opens a new draft at
    the next version number.
    """
    ensure_schema()
    pipeline = require_pipeline(pipeline_id)
    draft = get_version(pipeline.get("draft_version_id") or "") if pipeline.get("draft_version_id") else None

    with db.transaction() as tx:
        if draft and not draft.get("published_at"):
            tx.execute(
                """
                UPDATE cf_pipeline_versions
                SET nodes = ?, edges = ?, variables = ?, config = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    db.dumps(nodes), db.dumps(edges), db.dumps(variables or {}),
                    db.dumps(config or {}), notes, db.now_iso(), draft["id"],
                ),
            )
            version_id = draft["id"]
        else:
            row = tx.query_one(
                "SELECT MAX(version) AS top FROM cf_pipeline_versions WHERE pipeline_id = ?",
                (pipeline_id,),
            )
            next_version = int((row or {}).get("top") or 0) + 1
            version_id = _insert_version(
                tx,
                pipeline_id=pipeline_id,
                workspace_id=pipeline["workspace_id"],
                version=next_version,
                nodes=nodes,
                edges=edges,
                variables=variables or {},
                config=config,
                notes=notes,
                created_by=created_by,
            )
            tx.execute(
                "UPDATE cf_pipelines SET draft_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, db.now_iso(), pipeline_id),
            )
    return get_version(version_id)  # type: ignore[return-value]


def publish_version(version_id: str) -> dict[str, Any]:
    """Freezes a draft and makes it the pipeline's active version."""
    version = get_version(version_id)
    if not version:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such version: {version_id}", retryable=False)
    now = db.now_iso()
    with db.transaction() as tx:
        if not version.get("published_at"):
            tx.execute(
                "UPDATE cf_pipeline_versions SET published_at = ?, updated_at = ? WHERE id = ?",
                (now, now, version_id),
            )
        tx.execute(
            "UPDATE cf_pipelines SET active_version_id = ?, updated_at = ? WHERE id = ?",
            (version_id, now, version["pipeline_id"]),
        )
    return get_version(version_id)  # type: ignore[return-value]


def get_version(version_id: str) -> Optional[dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM cf_pipeline_versions WHERE id = ?", (version_id,))


def list_versions(pipeline_id: str) -> list[dict[str, Any]]:
    ensure_schema()
    return db.query(
        "SELECT * FROM cf_pipeline_versions WHERE pipeline_id = ? ORDER BY version DESC",
        (pipeline_id,),
    )


def runnable_version(pipeline_id: str, *, allow_draft: bool = False) -> dict[str, Any]:
    """The version a run should use: the active one, or the draft for a test
    run (§107) — never a draft for a scheduled or triggered run."""
    pipeline = require_pipeline(pipeline_id)
    version_id = pipeline.get("active_version_id")
    if not version_id and allow_draft:
        version_id = pipeline.get("draft_version_id")
    if not version_id:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "Pipeline has no active version — activate it first",
            retryable=False,
        )
    version = get_version(version_id)
    if not version:
        raise ContentFactoryError(ErrorCode.INTERNAL, "Pipeline version is missing", retryable=False)
    return version


# ── Validation (§96) ─────────────────────────────────────────────────────────

def validate_version(version_id: str) -> dict[str, Any]:
    """Returns `{"errors": [...], "warnings": [...]}`.

    Errors block activation; warnings do not. The split matters: a node with no
    fallback provider is worth flagging but is a legitimate choice, whereas a
    cycle or a publish node with no channel will fail at 09:00 unattended.
    """
    version = get_version(version_id)
    if not version:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such version: {version_id}", retryable=False)

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    try:
        graph = PipelineGraph.from_version(version)
    except ValueError as exc:
        return {"errors": [{"code": "GRAPH_INVALID", "message": str(exc)}], "warnings": []}

    from backend.content_factory.nodes import get_node_handler, is_known_type

    if not graph.nodes:
        errors.append({"code": "EMPTY_PIPELINE", "message": "Pipeline has no nodes"})

    # Unknown node types
    for node in graph.nodes.values():
        if not is_known_type(node.type):
            errors.append({"code": "UNKNOWN_NODE_TYPE", "message": f"{node.id}: unknown node type '{node.type}'"})

    # Dangling edges
    for edge in graph.edges:
        if edge.source not in graph.nodes:
            errors.append({"code": "DANGLING_EDGE", "message": f"Edge from unknown node '{edge.source}'"})
        if edge.target not in graph.nodes:
            errors.append({"code": "DANGLING_EDGE", "message": f"Edge to unknown node '{edge.target}'"})

    # A trigger must exist, and must be a root
    triggers = [node for node in graph.nodes.values() if node.type.endswith("trigger")]
    if not triggers:
        errors.append({"code": "NO_TRIGGER", "message": "Pipeline has no trigger node"})

    # Cycles
    cycle = _find_cycle(graph)
    if cycle:
        errors.append({"code": "CYCLE", "message": "Circular dependency: " + " → ".join(cycle)})

    # Unreachable nodes
    if graph.nodes and not cycle:
        reachable = _reachable_from_roots(graph)
        for node_id in graph.nodes:
            if node_id not in reachable:
                warnings.append({"code": "UNREACHABLE", "message": f"{node_id} is never reached"})

    # Per-node configuration checks, delegated to the node implementations so a
    # new node type ships its own validation instead of editing this function.
    for node in graph.nodes.values():
        if not is_known_type(node.type):
            continue
        handler = get_node_handler(node.type)
        for problem in handler.validate(node):
            (errors if problem.get("severity", "error") == "error" else warnings).append(
                {"code": problem.get("code", "NODE_CONFIG"), "message": f"{node.id}: {problem['message']}"}
            )

    return {"errors": errors, "warnings": warnings, "versionId": version_id}


def _find_cycle(graph: PipelineGraph) -> list[str]:
    """Depth-first search returning one cycle's node ids, or []."""
    WHITE, GREY, BLACK = 0, 1, 2
    colors = {node_id: WHITE for node_id in graph.nodes}
    stack: list[str] = []

    def visit(node_id: str) -> list[str]:
        colors[node_id] = GREY
        stack.append(node_id)
        for child in graph.children(node_id):
            if child not in colors:
                continue
            if colors[child] == GREY:
                return stack[stack.index(child):] + [child]
            if colors[child] == WHITE:
                found = visit(child)
                if found:
                    return found
        colors[node_id] = BLACK
        stack.pop()
        return []

    for node_id in graph.nodes:
        if colors[node_id] == WHITE:
            found = visit(node_id)
            if found:
                return found
    return []


def _reachable_from_roots(graph: PipelineGraph) -> set[str]:
    reachable: set[str] = set()
    frontier = list(graph.roots())
    while frontier:
        node_id = frontier.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        frontier.extend(graph.children(node_id))
    return reachable
