"""Per-agent Telegram bot runtime.

Each active row in agent_messenger_bindings (backend/agent_messenger_governance.py)
gets its own python-telegram-bot Application here, polling with its own bot
token. Messages sent to that bot go straight to that one agent via
backend.agent.agent_instance._respond_as_subagent — bypassing the main router
entirely, same call the dashboard/orchestrator already use for subagent turns.
This is deliberately separate from backend/bot.py (the single global Vexa
bot): that one dispatches across the whole agent network and owns the
Control Plane /approve flow, this one is a dedicated line to a single agent.

Besides text, a binding's bot can receive a photo (fed to the model as vision
input) or a document (text-like files are read and injected as context; other
formats are announced to the model by name/type/size, since no PDF/DOCX parser
is bundled here). It can also send files back: any ``/api/plots/...`` or
``/api/generated-images/...`` markdown image link the model's response embeds
(e.g. from the generate_image tool) is resolved to the actual file on disk and
delivered as a Telegram photo instead of a dead-looking link — both from this
module's own auto-reply path and from a draft approved later in the dashboard
(backend/channel_replies.py imports extract_local_images for that).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

logger = logging.getLogger("hermes.agent_bot")

TELEGRAM_TEXT_LIMIT = 3900

# Telegram bots can't fetch a file from our own localhost, so a generated image
# markdown link only works if we resolve it to the file on disk ourselves.
_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\((?:https?://[^/\s]+)?/api/(plots|generated-images)/([^)\s]+)\)")

_TEXT_DOC_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".log", ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".html", ".htm", ".css", ".sh",
}
_MAX_DOC_TEXT_CHARS = 12000
_MAX_DOC_BYTES = 15 * 1024 * 1024

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PLOTS_DIR = os.path.join(_BASE_DIR, "data", "plots")
_GENERATED_IMAGES_DIR = os.path.join(_BASE_DIR, "data", "generated_images")


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


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def extract_local_images(text: str) -> Tuple[str, List[str]]:
    """Strips markdown image links pointing at this server's own /api/plots or
    /api/generated-images out of `text` and resolves each to a local file path.
    Returns (text_without_those_links, [existing_file_paths]). Links that don't
    resolve to a real file on disk are left in the text untouched."""
    kind_dirs = {"plots": _PLOTS_DIR, "generated-images": _GENERATED_IMAGES_DIR}
    paths: List[str] = []

    def _sub(match: "re.Match[str]") -> str:
        kind, filename = match.group(1), match.group(2)
        candidate = os.path.join(kind_dirs[kind], filename)
        if os.path.isfile(candidate):
            paths.append(candidate)
            return ""
        return match.group(0)

    cleaned = _IMAGE_LINK_RE.sub(_sub, text).strip()
    return cleaned, paths


class AgentBotManager:
    def __init__(self) -> None:
        self._apps: Dict[str, Application] = {}
        self._allowed_chat_ids: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}

    async def _process_incoming(
        self,
        binding_id: str,
        subagent_id: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        gate_text: str,
        agent_text: str,
        images: Optional[List[str]] = None,
    ) -> None:
        """Shared turn logic for text/photo/document messages: gate check,
        subagent resolution, model call, then dispatch by response_mode."""
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
            gate_text,
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
                agent_text, subagent, chat_id=session_id, images=images
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
            clean_text, image_paths = extract_local_images(response_text)
            for path in image_paths:
                try:
                    with open(path, "rb") as photo:
                        await update.message.reply_photo(photo=photo)
                except Exception:
                    logger.exception("Agent bot %s: failed to send image %s", binding_id, path)
            disclosure = auto_reply_disclosure(binding_id)
            text_to_send = (clean_text + disclosure) if clean_text else disclosure
            if text_to_send.strip():
                for chunk in _split_text(text_to_send):
                    await update.message.reply_text(chunk)
            return

        # draft mode: never send anything to the other person automatically —
        # queue it and let the owner review/send from the dashboard. The
        # drafted text keeps any image markdown links as-is; send_pending_reply
        # (backend/channel_replies.py) resolves and attaches them at send time.
        from backend.channel_replies import create_pending_reply
        incoming_from = f"@{sender.username}" if sender and sender.username else (str(sender.id) if sender else "")
        create_pending_reply(
            binding_id=binding_id,
            platform="telegram",
            subagent_id=subagent_id,
            chat_id=chat_id,
            incoming_text=gate_text,
            drafted_reply=response_text,
            incoming_from=incoming_from,
        )

    def _make_handler(self, binding_id: str, subagent_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not update.message or not update.message.text:
                return
            await self._process_incoming(
                binding_id, subagent_id, update, context,
                gate_text=update.message.text, agent_text=update.message.text,
            )

        return handler

    def _make_photo_handler(self, binding_id: str, subagent_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not update.message or not update.message.photo:
                return
            try:
                # Telegram sends the same photo at several resolutions; the last is the largest.
                largest = update.message.photo[-1]
                tg_file = await largest.get_file()
                photo_bytes = await tg_file.download_as_bytearray()
                encoded = base64.b64encode(bytes(photo_bytes)).decode("ascii")
            except Exception:
                logger.exception("Agent bot %s: failed to download photo", binding_id)
                await update.message.reply_text("Не удалось загрузить изображение.")
                return

            caption = (update.message.caption or "").strip()
            prompt = caption or "Опиши, что изображено на этом фото, и отметь всё важное или необычное."
            await self._process_incoming(
                binding_id, subagent_id, update, context,
                gate_text=caption or "[фото]", agent_text=prompt, images=[encoded],
            )

        return handler

    def _make_document_handler(self, binding_id: str, subagent_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            doc = update.message.document if update.message else None
            if not doc:
                return

            caption = (update.message.caption or "").strip()
            file_name = doc.file_name or "без имени"

            if doc.file_size and doc.file_size > _MAX_DOC_BYTES:
                prompt = (
                    f"Пользователь прислал файл «{file_name}» ({_human_size(doc.file_size)}) — он слишком "
                    f"большой, чтобы прочитать содержимое (лимит {_human_size(_MAX_DOC_BYTES)}). Сообщи "
                    f"об этом и уточни, что нужно сделать с файлом."
                )
            else:
                try:
                    tg_file = await doc.get_file()
                    data = bytes(await tg_file.download_as_bytearray())
                except Exception:
                    logger.exception("Agent bot %s: failed to download document", binding_id)
                    await update.message.reply_text("Не удалось загрузить файл.")
                    return

                ext = os.path.splitext(file_name)[1].lower()
                text_content: Optional[str] = None
                if ext in _TEXT_DOC_EXTENSIONS or (doc.mime_type or "").startswith("text/"):
                    try:
                        text_content = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text_content = None

                if text_content is not None:
                    truncated = text_content[:_MAX_DOC_TEXT_CHARS]
                    note = (
                        "\n\n[Файл обрезан — показана только начальная часть.]"
                        if len(text_content) > _MAX_DOC_TEXT_CHARS else ""
                    )
                    prompt = (
                        (caption or f"Пользователь прислал файл «{file_name}». Проанализируй его содержимое и ответь по существу.")
                        + f"\n\n[Содержимое файла «{file_name}»]:\n{truncated}{note}"
                    )
                else:
                    prompt = caption or (
                        f"Пользователь прислал файл «{file_name}» ({doc.mime_type or 'неизвестный тип'}, "
                        f"{_human_size(doc.file_size or len(data))}). Текст в этом формате нельзя прочитать "
                        f"автоматически — уточни у пользователя, что нужно сделать с этим файлом."
                    )

            await self._process_incoming(
                binding_id, subagent_id, update, context,
                gate_text=caption or f"[файл: {file_name}]", agent_text=prompt,
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
        app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, self._make_photo_handler(binding_id, subagent_id)))
        app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, self._make_document_handler(binding_id, subagent_id)))
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
