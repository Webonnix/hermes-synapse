"""Telegram approval cards: a pending Control Plane action reaches the owner's
phone with Approve / Reject buttons instead of waiting for someone to open the
dashboard.

control_plane.create_tool_task calls ``notify_pending`` for every new task that
waits for an approval. Tasks are created on the event loop and in worker
threads alike, so the card is always scheduled onto the bot's own loop
(``bind`` records it when the bot starts). Without a running bot this is a no-op.

A card names the action, its risk and who or what it reaches (the same
judgement values the decision log keeps), never the raw arguments. R4 cards
carry no Approve button: R4 needs a recent sign-in, which only the web has.
At most CARDS_PER_MINUTE cards go out a minute; the rest are summed up in one
message, so a busy agent desktop cannot flood the chat.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.approval_notify")

CARDS_PER_MINUTE = 5
CALLBACK_PATTERN = r"^cp:(approve|reject):(T-[0-9a-f]{12})$"
_loop: Optional[asyncio.AbstractEventLoop] = None
_sent: Deque[float] = deque()
_held = 0
_summary_scheduled = False


def enabled() -> bool:
    return os.getenv("TELEGRAM_APPROVAL_CARDS", "true").strip().lower() not in ("0", "false", "no", "off")


def bind(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop
    logger.info("Telegram approval cards %s", "on" if enabled() else "off (TELEGRAM_APPROVAL_CARDS)")


def card(task: Dict[str, Any]) -> Tuple[str, List[List[Tuple[str, str]]]]:
    """(text, button rows of (label, callback_data)) for one pending task."""
    from backend.control_plane import decision_features

    risk = task.get("risk_class", "R?")
    features = decision_features(task.get("tool_arguments"))
    lines = [f"⏳ Нужно решение · {risk}", f"Действие: {task.get('tool_name') or task.get('goal') or '—'}"]
    lines += [f"{key}: {value}" for key, value in list(features.items())[:5]]
    lines.append(f"Подтверждений: {task.get('approval_count', 0)}/{task.get('approvals_required', 1)} · {task['id']}")
    reject = ("❌ Отклонить", f"cp:reject:{task['id']}")
    if risk == "R4":
        lines.append("R4 одобряется только в веб-панели после повторного входа.")
        return "\n".join(lines), [[reject]]
    return "\n".join(lines), [[("✅ Одобрить", f"cp:approve:{task['id']}"), reject]]


def _admit(now: float) -> bool:
    while _sent and now - _sent[0] > 60:
        _sent.popleft()
    if len(_sent) < CARDS_PER_MINUTE:
        _sent.append(now)
        return True
    return False


async def _send(text: str, rows: Optional[List[List[Tuple[str, str]]]] = None) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    import backend.bot as bot

    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").split(",")[0].strip()
    app = bot.telegram_app
    if not chat or not app or not app.bot:
        return
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                   for row in rows]) if rows else None
    try:
        await app.bot.send_message(chat_id=int(chat), text=text, reply_markup=markup)
    except Exception as exc:  # the dashboard still shows the task
        logger.warning("Approval card not delivered: %s", type(exc).__name__)


async def _send_summary_later() -> None:
    global _held, _summary_scheduled
    await asyncio.sleep(60)
    count, _held, _summary_scheduled = _held, 0, False
    if count:
        await _send(f"⏳ Ещё {count} действ. ждут решения. Список: /approvals или раздел «Операции».")


def _deliver(task: Dict[str, Any]) -> None:
    global _held, _summary_scheduled
    if _admit(time.monotonic()):
        text, rows = card(task)
        asyncio.ensure_future(_send(text, rows))
        return
    _held += 1
    if not _summary_scheduled:
        _summary_scheduled = True
        asyncio.ensure_future(_send_summary_later())


def notify_pending(task: Dict[str, Any]) -> None:
    """Schedule a card for a task that waits for approval; never raises."""
    if not enabled() or _loop is None or _loop.is_closed() or task.get("status") != "awaiting_approval":
        return
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is _loop:
            _deliver(task)
        else:
            _loop.call_soon_threadsafe(_deliver, task)
    except Exception:
        logger.exception("Could not schedule an approval card")
