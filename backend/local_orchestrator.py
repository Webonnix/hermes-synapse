"""Local-model-orchestrated delegation for the AI Router fallback chain.

The local model is the default handler for every request (unchanged from
before this module existed). What's new: when an admin configures at least
one tier in backend/router_tiers.py, the local model itself decides — per
user turn, with a cheap classification call — whether a request is worth
escalating to a paid/external tier, escalates up the chain on failure or
exhausted quota, and always falls back to answering locally itself if every
configured tier is unavailable. A deployment with zero tiers configured (the
default) sees no behavior change at all: `route_llm_call` degrades to a
straight passthrough to `call_llm_normalized`.

Sensitive-data redaction is not reimplemented here — `call_llm_normalized`
already runs backend/redaction.py's regex + local-model-confirm gateway on
every call to a non-local `api_base` (see its own module docstring), so every
tier this module escalates to gets exactly the same protection an explicit
provider binding already gets today. This module's only job is deciding
*whether and where* to send a call, never what's in it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

logger = logging.getLogger("hermes.router")

_DELEGATE_SYSTEM_PROMPT = """You are a routing gate for a local AI assistant. Decide whether the user's
request should stay with the local model, or is worth escalating to a stronger (paid) external model.

Escalate ("yes") only for: substantial code generation/refactoring across multiple files, deep multi-step
analysis or research synthesis, long-form writing requiring top-tier quality, or anything the user
explicitly asks to be done "as well as possible" / "thoroughly". Keep it local ("no") for: greetings,
short questions, tool calls (weather, timers, search, calendar), simple explanations, casual conversation.

