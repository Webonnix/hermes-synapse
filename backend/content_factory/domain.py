"""Domain vocabulary for Content Factory — statuses, the node contract, and the
structured shapes agents must produce.

Nothing here touches the database, the network or any provider SDK. It is the
layer both the execution engine and the API agree on, and the reason the
pipeline can stay provider-independent (§1, §145): a node declares what it
needs and what it returns, and only an adapter knows whose API answered.

Statuses are plain string constants rather than `enum.Enum` because they cross
JSON, SQLite TEXT columns and the WebSocket wire on every hop; an enum would be
converted back to its value at each of those boundaries anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Statuses ─────────────────────────────────────────────────────────────────

class RunStatus:
    """§16."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    TERMINAL = frozenset({COMPLETED, PARTIALLY_COMPLETED, FAILED, CANCELLED})
    ACTIVE = frozenset({QUEUED, RUNNING, WAITING, WAITING_APPROVAL})


class NodeStatus:
    """§15."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"

    TERMINAL = frozenset({COMPLETED, FAILED, SKIPPED, CANCELLED})
    # Statuses that mean "this node still owes the run something", so the run
    # is not finished even though nothing is executing this instant.
    PENDING_WORK = frozenset({PENDING, QUEUED, RUNNING, WAITING_EXTERNAL, WAITING_APPROVAL, RETRYING})


class ContentStatus:
    """§8."""

    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    RESEARCHING = "RESEARCHING"
    SCRIPTING = "SCRIPTING"
    GENERATING = "GENERATING"
    RENDERING = "RENDERING"
    QA = "QA"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublicationStatus:
    """§54."""

    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublishingMode:
    """§36."""

    MANUAL = "MANUAL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FULL_AUTO = "FULL_AUTO"

    ALL = frozenset({MANUAL, REVIEW_REQUIRED, FULL_AUTO})


class ErrorStrategy:
    """§95."""

    FAIL_FAST = "FAIL_FAST"
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    FALLBACK = "FALLBACK"
    WAIT_FOR_USER = "WAIT_FOR_USER"

    ALL = frozenset({FAIL_FAST, CONTINUE, RETRY, FALLBACK, WAIT_FOR_USER})


class AssetType:
    """§43."""

    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    VOICE = "VOICE"
    MUSIC = "MUSIC"
    SUBTITLE = "SUBTITLE"
    THUMBNAIL = "THUMBNAIL"
    DOCUMENT = "DOCUMENT"
    RENDER = "RENDER"

    ALL = frozenset({IMAGE, VIDEO, AUDIO, VOICE, MUSIC, SUBTITLE, THUMBNAIL, DOCUMENT, RENDER})


class TriggerType:
    MANUAL = "MANUAL"
    SCHEDULE = "SCHEDULE"
    WEBHOOK = "WEBHOOK"
    EVENT = "EVENT"


# ── Error codes (§76) ────────────────────────────────────────────────────────
# The frontend switches on these; it must never parse a human-readable message.

class ErrorCode:
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    BUDGET_LIMIT_REACHED = "BUDGET_LIMIT_REACHED"
    QA_FAILED = "QA_FAILED"
    RENDER_FAILED = "RENDER_FAILED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    CANCELLED = "CANCELLED"
    NODE_TIMEOUT = "NODE_TIMEOUT"
    INTERNAL = "INTERNAL"

    # Errors worth another attempt (§20). Everything else fails the node
    # immediately rather than burning budget on a request that cannot succeed.
    RETRYABLE = frozenset({
        PROVIDER_RATE_LIMIT,
        PROVIDER_TIMEOUT,
        PROVIDER_ERROR,
        PROVIDER_UNAVAILABLE,
        INVALID_OUTPUT,
        NODE_TIMEOUT,
    })


class ContentFactoryError(Exception):
    """Base error carrying a machine-readable code (§76). Anything raised inside
    a node is normalized to one of these before it reaches the engine."""

    def __init__(self, code: str, message: str, *, retryable: Optional[bool] = None,
                 details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = (code in ErrorCode.RETRYABLE) if retryable is None else retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


# ── Node contract (§14, §20, §21) ────────────────────────────────────────────

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 5.0
    max_delay: float = 60.0
    backoff_multiplier: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Delay before `attempt` (1-based: attempt 2 is the first retry)."""
        if attempt <= 1:
            return 0.0
        delay = self.initial_delay * (self.backoff_multiplier ** (attempt - 2))
        return min(delay, self.max_delay)

    @classmethod
    def from_config(cls, raw: Any) -> "RetryPolicy":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            max_attempts=max(1, int(raw.get("maxAttempts", raw.get("max_attempts", 3)))),
            initial_delay=float(raw.get("initialDelay", raw.get("initial_delay", 5.0))),
            max_delay=float(raw.get("maxDelay", raw.get("max_delay", 60.0))),
            backoff_multiplier=float(raw.get("backoffMultiplier", raw.get("backoff_multiplier", 2.0))),
        )


