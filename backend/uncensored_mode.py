"""Global switch for the local model's uncensored mode, and the door Cline uses to get it.

The local Strata engine can project its refusal direction out of the residual stream per request
(``experimental_speed_projection``). Strata itself stays on "off" for any request that leaves the field out,
so nothing reaches the uncensored model by accident. This module holds the one owner-facing switch:

* web chats (and their per-chat checkbox) are uncensored only while the switch is on;
* external OpenAI-compatible clients (Cline) reach the model through nginx ``/llm/`` -> here
  (``/llm-open/...``), where the field is added to every JSON request according to the switch.

Agents, bots and the browser runner never ask for it, so the switch cannot reach them.
"""

from __future__ import annotations

import hmac
import json
import logging
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from backend import database

logger = logging.getLogger("hermes.uncensored_mode")

SETTING_KEY = "uncensored_mode"
FIELD = "experimental_speed_projection"
_TRUE = ("1", "true", "on", "yes")
# Not forwarded to Strata: hop-by-hop headers, the caller's own credentials (Strata gets the service key).
_DROP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "authorization", "accept-encoding"}
_KEEP_RESPONSE_HEADERS = ("content-type", "cache-control")

router = APIRouter()


def enabled() -> bool:
    """Off unless the owner turned it on; any read failure also means off."""
    try:
        return str(database.get_setting(SETTING_KEY) or "").strip().lower() in _TRUE
    except Exception:
        logger.exception("Could not read the uncensored-mode setting; treating it as off")
        return False


def set_enabled(value: bool) -> bool:
    database.set_setting(SETTING_KEY, "1" if value else "0")
    return enabled()


class UncensoredModePayload(BaseModel):
    enabled: bool


@router.get("/api/uncensored-mode")
async def get_uncensored_mode():
    return {"enabled": enabled()}


@router.put("/api/uncensored-mode")
async def put_uncensored_mode(payload: UncensoredModePayload):
    return {"enabled": set_enabled(payload.enabled)}


@router.api_route("/llm-open/{path:path}", methods=["GET", "POST"])
async def llm_open(path: str, request: Request):
    """Token-gated pass-through to Strata that states the switch on every JSON request."""
    token = os.getenv("LLM_PROXY_TOKEN", "")
    given = request.headers.get("authorization", "")
    if not token or not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    body = await request.body()
    if request.method == "POST" and body:
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data[FIELD] = enabled()
            body = json.dumps(data).encode()

    base = (os.getenv("LLAMACPP_BASE_URL") or "http://hermes-llama:8080").rstrip("/")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_HEADERS}
    key = os.getenv("LLAMACPP_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    client = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0))
    try:
        upstream = await client.send(
            client.build_request(request.method, f"{base}/{path}", params=request.query_params,
                                 content=body, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("llm-open: Strata unreachable (%s)", exc)
        return JSONResponse(status_code=502, content={"detail": "The local model is unreachable"})

    async def _close() -> None:
        await upstream.aclose()
        await client.aclose()

    out = {k: v for k, v in upstream.headers.items() if k.lower() in _KEEP_RESPONSE_HEADERS}
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=out,
                             background=BackgroundTask(_close))
