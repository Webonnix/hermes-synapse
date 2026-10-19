"""Key material for the privacy gateway, all derived from PRIVACY_KEY.

PRIVACY_KEY (64+ hex chars, `openssl rand -hex 32`) makes placeholders and
cache keys stable across restarts: the same secret always becomes the same
placeholder, so a long conversation re-sent every turn keeps a byte-identical
prefix and the provider's prompt cache keeps hitting. Without it a random
per-process key is used — still correct, but every restart changes all
placeholders and empties the Valkey cache.

Trade-off: placeholders are global, not per conversation, so a provider can
tell that two conversations mention the same (still hidden) secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from functools import lru_cache

logger = logging.getLogger("hermes.privacy")


@lru_cache(maxsize=1)
def _master() -> bytes:
    raw = (os.getenv("PRIVACY_KEY") or "").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    if len(key) >= 32:
        return key
    logger.warning(
        "privacy: PRIVACY_KEY missing or shorter than 32 bytes; using a random per-process key "
        "(placeholders and the L2 cache will not survive a restart)"
    )
    return secrets.token_bytes(32)


def _subkey(label: bytes) -> bytes:
    return hmac.new(_master(), label, hashlib.sha256).digest()


def placeholder_digest(value: str) -> str:
    return hmac.new(_subkey(b"placeholder"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def cache_key(text: str) -> str:
    """HMAC, not a bare hash: a short window like "pin 1234" must not be
    brute-forceable from its cache key."""
    return hmac.new(_subkey(b"l2-cache"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def encryption_key() -> bytes:
    return _subkey(b"l2-cache-aesgcm")


def reset_for_tests() -> None:
    _master.cache_clear()