@dataclass(frozen=True)
class PipelineNode:
    """§14. `config` stays an opaque dict: the engine routes by `type` and only
    the node implementation knows what its own config means."""

    id: str
    type: str
    name: str = ""
    position: dict[str, float] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    timeout: int = 300
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    # Ordered provider ids to try when the primary fails (§21).
    fallback_policy: list[str] = field(default_factory=list)
    continue_on_error: bool = False
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PipelineNode":
        node_id = str(raw.get("id") or "").strip()
        node_type = str(raw.get("type") or "").strip()
        if not node_id:
            raise ValueError("node.id is required")
        if not node_type:
            raise ValueError(f"node {node_id}: type is required")
        return cls(
            id=node_id,
            type=node_type,
            name=str(raw.get("name") or node_id),
            position=dict(raw.get("position") or {}),
            config=dict(raw.get("config") or {}),
            input_mapping=dict(raw.get("inputMapping") or raw.get("input_mapping") or {}),
            output_schema=dict(raw.get("outputSchema") or raw.get("output_schema") or {}),
            timeout=int(raw.get("timeout") or 300),
            retry_policy=RetryPolicy.from_config(raw.get("retryPolicy") or raw.get("retry_policy")),
            fallback_policy=[str(item) for item in (raw.get("fallbackPolicy") or raw.get("fallback_policy") or [])],
            continue_on_error=bool(raw.get("continueOnError") or raw.get("continue_on_error") or False),
            enabled=bool(raw.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "position": self.position,
            "config": self.config,
            "inputMapping": self.input_mapping,
            "outputSchema": self.output_schema,
            "timeout": self.timeout,
            "retryPolicy": {
                "maxAttempts": self.retry_policy.max_attempts,
                "initialDelay": self.retry_policy.initial_delay,
                "maxDelay": self.retry_policy.max_delay,
                "backoffMultiplier": self.retry_policy.backoff_multiplier,
            },
            "fallbackPolicy": self.fallback_policy,
            "continueOnError": self.continue_on_error,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class PipelineEdge:
    source: str
    target: str
    # Set on edges leaving a condition/router node: the branch label this edge
    # carries (e.g. "PASS" / "FAIL"). Empty means unconditional.
    branch: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PipelineEdge":
        source = str(raw.get("source") or raw.get("from") or "").strip()
        target = str(raw.get("target") or raw.get("to") or "").strip()
        if not source or not target:
            raise ValueError("edge requires source and target")
        return cls(source=source, target=target, branch=str(raw.get("branch") or ""))

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "branch": self.branch}


@dataclass
class PipelineGraph:
    """A parsed pipeline version. Built once per run and never mutated."""

    nodes: dict[str, PipelineNode]
    edges: list[PipelineEdge]
    variables: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_version(cls, version_row: dict[str, Any]) -> "PipelineGraph":
        from backend.content_factory import db

        raw_nodes = db.loads(version_row.get("nodes"), []) or []
        raw_edges = db.loads(version_row.get("edges"), []) or []
        nodes = {}
        for raw in raw_nodes:
            node = PipelineNode.from_dict(raw)
            if node.id in nodes:
                raise ValueError(f"duplicate node id: {node.id}")
            nodes[node.id] = node
        edges = [PipelineEdge.from_dict(raw) for raw in raw_edges]
        return cls(nodes=nodes, edges=edges, variables=db.loads(version_row.get("variables"), {}) or {})

    def parents(self, node_id: str) -> list[str]:
        return [edge.source for edge in self.edges if edge.target == node_id]

    def children(self, node_id: str) -> list[str]:
        return [edge.target for edge in self.edges if edge.source == node_id]

    def roots(self) -> list[str]:
        targets = {edge.target for edge in self.edges}
        return [node_id for node_id in self.nodes if node_id not in targets]


# ── Node execution result ────────────────────────────────────────────────────

@dataclass
class NodeResult:
    """What a node implementation hands back to the engine.

    `external_job_id` + `poll_after` are how a node parks a paid, long-running
    provider job (video generation, a render) without holding a worker: the
    engine writes the id, sets WAITING_EXTERNAL and comes back later. Because
    the id is durable, a backend restart resumes the same job instead of
    submitting — and paying for — a second one.
    """

    output: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    provider: str = ""
    model: str = ""
    logs: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    assets: list[str] = field(default_factory=list)
    external_job_id: str = ""
    poll_after: float = 0.0
    # Set by control nodes that suspend the run rather than finish (§36).
    waiting_approval: bool = False
    # Branch label chosen by a condition/router node; edges with a different
    # branch label are skipped (§18).
    branch: str = ""


# ── Structured agent outputs (§29, §30, §113) ────────────────────────────────
# Declared as JSON Schema so the same definition can drive the provider's
# structured-output mode and post-hoc validation. Regex-scraping an LLM's
# markdown is explicitly out (§113).

RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["topic", "summary", "facts", "sources"],
    "properties": {
        "topic": {"type": "string"},
        "summary": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "sourceId"],
                "properties": {"text": {"type": "string"}, "sourceId": {"type": "string"}},
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "url", "title"],
                "properties": {
                    "id": {"type": "string"},
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "publisher": {"type": "string"},
                    "publishedAt": {"type": "string"},
                },
            },
        },
        "riskFlags": {"type": "array", "items": {"type": "string"}},
    },
}

