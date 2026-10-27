"""Saved browser logins: the owner signs in to a site themselves, agents reuse it.

The browser itself lives in the browser-runner sidecar (browser_runner/server.py,
"Saved logins"). This module is the dashboard's door to it: list and delete
saved logins, and drive the remote login window (screenshots in, clicks and
keystrokes out). Every route sits under /api/, so the dashboard session auth
in main.py guards it; nothing here is reachable by an agent as a tool.

Typed text is forwarded and never logged — that is where passwords and SMS
codes go.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("hermes.browser_sessions")

router = APIRouter()

_LIST_TTL_SECONDS = 30.0
_list_cache: Dict[str, Any] = {"at": 0.0, "profiles": []}


def _runner() -> tuple[str, str]:
    if os.getenv("BROWSER_AGENT_ENABLED", "false").strip().lower() != "true":
        raise HTTPException(status_code=503, detail="Browser automation is disabled (BROWSER_AGENT_ENABLED).")
    token = os.getenv("BROWSER_RUNNER_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="BROWSER_RUNNER_TOKEN is not configured.")
    return os.getenv("BROWSER_RUNNER_URL", "http://browser-runner:8800").rstrip("/"), token


async def _call(method: str, path: str, *, json: Optional[Dict[str, Any]] = None, timeout: float = 45.0) -> Any:
    base, token = _runner()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{base}{path}", json=json,
                                            headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Browser runner is unreachable ({type(exc).__name__}).") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "")
        except Exception:
            detail = response.text[:300]
        raise HTTPException(status_code=response.status_code, detail=detail or "Browser runner error")
    return response.json()


def list_profiles_cached() -> List[Dict[str, Any]]:
    """Saved logins for the agent's prompt. Short cache and timeout: this runs
    on every turn that uses the browser, and a down runner must cost nothing."""
    now = time.monotonic()
    if now - _list_cache["at"] < _LIST_TTL_SECONDS:
        return _list_cache["profiles"]
    profiles: List[Dict[str, Any]] = []
    try:
        base, token = _runner()
        response = httpx.get(f"{base}/profiles", headers={"Authorization": f"Bearer {token}"}, timeout=2.0)
        if response.status_code < 400:
            profiles = list(response.json().get("profiles") or [])
    except Exception:
        profiles = []
    _list_cache.update({"at": now, "profiles": profiles})
    return profiles


def _forget_cache() -> None:
    _list_cache["at"] = 0.0


class LoginStartPayload(BaseModel):
    profile: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=2000)
    label: Optional[str] = Field(default=None, max_length=80)


class LoginInputPayload(BaseModel):
    type: Literal["click", "type", "key", "scroll", "navigate", "back"]
    x: Optional[float] = None
    y: Optional[float] = None
    text: Optional[str] = Field(default=None, max_length=500)
    key: Optional[str] = Field(default=None, max_length=20)
    dy: Optional[float] = None
    url: Optional[str] = Field(default=None, max_length=2000)


@router.get("/api/browser/sessions")
async def get_browser_sessions():
    return await _call("GET", "/profiles", timeout=10.0)


@router.delete("/api/browser/sessions/{name}")
async def delete_browser_session(name: str):
    result = await _call("DELETE", f"/profiles/{name}", timeout=20.0)
    _forget_cache()
    logger.warning("Saved browser login '%s' deleted by the owner", name)
    return result


@router.post("/api/browser/login/start")
async def start_browser_login(payload: LoginStartPayload):
    result = await _call("POST", "/login/start", json=payload.model_dump(), timeout=60.0)
    _forget_cache()
    logger.info("Login window opened for profile '%s'", payload.profile)
    return result


@router.get("/api/browser/login/frame")
async def browser_login_frame():
    return await _call("GET", "/login/frame", timeout=20.0)


@router.post("/api/browser/login/input")
async def browser_login_input(payload: LoginInputPayload):
    return await _call("POST", "/login/input", json=payload.model_dump(exclude_none=True), timeout=45.0)


@router.post("/api/browser/login/stop")
async def stop_browser_login():
    result = await _call("POST", "/login/stop", timeout=30.0)
    _forget_cache()
    return result
