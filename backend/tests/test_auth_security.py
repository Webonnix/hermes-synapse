"""Authentication abuse regressions. No network, database or bot startup."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import auth


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch):
    monkeypatch.setattr(auth, "current_otp", {})
    monkeypatch.setattr(auth, "_last_otp_request", None, raising=False)
    monkeypatch.setattr(auth, "_auth_attempts", {}, raising=False)


def test_otp_is_invalidated_after_five_wrong_attempts():
    code = auth.generate_otp()
    for _ in range(5):
        assert not auth.verify_otp("000000")
    assert not auth.verify_otp(code)
    assert not auth.current_otp


def test_fifth_attempt_can_succeed_but_code_cannot_be_replayed():
    code = auth.generate_otp()
    for _ in range(4):
        assert not auth.verify_otp("000000")
    assert auth.verify_otp(f" {code} ")
    assert not auth.verify_otp(code)


def test_live_code_cannot_be_replaced_even_after_resend_cooldown(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    code = auth.generate_otp()
    clock[0] += 61
    with pytest.raises(auth.OTPRequestThrottled) as exc:
        auth.generate_otp()
    assert exc.value.retry_after == 239
    assert auth.verify_otp(code)


def test_otp_expires_at_exact_deadline(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    code = auth.generate_otp()
    clock[0] += 300
    assert not auth.verify_otp(code)
    assert auth.generate_otp()


def test_exhaustion_does_not_bypass_request_cooldown(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    auth.generate_otp()
    for _ in range(5):
        auth.verify_otp("000000")
    with pytest.raises(auth.OTPRequestThrottled) as exc:
        auth.generate_otp()
    assert exc.value.retry_after == 60
    clock[0] += 60
    assert auth.generate_otp()


def test_non_ascii_guesses_fail_without_crashing():
    code = auth.generate_otp()
    assert not auth.verify_otp("１２３４５６")
    assert not auth.verify_otp("🔑" * 6)
    assert auth.verify_otp(code)


def test_concurrent_verification_issues_only_one_success():
    code = auth.generate_otp()
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(auth.verify_otp, [code] * 16)) == 1


def test_concurrent_requests_issue_only_one_code():
    def issue(_):
        try:
            return auth.generate_otp()
        except auth.OTPRequestThrottled:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(code is not None for code in pool.map(issue, range(16))) == 1


def test_delivery_failure_revokes_only_that_code_and_keeps_cooldown():
    code = auth.generate_otp()
    auth.invalidate_otp("another-code")
    assert auth.current_otp
    auth.invalidate_otp(code)
    assert not auth.verify_otp(code)
    with pytest.raises(auth.OTPRequestThrottled):
        auth.generate_otp()


@pytest.mark.parametrize("path,limit", [
    ("/api/auth/request-code", 10),
    ("/api/auth/verify-code", 20),
    ("/api/auth/login", 5),
])
def test_auth_rate_limit_and_window_expiry(monkeypatch, path, limit):
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    for _ in range(limit):
        assert auth.auth_retry_after(path) == 0
    assert auth.auth_retry_after(path) == 60
    clock[0] += 59.1
    assert auth.auth_retry_after(path) == 1
    clock[0] += 0.9
    assert auth.auth_retry_after(path) == 0


def test_unknown_auth_paths_do_not_allocate_limiter_state():
    for index in range(1000):
        assert auth.auth_retry_after(f"/api/auth/unknown-{index}") == 0
    assert not auth._auth_attempts


def test_login_limit_does_not_lock_out_otp():
    for _ in range(5):
        auth.auth_retry_after("/api/auth/login")
    assert auth.auth_retry_after("/api/auth/login") > 0
    assert auth.auth_retry_after("/api/auth/verify-code") == 0