Respond with ONLY one word: yes or no."""

# Keyword fallback used only if the local classifier call itself fails.
_DELEGATE_KEYWORDS = [
    "напиши код", "рефактор", "проанализируй подробно", "глубокий анализ", "исследуй подробно",
    "как можно лучше", "максимально качественно", "полный отчёт", "write code", "refactor",
    "in-depth analysis", "thorough analysis", "comprehensive",
]
_DELEGATE_LENGTH_THRESHOLD = 600  # chars — a long ask is itself a complexity signal

_FAILURE_STATUSES_CACHE: Optional[frozenset] = None


def _failure_statuses() -> frozenset:
    global _FAILURE_STATUSES_CACHE
    if _FAILURE_STATUSES_CACHE is None:
        from backend.llm_client import STATUS_PARSE_ERROR, STATUS_PROVIDER_ERROR, STATUS_TIMEOUT
        _FAILURE_STATUSES_CACHE = frozenset({STATUS_TIMEOUT, STATUS_PROVIDER_ERROR, STATUS_PARSE_ERROR})
    return _FAILURE_STATUSES_CACHE


def _keyword_delegate(text: str) -> bool:
    lowered = (text or "").lower()
    if len(text or "") >= _DELEGATE_LENGTH_THRESHOLD:
        return True
    return any(kw in lowered for kw in _DELEGATE_KEYWORDS)


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return " ".join(parts)
    return ""


async def _should_delegate(text: str, local_api_base: str, local_api_key: str, local_model: str, local_provider: str) -> bool:
    mode = os.getenv("ROUTER_DELEGATION_MODE", "auto").strip().lower()
    if mode == "always_local":
        return False
    if mode == "always_delegate":
        return True
    if not text.strip():
        return False

    try:
        from backend.llm_client import call_llm_normalized

        result = await call_llm_normalized(
            api_base=local_api_base,
            api_key=local_api_key,
            model=local_model,
            messages=[
                {"role": "system", "content": _DELEGATE_SYSTEM_PROMPT},
                {"role": "user", "content": text[:4000]},
            ],
            temperature=0.0,
            max_tokens=5,
            timeout=8.0,
            max_retries=0,
            provider_options={"provider": local_provider, "think": False},
        )
        answer = (result.content or "").strip().lower()
        if answer.startswith("yes"):
            return True
        if answer.startswith("no"):
            return False
    except Exception as exc:
        logger.warning("Delegation classifier call failed (%s), falling back to keyword heuristic", exc)

    return _keyword_delegate(text)


async def _attempt(
    *, api_base: str, api_key: str, model: str, provider: str,
    messages: List[Dict[str, Any]], temperature: float, max_tokens: Optional[int],
    tools: Optional[List[Dict[str, Any]]], timeout: Optional[float], max_retries: Optional[int],
    client: Any, stream_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    provider_options: Optional[Dict[str, Any]], extra_payload: Optional[Dict[str, Any]] = None,
):
    from backend.llm_client import STATUS_PROVIDER_ERROR, NormalizedLLMResponse, call_llm_normalized

    opts = dict(provider_options or {})
    opts["provider"] = provider
    start = time.time()
    try:
        return await call_llm_normalized(
            api_base=api_base, api_key=api_key, model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, tools=tools, timeout=timeout,
            max_retries=max_retries, client=client, stream_callback=stream_callback,
            provider_options=opts, extra_payload=extra_payload,
        )
    except Exception as exc:  # a tier misbehaving must not take the whole turn down
        logger.warning("Tier call to %s raised (%s); treating as a failed attempt", api_base, exc)
        return NormalizedLLMResponse(
            status=STATUS_PROVIDER_ERROR, provider=provider, model=model,
            latency_ms=int((time.time() - start) * 1000), error_message=str(exc),
        )


def _log(tier_id: Optional[str], label: str, rank: int, kind: str, normalized,
          provider_binding_id: Optional[str] = None) -> None:
    try:
        from backend import router_usage

        success = normalized.status not in _failure_statuses()
        input_tokens = normalized.usage.input_tokens if normalized.usage else None
        output_tokens = normalized.usage.output_tokens if normalized.usage else None
        cost_usd = 0.0
        if kind == "binding" and success:
            from backend.cost import calculate_cost
            cost_usd = calculate_cost(
                normalized.model, input_tokens or 0, output_tokens or 0, provider_id=provider_binding_id,
            )
        router_usage.log_usage(
            tier_id=tier_id, tier_label=label, tier_rank=rank, kind=kind, success=success,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd,
            latency_ms=normalized.latency_ms, error=normalized.error_message,
        )
    except Exception:
        logger.exception("Failed to log router usage (non-fatal)")


def _filter_tiers_by_allowlist(
    tiers: List[Dict[str, Any]], allowed_binding_ids: Optional[Set[str]],
) -> List[Dict[str, Any]]:
    """Narrows the resolved tier list to a specific agent's allowed provider
    set (see backend/agent_provider_access.py). None = no narrowing (the
    default, full global chain); local tiers never get filtered out — they
    cost nothing and are always available."""
    if allowed_binding_ids is None:
        return tiers
    return [t for t in tiers if t["kind"] == "local" or t.get("provider_binding_id") in allowed_binding_ids]


async def route_llm_call(
    *,
    route_state: Dict[str, Any],
    local_api_base: str,
    local_api_key: str,
    local_model: str,
    local_provider: str,
    messages: List[Dict[str, Any]],
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
    client: Any = None,
    stream_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    provider_options: Optional[Dict[str, Any]] = None,
    allowed_binding_ids: Optional[Set[str]] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
):
    """Drop-in replacement for call_llm_normalized at an agent's primary call
    site. `route_state` is a plain dict the caller creates once per user turn
    (empty `{}`) and passes to every call within that turn's tool loop, so a
    turn stays pinned to whichever tier first succeeded instead of
    re-classifying (and potentially switching provider mid-tool-loop) on
    every iteration. After a call, `route_state['resolved']` holds the
    {api_base, api_key, model, provider} actually used, for any secondary
    call site (e.g. an empty-response retry) that must target the same tier.

    `allowed_binding_ids` narrows which tiers this particular agent may use
    (see backend/agent_provider_access.py) — None (the default) means no
    narrowing, the full global chain, unchanged from before this parameter
    existed.
    """
    from backend import router_tiers

    async def _call(api_base: str, api_key: str, model: str, provider: str):
        return await _attempt(
            api_base=api_base, api_key=api_key, model=model, provider=provider, messages=messages,
            temperature=temperature, max_tokens=max_tokens, tools=tools, timeout=timeout,
            max_retries=max_retries, client=client, stream_callback=stream_callback,
            provider_options=provider_options, extra_payload=extra_payload,
        )

    def _pin(api_base: str, api_key: str, model: str, provider: str, tier_id: Optional[str], label: str,
             rank: int, provider_binding_id: Optional[str]) -> None:
        route_state["decided"] = True
        route_state["resolved"] = {"api_base": api_base, "api_key": api_key, "model": model, "provider": provider}
        route_state["tier"] = {"id": tier_id, "label": label, "rank": rank, "provider_binding_id": provider_binding_id}

    # Already pinned for this turn — skip the tier-list/classification work
    # entirely on the common case (a healthy tier keeps answering) so a long
    # tool loop doesn't repeat a SQLite+Valkey round trip on every iteration.
    if route_state.get("decided") and route_state.get("resolved"):
        resolved = route_state["resolved"]
        tier_meta = route_state.get("tier") or {"id": None, "label": "local", "rank": 0, "provider_binding_id": None}
        result = await _call(resolved["api_base"], resolved["api_key"], resolved["model"], resolved["provider"])
        _log(tier_meta["id"], tier_meta["label"], tier_meta["rank"],
             "local" if tier_meta["rank"] == 0 else "binding", result, tier_meta.get("provider_binding_id"))
        if result.status in _failure_statuses() and tier_meta["rank"] > 0:
            # The pinned external tier degraded mid-turn — escalate onward
            # from here instead of surfacing an error for the rest of the turn.
            next_tiers = _filter_tiers_by_allowlist(router_tiers.list_active_tiers_resolved(), allowed_binding_ids)
            return await _escalate_and_finish(
                route_state, next_tiers, start_rank=tier_meta["rank"] + 1,
                local_api_base=local_api_base, local_api_key=local_api_key,
                local_model=local_model, local_provider=local_provider, call=_call, pin=_pin,
            )
        return result

    tiers = _filter_tiers_by_allowlist(router_tiers.list_active_tiers_resolved(), allowed_binding_ids)
    if not tiers:
        result = await _call(local_api_base, local_api_key, local_model, local_provider)
        _log(None, "local", 0, "local", result)
        _pin(local_api_base, local_api_key, local_model, local_provider, None, "local", 0, None)
        return result

    text = _last_user_text(messages)
    delegate = await _should_delegate(text, local_api_base, local_api_key, local_model, local_provider)

    if not delegate:
        result = await _call(local_api_base, local_api_key, local_model, local_provider)
        _log(None, "local", 0, "local", result)
        _pin(local_api_base, local_api_key, local_model, local_provider, None, "local", 0, None)
        return result

    return await _escalate_and_finish(
        route_state, tiers, start_rank=1,
        local_api_base=local_api_base, local_api_key=local_api_key,
        local_model=local_model, local_provider=local_provider, call=_call, pin=_pin,
    )


async def _escalate_and_finish(
    route_state: Dict[str, Any], tiers: List[Dict[str, Any]], start_rank: int, *,
    local_api_base: str, local_api_key: str, local_model: str, local_provider: str,
    call: Callable[[str, str, str, str], Awaitable[Any]],
    pin: Callable[..., None],
):
    from backend import router_usage

    for tier in tiers:
        if tier["tier_rank"] < start_rank:
            continue
        quota = router_usage.tier_quota_status(tier["id"], tier.get("quota_limit"), tier["quota_window_hours"])
        if quota["exhausted"]:
            continue
        if tier["kind"] == "local":
            api_base, api_key, model, provider = local_api_base, local_api_key, tier.get("model_override") or local_model, local_provider
        else:
            api_base, api_key, model, provider = tier["api_base"], tier["api_key"], tier.get("model_override") or "", "openai_compatible"
            if not model:
                logger.warning("Tier '%s' has no model configured; skipping", tier["label"])
                continue
        provider_binding_id = tier.get("provider_binding_id") if tier["kind"] == "binding" else None
        result = await call(api_base, api_key, model, provider)
        _log(tier["id"], tier["label"], tier["tier_rank"], tier["kind"], result, provider_binding_id)
        if result.status not in _failure_statuses():
            pin(api_base, api_key, model, provider, tier["id"], tier["label"], tier["tier_rank"], provider_binding_id)
            return result

    # Every configured tier failed or is quota-exhausted — never leave the
    # user stuck, fall back to the always-available local model.
    result = await call(local_api_base, local_api_key, local_model, local_provider)
    _log(None, "local", 0, "local", result)
    pin(local_api_base, local_api_key, local_model, local_provider, None, "local", 0, None)
    return result
