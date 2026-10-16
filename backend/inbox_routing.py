"""Conversation routing: who executes the next AI turn of an inbox conversation.

How this relates to the AI Router (9Router) that already exists
───────────────────────────────────────────────────────────────
backend/local_orchestrator.py + backend/router_tiers.py decide **which model/tier serves
an LLM call** — local first, escalate up the configured chain on complexity, quota or
failure. They never decide who answers a conversation. This module decides exactly that
and nothing about models: the *executor* of a turn is either a sub-agent (whose own LLM
calls then go through the AI Router chain as before) or an external voice target
(backend/voice_providers.py). One decision per layer, so no routing state is stored twice.

Precedence, highest first:

1. ``handling`` — a taken-over (``human``) or ``blocked`` conversation never reaches this
   module at all: the gate answers IGNORE before routing is consulted.
2. A manual pin from the dashboard, until it is removed. A pin set during a takeover is
   kept and applies from the first turn after "Вернуть агенту".
3. Otherwise ``auto``: the binding's own agent, i.e. exactly what happened before this
   module existed.

A route that cannot be honoured (pinned agent disabled, voice target unavailable) falls
back by the target's policy — ``fallback_internal`` (default), ``fail_closed`` or
``human_takeover`` — and the fallback is recorded with its reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.inbox_routing")

TARGET_SUBAGENT = "subagent"
TARGET_EXTERNAL_VOICE = "external_voice"
TARGET_TYPES = (TARGET_SUBAGENT, TARGET_EXTERNAL_VOICE)
SOURCES = ("manual", "auto", "fallback")

VOICE_AGENT_TIMEOUT_SECONDS = 45.0
HANDOFF_TEXT = "Передаю ваш вопрос сотруднику — он ответит здесь, как только освободится."

_schema_ready_for: Optional[str] = None
_schema_lock = threading.Lock()


@dataclass
class RouteResolution:
    target_type: str
    target_id: str
    executor_subagent_id: str
    source: str
    reason: str = ""
    latency_ms: int = 0
    failure_policy: str = ""
    capability_mode: str = ""


def _db_path() -> str:
    from backend import database

    return database.DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=3000")
    return connection


def reset_schema_cache() -> None:
    global _schema_ready_for
    _schema_ready_for = None


def _init_schema() -> None:
    global _schema_ready_for
    path = _db_path()
    if _schema_ready_for == path:
        return
    with _schema_lock:
        if _schema_ready_for == path:
            return
        with _connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_routes (
                    conversation_id TEXT PRIMARY KEY,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    target_type TEXT,
                    target_id TEXT,
                    pinned_by TEXT,
                    pinned_at TEXT,
                    last_target_type TEXT,
                    last_target_id TEXT,
                    last_source TEXT,
                    last_reason TEXT,
                    last_latency_ms INTEGER,
                    last_event_id TEXT,
                    last_decided_at TEXT
                )
                """
            )
        _schema_ready_for = path


# ── targets ─────────────────────────────────────────────────────────────────
def list_targets() -> List[Dict[str, Any]]:
    """Every executor a conversation may be pinned to, with safe display data only."""
    from backend.database import get_all_subagents
    from backend.tool_permissions import MAIN_AGENT_IDS

    targets: List[Dict[str, Any]] = []
    for agent in get_all_subagents():
        if agent.get("id") in MAIN_AGENT_IDS:
            continue  # Vexa never answers strangers — same lockdown as bindings
        targets.append({
            "type": TARGET_SUBAGENT, "id": agent["id"], "name": agent.get("name") or agent["id"],
            "enabled": bool(agent.get("is_enabled")), "available": bool(agent.get("is_enabled")),
            "project_id": agent.get("project_id"),
        })
    try:
        from backend import voice_providers

        for target in voice_providers.list_targets():
            targets.append({
                "type": TARGET_EXTERNAL_VOICE, "id": target["id"], "name": target["name"],
                "enabled": target["enabled"], "available": target["available"],
                "provider_name": target["provider_name"], "provider_kind": target["provider_kind"],
                "capability_mode": target["capability_mode"], "voice_id": target["voice_id"],
                "failure_policy": target["failure_policy"],
                "health": {k: target["health"].get(k) for k in ("ok", "latency_ms", "checked_at", "error")}
                if isinstance(target.get("health"), dict) else {},
            })
    except Exception:
        logger.exception("Voice targets could not be listed")
    return targets


