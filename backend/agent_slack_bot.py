"""Per-agent Slack bot runtime, using Socket Mode (no public webhook needed —
consistent with every other channel here being an outbound connection from
this server, same as Telegram polling / Matrix sync / Discord's gateway).

Mirrors backend/agent_bot.py's AgentBotManager shape, one slack_bolt AsyncApp
+ Socket Mode handler per active binding in agent_messenger_bindings
(platform='slack'). Messages go straight to that one agent via
backend.agent.agent_instance._respond_as_subagent. response_mode governs
whether replies go out automatically (with a disclosure) or get queued as a
draft for the owner to send by hand — see backend/agent_messenger_governance.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

logger = logging.getLogger("hermes.agent_slack_bot")

SLACK_TEXT_LIMIT = 3000


def _split_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> List[str]:
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


class AgentSlackBotManager:
    def __init__(self) -> None:
        self._handlers: Dict[str, AsyncSocketModeHandler] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._allowed_channel_ids: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}

    def _make_app(self, binding_id: str, subagent_id: str, bot_token: str) -> AsyncApp:
        app = AsyncApp(token=bot_token)

        @app.event("message")
        async def handle_message(event, say):  # noqa: ANN001
            if event.get("bot_id") or event.get("subtype") == "bot_message":
                return
            text = (event.get("text") or "").strip()
            if not text:
                return
            channel_id = str(event.get("channel", ""))
            slack_user = str(event.get("user", ""))

            from backend import bot_access_gate

            # The allow-list may name the channel or the user; ask about both,
            # same as the pre-gate behaviour.
            decision = await asyncio.to_thread(
                bot_access_gate.authorize, binding_id, "slack", channel_id, text, slack_user, slack_user,
            )
            if decision.action == bot_access_gate.IGNORE and slack_user:
                decision = await asyncio.to_thread(
                    bot_access_gate.authorize, binding_id, "slack", slack_user, text, slack_user, slack_user,
                )
            if decision.action == bot_access_gate.IGNORE:
                return
            if decision.action == bot_access_gate.REPLY:
                for chunk in _split_text(decision.reply_text):
                    await say(text=chunk)
                return

            from backend.database import get_subagent

            subagent = get_subagent(subagent_id)
            if not subagent or not subagent.get("is_enabled"):
                await say(
                    text=bot_access_gate.AGENT_OFFLINE_MESSAGE
                    if decision.scope == "public"
                    else "Этот агент сейчас отключён, Альберт."
                )
                return

            from backend.agent_messenger_governance import apply_binding_overrides
            subagent = apply_binding_overrides(subagent, self._overrides.get(binding_id))
            subagent = bot_access_gate.apply_subscriber_context(subagent, decision)

            from backend.agent import agent_instance

            session_id = decision.session_id or f"slackbot:{binding_id}:{channel_id}"
            try:
                response_text = await agent_instance._respond_as_subagent(
                    text, subagent, chat_id=session_id
                )
            except Exception:
                logger.exception("Agent Slack bot %s: error handling message", binding_id)
                bot_access_gate.record_turn(decision, agent_instance.last_run_metadata.get(session_id), "")
                await say(text="Произошла ошибка при обработке запроса.")
                return
            bot_access_gate.record_turn(
                decision, agent_instance.last_run_metadata.get(session_id), response_text
            )

            mode = bot_access_gate.effective_response_mode(
                decision, self._response_modes.get(binding_id, "draft")
            )
            if mode == "auto_labeled":
                from backend.agent_messenger_governance import auto_reply_disclosure
                for chunk in _split_text(response_text + auto_reply_disclosure(binding_id)):
                    await say(text=chunk)
                return

            from backend.channel_replies import create_pending_reply
            create_pending_reply(
                binding_id=binding_id,
                platform="slack",
                subagent_id=subagent_id,
                chat_id=channel_id,
                incoming_text=text,
                drafted_reply=response_text,
                incoming_from=str(event.get("user", "")),
            )

        return app

    async def start(
        self, binding_id: str, subagent_id: str, credentials: dict,
        allowed_channel_ids: Optional[List[str]] = None, overrides: Optional[dict] = None,
        response_mode: str = "draft",
    ) -> None:
        if binding_id in self._handlers:
            return
        app = self._make_app(binding_id, subagent_id, credentials["bot_token"])
        handler = AsyncSocketModeHandler(app, credentials["app_token"])
        self._handlers[binding_id] = handler
        self._allowed_channel_ids[binding_id] = list(allowed_channel_ids or [])
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode
        self._tasks[binding_id] = asyncio.create_task(handler.start_async())
        logger.info("Agent Slack bot started: binding=%s subagent=%s mode=%s", binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        if binding_id not in self._handlers:
            return
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode

    async def stop(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        handler = self._handlers.pop(binding_id, None)
        self._allowed_channel_ids.pop(binding_id, None)
        self._overrides.pop(binding_id, None)
        self._response_modes.pop(binding_id, None)
        if handler:
            try:
                await handler.close_async()
            except Exception:
                logger.exception("Agent Slack bot %s: error during shutdown", binding_id)
        if task:
            task.cancel()
        logger.info("Agent Slack bot stopped: binding=%s", binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_slack_bindings, resolve_slack_binding_credentials

        for binding in list_slack_bindings():
            if binding.get("status") != "active":
                continue
            credentials = resolve_slack_binding_credentials(binding["id"])
            if not credentials:
                logger.warning("Agent Slack bot %s: active binding has no resolvable credentials, skipping", binding["id"])
                continue
            overrides = {
                "system_prompt": binding.get("system_prompt_override"),
                "model": binding.get("model_override"),
                "model_provider": binding.get("model_provider_override"),
            }
            try:
                await self.start(
                    binding["id"], binding["subagent_id"], credentials, binding.get("allowed_chat_ids"),
                    overrides, binding.get("response_mode") or "draft",
                )
            except Exception:
                logger.exception("Failed to start agent Slack bot %s at startup", binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._handlers.keys()):
            await self.stop(binding_id)


manager = AgentSlackBotManager()
