"""Per-agent Telegram bot runtime.

Each active row in agent_messenger_bindings (backend/agent_messenger_governance.py)
gets its own python-telegram-bot Application here, polling with its own bot
token. Messages sent to that bot go straight to that one agent via
backend.agent.agent_instance._respond_as_subagent — bypassing the main router
entirely, same call the dashboard/orchestrator already use for subagent turns.
This is deliberately separate from backend/bot.py (the single global Vexa
bot): that one dispatches across the whole agent network and owns the
Control Plane /approve flow, this one is a dedicated line to a single agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

logger = logging.getLogger("hermes.agent_bot")

TELEGRAM_TEXT_LIMIT = 3900


def _split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> List[str]:
    """Same paragraph/line-boundary splitting as backend/bot.py's _split_telegram_text."""
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


class AgentBotManager:
    def __init__(self) -> None:
        self._apps: Dict[str, Application] = {}
        self._allowed_chat_ids: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}

    def _make_handler(self, binding_id: str, subagent_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not update.message or not update.message.text:
                return
            chat_id = str(update.effective_chat.id) if update.effective_chat else ""

            # Who is this and are they allowed to spend anything? The gate owns
            # the whitelist check, token redemption and every quota — nothing
            # below it costs money until it says PROCEED.
            from backend import bot_access_gate

            sender = update.effective_user
            decision = await asyncio.to_thread(
                bot_access_gate.authorize,
                binding_id,
                "telegram",
                chat_id,
                update.message.text,
                str(sender.id) if sender else "",
                (f"@{sender.username}" if sender and sender.username else (sender.full_name if sender else "")),
            )
            if decision.action == bot_access_gate.IGNORE:
                return
            if decision.action == bot_access_gate.REPLY:
                for chunk in _split_text(decision.reply_text):
                    await update.message.reply_text(chunk)
                return

            from backend.database import get_subagent

            subagent = get_subagent(subagent_id)
            if not subagent or not subagent.get("is_enabled"):
                await update.message.reply_text(
                    bot_access_gate.AGENT_OFFLINE_MESSAGE
                    if decision.scope == "public"
                    else "Этот агент сейчас отключён, Альберт."
                )
                return

            from backend.agent_messenger_governance import apply_binding_overrides
            subagent = apply_binding_overrides(subagent, self._overrides.get(binding_id))
            subagent = bot_access_gate.apply_subscriber_context(subagent, decision)

            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            from backend.agent import agent_instance

            session_id = decision.session_id or f"tgbot:{binding_id}:{chat_id}"
            try:
                response_text = await agent_instance._respond_as_subagent(
                    update.message.text, subagent, chat_id=session_id
                )
            except Exception:
                logger.exception("Agent bot %s: error handling message", binding_id)
                bot_access_gate.record_turn(decision, agent_instance.last_run_metadata.get(session_id), "")
                await update.message.reply_text("Произошла ошибка при обработке запроса.")
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
                    await update.message.reply_text(chunk)
                return

            # draft mode: never send anything to the other person automatically —
            # queue it and let the owner review/send from the dashboard.
            from backend.channel_replies import create_pending_reply
            incoming_from = f"@{sender.username}" if sender and sender.username else (str(sender.id) if sender else "")
            create_pending_reply(
                binding_id=binding_id,
                platform="telegram",
                subagent_id=subagent_id,
                chat_id=chat_id,
                incoming_text=update.message.text,
                drafted_reply=response_text,
                incoming_from=incoming_from,
            )

        return handler

    async def start(
        self, binding_id: str, subagent_id: str, bot_token: str,
        allowed_chat_ids: Optional[List[str]] = None, overrides: Optional[dict] = None,
        response_mode: str = "draft",
    ) -> None:
        if binding_id in self._apps:
            return
        app = ApplicationBuilder().token(bot_token).build()
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._make_handler(binding_id, subagent_id)))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        self._apps[binding_id] = app
        self._allowed_chat_ids[binding_id] = list(allowed_chat_ids or [])
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode
        logger.info("Agent bot started: binding=%s subagent=%s mode=%s", binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        """Applies an edited prompt/mode to an already-running bot without restarting
        its polling loop — called from agent_messenger_governance.update_binding_settings."""
        if binding_id not in self._apps:
            return
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode

    async def stop(self, binding_id: str) -> None:
        app = self._apps.pop(binding_id, None)
        self._allowed_chat_ids.pop(binding_id, None)
        self._overrides.pop(binding_id, None)
        self._response_modes.pop(binding_id, None)
        if not app:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            logger.exception("Agent bot %s: error during shutdown", binding_id)
        logger.info("Agent bot stopped: binding=%s", binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_telegram_bindings, resolve_telegram_binding_token

        for binding in list_telegram_bindings():
            if binding.get("status") != "active":
                continue
            token = resolve_telegram_binding_token(binding["id"])
            if not token:
                logger.warning("Agent bot %s: active binding has no resolvable token, skipping", binding["id"])
                continue
            overrides = {
                "system_prompt": binding.get("system_prompt_override"),
                "model": binding.get("model_override"),
                "model_provider": binding.get("model_provider_override"),
            }
            try:
                await self.start(
                    binding["id"], binding["subagent_id"], token, binding.get("allowed_chat_ids"), overrides,
                    binding.get("response_mode") or "draft",
                )
            except Exception:
                logger.exception("Failed to start agent bot %s at startup", binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._apps.keys()):
            await self.stop(binding_id)


manager = AgentBotManager()
