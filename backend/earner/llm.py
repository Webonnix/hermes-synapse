"""The Earner's paths to a model: local llama.cpp, and — only when the
operator switched it on — one priced external model through OmniRoute.

Local is the default and is pinned: its endpoint is LLAMACPP_BASE_URL (or
EARNER_LLAMACPP_BASE_URL), and a host that is not local or on the private
compose network is refused. The chat's provider machinery (router tiers, agent
providers) is never used, so nothing here can fall through to a paid API on
its own.

The external path is explicit, not a silent fallback: it needs
``external_enabled`` in the Earner settings, a model whose price OmniRoute
publishes (an unpriced model is refused — there would be nothing to budget
against), and room in the monthly dollar cap, reserved before the call at that
price for the whole ``max_tokens``. Every use is recorded as an event and in
earner_llm_calls with its cost.

Every call:
* reserves budget first (budget.reserve_llm_call) — no reservation, no call;
* goes through backend.llm_gate at PRIORITY_BACKGROUND, so live chat is served
  first while llama.cpp has its single slot (--parallel 1);
* runs one at a time inside the Earner (``_earner_lock``);
* has reasoning off and a hard ``max_tokens``; no tools are ever sent;
* is never retried here — a failure fails the stage.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from backend.earner import budget

_LOCAL_NAMES = {"localhost", "hermes-llama", "llama", "llamacpp", "host.docker.internal"}


class InferenceUnavailable(RuntimeError):
    """The local model did not answer. Never followed by a fallback."""


class ModelOutputError(RuntimeError):
    """The model answered, but not with the JSON object the stage needs."""


class ExternalUnavailable(RuntimeError):
    """The external model may not be used right now (off, unpriced, no key)."""


def _base_url() -> str:
    from backend.llamacpp_client import normalize_base_url

    return normalize_base_url(os.getenv("EARNER_LLAMACPP_BASE_URL") or os.getenv("LLAMACPP_BASE_URL"))


def _model() -> str:
    return (os.getenv("EARNER_MODEL") or os.getenv("LLM_MODEL") or "").strip()


def _is_local_host(host: str) -> bool:
    host = (host or "").lower()
    if host in _LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def describe() -> Dict[str, Any]:
    base = _base_url()
    host = urlparse(base).hostname or ""
    model = _model()
    local = _is_local_host(host)
    reason = ""
    if not local:
        reason = (f"Earner закреплён за локальной моделью, а настроен адрес {host or base!r} — "
                  "внешние провайдеры для него запрещены")
    elif not model:
        local = False
        reason = "Не задано имя локальной модели (LLM_MODEL / EARNER_MODEL)"
    return {"provider": "llama.cpp", "host": host, "model": model, "local": local, "reason": reason}


_locks: Dict[int, Tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _earner_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _locks.get(id(loop))
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _locks.clear()
        _locks[id(loop)] = entry
    return entry[1]


def estimate_reservation(messages: List[Dict[str, Any]], max_tokens: int) -> int:
    chars = sum(len(str(message.get("content") or "")) for message in messages)
    return chars // 3 + 64 * len(messages) + max_tokens


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json_object(text: str) -> Dict[str, Any]:
    raw = _FENCE.sub("", (text or "").strip())
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        raw = raw[start:end + 1] if start >= 0 and end > start else raw
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ModelOutputError("модель вернула не JSON") from exc
    if not isinstance(value, dict):
        raise ModelOutputError("модель вернула не JSON-объект")
    return value


# ── external (OmniRoute) ─────────────────────────────────────────────────────

_PRICES: Dict[str, Any] = {"at": 0.0, "models": {}}
PRICE_SAFETY = 1.2  # reserve 20 % above the published price


def _router_base() -> str:
    return (os.getenv("ROUTER_API_BASE") or "http://omniroute:20128").rstrip("/")


def _router_key() -> str:
    from backend import database

    return database.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")


async def priced_models(refresh: bool = False) -> Dict[str, Dict[str, float]]:
    """{model_id: {"input": $/1M, "output": $/1M, "max_output_tokens": n}} as
    OmniRoute publishes it; cached for ten minutes."""
    import httpx

    if not refresh and _PRICES["models"] and time.monotonic() - _PRICES["at"] < 600:
        return _PRICES["models"]
    key = _router_key()
    if not key:
        raise ExternalUnavailable("не задан ключ OmniRoute (Настройки → API-ключи → AI Router)")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{_router_base()}/v1/models", headers={"Authorization": f"Bearer {key}"})
    if response.status_code != 200:
        raise ExternalUnavailable(f"OmniRoute не отдал список моделей (HTTP {response.status_code})")
    models: Dict[str, Dict[str, float]] = {}
    for item in response.json().get("data") or []:
        pricing = item.get("pricing") if isinstance(item, dict) else None
        if not isinstance(pricing, dict):
            continue
        try:
            price_in, price_out = float(pricing.get("input")), float(pricing.get("output"))
        except (TypeError, ValueError):
            continue
        if price_in < 0 or price_out < 0:
            continue
        models[str(item.get("id"))] = {"input": price_in, "output": price_out,
                                       "max_output_tokens": int(item.get("max_output_tokens") or 0)}
    _PRICES.update(at=time.monotonic(), models=models)
    return models


async def external_info() -> Dict[str, Any]:
    from backend.earner import store

    settings = store.get_settings()
    info: Dict[str, Any] = {"enabled": settings["external_enabled"], "model": settings["external_model"],
                            "month_usd": settings["external_month_usd"], "available": False, "reason": ""}
    if not settings["external_enabled"]:
        info["reason"] = "внешние модели выключены в настройках Earner"
        return info
    try:
        prices = await priced_models()
    except ExternalUnavailable as exc:
        info["reason"] = str(exc)
        return info
    except Exception as exc:
        info["reason"] = f"OmniRoute недоступен ({type(exc).__name__})"
        return info
    price = prices.get(settings["external_model"])
    if not price:
        info["reason"] = f"у модели {settings['external_model']} нет опубликованной цены — вызов запрещён"
        return info
    info.update(available=True, price_input=price["input"], price_output=price["output"])
    return info


async def _complete_external(*, stage: str, entity_id: str, messages: List[Dict[str, Any]], max_tokens: int,
                             temperature: float, reason: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    import httpx
    from backend.earner import store

    info = await external_info()
    if not info["available"]:
        raise ExternalUnavailable(info["reason"])
    reserved_tokens = estimate_reservation(messages, max_tokens)
    prompt_estimate = reserved_tokens - max_tokens
    reserved_usd = round((prompt_estimate * info["price_input"] + max_tokens * info["price_output"])
                         / 1_000_000 * PRICE_SAFETY, 6)
    call_id = budget.reserve_llm_call(entity_id, stage, reserved_tokens, info["model"])
    try:
        budget.reserve_external(call_id, reserved_usd)
    except budget.BudgetExceeded:
        budget.settle_llm_call(call_id, status="refused", prompt_tokens=0, completion_tokens=0, duration_ms=0,
                               error="external budget")
        raise
    store.log_event(None, "opportunity" if entity_id.startswith("opp-") else "llm", entity_id, "external_model",
                    f"Стадия {stage}: внешняя модель {info['model']} ({reason}); резерв ${reserved_usd:.4f}")
    started = time.monotonic()
    payload = {"model": info["model"], "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "response_format": {"type": "json_object"}, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=float(os.getenv("EARNER_EXTERNAL_TIMEOUT", "300"))) as client:
            response = await client.post(f"{_router_base()}/v1/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {_router_key()}"})
        if response.status_code != 200:
            raise ExternalUnavailable(f"внешняя модель ответила HTTP {response.status_code}")
        data = response.json()
    except (httpx.HTTPError, ValueError, ExternalUnavailable) as exc:
        duration = int((time.monotonic() - started) * 1000)
        budget.settle_llm_call(call_id, status="failed", prompt_tokens=None, completion_tokens=None,
                               duration_ms=duration, error=type(exc).__name__)
        budget.settle_external(call_id, reserved_usd, None)  # unknown usage: keep the reservation
        raise InferenceUnavailable(f"Внешняя модель недоступна: {exc}") from exc
    duration = int((time.monotonic() - started) * 1000)
    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    actual_usd = None
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        actual_usd = (prompt_tokens * info["price_input"] + completion_tokens * info["price_output"]) / 1_000_000
    choice = (data.get("choices") or [{}])[0]
    budget.settle_llm_call(call_id, status="ok", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                           duration_ms=duration, model=str(data.get("model") or info["model"]))
    budget.settle_external(call_id, reserved_usd, actual_usd)
    meta = {"call_id": call_id, "model": info["model"], "provider": "external", "duration_ms": duration,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "finish_reason": choice.get("finish_reason"), "cost_usd": actual_usd if actual_usd is not None else reserved_usd}
    if choice.get("finish_reason") == "length":
        raise ModelOutputError(f"ответ обрезан лимитом {max_tokens} токенов")
    content = ((choice.get("message") or {}).get("content")) or ""
    return parse_json_object(content), meta


async def complete_json(*, stage: str, entity_id: str, messages: List[Dict[str, Any]], max_tokens: int,
                        temperature: float = 0.2, provider: str = "local",
                        fallback_external: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``provider="external"`` asks for the external model up front (the
    caller decided the task needs it); ``fallback_external`` lets a failed
    local attempt (model down or unusable output) move to it once."""
    if provider == "external":
        return await _complete_external(stage=stage, entity_id=entity_id, messages=messages,
                                        max_tokens=max_tokens, temperature=temperature, reason="сложная задача")
    try:
        return await _complete_local(stage=stage, entity_id=entity_id, messages=messages,
                                     max_tokens=max_tokens, temperature=temperature)
    except (InferenceUnavailable, ModelOutputError) as exc:
        if not fallback_external:
            raise
        try:
            return await _complete_external(stage=stage, entity_id=entity_id, messages=messages,
                                            max_tokens=max_tokens, temperature=temperature,
                                            reason=f"локальная модель не справилась: {exc}")
        except ExternalUnavailable:
            raise exc from None


