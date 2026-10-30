"""Cookie/CSRF and WebSocket authentication share the same session contract."""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from backend import auth, auth_state

SESSION_COOKIE = "hermes_session"
CSRF_COOKIE = "hermes_csrf"


def connection_token(connection) -> str:
    header = connection.headers.get("Authorization", connection.headers.get("authorization", ""))
    if header.startswith("Bearer "):
        return header.partition(" ")[2].strip()
    return getattr(connection, "cookies", {}).get(SESSION_COOKIE, "")


def origin_allowed(connection) -> bool:
    origin = connection.headers.get("origin")
    if not origin:
        return connection.headers.get("sec-fetch-site") != "cross-site"
    scheme = "https" if connection.url.scheme in ("https", "wss") else "http"
    host = connection.headers.get('host', '')
    own_origin = f"{scheme}://{host}"
    allowed = {x.strip().rstrip("/") for x in os.getenv("HERMES_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    # TLS may terminate at nginx. The Host still names the browser's origin;
    # forwarded headers supplied by arbitrary callers are never trusted.
    return origin.rstrip("/") in allowed | {own_origin, f"https://{host}"}


def csrf_valid(request, data: dict) -> bool:
    expected = str(data.get("csrf") or "")
    supplied = request.headers.get("x-csrf-token", "")
    return bool(expected and secrets.compare_digest(expected.encode(), supplied.encode()) and origin_allowed(request))


def login_response(token: str) -> JSONResponse:
    data = auth.session_data(token, touch=False)
    if not data:
        raise auth_state.AuthUnavailable("Session could not be persisted")
    response = JSONResponse({"status": "success", "csrf": data["csrf"], "expires_at": data["expires"]})
    secure = os.getenv("HERMES_AUTH_SECURE_COOKIE", "true").lower() != "false"
    response.set_cookie(SESSION_COOKIE, token, max_age=auth_state.SESSION_TTL,
                        httponly=True, secure=secure, samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, data["csrf"], max_age=auth_state.SESSION_TTL,
                        httponly=False, secure=secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


def require_recent_auth(request) -> None:
    token = connection_token(request)
    data = auth.session_data(token, touch=False)
    if not data:
        raise HTTPException(status_code=401, detail="Session expired")
    if data["role"] != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    if not data.get("fresh") or data["now"] - data["fresh"] >= 300:
        raise HTTPException(status_code=428, detail={"code": "reauth_required",
                                                   "message": "Confirm your password and Telegram code."})


async def websocket_session(connection) -> tuple[str, dict | None]:
    if not origin_allowed(connection):
        return "", None
    token = connection_token(connection)
    # Old non-browser clients may send their server-issued token this way.
    if not token:
        protocols = connection.headers.get("sec-websocket-protocol", "").split(",")
        token = next((p.strip()[12:] for p in protocols if p.strip().startswith("hermes-auth.")), "")
    try:
        data = await asyncio.to_thread(auth.session_data, token, touch=False)
    except auth_state.AuthUnavailable:
        return "", None
    return token, data


async def watch_websocket_session(connection, token: str) -> None:
    """Revoke idle live streams as well as future REST requests."""
    while True:
        await asyncio.sleep(5)
        try:
            valid = await asyncio.to_thread(auth.validate_session, token, touch=False)
        except auth_state.AuthUnavailable:
            valid = False
        if not valid:
            with contextlib.suppress(Exception):
                await connection.close(code=1008)
            return
