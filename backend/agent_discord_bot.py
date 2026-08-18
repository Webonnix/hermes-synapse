"""Per-agent Discord bot runtime.

Mirrors backend/agent_bot.py's AgentBotManager exactly, one discord.Client per
active binding in agent_messenger_bindings (platform='discord'). Messages sent
to that bot (DM or an allowed channel) go straight to that one agent via
backend.agent.agent_instance._respond_as_subagent — same call every other
channel manager uses. response_mode governs whether replies go out
automatically (with a disclosure) or get queued as a draft for the owner to
send by hand — see backend/agent_messenger_governance.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import discord

logger = logging.getLogger("hermes.agent_discord_bot")

DISCORD_TEXT_LIMIT = 1900


def _split_text(text: str, limit: int = DISCORD_TEXT_LIMIT) -> List[str]:
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


class AgentDiscordBotManager:
    def __init__(self) -> None:
        self._clients: Dict[str, discord.Client] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._allowed_channel_ids: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}

    def _make_client(self, binding_id: str, subagent_id: str) -> discord.Client:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_message(message: discord.Message) -> None:  # noqa: ANN001
            if message.author.bot or not message.content:
                return
            channel_id = str(message.channel.id)
            is_dm = isinstance(message.channel, discord.DMChannel)

            from backend import bot_access_gate

            # A DM allow-list may name either the DM channel id or the user id, so
            # the gate is asked about the channel first and the author second —
            # whichever the owner whitelisted wins.
            decision = await asyncio.to_thread(
                bot_access_gate.authorize,
                binding_id, "discord", channel_id, message.content,
                str(message.author.id), str(message.author),
            )
            if is_dm and decision.action == bot_access_gate.IGNORE:
                decision = await asyncio.to_thread(
                    bot_access_gate.authorize,
                    binding_id, "discord", str(message.author.id), message.content,
                    str(message.author.id), str(message.author),
                )
            if decision.action == bot_access_gate.IGNORE:
                return
            if decision.action == bot_access_gate.REPLY:
                for chunk in _split_text(decision.reply_text):
                    await message.channel.send(chunk)
                return

            from backend.database import get_subagent

            subagent = get_subagent(subagent_id)
            if not subagent or not subagent.get("is_enabled"):
                await message.channel.send(
                    bot_access_gate.AGENT_OFFLINE_MESSAGE
                    if decision.scope == "public"
                    else "Этот агент сейчас отключён, Альберт."
                )
                return

            from backend.agent_messenger_governance import apply_binding_overrides
            subagent = apply_binding_overrides(subagent, self._overrides.get(binding_id))
            subagent = bot_access_gate.apply_subscriber_context(subagent, decision)

            async with message.channel.typing():
                from backend.agent import agent_instance

                session_id = decision.session_id or f"discordbot:{binding_id}:{channel_id}"
                try:
                    response_text = await agent_instance._respond_as_subagent(
                        message.content, subagent, chat_id=session_id
                    )
                except Exception:
                    logger.exception("Agent Discord bot %s: error handling message", binding_id)
                    bot_access_gate.record_turn(decision, agent_instance.last_run_metadata.get(session_id), "")
                    await message.channel.send("Произошла ошибка при обработке запроса.")
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
                    await message.channel.send(chunk)
                return

            from backend.channel_replies import create_pending_reply
            create_pending_reply(
                binding_id=binding_id,
                platform="discord",
                subagent_id=subagent_id,
                chat_id=channel_id,
                incoming_text=message.content,
                drafted_reply=response_text,
                incoming_from=str(message.author),
            )

        return client

    async def start(
        self, binding_id: str, subagent_id: str, bot_token: str,
        allowed_channel_ids: Optional[List[str]] = None, overrides: Optional[dict] = None,
        response_mode: str = "draft",
    ) -> None:
        if binding_id in self._clients:
            return
        client = self._make_client(binding_id, subagent_id)
        self._clients[binding_id] = client
        self._allowed_channel_ids[binding_id] = list(allowed_channel_ids or [])
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode
        self._tasks[binding_id] = asyncio.create_task(client.start(bot_token))
        logger.info("Agent Discord bot started: binding=%s subagent=%s mode=%s", binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        if binding_id not in self._clients:
            return
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode

    async def stop(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        client = self._clients.pop(binding_id, None)
        self._allowed_channel_ids.pop(binding_id, None)
        self._overrides.pop(binding_id, None)
        self._response_modes.pop(binding_id, None)
        if client:
            try:
                await client.close()
            except Exception:
                logger.exception("Agent Discord bot %s: error during shutdown", binding_id)
        if task:
            task.cancel()
        logger.info("Agent Discord bot stopped: binding=%s", binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_discord_bindings, resolve_discord_binding_token

        for binding in list_discord_bindings():
            if binding.get("status") != "active":
                continue
            token = resolve_discord_binding_token(binding["id"])
            if not token:
                logger.warning("Agent Discord bot %s: active binding has no resolvable token, skipping", binding["id"])
                continue
            overrides = {
                "system_prompt": binding.get("system_prompt_override"),
                "model": binding.get("model_override"),
                "model_provider": binding.get("model_provider_override"),
            }
            try:
                await self.start(
                    binding["id"], binding["subagent_id"], token, binding.get("allowed_chat_ids"),
                    overrides, binding.get("response_mode") or "draft",
                )
            except Exception:
                logger.exception("Failed to start agent Discord bot %s at startup", binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._clients.keys()):
            await self.stop(binding_id)


manager = AgentDiscordBotManager()
