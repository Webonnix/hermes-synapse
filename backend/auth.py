import time
import secrets
import hashlib
import logging
import math
from collections import deque
from threading import Lock
from typing import Dict, Set, Any, Optional, Tuple

logger = logging.getLogger("hermes.auth")
from backend import auth_state

# Enabled only by pytest's fixture, never by an environment variable. Production
# always uses shared Valkey and fails closed, including during outages/restarts.
_test_memory = False
_session_meta: Dict[str, dict] = {}

# In-memory stores
active_sessions: Set[str] = set()
# Map session ID to username / meta if needed, or just set of active tokens.

# Role attached to a session token, consumed by backend/permissions.py. The
# dashboard has had exactly one principal (the owner) since it shipped, so an
# absent entry means owner and nothing about existing logins changes. The map
# exists so a narrower role can be issued — that is what makes the clients
# module's RBAC checks testable and, later, real.
session_roles: Dict[str, str] = {}

# One process owns authentication state. Do not run multiple API workers until
# OTPs, throttles and sessions have moved together to a shared store.
OTP_TTL_SECONDS = 300
OTP_REQUEST_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
_auth_lock = Lock()
_last_otp_request: Optional[float] = None
current_otp: Dict[str, Any] = {}

_AUTH_LIMITS = {
    "/api/auth/request-code": 10,
    "/api/auth/verify-code": 20,
    "/api/auth/login": 5,
    "/api/auth/reauth": 5,
    "/api/auth/activity": 5,
}
_AUTH_WINDOW_SECONDS = 60
_auth_attempts: Dict[str, deque] = {}


class OTPRequestThrottled(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__("An access code was already requested. Please wait.")


def auth_retry_after(path: str) -> int:
    """Reserve an auth attempt, or return the seconds until it is allowed.

    Limits are installation-wide: forwarded IP headers cannot buy more password
    guesses, and attacker-controlled addresses cannot grow an unbounded map.
    """
    limit = _AUTH_LIMITS.get(path)
    if limit is None:
        return 0
    if not _test_memory:
        return auth_state.retry_after(path, limit)
    with _auth_lock:
        now = time.monotonic()
        attempts = _auth_attempts.setdefault(path, deque())
        while attempts and attempts[0] <= now - _AUTH_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= limit:
            return max(1, math.ceil(attempts[0] + _AUTH_WINDOW_SECONDS - now))
        attempts.append(now)
        return 0


def generate_otp() -> str:
    """Issue a code without replacing a live one or bypassing the cooldown."""
    global current_otp, _last_otp_request
    if not _test_memory:
        code = str(secrets.randbelow(900000) + 100000)
        wait = auth_state.issue_otp(code)
        if wait:
            raise OTPRequestThrottled(wait)
        return code
    with _auth_lock:
        now = time.monotonic()
        retry_after = max(0, current_otp.get("expires_at", 0) - now)
        if _last_otp_request is not None:
            retry_after = max(retry_after, _last_otp_request + OTP_REQUEST_COOLDOWN_SECONDS - now)
        if retry_after > 0:
            raise OTPRequestThrottled(retry_after)
        code = str(secrets.randbelow(900000) + 100000)
        current_otp = {"code": code, "expires_at": now + OTP_TTL_SECONDS, "attempts": 0}
        _last_otp_request = now
    logger.info("Generated new OTP code. Expires in 5 minutes.")
    return code


def invalidate_otp(code: str) -> None:
    """Revoke a failed delivery without clearing a later request's code."""
    global current_otp
    if not _test_memory:
        auth_state.invalidate_otp(code)
        return
    with _auth_lock:
        if secrets.compare_digest(current_otp.get("code", ""), code):
            current_otp = {}

def verify_otp(code: str) -> bool:
    """Consume a single-use code, with at most five guesses before revocation."""
    global current_otp
    if not _test_memory:
        return auth_state.verify_otp(code)
    with _auth_lock:
        if not current_otp:
            return False
        if time.monotonic() >= current_otp.get("expires_at", 0):
            current_otp = {}
            return False
        current_otp["attempts"] += 1
        valid = secrets.compare_digest(current_otp["code"].encode(), code.strip().encode())
        if valid or current_otp["attempts"] >= OTP_MAX_ATTEMPTS:
            current_otp = {}
        return valid

def create_session(role: str = "owner") -> str:
    """Generates a secure session token and adds it to the active sessions set."""
    token = secrets.token_hex(32)
    csrf = secrets.token_urlsafe(32)
    if not _test_memory:
        auth_state.create(token, role, csrf)
        return token
    active_sessions.add(token)
    session_roles[token] = role
    now = time.time()
    _session_meta[token] = {"role": role, "csrf": csrf, "fresh": 0,
                            "expires": now + auth_state.SESSION_TTL, "idle": now + auth_state.IDLE_TTL}
    logger.info(f"New session created. Total active sessions: {len(active_sessions)}")
    return token

def session_data(token: str, *, touch: bool = True) -> dict | None:
    if not _test_memory:
        return auth_state.read(token, touch=touch)
    if token not in active_sessions:
        return None
    # Legacy tests register synthetic bearer tokens directly, never a prod path.
    now = time.time()
    meta = _session_meta.get(token, {"csrf": "test-csrf", "fresh": 0,
                                   "expires": now + auth_state.SESSION_TTL, "idle": now + auth_state.IDLE_TTL})
    if now >= min(meta["expires"], meta["idle"]):
        destroy_session(token)
        return None
    if touch:
        meta["idle"] = min(meta["expires"], now + auth_state.IDLE_TTL)
    return {**meta, "role": session_roles.get(token, "owner"), "now": now}


def validate_session(token: str, *, touch: bool = True) -> bool:
    """Checks if a session token is valid."""
    return session_data(token, touch=touch) is not None


def role_for_session(token: Optional[str]) -> str:
    """Role bound to a session token; 'owner' for any token issued before roles
    existed (or issued without one)."""
    data = session_data(token, touch=False) if token else None
    return data["role"] if data else "nobody"

def destroy_session(token: str):
    """Removes a session token from active sessions."""
    if not _test_memory:
        auth_state.destroy(token)
        return
    _session_meta.pop(token, None)
    if token in active_sessions:
        active_sessions.remove(token)
        session_roles.pop(token, None)
        logger.info(f"Session destroyed. Total active sessions: {len(active_sessions)}")


def revoke_all_sessions():
    if not _test_memory:
        auth_state.revoke_all()
    else:
        active_sessions.clear()
        session_roles.clear()
        _session_meta.clear()


def mark_session_fresh(token: str) -> bool:
    if not _test_memory:
        return auth_state.mark_fresh(token)
    if not session_data(token, touch=False):
        return False
    _session_meta[token]["fresh"] = time.time()
    return True

# ─── USERNAME / PASSWORD LOGIN ─────────────────────────────────────────────
# Single-admin credential, stored via database.get_setting/set_setting under
# 'admin_username', 'admin_password_hash', 'admin_password_salt'. PBKDF2-HMAC
# (stdlib only, no new dependency) with a per-install random salt.

_PBKDF2_ITERATIONS = 260_000

def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Returns (hash_hex, salt_hex) for a password, generating a salt if not given."""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, _PBKDF2_ITERATIONS)
    return digest.hex(), salt_bytes.hex()

def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Constant-time comparison of a password against a stored PBKDF2 hash."""
    candidate_hash, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate_hash, password_hash)
