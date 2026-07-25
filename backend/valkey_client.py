"""Thin wrapper around a shared Valkey (Redis-protocol-compatible) connection.

Used for two purposes that both want a fast, TTL-capable key-value store outside
the main SQL database: external-provider API key storage (provider_governance.py)
and short-lived placeholder->secret mappings for the redaction gateway
(redaction.py). Neither belongs in the SQL DB — one is a credential, the other is
inherently transient.
"""
import os
from typing import Optional

import redis

_client: Optional["redis.Redis"] = None


def get_client() -> "redis.Redis":
    global _client
    if _client is None:
        _client = redis.Redis(
            host=os.getenv("VALKEY_HOST", "valkey"),
            port=int(os.getenv("VALKEY_PORT", "6379")),
            password=os.getenv("VALKEY_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _client


def set_value(key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
    client = get_client()
    if ttl_seconds:
        client.setex(key, ttl_seconds, value)
    else:
        client.set(key, value)


def get_value(key: str) -> Optional[str]:
    return get_client().get(key)


def delete_value(key: str) -> None:
    get_client().delete(key)


def ping() -> bool:
    try:
        return bool(get_client().ping())
    except redis.RedisError:
        return False
