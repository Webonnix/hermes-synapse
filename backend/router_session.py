"""9Router dashboard session management.

9Router's own dashboard API (GET /api/combos, GET /api/providers — its
connected upstream accounts, distinct from this app's own provider_bindings)
only accepts its dashboard session cookie, never a Bearer API key. This
module governs the one credential that unlocks it (the dashboard password)
and keeps a cached, auto-refreshing login session so the backend can read
that data without a human re-logging in on every request.

Confirmed against a live instance before building this: GET /api/combos and
GET /api/providers return 200 with a valid session cookie. GET /api/quota,
GET /api/usage, and every plausible variant (/api/dashboard/*, /api/stats,
/api/analytics/*, /api/usage/*, /api/quota/*) return 404 regardless of auth
method — that data isn't retrievable via any API in this version, so this
module doesn't attempt to proxy it; see GET /api/router/stats's dashboard_url
for the one place it's actually visible (9Router's own UI).

Credential handling mirrors provider_governance.py's proposal/approve shape:
the password is never written to the SQL DB, only to Valkey (internal-only),
under a short TTL until an owner approves it via the same Control Plane /
Telegram /approve flow used for external LLM provider bindings. A dashboard
password is more sensitive than a scoped API key — it grants full control
over 9Router itself (revoke keys, disconnect accounts, change settings) — so
it gets the same governance, not less.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("hermes.router_session")

_PASSWORD_KEY = "router_dashboard_password"
_PASSWORD_PENDING_KEY = "router_dashboard_password:pending"
_PASSWORD_PENDING_TASK_KEY = "router_dashboard_password:pending_task"
_SESSION_TOKEN_KEY = "router_dashboard_session"
_PENDING_TTL_SECONDS = 24 * 60 * 60
# Refresh a bit before the token's own expiry so a request never races a
# session that's valid when checked but expired by the time it reaches
# 9Router.
_REFRESH_SKEW_SECONDS = 60
# 9Router locks the password out after repeated failed attempts (observed:
# 5) — don't retry a failing login on every single incoming request.
_LOGIN_RETRY_COOLDOWN_SECONDS = 30

_last_login_attempt: float = 0.0


def _router_api_base() -> str:
    return os.getenv("ROUTER_API_BASE", "http://9router:20128").rstrip("/")


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Best-effort read of a JWT's `exp` claim without a JWT library or
    signature check — this is only ever used to decide when *our own* cached
    copy should be refreshed. 9Router is the one actually validating the
    token's signature and expiry on every request it receives."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def propose_router_password(password: str) -> Dict[str, Any]:
    """Validate + stage the 9Router dashboard password, opening one R3
    Telegram-approval task before it becomes usable for anything — same
    shape as provider_governance.create_binding_proposal."""
    clean = str(password).strip()
    if not clean:
        raise ValueError("password is required")
    if len(clean) > 512:
        raise ValueError("password is too long")

    from backend.control_plane import create_review_task
    from backend.valkey_client import set_value

    task = create_review_task(
        goal="Configure 9Router dashboard password for backend session access",
        arguments={
            "secret_policy": "Password stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "Password never appears in the SQL database or application logs",
            "Used only server-side to establish a 9Router dashboard session; never returned to the frontend",
            "Grants read access to 9Router's own combos/connected-accounts only — not agent LLM routing, "
            "which stays a separate, already-governed provider binding",
        ],
        rollback="DELETE /api/router/session-credential clears the stored password and any cached session immediately.",
        requester="autonomy:router_session",
    )
    set_value(_PASSWORD_PENDING_KEY, clean, ttl_seconds=_PENDING_TTL_SECONDS)
    set_value(_PASSWORD_PENDING_TASK_KEY, task["id"], ttl_seconds=_PENDING_TTL_SECONDS)
    return {"status": "awaiting_approval", "task_id": task["id"], "risk_class": "R3"}


def get_router_password_proposal(control_task_id: str) -> Optional[Dict[str, Any]]:
    """Used by approval_dispatch.py to route an approved task to this module
    instead of provider_governance/mcp_governance/etc."""
    from backend.valkey_client import get_value

    pending_task_id = get_value(_PASSWORD_PENDING_TASK_KEY)
    if pending_task_id == control_task_id:
        return {"control_task_id": control_task_id}
    return None


async def execute_approved_router_password(control_task_id: str) -> Dict[str, Any]:
    from backend.control_plane import finish_task, get_task, start_task
    from backend.valkey_client import get_value, set_value, delete_value

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Router password change is not approved")
    if get_value(_PASSWORD_PENDING_TASK_KEY) != control_task_id:
        raise KeyError("No pending router password proposal for this task")

    start_task(control_task_id)
    try:
        password = get_value(_PASSWORD_PENDING_KEY)
        if not password:
            raise RuntimeError("Password expired before approval — propose it again")
        set_value(_PASSWORD_KEY, password, ttl_seconds=None)
        delete_value(_PASSWORD_PENDING_KEY)
        delete_value(_PASSWORD_PENDING_TASK_KEY)
        delete_value(_SESSION_TOKEN_KEY)  # force a fresh login with the new password
        result = {"status": "active"}
    except Exception as exc:
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result))
    return result


def revoke_router_password() -> None:
    from backend.valkey_client import delete_value

    delete_value(_PASSWORD_KEY)
    delete_value(_PASSWORD_PENDING_KEY)
    delete_value(_PASSWORD_PENDING_TASK_KEY)
    delete_value(_SESSION_TOKEN_KEY)


def is_configured() -> bool:
    from backend.valkey_client import get_value

    return bool(get_value(_PASSWORD_KEY))


def has_pending_proposal() -> Optional[str]:
    """Returns the pending control_task_id if a proposal is awaiting approval, else None."""
    from backend.valkey_client import get_value

    return get_value(_PASSWORD_PENDING_TASK_KEY)


async def get_session_token(*, force_refresh: bool = False) -> Optional[str]:
    """Returns a valid `auth_token` for 9Router's dashboard API, logging in
    (and caching the result, TTL'd to the token's own expiry) as needed.
    None if no password is configured, login fails, or 9Router is
    unreachable — callers treat that as "session unavailable", not a hard
    error, since this whole feature is optional."""
    from backend.valkey_client import get_value, set_value

    global _last_login_attempt

    if not force_refresh:
        cached = get_value(_SESSION_TOKEN_KEY)
        if cached:
            exp = _decode_jwt_exp(cached)
            if exp is None or exp - _REFRESH_SKEW_SECONDS > time.time():
                return cached

    password = get_value(_PASSWORD_KEY)
    if not password:
        return None

    now = time.time()
    if now - _last_login_attempt < _LOGIN_RETRY_COOLDOWN_SECONDS:
        return get_value(_SESSION_TOKEN_KEY)
    _last_login_attempt = now

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{_router_api_base()}/api/auth/login",
                json={"password": password},
            )
    except httpx.HTTPError as exc:
        logger.warning("9Router login failed (network): %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning("9Router login failed: HTTP %s", resp.status_code)
        return None

    token = resp.cookies.get("auth_token")
    if not token:
        logger.warning("9Router login succeeded but returned no auth_token cookie")
        return None

    exp = _decode_jwt_exp(token)
    ttl = max(60, exp - int(time.time())) if exp else 3600
    set_value(_SESSION_TOKEN_KEY, token, ttl_seconds=ttl)
    return token


async def session_request(method: str, path: str, **kwargs: Any) -> Optional[httpx.Response]:
    """Authenticated request against 9Router's session-cookie dashboard API
    (distinct from the Bearer-key /v1/* surface used elsewhere). Retries
    once with a forced re-login on 401. Returns None if no session is
    available at all — not configured, or login is failing — so callers can
    render a clean "not connected" state instead of a raw error."""
    token = await get_session_token()
    if not token:
        return None

    async def _do(tok: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=5.0) as client:
            return await client.request(
                method, f"{_router_api_base()}{path}",
                cookies={"auth_token": tok}, **kwargs,
            )

    try:
        resp = await _do(token)
        if resp.status_code == 401:
            fresh = await get_session_token(force_refresh=True)
            if fresh and fresh != token:
                resp = await _do(fresh)
        return resp
    except httpx.HTTPError as exc:
        logger.warning("9Router session request failed: %s %s -> %s", method, path, exc)
        return None