def validate_target(target_type: str, target_id: str) -> Dict[str, Any]:
    if target_type not in TARGET_TYPES:
        raise ValueError(f"target_type must be one of {TARGET_TYPES}")
    for target in list_targets():
        if target["type"] == target_type and target["id"] == target_id:
            return target
    raise ValueError("Такой цели маршрутизации нет")


# ── route state ─────────────────────────────────────────────────────────────
def _route_row(conversation_id: str) -> Optional[sqlite3.Row]:
    _init_schema()
    with _connect() as connection:
        return connection.execute("SELECT * FROM inbox_routes WHERE conversation_id = ?", (conversation_id,)).fetchone()


def get_route(conversation_id: str) -> Dict[str, Any]:
    row = _route_row(conversation_id)
    data = dict(row) if row else {"conversation_id": conversation_id, "pinned": 0}
    data["pinned"] = bool(data.get("pinned"))
    target = None
    if data["pinned"] and data.get("target_type") and data.get("target_id"):
        target = next(
            (t for t in list_targets() if t["type"] == data["target_type"] and t["id"] == data["target_id"]), None
        )
    data["target"] = target
    return data


def pin_route(conversation_id: str, target_type: str, target_id: str, actor: str = "") -> Dict[str, Any]:
    validate_target(target_type, target_id)
    _init_schema()
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO inbox_routes (conversation_id, pinned, target_type, target_id, pinned_by, pinned_at)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT (conversation_id) DO UPDATE SET pinned = 1, target_type = excluded.target_type,
                target_id = excluded.target_id, pinned_by = excluded.pinned_by, pinned_at = excluded.pinned_at
            """,
            (conversation_id, target_type, target_id, actor[:64], now),
        )
    _emit(conversation_id)
    return get_route(conversation_id)


def unpin_route(conversation_id: str) -> Dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        connection.execute(
            "UPDATE inbox_routes SET pinned = 0, target_type = NULL, target_id = NULL, pinned_by = NULL, pinned_at = NULL "
            "WHERE conversation_id = ?",
            (conversation_id,),
        )
    _emit(conversation_id)
    return get_route(conversation_id)


def _emit(conversation_id: str) -> None:
    try:
        from backend import inbox

        inbox._emit(conversation_id, "route")
    except Exception:
        logger.exception("Route change for %s could not be announced", conversation_id)


def _record_decision(conversation_id: str, resolution: RouteResolution, event_id: str) -> None:
    try:
        _init_schema()
        with _connect() as connection:
            previous = connection.execute(
                "SELECT last_target_type, last_target_id, last_source FROM inbox_routes WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO inbox_routes (conversation_id, last_target_type, last_target_id, last_source, last_reason,
                                          last_latency_ms, last_event_id, last_decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (conversation_id) DO UPDATE SET last_target_type = excluded.last_target_type,
                    last_target_id = excluded.last_target_id, last_source = excluded.last_source,
                    last_reason = excluded.last_reason, last_latency_ms = excluded.last_latency_ms,
                    last_event_id = excluded.last_event_id, last_decided_at = excluded.last_decided_at
                """,
                (conversation_id, resolution.target_type, resolution.target_id, resolution.source,
                 resolution.reason[:300], resolution.latency_ms, (event_id or None), _now()),
            )
        changed = previous is None or (
            previous["last_target_type"], previous["last_target_id"], previous["last_source"]
        ) != (resolution.target_type, resolution.target_id, resolution.source)
        if changed:
            _emit(conversation_id)
    except Exception:
        logger.exception("Route decision for %s could not be recorded", conversation_id)


# ── resolution ──────────────────────────────────────────────────────────────
def _default_subagent_id(binding_id: str) -> str:
    from backend.agent_messenger_governance import get_binding

    return str((get_binding(binding_id) or {}).get("subagent_id") or "")


def _fallback(default_id: str, reason: str, *, started: float, policy: str = "fallback_internal",
              capability_mode: str = "", target: Optional[Dict[str, Any]] = None) -> RouteResolution:
    if target is not None and policy in ("fail_closed", "human_takeover"):
        # The pinned target stays the recorded target: nothing else is allowed to
        # answer in its place, the policy decides what happens instead.
        return RouteResolution(
            target_type=TARGET_EXTERNAL_VOICE, target_id=target["id"], executor_subagent_id="",
            source="fallback", reason=reason, latency_ms=int((time.monotonic() - started) * 1000),
            failure_policy=policy, capability_mode=capability_mode,
        )
    return RouteResolution(
        target_type=TARGET_SUBAGENT, target_id=default_id, executor_subagent_id=default_id,
        source="fallback", reason=reason, latency_ms=int((time.monotonic() - started) * 1000),
        failure_policy=policy,
    )


