"""Short-lived, path-bound public links; normal artifact reads need a session."""
import hashlib
import hmac
import os
import re
import secrets
import time

from backend import auth_state

PATH_RE = re.compile(r"^/api/(?:plots|generated-images)/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,180}\.(?:png|jpg|jpeg|webp|gif)$")
MAX_TTL = 300


def _key() -> bytes:
    configured = os.getenv("ARTIFACT_SIGNING_KEY", "")
    if configured:
        if len(configured) < 32:
            raise auth_state.AuthUnavailable("Artifact signing key is too short")
        return configured.encode()
    key = auth_state.PREFIX + "artifact-signing-key"
    auth_state._call("set", key, secrets.token_hex(32), nx=True)
    return auth_state._call("get", key).encode()


def sign(path: str, ttl: int = MAX_TTL) -> str:
    if not PATH_RE.fullmatch(path):
        raise ValueError("Only generated image artifacts can be shared")
    expires = int(time.time()) + max(1, min(ttl, MAX_TTL))
    signature = hmac.new(_key(), f"{path}\n{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{path}?expires={expires}&signature={signature}"


def valid(path: str, expires: str, signature: str) -> bool:
    if not PATH_RE.fullmatch(path) or not re.fullmatch(r"[0-9]{1,12}", expires) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    now = int(time.time())
    if not now < int(expires) <= now + MAX_TTL:
        return False
    expected = hmac.new(_key(), f"{path}\n{expires}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
