"""Shared "what to actually do once a Control Plane task is approved" dispatch.

Used by both the web dashboard's approve endpoint and the Telegram /approve command
so proposal-style tasks (MCP connections, provider bindings, capability installs)
execute regardless of which surface approved them. Before this existed, only the
web endpoint had the full dispatch chain — approving an MCP connection or provider
binding via Telegram would mark it approved in the DB but never actually activate
it, silently leaving the owner waiting on nothing.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.approval_dispatch")


async def execute_if_ready(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if task.get("status") != "approved":
        return None
    task_id = task["id"]

    from backend.autonomy import get_capability_proposal, execute_approved_capability
    from backend.mcp_governance import get_connection_proposal, execute_approved_connection
    from backend.provider_governance import get_provider_proposal, execute_approved_provider
    from backend.agent_messenger_governance import (
        get_telegram_binding_proposal, execute_approved_telegram_binding,
        get_matrix_binding_proposal, execute_approved_matrix_binding,
    )

    try:
        if get_capability_proposal(control_task_id=task_id):
            return await asyncio.to_thread(execute_approved_capability, task_id)
        if get_connection_proposal(task_id):
            return await execute_approved_connection(task_id)
        if get_provider_proposal(task_id):
            return await execute_approved_provider(task_id)
        if get_telegram_binding_proposal(task_id):
            return await execute_approved_telegram_binding(task_id)
        if get_matrix_binding_proposal(task_id):
            return await execute_approved_matrix_binding(task_id)
        if task.get("tool_name"):
            from backend.control_plane import execute_governed_tool

            return await asyncio.to_thread(
                execute_governed_tool,
                task["tool_name"],
                task.get("tool_arguments") or {},
                task.get("requester") or "control-plane",
                approved_task_id=task_id,
            )
    except Exception as exc:
        logger.error("Approved Control Plane task %s failed: %s", task_id, exc)
        return {"status": "failed", "error": str(exc)}
    return None
