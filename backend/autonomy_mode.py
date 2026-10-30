"""One owner switch for how far every agent may act without asking.

Applies to Vexa and to every sub-agent alike, because all of them reach tools
through ``control_plane.execute_governed_tool`` — the mode only changes how many
owner approvals ``create_tool_task`` asks for. It never widens what a principal
may call (backend/tool_permissions.py still decides that), never lifts the
shell deny-list, the kill switch, command budgets or paid-tool budgets.

``supervised``
    The historical behaviour: R3 needs one approval, R4 two.
``auto``
    Everything runs on its own except the sensitive set below and every R4
    action (including unknown tools, which classify as R4). Those keep the
    supervised approval count.
``full``
    Disabled: stored legacy values fall back to supervised. Unknown or external
    context cannot waive approval for side effects.
"""

from __future__ import annotations

import logging
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend import database

logger = logging.getLogger("hermes.autonomy_mode")

SETTING_KEY = "agent_autonomy_mode"
SUPERVISED = "supervised"
AUTO = "auto"
FULL = "full"
MODES = (SUPERVISED, AUTO)
DEFAULT_MODE = SUPERVISED

# Still confirmed in ``auto``: actions that reach people or sites outside the
# server in the owner's name, change routing, or install software. Every R4
# tool (shell, deletions, unknown MCP tools) is confirmed in ``auto`` too.
SENSITIVE_TOOLS = frozenset({
    "phone_call",
    "browser_task",
    "request_capability",
    "request_mcp_connection",
})

# Trading tools move real money; no mode lets them run unattended.
ALWAYS_CONFIRMED_PREFIXES = ("ctrader_", "bcm_")

_BASE_APPROVALS = {"R3": 1, "R4": 2}

router = APIRouter()


def get_mode() -> str:
    """The stored mode; anything unreadable or unknown means supervised."""
    try:
        value = str(database.get_setting(SETTING_KEY) or "").strip().lower()
    except Exception:
        logger.exception("Could not read the autonomy mode; treating it as supervised")
        return DEFAULT_MODE
    return value if value in MODES else DEFAULT_MODE


def set_mode(mode: str) -> str:
    value = (mode or "").strip().lower()
    if value not in MODES:
        raise ValueError(f"Unknown autonomy mode: {mode}")
    database.set_setting(SETTING_KEY, value)
    return get_mode()


def is_sensitive(tool_name: str, risk_class: str) -> bool:
    if risk_class == "R4" or tool_name in SENSITIVE_TOOLS:
        return True
    if tool_name.startswith("omniroute_"):
        from backend.omniroute_mcp import OWNER_ONLY_TOOLS

        return tool_name in OWNER_ONLY_TOOLS
    return False


def approvals_required(tool_name: str, risk_class: str, mode: str | None = None,
                       arguments: dict | None = None) -> int:
    """Owner approvals a new tool task needs under ``mode`` (default: the stored one).

    In ``auto``, an owner-confirmed rule for this exact R2 tool and argument value
    (control_plane.create_approval_rule) waives its one approval. Nothing else does."""
    from backend.context_trust import approval_floor
    base = max(_BASE_APPROVALS.get(risk_class, 0 if risk_class in ("R0", "R1", "R2") else 2),
               approval_floor(tool_name, risk_class))
    if not base or tool_name.startswith(ALWAYS_CONFIRMED_PREFIXES):
        return base
    mode = mode or get_mode()
    if mode == AUTO and risk_class == "R2" and arguments is not None:
        from backend.control_plane import matching_rule
        if matching_rule(tool_name, arguments):
            return 0
    return base


def prompt_directive(mode: str | None = None) -> str:
    """How the agent should behave under the current mode, for its system prompt."""
    mode = mode or get_mode()
    if mode == SUPERVISED:
        return ""
    gate = (
        "Владелец разрешил автономную работу с чтением и локальными напоминаниями. "
        "Изменения и исходящие действия требуют подтверждения владельца, "
        "потому что контекст может содержать недоверенные данные."
    )
    return (
        f"\n\n[Режим автономии]: {gate}\n"
        "Работай как автономный агент: доводи задачу до результата сам, цепочкой вызовов "
        "инструментов, не спрашивая разрешения на каждый шаг. Не переспрашивай, если недостающее "
        "можно найти (web_search, list_tools, файлы, другие агенты) или разумно предположить — "
        "назови допущение одной фразой в ответе. Вопрос задавай, только если без ответа задачу "
        "выполнить нельзя. Свежие факты проверяй поиском, не по памяти. После действия проверь "
        "результат. Если шаг вернул awaiting_approval — назови ID задачи и продолжай остальную "
        "часть работы. Ответ — прямой и по делу: результат первым, без воды и без морализаторства."
    )


class AutonomyModePayload(BaseModel):
    mode: str


@router.get("/api/agent-autonomy")
async def get_agent_autonomy():
    return {"mode": get_mode(), "modes": list(MODES)}


@router.put("/api/agent-autonomy")
async def put_agent_autonomy(payload: AutonomyModePayload, request: Request):
    if payload.mode.strip().lower() == AUTO:
        from backend.auth_http import require_recent_auth
        await asyncio.to_thread(require_recent_auth, request)
    try:
        mode = set_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    logger.warning("Agent autonomy mode set to %s by the owner", mode)
    return {"mode": mode, "modes": list(MODES)}