async def _complete_local(*, stage: str, entity_id: str, messages: List[Dict[str, Any]], max_tokens: int,
                          temperature: float = 0.2) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from backend.llamacpp_client import LlamaCppClient, LlamaCppError
    from backend.llm_gate import PRIORITY_BACKGROUND, GateTimeout

    info = describe()
    if not info["local"]:
        raise InferenceUnavailable(info["reason"])
    reserved = estimate_reservation(messages, max_tokens)
    call_id = budget.reserve_llm_call(entity_id, stage, reserved, info["model"])  # may raise BudgetExceeded
    try:
        timeout = float(os.getenv("EARNER_LLM_TIMEOUT", "900"))
        queue_timeout = float(os.getenv("EARNER_LLM_QUEUE_TIMEOUT", "1800"))
    except ValueError:
        timeout, queue_timeout = 900.0, 1800.0
    started = time.monotonic()
    try:
        async with _earner_lock():
            result = await LlamaCppClient(_base_url(), timeout=timeout).chat(
                model=info["model"],
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=None,
                # Reasoning off: with LOCAL_THINK the local Qwen can spend the
                # whole max_tokens before the JSON starts (same failure as the
                # agent-learning distiller, prod 2026-10-17).
                think=False,
                extra_payload={"response_format": {"type": "json_object"}},
                priority=PRIORITY_BACKGROUND,
                queue_timeout=queue_timeout,
            )
    except (LlamaCppError, GateTimeout, OSError) as exc:
        duration = int((time.monotonic() - started) * 1000)
        budget.settle_llm_call(call_id, status="failed", prompt_tokens=None, completion_tokens=None,
                               duration_ms=duration, error=type(exc).__name__)
        raise InferenceUnavailable(f"Локальная модель недоступна: {exc}") from exc
    except BaseException:
        budget.settle_llm_call(call_id, status="failed", prompt_tokens=None, completion_tokens=None,
                               duration_ms=int((time.monotonic() - started) * 1000), error="cancelled")
        raise
    duration = int((time.monotonic() - started) * 1000)
    budget.settle_llm_call(call_id, status="ok", prompt_tokens=result.prompt_tokens,
                           completion_tokens=result.completion_tokens, duration_ms=duration,
                           model=result.model or info["model"])
    meta = {"call_id": call_id, "model": result.model or info["model"], "provider": "local", "duration_ms": duration,
            "prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
            "finish_reason": result.done_reason}
    if result.done_reason == "length":
        raise ModelOutputError(f"ответ обрезан лимитом {max_tokens} токенов")
    return parse_json_object(result.content), meta
