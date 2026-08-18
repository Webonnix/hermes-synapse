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
    try:
        result = await _dispatch(task, task_id)
    except Exception as exc:
        logger.error("Approved Control Plane task %s failed: %s", task_id, exc)
        result = {"status": "failed", "error": str(exc)}

    # A dev-run blocked on this approval already has the human decision it was
    # waiting for — hand it back to the worker instead of also requiring a
    # manual Resume. Done even when execution failed: observing the failure and
    # reacting beats hanging in awaiting_approval forever.
    if str(task.get("requester") or "").startswith("dev-run:"):
        try:
            from backend import dev_runs

            await asyncio.to_thread(dev_runs.on_control_task_approved, task, result)
        except Exception as exc:
            logger.error("Could not resume the dev-run waiting on task %s: %s", task_id, exc)
    return result


async def _dispatch(task: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
    from backend.autonomy import get_capability_proposal, execute_approved_capability
    from backend.mcp_governance import get_connection_proposal, execute_approved_connection
    from backend.provider_governance import get_provider_proposal, execute_approved_provider
    from backend.router_session import get_router_password_proposal, execute_approved_router_password
    from backend.agent_messenger_governance import (
        get_telegram_binding_proposal, execute_approved_telegram_binding,
        get_matrix_binding_proposal, execute_approved_matrix_binding,
        get_discord_binding_proposal, execute_approved_discord_binding,
        get_slack_binding_proposal, execute_approved_slack_binding,
        get_email_binding_proposal, execute_approved_email_binding,
    )

    if get_capability_proposal(control_task_id=task_id):
        return await asyncio.to_thread(execute_approved_capability, task_id)
    if get_connection_proposal(task_id):
        return await execute_approved_connection(task_id)
    if get_provider_proposal(task_id):
        return await execute_approved_provider(task_id)
    if get_router_password_proposal(task_id):
        return await execute_approved_router_password(task_id)
    if get_telegram_binding_proposal(task_id):
        return await execute_approved_telegram_binding(task_id)
    if get_matrix_binding_proposal(task_id):
        return await execute_approved_matrix_binding(task_id)
    if get_discord_binding_proposal(task_id):
        return await execute_approved_discord_binding(task_id)
    if get_slack_binding_proposal(task_id):
        return await execute_approved_slack_binding(task_id)
    if get_email_binding_proposal(task_id):
        return await execute_approved_email_binding(task_id)
    if task.get("tool_name"):
        from backend.control_plane import execute_governed_tool
        from backend.tool_permissions import OWNER

        # Reaching here means the owner personally approved this task, in the
        # dashboard or on the admin-gated Telegram bot — the owner principal,
        # not whichever agent originally proposed it.
        return await asyncio.to_thread(
            execute_governed_tool,
            task["tool_name"],
            task.get("tool_arguments") or {},
            task.get("requester") or "control-plane",
            approved_task_id=task_id,
            principal=OWNER,
        )
    return None
