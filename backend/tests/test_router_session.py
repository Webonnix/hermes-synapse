"""Tests for backend/router_session.py — the governed credential + cached
session for 9Router's dashboard-only API (combos, connected accounts).

Valkey is faked with a plain in-memory dict rather than a real connection:
there's no existing Valkey-backed test convention in this suite (see
provider_governance.py, which is untested the same way), and the module's
own logic — TTL bookkeeping, JWT-exp-driven refresh, login cooldown — is
what's worth verifying here, not the Valkey client itself.
"""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend import control_plane, database, router_session as rs
from backend.approval_dispatch import execute_if_ready


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "control-plane.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    return db_path


@pytest.fixture()
def fake_valkey(monkeypatch):
    store: dict[str, str] = {}

    def _set(key, value, ttl_seconds=None):
        store[key] = value

    def _get(key):
        return store.get(key)

    def _delete(key):
        store.pop(key, None)

    monkeypatch.setattr("backend.valkey_client.set_value", _set)
    monkeypatch.setattr("backend.valkey_client.get_value", _get)
    monkeypatch.setattr("backend.valkey_client.delete_value", _delete)
    monkeypatch.setattr(rs, "_last_login_attempt", 0.0)
    return store


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def test_decode_jwt_exp_reads_the_claim():
    token = _make_jwt(1234567890)
    assert rs._decode_jwt_exp(token) == 1234567890


def test_decode_jwt_exp_tolerates_garbage():
    assert rs._decode_jwt_exp("not-a-jwt") is None
    assert rs._decode_jwt_exp("a.b.c") is None


@pytest.mark.asyncio
async def test_get_session_token_none_when_not_configured(fake_valkey):
    assert await rs.get_session_token() is None


@pytest.mark.asyncio
async def test_get_session_token_logs_in_and_caches(fake_valkey):
    fake_valkey[rs._PASSWORD_KEY] = "correct horse"
    token = _make_jwt(int(time.time()) + 3600)
    resp = MagicMock(status_code=200, cookies={"auth_token": token})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=resp)) as post:
        result = await rs.get_session_token()

    assert result == token
    assert fake_valkey[rs._SESSION_TOKEN_KEY] == token
    assert post.await_args.kwargs["json"] == {"password": "correct horse"}


@pytest.mark.asyncio
async def test_get_session_token_reuses_cached_token_without_a_login_call(fake_valkey):
    fake_valkey[rs._PASSWORD_KEY] = "correct horse"
    token = _make_jwt(int(time.time()) + 3600)
    fake_valkey[rs._SESSION_TOKEN_KEY] = token

    with patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
        result = await rs.get_session_token()

    assert result == token
    post.assert_not_called()


@pytest.mark.asyncio
async def test_get_session_token_refreshes_a_token_near_expiry(fake_valkey):
    fake_valkey[rs._PASSWORD_KEY] = "correct horse"
    stale = _make_jwt(int(time.time()) + 10)  # inside the refresh skew window
    fresh = _make_jwt(int(time.time()) + 3600)
    fake_valkey[rs._SESSION_TOKEN_KEY] = stale
    resp = MagicMock(status_code=200, cookies={"auth_token": fresh})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=resp)):
        result = await rs.get_session_token()

    assert result == fresh


@pytest.mark.asyncio
async def test_login_failure_does_not_retry_within_the_cooldown(fake_valkey):
    fake_valkey[rs._PASSWORD_KEY] = "wrong-but-configured"
    resp = MagicMock(status_code=401, cookies={})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=resp)) as post:
        first = await rs.get_session_token()
        second = await rs.get_session_token()

    assert first is None
    assert second is None
    post.assert_called_once()  # the second call hit the cooldown, not a second login attempt


@pytest.mark.asyncio
async def test_session_request_retries_once_on_401_with_forced_relogin(fake_valkey):
    fake_valkey[rs._PASSWORD_KEY] = "correct horse"
    old_token = _make_jwt(int(time.time()) + 3600)
    new_token = _make_jwt(int(time.time()) + 7200)  # distinct exp -> distinct token string
    fake_valkey[rs._SESSION_TOKEN_KEY] = old_token

    login_resp = MagicMock(status_code=200, cookies={"auth_token": new_token})
    unauth_resp = MagicMock(status_code=401)
    ok_resp = MagicMock(status_code=200)

    request_calls = []

    async def fake_request(method, url, cookies=None, **kwargs):
        request_calls.append(cookies["auth_token"])
        return unauth_resp if cookies["auth_token"] == old_token else ok_resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=login_resp)), \
         patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=fake_request)):
        resp = await rs.session_request("GET", "/api/combos")

    assert resp is ok_resp
    assert request_calls == [old_token, new_token]


@pytest.mark.asyncio
async def test_session_request_returns_none_without_a_password(fake_valkey):
    assert await rs.session_request("GET", "/api/combos") is None


@pytest.mark.asyncio
async def test_propose_and_approve_flow_activates_password(control_db, fake_valkey):
    proposal = rs.propose_router_password("hunter2")
    assert proposal["status"] == "awaiting_approval"
    task_id = proposal["task_id"]

    # Not yet usable — still pending.
    assert rs.is_configured() is False
    assert rs.get_router_password_proposal(task_id) is not None
    assert rs.get_router_password_proposal("some-other-task") is None

    control_plane.approve_task(task_id, actor="owner")
    result = await rs.execute_approved_router_password(task_id)

    assert result == {"status": "active"}
    assert rs.is_configured() is True
    assert fake_valkey[rs._PASSWORD_KEY] == "hunter2"
    assert rs.has_pending_proposal() is None


@pytest.mark.asyncio
async def test_execute_approved_rejects_a_task_that_was_never_approved(control_db, fake_valkey):
    proposal = rs.propose_router_password("hunter2")
    with pytest.raises(PermissionError):
        await rs.execute_approved_router_password(proposal["task_id"])


@pytest.mark.asyncio
async def test_approval_dispatch_routes_to_router_session_when_others_dont_match(control_db, fake_valkey):
    """Verifies the wiring in approval_dispatch.py — that an approved router-
    password task actually reaches execute_approved_router_password — without
    depending on the other governance modules' own DB state, which this test
    doesn't exercise or care about."""
    proposal = rs.propose_router_password("hunter2")
    task_id = proposal["task_id"]
    control_plane.approve_task(task_id, actor="owner")

    with patch("backend.autonomy.get_capability_proposal", return_value=None), \
         patch("backend.mcp_governance.get_connection_proposal", return_value=None), \
         patch("backend.provider_governance.get_provider_proposal", return_value=None), \
         patch("backend.agent_messenger_governance.get_telegram_binding_proposal", return_value=None), \
         patch("backend.agent_messenger_governance.get_matrix_binding_proposal", return_value=None), \
         patch("backend.agent_messenger_governance.get_discord_binding_proposal", return_value=None), \
         patch("backend.agent_messenger_governance.get_slack_binding_proposal", return_value=None), \
         patch("backend.agent_messenger_governance.get_email_binding_proposal", return_value=None):
        result = await execute_if_ready(control_plane.get_task(task_id))

    assert result == {"status": "active"}
    assert rs.is_configured() is True


def test_revoke_clears_everything(fake_valkey):
    fake_valkey[rs._PASSWORD_KEY] = "x"
    fake_valkey[rs._SESSION_TOKEN_KEY] = "y"
    rs.revoke_router_password()
    assert rs.is_configured() is False
    assert rs._SESSION_TOKEN_KEY not in fake_valkey


def test_propose_rejects_empty_password(control_db, fake_valkey):
    with pytest.raises(ValueError):
        rs.propose_router_password("   ")
