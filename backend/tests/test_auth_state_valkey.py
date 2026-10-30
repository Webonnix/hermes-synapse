"""Real Lua/TTL/concurrency checks; opt-in ephemeral Valkey, never production."""
import os
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from backend import auth_state

URL = os.getenv("HERMES_AUTH_TEST_VALKEY_URL")
pytestmark = pytest.mark.skipif(not URL, reason="ephemeral auth Valkey not configured")


@pytest.fixture
def store(monkeypatch):
    client = redis.Redis.from_url(URL, decode_responses=True)
    prefix = "hermes:{auth-test-" + secrets.token_hex(6) + "}:"
    monkeypatch.setattr(auth_state, "PREFIX", prefix)
    monkeypatch.setattr(auth_state, "get_client", lambda: client)
    yield client
    keys = list(client.scan_iter(prefix + "*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_shared_sessions_ttl_revoke_and_epoch(store):
    auth_state.create("synthetic-session", "viewer", "csrf")
    key = auth_state._session_key("synthetic-session")
    assert 0 < store.ttl(key) <= auth_state.IDLE_TTL
    assert auth_state.read("synthetic-session")["role"] == "viewer"
    auth_state.mark_fresh("synthetic-session")
    assert auth_state.read("synthetic-session")["fresh"] > 0
    auth_state.revoke_all()
    assert auth_state.read("synthetic-session") is None


def test_absolute_expiry_does_not_refresh(store):
    auth_state.create("synthetic-session", "owner", "csrf")
    key = auth_state._session_key("synthetic-session")
    store.hset(key, "expires", int(store.time()[0]) - 1)
    assert auth_state.read("synthetic-session") is None
    assert not store.exists(key)


def test_otp_five_guesses_and_no_early_replacement(store):
    assert auth_state.issue_otp("123456") == 0
    assert auth_state.issue_otp("654321") > 0
    for _ in range(5):
        assert not auth_state.verify_otp("000000")
    assert not auth_state.verify_otp("123456")
    assert auth_state.issue_otp("654321") > 0


def test_correct_otp_is_consumed_exactly_once_across_threads(store):
    auth_state.issue_otp("123456")
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(auth_state.verify_otp, ["123456"] * 20)) == 1


def test_shared_rate_limit_is_atomic(store):
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: auth_state.retry_after("login", 5), range(32)))
    assert results.count(0) == 5
    assert all(value == 0 or 1 <= value <= 60 for value in results)


def test_subsecond_otp_ttl_still_prevents_overwrite(store):
    auth_state.issue_otp("123456")
    store.pexpire(auth_state.PREFIX + "otp", 900)
    store.delete(auth_state.PREFIX + "otp-cooldown")
    assert auth_state.issue_otp("654321") == 1
    assert auth_state.verify_otp("123456")
