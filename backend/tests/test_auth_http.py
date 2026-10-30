"""Exercise the real HTTP middleware and routes without starting services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from backend import auth, bot, database, main


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def api(monkeypatch):
    monkeypatch.setattr(auth, "current_otp", {})
    monkeypatch.setattr(auth, "_last_otp_request", None)
    monkeypatch.setattr(auth, "_auth_attempts", {})
    monkeypatch.setattr(auth, "active_sessions", set())
    monkeypatch.setattr(auth, "session_roles", {})
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    send = AsyncMock()
    monkeypatch.setattr(bot, "telegram_app", SimpleNamespace(bot=SimpleNamespace(send_message=send)))
    # Even password tests must never read the installation's credentials.
    monkeypatch.setattr(database, "get_setting", lambda _key, *args: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        yield client, send


async def test_repeat_request_preserves_code_and_sends_only_once(api):
    client, send = api
    assert (await client.post("/api/auth/request-code")).json()["status"] == "success"
    code = auth.current_otp["code"]
    response = await client.post("/api/auth/request-code")
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 300
    assert response.headers["Cache-Control"] == "no-store"
    assert auth.current_otp["code"] == code
    assert send.await_count == 1
    verified = await client.post("/api/auth/verify-code", json={"code": code})
    assert verified.status_code == 200
    assert "token" not in verified.json()
    assert auth.validate_session(client.cookies["hermes_session"])
    assert verified.headers["Cache-Control"] == "no-store"
    assert (await client.post("/api/auth/verify-code", json={"code": code})).status_code == 401


async def test_http_guesses_exhaust_code(api):
    client, _send = api
    await client.post("/api/auth/request-code")
    code = auth.current_otp["code"]
    for _ in range(5):
        assert (await client.post("/api/auth/verify-code", json={"code": "000000"})).status_code == 401
    assert (await client.post("/api/auth/verify-code", json={"code": code})).status_code == 401


async def test_bad_payloads_count_and_forwarded_ips_cannot_reset_budget(api):
    client, _send = api
    for index in range(20):
        response = (await client.post("/api/auth/verify-code", json={}, headers={"X-Forwarded-For": f"192.0.2.{index}"}))
        assert response.status_code == 422
    response = (await client.post("/api/auth/verify-code", json={"code": "000000"}))
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["Cache-Control"] == "no-store"


async def test_password_limit_runs_before_hashing(api, monkeypatch):
    client, _send = api
    monkeypatch.setattr(database, "get_setting", lambda _key: "configured")
    checks = []
    monkeypatch.setattr(auth, "verify_password", lambda *args: checks.append(args) or False)
    for _ in range(5):
        assert (await client.post("/api/auth/login", json={"username": "configured", "password": "wrong"})).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "configured", "password": "wrong"})).status_code == 429
    assert len(checks) == 5
    assert (await client.post("/api/auth/request-code")).status_code == 200


async def test_delivery_error_revokes_code_without_logging_exception_body(api, caplog):
    client, send = api
    send.side_effect = RuntimeError("secret-in-provider-error")
    response = await client.post("/api/auth/request-code")
    assert response.json()["status"] == "error"
    assert not auth.current_otp
    assert "secret-in-provider-error" not in caplog.text
    assert (await client.post("/api/auth/request-code")).status_code == 429
    assert send.await_count == 1


async def test_missing_bot_does_not_issue_code(api, monkeypatch):
    client, _send = api
    monkeypatch.setattr(bot, "telegram_app", None)
    assert (await client.post("/api/auth/request-code")).json()["status"] == "error"
    assert not auth.current_otp
    assert auth._last_otp_request is None


async def test_missing_chat_id_does_not_issue_code(api, monkeypatch):
    client, send = api
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert (await client.post("/api/auth/request-code")).json()["status"] == "error"
    assert not auth.current_otp
    send.assert_not_awaited()


async def test_cancelled_delivery_revokes_code(api):
    _client, send = api
    send.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await main.request_code()
    assert not auth.current_otp
    with pytest.raises(auth.OTPRequestThrottled):
        auth.generate_otp()


async def test_oversized_credentials_are_rejected(api):
    client, _send = api
    assert (await client.post("/api/auth/verify-code", json={"code": "1" * 65})).status_code == 422
    assert (await client.post("/api/auth/login", json={"username": "x" * 257, "password": "p"})).status_code == 422
    assert (await client.post("/api/auth/login", json={"username": "x", "password": "p" * 4097})).status_code == 422


async def test_password_changes_cannot_create_unusable_credentials(api):
    client, _send = api
    headers = {"Authorization": f"Bearer {auth.create_session()}"}
    for field, value in (("new_username", "x" * 257), ("new_password", "p" * 4097),
                         ("current_password", "p" * 4097)):
        payload = {"new_username": "owner", "new_password": "a-valid-password", field: value}
        response = await client.post("/api/auth/change-password", json=payload, headers=headers)
        assert response.status_code == 422


async def test_preflight_does_not_consume_auth_budget(api):
    client, _send = api
    for _ in range(25):
        response = (await client.options("/api/auth/login", headers={
            "Origin": "http://localhost:9119", "Access-Control-Request-Method": "POST",
        }))
        assert response.status_code == 200
    assert not auth._auth_attempts