SCRIPT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "hook", "script", "scenes"],
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "script": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["narration", "duration"],
                "properties": {
                    "narration": {"type": "string"},
                    "duration": {"type": "number"},
                    "overlayText": {"type": "string"},
                    "visualPrompt": {"type": "string"},
                },
            },
        },
        "cta": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
}

STORYBOARD_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "duration", "scenes"],
    "properties": {
        "version": {"type": "integer"},
        "title": {"type": "string"},
        "duration": {"type": "number"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "start", "duration", "narration", "visualPrompt"],
                "properties": {
                    "id": {"type": "string"},
                    "start": {"type": "number"},
                    "duration": {"type": "number"},
                    "narration": {"type": "string"},
                    "overlayText": {"type": "string"},
                    "visualPrompt": {"type": "string"},
                    "visualType": {"type": "string", "enum": ["image", "video"]},
                    "transition": {"type": "string"},
                    "camera": {"type": "string"},
                },
            },
        },
    },
}

QA_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "score"],
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "FAIL"]},
        "score": {"type": "number"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": {"type": "string"}},
    },
}

METADATA_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "description"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
}


# ── Render presets (§33) ─────────────────────────────────────────────────────
# The renderer never hardcodes a format; it is handed one of these.

@dataclass(frozen=True)
class RenderPreset:
    id: str
    width: int
    height: int
    fps: int = 30

    @property
    def aspect_ratio(self) -> str:
        from math import gcd

        divisor = gcd(self.width, self.height) or 1
        return f"{self.width // divisor}:{self.height // divisor}"


RENDER_PRESETS: dict[str, RenderPreset] = {
    "YOUTUBE_SHORT": RenderPreset("YOUTUBE_SHORT", 1080, 1920, 30),
    "TIKTOK": RenderPreset("TIKTOK", 1080, 1920, 30),
    "YOUTUBE_STANDARD": RenderPreset("YOUTUBE_STANDARD", 1920, 1080, 30),
}


def resolve_preset(preset_id: str, custom: Optional[dict[str, Any]] = None) -> RenderPreset:
    """Named preset, or a CUSTOM one built from explicit dimensions."""
    if preset_id in RENDER_PRESETS:
        return RENDER_PRESETS[preset_id]
    if preset_id == "CUSTOM" and custom:
        return RenderPreset(
            "CUSTOM",
            int(custom.get("width", 1080)),
            int(custom.get("height", 1920)),
            int(custom.get("fps", 30)),
        )
    raise ContentFactoryError(
        ErrorCode.VALIDATION_FAILED, f"Unknown render preset: {preset_id}", retryable=False
    )


# ── Input mapping (§112) ─────────────────────────────────────────────────────

_TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]]+)\s*\}\}")


def resolve_path(context: dict[str, Any], path: str) -> Any:
    """Walks `nodes.script.output.scenes[0].narration` style paths.

    Returns None for anything missing rather than raising: a node that needs a
    value validates it itself, with its own error code, instead of every
    mapping failure surfacing as an opaque KeyError.
    """
    current: Any = context
    for part in path.replace("]", "").replace("[", ".").split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def render_template(value: Any, context: dict[str, Any]) -> Any:
    """Substitutes `{{ nodes.x.output }}` references inside strings, lists and
    dicts. A string that is *exactly* one reference keeps the referenced value's
    type (so a scene list stays a list instead of becoming its repr)."""
    if isinstance(value, str):
        whole = _TEMPLATE.fullmatch(value.strip())
        if whole:
            return resolve_path(context, whole.group(1))
        return _TEMPLATE.sub(
            lambda match: _stringify(resolve_path(context, match.group(1))), value
        )
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render_template(item, context) for key, item in value.items()}
    return value


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, ensure_ascii=False)
