"""Per-agent Element/Matrix bot runtime.

Mirrors backend/agent_bot.py's AgentBotManager exactly, one nio.AsyncClient per
active binding in agent_messenger_bindings (platform='matrix'). Messages sent to
that Matrix account go straight to that one agent via
backend.agent.agent_instance._respond_as_subagent — same call every other channel
manager uses.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from nio import AsyncClient, MatrixRoom, RoomMessageText

logger = logging.getLogger("hermes.agent_matrix_bot")

MATRIX_TEXT_LIMIT = 8000


def _split_text(text: str, limit: int = MATRIX_TEXT_LIMIT) -> List[str]:
    """Same paragraph/line-boundary splitting as backend/agent_bot.py's _split_text."""
    remaining = (text or "").strip()
    if not remaining:
        return ["Ответ модели пуст."]
    chunks = []
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split_at < limit // 3:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


class AgentMatrixBotManager:
    def __init__(self) -> None:
        self._clients: Dict[str, AsyncClient] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._allowed_room_ids: Dict[str, List[str]] = {}

    def _make_callback(self, binding_id: str, subagent_id: str):
        async def callback(room: MatrixRoom, event: RoomMessageText) -> None:
            client = self._clients.get(binding_id)
            if not client or event.sender == client.user_id:
                return
            allowed = self._allowed_room_ids.get(binding_id) or []
            if allowed and room.room_id not in allowed:
                logger.warning(
                    "Agent matrix bot %s: ignoring message from unauthorized room %s", binding_id, room.room_id
                )
                return

            from backend.database import get_subagent

            subagent = get_subagent(subagent_id)
            if not subagent or not subagent.get("is_enabled"):
                await client.room_send(
                    room.room_id,
                    message_type="m.room.message",
                    content={"msgtype": "m.text", "body": "Этот агент сейчас отключён, Альберт."},
                )
                return

            from backend.agent import agent_instance

            session_id = f"matrixbot:{binding_id}:{room.room_id}"
            try:
                response_text = await agent_instance._respond_as_subagent(
                    event.body, subagent, chat_id=session_id
                )
            except Exception:
                logger.exception("Agent matrix bot %s: error handling message", binding_id)
                response_text = "Произошла ошибка при обработке запроса."
            for chunk in _split_text(response_text):
                await client.room_send(
                    room.room_id, message_type="m.room.message", content={"msgtype": "m.text", "body": chunk}
                )

        return callback

    async def start(
        self, binding_id: str, subagent_id: str, credentials: dict, allowed_room_ids: Optional[List[str]] = None
    ) -> None:
        if binding_id in self._clients:
            return
        client = AsyncClient(credentials["homeserver_url"], credentials["user_id"])
        client.access_token = credentials["access_token"]
        client.user_id = credentials["user_id"]
        client.add_event_callback(self._make_callback(binding_id, subagent_id), RoomMessageText)

        # Establish a sync token before subscribing so history predating this binding
        # never gets replayed through the agent.
        await client.sync(timeout=30000, full_state=True)

        self._clients[binding_id] = client
        self._allowed_room_ids[binding_id] = list(allowed_room_ids or [])
        self._tasks[binding_id] = asyncio.create_task(client.sync_forever(timeout=30000))
        logger.info("Agent matrix bot started: binding=%s subagent=%s", binding_id, subagent_id)

    async def stop(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        client = self._clients.pop(binding_id, None)
        self._allowed_room_ids.pop(binding_id, None)
        if task:
            task.cancel()
        if client:
            try:
                await client.close()
            except Exception:
                logger.exception("Agent matrix bot %s: error during shutdown", binding_id)
        logger.info("Agent matrix bot stopped: binding=%s", binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_matrix_bindings, resolve_matrix_binding_credentials

        for binding in list_matrix_bindings():
            if binding.get("status") != "active":
                continue
            credentials = resolve_matrix_binding_credentials(binding["id"])
            if not credentials:
                logger.warning(
                    "Agent matrix bot %s: active binding has no resolvable credentials, skipping", binding["id"]
                )
                continue
            try:
                await self.start(binding["id"], binding["subagent_id"], credentials, binding.get("allowed_chat_ids"))
            except Exception:
                logger.exception("Failed to start agent matrix bot %s at startup", binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._clients.keys()):
            await self.stop(binding_id)


manager = AgentMatrixBotManager()
