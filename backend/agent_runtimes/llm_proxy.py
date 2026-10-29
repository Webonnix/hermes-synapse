"""OpenAI-compatible door from agent desktops to the local model (Strata).

CLI agents in desktops (OpenCode, and later Goose / Qwen Code) speak the
OpenAI chat API. This is that API, served by the backend on the internal
dev-net, so an agent can work on the server's own model:

* **Auth per desktop.** The bearer token is ``<key>.<hmac>`` from
  desktop.llm_token(); the key says whose it is and the HMAC proves it, so a
  desktop cannot spend under another's name. Not the dashboard session and
  not LLM_PROXY_TOKEN: a leaked desktop token reaches this endpoint and only
  this one. The path is outside /api/ (main.py's session middleware does not
  apply) and nginx does not route it, so it is reachable only from the
  backend's own networks.
* **The shared queue.** Every request waits in backend.llm_gate at
  PRIORITY_BACKGROUND, and holds the slot until its stream ends: Vexa's chat
  still goes first, and agents never run beside it on a server configured for
  one generation at a time.
* **Limits.** One model (the deployment's local one), capped max_tokens, one
  request in flight per desktop, bounded request size.

Usage is counted per desktop (``usage_by_desktop``) and logged as metadata —
never prompts or completions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, Optional

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

_locks: Dict[str, asyncio.Lock] = {}
usage_by_desktop: Dict[str, Dict[str, int]] = {}


def model_id() -> str:
    return os.getenv("AGENT_LLM_MODEL") or os.getenv("LLM_MODEL") or "local"


def _max_tokens() -> int:
    return max(256, int(os.getenv("AGENT_LLM_MAX_TOKENS", "8192")))


def _queue_timeout() -> float:
    return float(os.getenv("AGENT_LLM_QUEUE_TIMEOUT", "600"))


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


def _record(key: str, usage: Optional[Dict[str, Any]], started: float) -> None:
    totals = usage_by_desktop.setdefault(key, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
    totals["requests"] += 1
    prompt = int((usage or {}).get("prompt_tokens") or 0)
    completion = int((usage or {}).get("completion_tokens") or 0)
    totals["prompt_tokens"] += prompt
    totals["completion_tokens"] += completion
    logger.info("agent-llm %s: %d prompt + %d completion tokens in %.1fs",
                key, prompt, completion, time.monotonic() - started)


def _http_client() -> httpx.AsyncClient:
    # No read timeout: a long generation is normal; the agent's disconnect or
    # the turn's cancel ends it.
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0))


def _upstream() -> tuple[str, Dict[str, str]]:
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("LLAMACPP_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return f"{normalize_base_url()}/v1/chat/completions", headers


@router.get("/agent-llm/v1/models")
async def list_models(request: Request):
    try:
        _authenticate(request)
    except HTTPException as exc:
        return _error(exc.status_code, str(exc.detail), "unauthorized" if exc.status_code == 401 else "unavailable")
    return {"object": "list", "data": [{"id": model_id(), "object": "model", "owned_by": "hermes"}]}


@router.post("/agent-llm/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        key = _authenticate(request)
    except HTTPException as exc:
        return _error(exc.status_code, str(exc.detail), "unauthorized" if exc.status_code == 401 else "unavailable")

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(413, "request too large", "request_too_large")
    try:
        payload = json.loads(raw)
    except ValueError:
        return _error(400, "body is not JSON", "invalid_request")
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return _error(400, "messages are required", "invalid_request")
    if payload.get("model") != model_id():
        return _error(404, f"model {payload.get('model')!r} is not served here; use {model_id()!r}", "model_not_found")

    # Cap rather than refuse: SDKs send their own defaults, often the model's
    # whole context, which would let one call hold the GPU for an hour.
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

    lock = _locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        return _error(429, "this desktop already has a request in flight", "rate_limited")
    await lock.acquire()
    gate = llamacpp_gate()
    try:
        await gate.acquire(PRIORITY_BACKGROUND, _queue_timeout())
    except GateTimeout:
        lock.release()
        return _error(503, "the local model is busy; try again later", "model_busy")
    except BaseException:
        lock.release()
        raise

    released = False

    def release() -> None:
        # Idempotent: the stream's finally and the response's background task
        # both call it, because neither alone covers every exit (an agent that
        # disconnects before the first byte never starts the stream at all).
        nonlocal released
        if not released:
            released = True
            gate.release()
            lock.release()

    started = time.monotonic()
    url, headers = _upstream()
    client = _http_client()

    if not stream:
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return _error(502, f"local model unreachable ({type(exc).__name__})", "upstream_error")
        finally:
            await client.aclose()
            release()
        try:
            data = response.json()
        except ValueError:
            return _error(502, "local model returned no JSON", "upstream_error")
        if response.status_code < 400:
            _record(key, data.get("usage") if isinstance(data, dict) else None, started)
        return JSONResponse(status_code=response.status_code, content=data)

    try:
        upstream = await client.send(client.build_request("POST", url, json=payload, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        release()
        return _error(502, f"local model unreachable ({type(exc).__name__})", "upstream_error")
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        release()
        return JSONResponse(status_code=upstream.status_code,
                            content=_json_or_error(body, upstream.status_code))

    usage: Optional[Dict[str, Any]] = None
    recorded = False

    async def finish() -> None:
        nonlocal recorded
        await upstream.aclose()
        await client.aclose()
        release()
        if not recorded:
            recorded = True
            _record(key, usage, started)

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
    return {"error": {"message": f"local model error {status}", "type": "upstream_error", "code": "upstream_error"}}