def resolve(decision: Any, event_id: str = "") -> Dict[str, Any]:
    """Decides the executor for one PROCEED turn. Local lookups only (bounded by the
    SQLite busy timeout), so it cannot stall a channel on a remote call; the external
    call itself happens later in :func:`respond_turn`, under its own timeout."""
    started = time.monotonic()
    default_id = _default_subagent_id(decision.binding_id)
    conversation_id = decision.conversation_id
    try:
        row = _route_row(conversation_id)
        if row is None or not row["pinned"]:
            resolution = RouteResolution(
                target_type=TARGET_SUBAGENT, target_id=default_id, executor_subagent_id=default_id,
                source="auto", latency_ms=int((time.monotonic() - started) * 1000),
            )
        elif row["target_type"] == TARGET_SUBAGENT:
            from backend.database import get_subagent

            agent = get_subagent(row["target_id"])
            if agent and agent.get("is_enabled"):
                resolution = RouteResolution(
                    target_type=TARGET_SUBAGENT, target_id=agent["id"], executor_subagent_id=agent["id"],
                    source="manual", latency_ms=int((time.monotonic() - started) * 1000),
                )
            else:
                resolution = _fallback(default_id, "закреплённый агент отключён или удалён", started=started)
        else:
            from backend import voice_providers

            target = voice_providers.get_target(row["target_id"])
            if target is None:
                resolution = _fallback(default_id, "закреплённая голосовая цель удалена", started=started)
            elif not target["available"]:
                resolution = _fallback(
                    default_id, "голосовая цель или её провайдер отключены", started=started,
                    policy=target["failure_policy"], capability_mode=target["capability_mode"], target=target,
                )
            else:
                resolution = RouteResolution(
                    target_type=TARGET_EXTERNAL_VOICE, target_id=target["id"],
                    # A tts target still needs a text author: the binding's agent writes,
                    # the provider voices it. A voice_agent answers on its own.
                    executor_subagent_id=default_id if target["capability_mode"] == "tts" else "",
                    source="manual", latency_ms=int((time.monotonic() - started) * 1000),
                    failure_policy=target["failure_policy"], capability_mode=target["capability_mode"],
                )
    except Exception as exc:
        logger.exception("Route resolution failed for %s", conversation_id)
        resolution = _fallback(default_id, f"ошибка маршрутизации: {type(exc).__name__}", started=started)
    logger.info(
        "route conversation=%s event=%s target=%s:%s source=%s latency_ms=%d reason=%s",
        conversation_id, event_id or "-", resolution.target_type, resolution.target_id or "-",
        resolution.source, resolution.latency_ms, resolution.reason or "-",
    )
    _record_decision(conversation_id, resolution, event_id)
    return asdict(resolution)


def effective_subagent_id(decision: Any, default_subagent_id: str) -> str:
    """The agent a channel runtime should load for this turn. Unchanged behaviour
    whenever routing did not run or chose nothing different."""
    route = getattr(decision, "route", None) or {}
    return str(route.get("executor_subagent_id") or default_subagent_id)


# ── execution ───────────────────────────────────────────────────────────────
async def respond_turn(
    decision: Any,
    text: str,
    subagent: Dict[str, Any],
    session_id: str,
    images: Optional[List[str]] = None,
) -> str:
    """Runs the turn on whatever :func:`resolve` chose and returns the text the
    channel should deliver. Never raises for a routing reason: every failure path
    ends in a policy outcome (internal fallback, a hand-off line, or an offline
    line), so a degraded external service cannot break the dialogue."""
    from backend import inbox
    from backend.agent import agent_instance

    route = getattr(decision, "route", None) or {}
    target_type = route.get("target_type")
    mode = route.get("capability_mode")

    async def internal() -> str:
        return await agent_instance._respond_as_subagent(text, subagent, chat_id=session_id, images=images)

    if target_type == TARGET_EXTERNAL_VOICE and mode == "voice_agent" and route.get("source") == "manual":
        reply = await _run_voice_agent(decision, route, text, session_id)
        if reply is not None:
            return reply
        outcome = await _apply_failure_policy(decision, route)
        if outcome is not None:
            return outcome
        response = await internal()
    elif target_type == TARGET_EXTERNAL_VOICE and route.get("source") == "fallback" and route.get("failure_policy") in (
        "fail_closed", "human_takeover"
    ):
        outcome = await _apply_failure_policy(decision, route)
        response = outcome if outcome is not None else await internal()
    else:
        response = await internal()
        if target_type == TARGET_EXTERNAL_VOICE and mode == "tts" and route.get("source") == "manual" and response:
            await _voice_reply(decision, route, response)

    whispers = await asyncio.to_thread(inbox.pending_whisper_texts, session_id)
    return inbox.strip_whisper_echo(response, whispers)


