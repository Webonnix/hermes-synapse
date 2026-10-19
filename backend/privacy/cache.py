"""Cache of local-model (L2) verdicts, keyed by HMAC of the checked window.

A long conversation is rebuilt from the DB and re-sent every turn; without
this, every turn would re-check the whole history on the GPU. With it, only
text the gateway has not seen before reaches the model.

Two tiers: an in-process LRU, then Valkey (shared, survives restarts) under
`privacy:l2:<hmac>` with a TTL. A verdict is the list of secret substrings
found in the window, so the Valkey copy is AES-GCM encrypted with a key
derived from PRIVACY_KEY. Valkey being down only costs cache hits.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import OrderedDict
from typing import List, Optional

from backend.privacy import keys

logger = logging.getLogger("hermes.privacy")

_PREFIX = "privacy:l2:"
_memory: "OrderedDict[str, List[str]]" = OrderedDict()
# After a Valkey error, skip it for a while instead of paying the 3 s connect
# timeout on every window of a long message.
_VALKEY_BACKOFF_SECONDS = 60.0
_valkey_down_until = 0.0


def _ttl() -> int:
    try:
        return max(60, int(os.getenv("PRIVACY_CACHE_TTL", "") or 30 * 24 * 3600))
    except ValueError:
        return 30 * 24 * 3600


def _memory_size() -> int:
    try:
        return max(0, int(os.getenv("PRIVACY_CACHE_MEMORY_ITEMS", "") or 20000))
    except ValueError:
        return 20000


def _valkey_enabled() -> bool:
    if time.monotonic() < _valkey_down_until:
        return False
    return (os.getenv("PRIVACY_CACHE_VALKEY", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def _valkey_failed(exc: Exception, op: str) -> None:
    global _valkey_down_until
    _valkey_down_until = time.monotonic() + _VALKEY_BACKOFF_SECONDS
    logger.warning("privacy: L2 cache %s failed, Valkey skipped for %.0fs: %s", op, _VALKEY_BACKOFF_SECONDS, exc)


def _encrypt(spans: List[str]) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    blob = AESGCM(keys.encryption_key()).encrypt(nonce, json.dumps(spans, ensure_ascii=False).encode(), None)
    return base64.b64encode(nonce + blob).decode()


def _decrypt(token: str) -> Optional[List[str]]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.b64decode(token)
        spans = json.loads(AESGCM(keys.encryption_key()).decrypt(raw[:12], raw[12:], None))
    except Exception:
        return None  # other key (PRIVACY_KEY rotated) or garbage: treat as a miss
    return [s for s in spans if isinstance(s, str)] if isinstance(spans, list) else None


def _remember(key: str, spans: List[str]) -> None:
    limit = _memory_size()
    if not limit:
        return
    _memory[key] = list(spans)
    _memory.move_to_end(key)
    while len(_memory) > limit:
        _memory.popitem(last=False)


async def get(window: str) -> Optional[List[str]]:
    key = keys.cache_key(window)
    if key in _memory:
        _memory.move_to_end(key)
        return list(_memory[key])
    if not _valkey_enabled():
        return None
    try:
        from backend import valkey_client

        token = await asyncio.to_thread(valkey_client.get_value, _PREFIX + key)
    except Exception as exc:
        _valkey_failed(exc, "read")
        return None
    spans = _decrypt(token) if token else None
    if spans is not None:
        _remember(key, spans)
    return spans


async def put(window: str, spans: List[str]) -> None:
    key = keys.cache_key(window)
    _remember(key, spans)
    if not _valkey_enabled():
        return
    try:
        from backend import valkey_client

        await asyncio.to_thread(valkey_client.set_value, _PREFIX + key, _encrypt(spans), _ttl())
    except Exception as exc:
        _valkey_failed(exc, "write")


def clear_memory() -> None:
    global _valkey_down_until
    _memory.clear()
    _valkey_down_until = 0.0
