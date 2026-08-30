"""Realtime events (§72–73).

Content Factory does not open a second realtime channel. It broadcasts on the
dashboard WebSocket the frontend is already connected to, with every event type
prefixed `cf.` so a client can subscribe by prefix and existing handlers are
unaffected.

Two properties matter more than throughput here:

* **Emitting never breaks a run.** A broadcast failure is logged and dropped. A
  pipeline that has already paid a provider must not fail because a browser
  disconnected.
* **Progress does not hit the database.** `node.progress` fires many times per
  node; writing each one would make SQLite's single writer the bottleneck long
  before 100 concurrent runs (see risk 1 in the integration plan). Progress is
  broadcast, and only *state changes* are persisted.

Payloads carry ids, never prompts, provider responses or credentials (§89).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from backend.content_factory.db import now_iso

logger = logging.getLogger("hermes.content_factory.events")

# §72
PIPELINE_STARTED = "cf.pipeline.started"
PIPELINE_COMPLETED = "cf.pipeline.completed"
PIPELINE_FAILED = "cf.pipeline.failed"
PIPELINE_CANCELLED = "cf.pipeline.cancelled"
NODE_STARTED = "cf.node.started"
NODE_PROGRESS = "cf.node.progress"
NODE_COMPLETED = "cf.node.completed"
NODE_FAILED = "cf.node.failed"
NODE_RETRYING = "cf.node.retrying"
NODE_WAITING = "cf.node.waiting"
CONTENT_CREATED = "cf.content.created"
CONTENT_READY_FOR_REVIEW = "cf.content.ready_for_review"
PUBLICATION_STARTED = "cf.publication.started"
PUBLICATION_COMPLETED = "cf.publication.completed"
PUBLICATION_FAILED = "cf.publication.failed"


def build_event(
    event_type: str,
    *,
    pipeline_run_id: str = "",
    node_run_id: str = "",
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """§73 event envelope."""
    return {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "timestamp": now_iso(),
        "pipelineRunId": pipeline_run_id,
        "nodeRunId": node_run_id,
        "data": data or {},
    }


async def emit(
    event_type: str,
    *,
    pipeline_run_id: str = "",
    node_run_id: str = "",
    data: Optional[dict[str, Any]] = None,
) -> None:
    event = build_event(
        event_type, pipeline_run_id=pipeline_run_id, node_run_id=node_run_id, data=data
    )
    try:
        from backend.websocket_manager import manager

        await manager.broadcast(event)
    except Exception:
        logger.debug("Content Factory event %s not delivered", event_type, exc_info=True)


def emit_soon(
    event_type: str,
    *,
    pipeline_run_id: str = "",
    node_run_id: str = "",
    data: Optional[dict[str, Any]] = None,
) -> None:
    """Fire-and-forget from synchronous code. No-ops outside a running loop
    rather than raising, so DB-layer helpers stay callable from tests."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        emit(event_type, pipeline_run_id=pipeline_run_id, node_run_id=node_run_id, data=data)
    )
