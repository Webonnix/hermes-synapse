"""Node handler contract.

A handler is a stateless function object: given a `NodeContext` it returns a
`NodeResult`, and it may be called again after a crash with the same inputs.
Anything a handler needs from the outside world — providers, storage, the run
row — arrives through the context rather than being reached for directly, which
is what keeps handlers unit-testable against mock providers (§128) and what
would let the whole set run as Temporal activities later without rewriting them
(see the integration plan, §1).

Handlers must not:

* swallow errors — raise `ContentFactoryError` with a code and let the engine
  decide about retry/fallback (§20, §21);
* write to `cf_node_runs` — the engine owns run state;
* perform a side effect twice for one call — the engine may retry them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from backend.content_factory.domain import (
    ContentFactoryError,
    ErrorCode,
    NodeResult,
    PipelineNode,
)

logger = logging.getLogger("hermes.content_factory.nodes")


@dataclass
class NodeContext:
    """Everything a node is allowed to see."""

    node: PipelineNode
    run: dict[str, Any]
    # Resolved node inputs: `input_mapping` rendered against the run context
    # (§112), merged over the node's own config.
    inputs: dict[str, Any]
    # Outputs of every completed node so far, keyed by node id — the source of
    # `{{ nodes.script.output }}` references.
    node_outputs: dict[str, Any] = field(default_factory=dict)
    # The cf_node_runs row this execution belongs to — assets and usage
    # records are attributed to it.
    node_run_id: str = ""
    # Set on a resumed WAITING_EXTERNAL node: the job id already submitted.
    external_job_id: str = ""
    attempt: int = 1
    # Which fallback rung to use for this attempt (§21).
    provider_index: int = 0
    dry_run: bool = False
    progress: Optional[Callable[[int, str], Awaitable[None]]] = None

    def config(self, key: str, default: Any = None) -> Any:
        """Input first, then node config.

        A mapped input wins — but only when it actually resolved. An unresolved
        mapping (`{{ trigger.topic }}` on a run started without one) arrives as
        None, and letting that shadow a configured default turns a missing
        optional value into an immediate hard failure of the whole run.
        """
        value = self.inputs.get(key)
        if value is not None:
            return value
        return self.node.config.get(key, default)

    def require(self, key: str) -> Any:
        value = self.config(key)
        if value in (None, ""):
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                f"{self.node.id}: '{key}' is required",
                retryable=False,
                details={"nodeId": self.node.id, "field": key},
            )
        return value

    async def report(self, percent: int, message: str = "") -> None:
        if self.progress:
            await self.progress(max(0, min(100, int(percent))), message)


class NodeHandler:
    """Base class. `type` is what a pipeline stores; keep it stable."""

    type: str = ""
    # Which provider category this node consumes, if any. The engine uses it to
    # build the fallback chain and to apply concurrency limits.
    provider_category: str = ""

    def validate(self, node: PipelineNode) -> list[dict[str, str]]:
        """Config problems for §96. Each item: {code, message, severity}."""
        return []

    async def execute(self, context: NodeContext) -> NodeResult:
        raise NotImplementedError

    async def poll(self, context: NodeContext) -> Optional[NodeResult]:
        """Called while a node sits in WAITING_EXTERNAL. Returns None while the
        external job is still running, a result when it has finished."""
        raise NotImplementedError(f"{self.type} does not support external jobs")

    async def cancel(self, context: NodeContext) -> None:
        """Best effort (§94)."""
        return None


_HANDLERS: dict[str, NodeHandler] = {}


def register(handler: NodeHandler) -> NodeHandler:
    if not handler.type:
        raise ValueError("Node handler must declare a type")
    _HANDLERS[handler.type] = handler
    return handler


def get_node_handler(node_type: str) -> NodeHandler:
    handler = _HANDLERS.get(node_type)
    if handler is None:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown node type: {node_type}", retryable=False
        )
    return handler


def is_known_type(node_type: str) -> bool:
    return node_type in _HANDLERS


def registered_types() -> list[str]:
    return sorted(_HANDLERS)