async def _run_voice_agent(decision: Any, route: Dict[str, Any], text: str, session_id: str) -> Optional[str]:
    from backend import inbox, voice_providers
    from backend.agent import agent_instance
    from backend.database import save_message

    target = await asyncio.to_thread(voice_providers.get_target, route["target_id"])
    provider = await asyncio.to_thread(voice_providers.get_provider, (target or {}).get("provider_id", ""))
    if not target or not provider:
        return None
    adapter = voice_providers.adapter_for(provider)
    instructions = ""
    if adapter.supports_instructions():
        # Only a provider with a separate instruction channel may see a whisper; for
        # any other the whisper stays queued for the next internal turn instead of
        # being glued onto the customer's text.
        instructions = await asyncio.to_thread(inbox.consume_whisper_block, session_id)
    started = time.monotonic()
    try:
        reply = await asyncio.wait_for(
            adapter.respond(
                conversation_id=decision.conversation_id, text=text, instructions=instructions,
                agent_id=target.get("agent_id") or "", context={"platform": decision.platform},
            ),
            timeout=VOICE_AGENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        reply = voice_providers.AgentReply(ok=False, error="таймаут")
    except Exception as exc:
        logger.warning("Voice agent %s crashed: %s", route["target_id"], type(exc).__name__)
        reply = voice_providers.AgentReply(ok=False, error="ошибка адаптера")
    latency_ms = int((time.monotonic() - started) * 1000)
    agent_instance.last_run_metadata[session_id] = {
        "status": "ok" if reply.ok else "provider_error", "model": target["name"],
        "provider": provider["name"], "latency_ms": latency_ms, "input_tokens": 0, "output_tokens": 0,
        "error": "" if reply.ok else reply.error,
    }
    if not reply.ok:
        logger.warning("Voice agent %s failed (%s) — applying policy %s", route["target_id"], reply.error, route.get("failure_policy"))
        await asyncio.to_thread(
            inbox.add_system_note, decision.conversation_id,
            f"Голосовой агент «{target['name']}» не ответил ({reply.error}).",
            {"event": "voice_agent_failed", "target_id": target["id"]},
        )
        return None
    # The agent's memory stays one continuous thread whichever executor answered,
    # so a later internal turn (after a fallback or an unpin) sees the whole dialogue.
    await asyncio.to_thread(save_message, session_id, "user", text)
    await asyncio.to_thread(save_message, session_id, "assistant", reply.text)
    return reply.text


async def _voice_reply(decision: Any, route: Dict[str, Any], response: str) -> None:
    """A tts target voices the agent's text. The audio is archived for playback in
    the inbox; channels that only carry text still receive the text."""
    from backend import voice_providers

    try:
        target = await asyncio.to_thread(voice_providers.get_target, route["target_id"])
        provider = await asyncio.to_thread(voice_providers.get_provider, (target or {}).get("provider_id", ""))
        if not target or not provider:
            return
        result = await voice_providers.adapter_for(provider).synthesize(text=response, voice_id=target["voice_id"])
    except Exception as exc:
        logger.warning("TTS for target %s crashed: %s", route.get("target_id"), type(exc).__name__)
        return
    if result.ok:
        decision.pending_attachments.append({
            "kind": "tts", "mime": result.mime, "data": result.audio,
            "meta": {"target_id": target["id"], "voice_id": target["voice_id"], "latency_ms": result.latency_ms},
        })
    else:
        logger.warning("TTS for target %s failed: %s", target["id"], result.error)


async def _apply_failure_policy(decision: Any, route: Dict[str, Any]) -> Optional[str]:
    policy = route.get("failure_policy") or "fallback_internal"
    if policy == "fallback_internal":
        return None
    from backend import inbox

    if policy == "human_takeover":
        try:
            await asyncio.to_thread(inbox.takeover, decision.conversation_id)
        except Exception:
            logger.exception("Could not hand conversation %s to a human", decision.conversation_id)
        return HANDOFF_TEXT
    from backend.bot_access_gate import AGENT_OFFLINE_MESSAGE

    return AGENT_OFFLINE_MESSAGE
