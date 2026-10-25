"""The live phone call an agent turn belongs to, and the tools that act on it.

A call reaches the agent through the ordinary sub-agent turn
(``agent_instance._respond_as_subagent``). What makes the turn a *phone* turn is the
:data:`CURRENT_CALL` context variable set around it by backend/phone_session.py:

* agent.py reads it to switch reasoning off (a caller will not wait through a thought
  process), shorten the answer budget and add the spoken-style directive;
* agent.py always offers the phone tools below on such a turn, whatever the keyword
  gate would have picked;
* the tools themselves read it to know which call they act on. A ContextVar survives
  the ``asyncio.to_thread`` hop that execute_governed_tool makes, and the tools mutate
  :attr:`CallContext.actions`, which the session reads back after the turn.

Every tool here is scoped to *this* call and refuses outside one, which is why they are
safe on the public-channel principal: a caller can end their own call or leave a
request, never read the owner's calendar entries or reach anything else.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes.phone_context")

# Answer budget on a phone turn: two spoken sentences plus room for a tool call's
# arguments. Keeps a runaway answer from turning into a monologue.
PHONE_MAX_TOKENS = 600

PHONE_TOOL_NAMES = (
    "phone_end_call",
    "phone_transfer_to_owner",
    "calendar_free_slots",
    "propose_meeting",
    "take_message",
)


@dataclass
class CallContext:
    call_id: str
    line_id: str
    conversation_id: str
    session_id: str
    direction: str  # inbound | outbound
    remote_number: str
    local_number: str = ""
    language: str = "auto"
    purpose: str = ""
    client_id: Optional[str] = None
    client_name: str = ""
    owner_number: str = ""
    auto_book: bool = False
    business_hours: str = ""
    timezone: str = "Asia/Jerusalem"
    line_name: str = ""
    actions: Dict[str, Any] = field(default_factory=dict)


CURRENT_CALL: contextvars.ContextVar[Optional[CallContext]] = contextvars.ContextVar("hermes_current_call", default=None)


def current() -> Optional[CallContext]:
    return CURRENT_CALL.get()


def voice_directive(ctx: CallContext) -> str:
    """Appended to the system prompt of a phone turn."""
    lines = [
        "\n\n[Телефонный разговор]:",
        "Ты разговариваешь с человеком ГОЛОСОМ по телефону. Всё, что ты пишешь, будет произнесено вслух.",
        "Отвечай коротко: одна–две фразы, живым разговорным языком. Без списков, markdown, эмодзи, ссылок и таблиц.",
        "Задавай не больше одного вопроса за раз. Не повторяй вопрос собеседника. Числа, даты и время говори так, как их произносят.",
        "Отвечай на языке собеседника (русский или английский).",
        "Если спросят, человек ли ты — честно скажи, что ты ИИ-ассистент.",
        "Если нужно позвать владельца — используй phone_transfer_to_owner; если он недоступен — предложи оставить сообщение (take_message).",
        "Для встречи: сначала calendar_free_slots, затем propose_meeting. Когда разговор окончен — попрощайся и вызови phone_end_call.",
    ]
    if ctx.direction == "outbound":
        lines.append(
            "Это ИСХОДЯЩИЙ звонок: ты звонишь сам. В первой фразе представься как ИИ-ассистент"
            + (f" «{ctx.line_name}»" if ctx.line_name else "")
            + " и сразу скажи, по какому вопросу звонишь. Если человеку неудобно говорить — извинись и заверши звонок."
        )
        if ctx.purpose:
            lines.append(f"Цель звонка: {ctx.purpose}")
    else:
        lines.append("Это ВХОДЯЩИЙ звонок.")
    if ctx.client_name:
        lines.append(f"Собеседник есть в CRM: {ctx.client_name}.")
    lines.append(f"Номер собеседника: {ctx.remote_number or 'скрыт'}.")
    return "\n".join(lines)


TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "phone_end_call",
            "description": "Завершает текущий телефонный звонок после того, как прозвучит твоя последняя фраза. Сначала попрощайся.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Коротко, почему разговор окончен"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "phone_transfer_to_owner",
            "description": "Переводит текущий звонок на владельца (живого человека). Используй, когда собеседник просит человека или вопрос требует владельца.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Суть вопроса для владельца"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_free_slots",
            "description": "Только во время телефонного звонка: свободные окна владельца на дату (только время, без "
                           "содержания встреч), чтобы предложить собеседнику время. В чате с владельцем смотри "
                           "календарь через get_calendar_events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Дата YYYY-MM-DD"},
                    "duration_minutes": {"type": "integer", "description": "Длительность встречи, по умолчанию 30"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_meeting",
            "description": "Только во время телефонного звонка: записывает встречу или звонок, о котором ты договорился "
                           "с собеседником на линии. Сначала узнай имя, удобное время и тему. Встречи, которые "
                           "владелец просит записать в чате, добавляй через add_calendar_event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя собеседника — как он представился"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM"},
                    "duration_minutes": {"type": "integer"},
                    "topic": {"type": "string", "description": "Тема встречи"},
                },
                "required": ["name", "date", "time", "topic"],
            },
        },
    },
]


def _refuse() -> str:
    return json.dumps({"status": "unavailable", "message": "Этот инструмент работает только во время телефонного звонка."}, ensure_ascii=False)


def execute(name: str, arguments: Dict[str, Any]) -> str:
    ctx = current()
    if ctx is None:
        return _refuse()
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "phone_end_call":
            ctx.actions["hangup"] = str(args.get("reason") or "conversation_finished")[:200]
            return json.dumps({"status": "ok", "message": "Звонок завершится после твоей прощальной фразы."}, ensure_ascii=False)
        if name == "phone_transfer_to_owner":
            return _transfer(ctx, str(args.get("reason") or "")[:500])
        if name == "calendar_free_slots":
            return _free_slots(ctx, args)
        if name == "propose_meeting":
            return _propose_meeting(ctx, args)
    except Exception:
        logger.exception("Phone tool %s failed", name)
        return json.dumps({"status": "error", "message": "Не получилось — извинись и предложи оставить сообщение."}, ensure_ascii=False)
    return _refuse()


def _transfer(ctx: CallContext, reason: str) -> str:
    if not ctx.owner_number:
        return json.dumps({
            "status": "unavailable",
            "message": "Перевод на владельца не настроен. Предложи оставить сообщение (take_message).",
        }, ensure_ascii=False)
    if ctx.business_hours:
        from zoneinfo import ZoneInfo

        from backend import inbox_rules

        try:
            windows = inbox_rules.parse_hours(ctx.business_hours)
            now = datetime.now(ZoneInfo(ctx.timezone or "Asia/Jerusalem"))
            if not inbox_rules.within_hours(windows, now):
                return json.dumps({
                    "status": "unavailable",
                    "message": "Сейчас нерабочее время — владелец недоступен. Предложи оставить сообщение (take_message).",
                }, ensure_ascii=False)
        except ValueError:
            pass
    ctx.actions["transfer"] = {"to": ctx.owner_number, "reason": reason}
    return json.dumps({"status": "ok", "message": "Скажи коротко, что соединяешь с владельцем. Перевод произойдёт после твоей фразы."}, ensure_ascii=False)


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _day_window(ctx: CallContext, day: datetime) -> Optional[Tuple[datetime, datetime]]:
    from backend import inbox_rules

    try:
        windows = inbox_rules.parse_hours(ctx.business_hours) if ctx.business_hours else []
    except ValueError:
        windows = []
    if not windows:
        return day.replace(hour=9, minute=0), day.replace(hour=18, minute=0)
    for window in windows:
        if day.weekday() in window.days and window.start < window.end:
            return (day.replace(hour=window.start.hour, minute=window.start.minute),
                    day.replace(hour=window.end.hour, minute=window.end.minute))
    return None


def compute_free_slots(busy: List[Tuple[datetime, datetime]], start: datetime, end: datetime,
                       duration: timedelta, not_before: Optional[datetime] = None, limit: int = 6) -> List[Tuple[datetime, datetime]]:
    cursor = max(start, not_before) if not_before else start
    if cursor.minute % 15:
        cursor = cursor + timedelta(minutes=15 - cursor.minute % 15)
        cursor = cursor.replace(second=0, microsecond=0)
    slots = []
    for busy_start, busy_end in sorted(busy):
        if busy_end <= cursor:
            continue
        if busy_start - cursor >= duration:
            slots.append((cursor, busy_start))
        cursor = max(cursor, busy_end)
    if end - cursor >= duration:
        slots.append((cursor, end))
    return slots[:limit]


def _free_slots(ctx: CallContext, args: Dict[str, Any]) -> str:
    from zoneinfo import ZoneInfo

    date_text = str(args.get("date") or "").strip()
    if not _DATE.match(date_text):
        return json.dumps({"status": "needs_input", "message": "Нужна дата в формате YYYY-MM-DD."}, ensure_ascii=False)
    duration = timedelta(minutes=max(15, min(int(args.get("duration_minutes") or 30), 240)))
    zone = ZoneInfo(ctx.timezone or "Asia/Jerusalem")
    day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=zone)
    window = _day_window(ctx, day)
    if window is None:
        return json.dumps({"status": "ok", "free_slots": [], "message": "В этот день владелец не работает."}, ensure_ascii=False)
    from backend import tools

    events, error = tools._list_events(window[0], window[1], max_results=50)
    if error:
        return json.dumps({"status": "unavailable", "message": "Календарь сейчас недоступен — запиши пожелание через propose_meeting."}, ensure_ascii=False)
    busy = []
    for event in events or []:
        try:
            busy.append((datetime.fromisoformat(event["start"]).astimezone(zone),
                         datetime.fromisoformat(event["end"]).astimezone(zone)))
        except (ValueError, KeyError):
            continue
    slots = compute_free_slots(busy, window[0], window[1], duration, not_before=datetime.now(zone))
    # Free/busy only: an event's title or attendees never leave this function.
    return json.dumps({
        "status": "ok", "date": date_text,
        "free_slots": [f"{a:%H:%M}–{b:%H:%M}" for a, b in slots],
    }, ensure_ascii=False)


def _propose_meeting(ctx: CallContext, args: Dict[str, Any]) -> str:
    from backend import inbox

    name = re.sub(r"\s+", " ", str(args.get("name") or "")).strip()[:120]
    topic = re.sub(r"\s+", " ", str(args.get("topic") or "")).strip()[:500]
    date_text = str(args.get("date") or "").strip()
    time_text = str(args.get("time") or "").strip()
    if not name or not re.search(r"[^\W\d_]", name):
        return json.dumps({"status": "needs_input", "message": "Уточни, как зовут собеседника."}, ensure_ascii=False)
    if not topic:
        return json.dumps({"status": "needs_input", "message": "Уточни тему встречи."}, ensure_ascii=False)
    if not _DATE.match(date_text) or not _TIME.match(time_text):
        return json.dumps({"status": "needs_input", "message": "Нужны дата YYYY-MM-DD и время HH:MM."}, ensure_ascii=False)
    duration = max(15, min(int(args.get("duration_minutes") or 30), 240))
    when = f"{date_text} {time_text}"
    booked = False
    if ctx.auto_book:
        from backend import tools

        event, error = tools._create_event(
            title=f"Звонок/встреча: {name} — {topic}"[:200], date=date_text, time=time_text,
            duration_minutes=duration, description=f"Записано ИИ-ассистентом по телефону. Номер: {ctx.remote_number}",
            event_type="CALL",
        )
        booked = bool(event and not error)
    note = f"📅 {'Записано в календарь' if booked else 'Предложена встреча (ждёт подтверждения)'}: {name}, {when}, {duration} мин — {topic}"
    inbox.add_system_note(ctx.conversation_id, note, {"event": "propose_meeting", "booked": booked})
    if ctx.client_id:
        try:
            from backend import client_tasks

            client_tasks.create_task({
                "title": f"{'Встреча' if booked else 'Подтвердить встречу'}: {name} — {when}"[:200],
                "description": f"{topic}\nНомер: {ctx.remote_number}",
                "clientId": ctx.client_id, "dueAt": date_text, "priority": "HIGH" if not booked else "NORMAL",
            }, actor_id="phone-agent")
        except Exception:
            logger.exception("propose_meeting could not create a task")
    inbox.notify_owner_once(
        f"meeting|{ctx.call_id}", {"id": ctx.line_id, "bot_username": ctx.line_name}, "phone", ctx.remote_number,
        name, topic, reason=f"договорился о встрече {when} ({'в календаре' if booked else 'нужно подтвердить'})",
    )
    message = ("Встреча записана в календарь. Подтверди собеседнику дату и время."
               if booked else "Пожелание записано, владелец подтвердит время. Скажи собеседнику, что с ним свяжутся для подтверждения.")
    return json.dumps({"status": "ok", "booked": booked, "message": message}, ensure_ascii=False)
