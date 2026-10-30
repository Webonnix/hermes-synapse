import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from backend import auth, auth_state, bot, main, database, permissions
from backend.auth_http import SESSION_COOKIE, csrf_valid, require_recent_auth


@pytest.fixture(autouse=True)
def state(monkeypatch):
    for field, value in (("active_sessions", set()), ("session_roles", {}), ("_session_meta", {}),
                         ("current_otp", {}), ("_auth_attempts", {}), ("_last_otp_request", None)):
        monkeypatch.setattr(auth, field, value)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(bot, "telegram_app", SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())))
    monkeypatch.setattr(database, "get_setting", lambda key, *args: None)


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://test") as c:
        yield c


async def sign_in(client):
    code = auth.generate_otp()
    response = await client.post("/api/auth/verify-code", json={"code": code})
    assert response.status_code == 200
    return response


@pytest.mark.asyncio
async def test_cookie_is_httponly_secure_strict_and_no_token_in_body(client, monkeypatch):
    monkeypatch.setenv("HERMES_AUTH_SECURE_COOKIE", "true")
    response = await sign_in(client)
    cookie = response.headers.get_list("set-cookie")[0].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "max-age=43200" in cookie
    assert "token" not in response.json()
    assert (await client.get("/api/auth/session")).json()["authenticated"]


@pytest.mark.asyncio
async def test_cookie_mutations_need_csrf_and_matching_origin(client):
    response = await sign_in(client)
    url = "/api/runs/absent/cancel"
    assert (await client.post(url)).status_code == 403
    headers = {"X-CSRF-Token": response.json()["csrf"], "Origin": "https://evil.example"}
    assert (await client.post(url, headers=headers)).status_code == 403
    headers["Origin"] = "https://test"
    assert (await client.post(url, headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_logout_revokes_server_session(client):
    response = await sign_in(client)
    token = client.cookies[SESSION_COOKIE]
    assert (await client.post("/api/auth/logout", headers={"X-CSRF-Token": response.json()["csrf"]})).status_code == 200
    assert not auth.validate_session(token)
    assert (await client.get("/api/auth/session")).status_code == 401


def test_idle_expiry_and_absolute_cap(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    token = auth.create_session()
    now[0] += auth_state.IDLE_TTL
    assert not auth.validate_session(token)
    token = auth.create_session()
    for _ in range(48):
        now[0] += 900
        if now[0] < auth._session_meta[token]["expires"]:
            assert auth.validate_session(token)
        else:
            assert not auth.validate_session(token)
            break


def test_cookie_role_is_server_owned_and_unknown_tokens_deny():
    token = auth.create_session("viewer")
    request = SimpleNamespace(headers={}, cookies={SESSION_COOKIE: token})
    assert permissions.role_from_request(request) == "viewer"
    assert auth.role_for_session("unknown") == "nobody"
    assert auth.role_for_session(None) == "nobody"


def test_revoke_all_and_fresh_auth_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    token = auth.create_session()
    request = SimpleNamespace(headers={"Authorization": "Bearer " + token})
    with pytest.raises(Exception) as error:
        require_recent_auth(request)
    assert error.value.status_code == 428
    assert auth.mark_session_fresh(token)
    require_recent_auth(request)
    now[0] += 300
    with pytest.raises(Exception) as error:
        require_recent_auth(request)
    assert error.value.status_code == 428
    auth.revoke_all_sessions()
    assert not auth.validate_session(token)


@pytest.mark.asyncio
async def test_backend_outage_never_accepts_memory_token(client, monkeypatch):
    token = auth.create_session()
    monkeypatch.setattr(auth, "_test_memory", False)
    def unavailable(*args, **kwargs):
        raise auth_state.AuthUnavailable("offline")
    monkeypatch.setattr(auth_state, "read", unavailable)
    response = await client.get("/api/status", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_password_and_new_otp_are_required_for_fresh_auth(client, monkeypatch):
    response = await sign_in(client)
    headers = {"X-CSRF-Token": response.json()["csrf"]}
    monkeypatch.setattr(auth, "_last_otp_request", None)
    code = auth.generate_otp()
    password_hash, salt = auth.hash_password("correct-password")
    monkeypatch.setattr(database, "get_setting", lambda key: {"admin_password_hash": password_hash, "admin_password_salt": salt}.get(key))
    verified = await client.post("/api/auth/reauth", headers=headers, json={"code": code, "password": "correct-password"})
    assert verified.status_code == 200
    require_recent_auth(SimpleNamespace(headers={}, cookies={SESSION_COOKIE: client.cookies[SESSION_COOKIE]}))
