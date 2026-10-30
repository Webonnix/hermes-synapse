"""OpenAI-compatible door from agent desktops to models: the local one (Strata)
and, when the owner allows it, the models connected in OmniRoute.

CLI agents in desktops (OpenCode) and OpenBot speak the OpenAI chat API. This
is that API, served by the backend on the internal dev-net:

* **Auth per desktop.** The bearer token is ``<key>.<hmac>`` from
  desktop.llm_token(); the key says whose it is and the HMAC proves it, so a
  desktop cannot spend under another's name. Not the dashboard session, not
  LLM_PROXY_TOKEN, not ROUTER_API_KEY: a leaked desktop token reaches this
  endpoint and only this one, and the router key never enters a desktop. The
  path is outside /api/ and net_guard.py serves it on the sandbox interface
  only.
* **The local model** (``model_id()``) waits in backend.llm_gate at
  PRIORITY_BACKGROUND and holds the slot until its stream ends: Vexa's chat
  still goes first.
* **OmniRoute models** (``omniroute/<id>``, the router's own /v1/models
  catalogue) cost money, so they are off unless AGENT_LLM_EXTERNAL_ENABLED,
  limited to AGENT_LLM_EXTERNAL_MODELS patterns, and budgeted per desktop per
  24 h — in dollars where cost.py knows the price, and in requests for every
  model, priced or not. Each call is logged to router_usage_log as
  ``agent-desktop:<key>``. Unlike Vexa's delegation, these calls do not pass
  the privacy gateway: a desktop's traffic is its agent's own work, and
  rewriting tool-calling conversations would break them — which is one more
  reason the switch is off by default.
* **Everywhere:** the Control Plane kill switch refuses every model; capped
  max_tokens; bounded request size; at most AGENT_LLM_PER_DESKTOP requests in
  flight per desktop (later ones wait, then 429).

Usage is logged as metadata — never prompts or completions.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from backend.agent_runtimes import desktop
from backend.llamacpp_client import normalize_base_url
from backend.llm_gate import PRIORITY_BACKGROUND, GateTimeout, llamacpp_gate

logger = logging.getLogger("hermes.agent_runtimes.llm_proxy")

router = APIRouter()

MAX_BODY_BYTES = 8 * 1024 * 1024
EXTERNAL_PREFIX = "omniroute/"
CATALOG_TTL_S = 300.0

_slots: Dict[str, asyncio.Semaphore] = {}
usage_by_desktop: Dict[str, Dict[str, int]] = {}
_catalog: Dict[str, Any] = {"at": 0.0, "ids": None}


def model_id() -> str:
    return os.getenv("AGENT_LLM_MODEL") or os.getenv("LLM_MODEL") or "local"


def _max_tokens() -> int:
    return max(256, int(os.getenv("AGENT_LLM_MAX_TOKENS", "8192")))


def _queue_timeout() -> float:
    return float(os.getenv("AGENT_LLM_QUEUE_TIMEOUT", "600"))


def _per_desktop() -> int:
    return max(1, int(os.getenv("AGENT_LLM_PER_DESKTOP", "1")))


def external_enabled() -> bool:
    return os.getenv("AGENT_LLM_EXTERNAL_ENABLED", "false").strip().lower() == "true"


def _external_patterns() -> List[str]:
    return [p.strip() for p in os.getenv("AGENT_LLM_EXTERNAL_MODELS", "*").split(",") if p.strip()]


def _router() -> tuple[str, str]:
    return (os.getenv("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/"),
            os.getenv("ROUTER_API_KEY", ""))


def _error(status: int, message: str, code: str) -> JSONResponse:
    # OpenAI's error shape, so the agent's SDK shows the reason instead of a parse error.
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": code, "code": code}})


def _authenticate(request: Request) -> str:
    if not desktop.enabled():
        raise HTTPException(status_code=503, detail="agent desktops are disabled")
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    key = desktop.verify_llm_token(token)
    if not key:
        raise HTTPException(status_code=401, detail="invalid desktop token")
    return key


def _http_client() -> httpx.AsyncClient:
    # No read timeout: a long generation is normal; the agent's disconnect or
    # the turn's cancel ends it.
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0))


def _killed() -> bool:
    from backend import control_plane

    try:
        return bool(control_plane.get_control_state()["kill_switch"])
    except Exception:
        logger.exception("agent-llm: cannot read the Control Plane state; refusing")
        return True


# ── OmniRoute catalogue ────────────────────────────────────────────────────

async def external_models(refresh: bool = False) -> Optional[List[str]]:
    """OmniRoute model ids this deployment lets desktops use; None when the
    router cannot be read (callers refuse rather than guess)."""
    if not external_enabled():
        return []
    now = time.monotonic()
    if not refresh and _catalog["ids"] is not None and now - _catalog["at"] < CATALOG_TTL_S:
        ids = _catalog["ids"]
    else:
        base, api_key = _router()
        try:
            async with _http_client() as client:
                response = await client.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {api_key}"},
                                            timeout=15.0)
            data = response.json().get("data") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("agent-llm: OmniRoute catalogue unavailable (%s)", type(exc).__name__)
            data = None
        if not isinstance(data, list):
            return _catalog["ids"] and [i for i in _catalog["ids"] if _allowed(i)]
        ids = sorted({str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")})
        _catalog.update(at=now, ids=ids)
    return [i for i in ids if _allowed(i)]


def _allowed(model: str) -> bool:
    return any(fnmatch.fnmatchcase(model, pattern) for pattern in _external_patterns())


def _spent_today(key: str) -> tuple[int, float]:
    from backend import router_usage

    router_usage._init_schema()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    with router_usage._connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM router_usage_log WHERE tier_label = ? AND ts >= ?",
            (f"agent-desktop:{key}", since),
        ).fetchone()
    return int(row[0]), float(row[1])


def _budget_refusal(key: str) -> Optional[str]:
    requests, dollars = _spent_today(key)
    max_requests = int(os.getenv("AGENT_LLM_EXTERNAL_DAILY_REQUESTS", "500"))
    max_dollars = float(os.getenv("AGENT_LLM_EXTERNAL_DAILY_USD", "5"))
    if requests >= max_requests:
        return f"daily OmniRoute request limit reached ({requests}/{max_requests})"
    if dollars >= max_dollars:
        return f"daily OmniRoute budget reached (${dollars:.2f}/${max_dollars:.2f})"
    return None


def _log_external(key: str, model: str, usage: Optional[Dict[str, Any]], started: float, error: str = "") -> None:
    from backend import router_usage

    prompt = int((usage or {}).get("prompt_tokens") or 0)
    completion = int((usage or {}).get("completion_tokens") or 0)
    cost = None
    try:
        from backend.cost import calculate_cost, is_model_priced

        if is_model_priced(model):
            cost = calculate_cost(model, prompt, completion)
    except Exception:
        logger.exception("agent-llm: cost calculation failed (non-fatal)")
    try:
        router_usage.log_usage(None, f"agent-desktop:{key}", 0, "agent_desktop", not error,
                               input_tokens=prompt, output_tokens=completion, cost_usd=cost,
                               latency_ms=int((time.monotonic() - started) * 1000), error=error or None)
    except Exception:
        # The request already ran; losing its log line must not fail it. The
        # request budget then undercounts, so say so loudly.
        logger.exception("agent-llm: could not log OmniRoute usage for %s", key)


def _record(key: str, model: str, usage: Optional[Dict[str, Any]], started: float) -> None:
    totals = usage_by_desktop.setdefault(key, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
    totals["requests"] += 1
    prompt = int((usage or {}).get("prompt_tokens") or 0)
    completion = int((usage or {}).get("completion_tokens") or 0)
    totals["prompt_tokens"] += prompt
    totals["completion_tokens"] += completion
    logger.info("agent-llm %s → %s: %d prompt + %d completion tokens in %.1fs",
                key, model, prompt, completion, time.monotonic() - started)


# ── routes ─────────────────────────────────────────────────────────────────

@dataclass
class _Target:
    url: str
    headers: Dict[str, str]
    model: str            # as the upstream knows it
    local: bool
    on_done: Callable[[Optional[Dict[str, Any]], float, str], None]


async def _target(key: str, requested: str) -> tuple[Optional[_Target], Optional[JSONResponse]]:
    if requested == model_id():
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("LLAMACPP_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return _Target(f"{normalize_base_url()}/v1/chat/completions", headers, requested, True,
                       lambda usage, started, error: None), None
    if requested.startswith(EXTERNAL_PREFIX) and external_enabled():
        upstream_model = requested[len(EXTERNAL_PREFIX):]
        catalogue = await external_models()
        if catalogue is None:
            return None, _error(503, "OmniRoute is unreachable; try again later", "upstream_error")
        if upstream_model not in catalogue:
            return None, _error(404, f"model {requested!r} is not allowed here", "model_not_found")
        refusal = await asyncio.to_thread(_budget_refusal, key)
        if refusal:
            return None, _error(429, refusal, "budget_exceeded")
        base, api_key = _router()
        return _Target(f"{base}/v1/chat/completions",
                       {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                       upstream_model, False,
                       lambda usage, started, error: _log_external(key, upstream_model, usage, started, error)), None
    return None, _error(404, f"model {requested!r} is not served here; see /v1/models", "model_not_found")


@router.get("/agent-llm/v1/models")
async def list_models(request: Request):
    try:
        _authenticate(request)
    except HTTPException as exc:
        return _error(exc.status_code, str(exc.detail), "unauthorized" if exc.status_code == 401 else "unavailable")
    data = [{"id": model_id(), "object": "model", "owned_by": "hermes"}]
    data += [{"id": f"{EXTERNAL_PREFIX}{model}", "object": "model", "owned_by": "omniroute"}
             for model in (await external_models() or [])]
    return {"object": "list", "data": data}


@router.post("/agent-llm/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        key = _authenticate(request)
    except HTTPException as exc:
        return _error(exc.status_code, str(exc.detail), "unauthorized" if exc.status_code == 401 else "unavailable")
    if await asyncio.to_thread(_killed):
        return _error(503, "the Control Plane is stopped (kill switch)", "killed")

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(413, "request too large", "request_too_large")
    try:
        payload = json.loads(raw)
    except ValueError:
        return _error(400, "body is not JSON", "invalid_request")
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return _error(400, "messages are required", "invalid_request")
    target, refusal = await _target(key, str(payload.get("model") or ""))
    if refusal is not None:
        return refusal
    assert target is not None
    payload["model"] = target.model

    # Cap rather than refuse: SDKs send their own defaults, often the model's
    # whole context, which would let one call hold the GPU (or the budget) for an hour.
    limit = _max_tokens()
    requested = payload.pop("max_completion_tokens", None)
    requested = payload.get("max_tokens", requested)
    try:
        requested = int(requested) if requested is not None else limit
    except (TypeError, ValueError):
        requested = limit
    payload["max_tokens"] = max(1, min(requested, limit))
    stream = bool(payload.get("stream"))
    if stream:
        payload["stream_options"] = {"include_usage": True}

    slot = _slots.setdefault(key, asyncio.Semaphore(_per_desktop()))
    try:
        await asyncio.wait_for(slot.acquire(), _queue_timeout())
    except asyncio.TimeoutError:
        return _error(429, "this desktop already has the maximum requests in flight", "rate_limited")
    gate = llamacpp_gate() if target.local else None
    if gate is not None:
        try:
            await gate.acquire(PRIORITY_BACKGROUND, _queue_timeout())
        except GateTimeout:
            slot.release()
            return _error(503, "the local model is busy; try again later", "model_busy")
        except BaseException:
            slot.release()
            raise

    released = False

    def release() -> None:
        # Idempotent: the stream's finally and the response's background task
        # both call it, because neither alone covers every exit (an agent that
        # disconnects before the first byte never starts the stream at all).
        nonlocal released
        if not released:
            released = True
            if gate is not None:
                gate.release()
            slot.release()

    started = time.monotonic()
    client = _http_client()

    if not stream:
        try:
            response = await client.post(target.url, json=payload, headers=target.headers)
        except httpx.HTTPError as exc:
            target.on_done(None, started, type(exc).__name__)
            return _error(502, f"model unreachable ({type(exc).__name__})", "upstream_error")
        finally:
            await client.aclose()
            release()
        try:
            data = response.json()
        except ValueError:
            target.on_done(None, started, "no JSON")
            return _error(502, "model returned no JSON", "upstream_error")
        usage = data.get("usage") if isinstance(data, dict) else None
        error = "" if response.status_code < 400 else f"HTTP {response.status_code}"
        target.on_done(usage, started, error)
        if not error:
            _record(key, target.model, usage, started)
        return JSONResponse(status_code=response.status_code, content=data)

    try:
        upstream = await client.send(client.build_request("POST", target.url, json=payload, headers=target.headers),
                                     stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        release()
        target.on_done(None, started, type(exc).__name__)
        return _error(502, f"model unreachable ({type(exc).__name__})", "upstream_error")
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        release()
        target.on_done(None, started, f"HTTP {upstream.status_code}")
        return JSONResponse(status_code=upstream.status_code, content=_json_or_error(body, upstream.status_code))

    usage: Optional[Dict[str, Any]] = None
    recorded = False

    async def finish() -> None:
        nonlocal recorded
        await upstream.aclose()
        await client.aclose()
        release()
        if not recorded:
            recorded = True
            target.on_done(usage, started, "")
            _record(key, target.model, usage, started)

    async def relay() -> AsyncIterator[bytes]:
        nonlocal usage
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("data:") and '"usage"' in line:
                    try:
                        usage = json.loads(line[5:]).get("usage") or usage
                    except ValueError:
                        pass
                yield (line + "\n").encode("utf-8")
        finally:
            # Also runs when the agent disconnects mid-stream: closing the
            # upstream stops the generation and frees the slot for the next.
            await finish()

    return StreamingResponse(relay(), media_type="text/event-stream", background=BackgroundTask(finish))


def _json_or_error(body: bytes, status: int) -> Dict[str, Any]:
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    return {"error": {"message": f"model error {status}", "type": "upstream_error", "code": "upstream_error"}}
