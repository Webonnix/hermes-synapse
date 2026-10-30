"""Shared authentication state. No local fallback when Valkey is unavailable."""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import os
from functools import lru_cache
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

SESSION_TTL = 12 * 60 * 60
IDLE_TTL = 30 * 60
PREFIX = "hermes:{auth}:"


@lru_cache(maxsize=1)
def get_client():
    return redis.Redis(host=os.getenv("VALKEY_HOST", "valkey"), port=int(os.getenv("VALKEY_PORT", "6379")),
                       password=os.getenv("VALKEY_PASSWORD") or None, decode_responses=True,
                       socket_connect_timeout=1, socket_timeout=2, max_connections=32,
                       retry=Retry(NoBackoff(), 0))


class AuthUnavailable(RuntimeError):
    pass


def _call(method: str, *args, **kwargs):
    try:
        return getattr(get_client(), method)(*args, **kwargs)
    except Exception as exc:
        raise AuthUnavailable("Authentication storage unavailable") from exc


def _session_key(token: str) -> str:
    return PREFIX + "session:" + hashlib.sha256(token.encode()).hexdigest()


def issue_otp(code: str) -> int:
    challenge = secrets.token_hex(16)
    digest = hashlib.sha256(code.encode()).hexdigest()
    return int(_call("eval", """
        local live = redis.call('PTTL', KEYS[1])
        local cooldown = redis.call('PTTL', KEYS[2])
        if live > 0 or cooldown > 0 then return math.ceil(math.max(live, cooldown) / 1000) end
        redis.call('HSET', KEYS[1], 'digest', ARGV[1], 'id', ARGV[2], 'attempts', 0)
        redis.call('EXPIRE', KEYS[1], 300)
        redis.call('SET', KEYS[2], '1', 'EX', 60)
        return 0
    """, 2, PREFIX + "otp", PREFIX + "otp-cooldown", digest, challenge))


def verify_otp(code: str) -> bool:
    reserved = _call("eval", """
        if redis.call('EXISTS', KEYS[1]) == 0 then return {} end
        if tonumber(redis.call('HGET', KEYS[1], 'attempts') or 5) >= 5 then return {} end
        redis.call('HINCRBY', KEYS[1], 'attempts', 1)
        return {redis.call('HGET', KEYS[1], 'digest'), redis.call('HGET', KEYS[1], 'id')}
    """, 1, PREFIX + "otp")
    if not reserved:
        return False
    digest, challenge = reserved
    valid = secrets.compare_digest(digest, hashlib.sha256(code.strip().encode()).hexdigest())
    consumed = _call("eval", """
        if redis.call('HGET', KEYS[1], 'id') ~= ARGV[1] then return 0 end
        if ARGV[2] == '1' then redis.call('DEL', KEYS[1]); return 1 end
        if tonumber(redis.call('HGET', KEYS[1], 'attempts') or 5) >= 5 then
            redis.call('DEL', KEYS[1])
        end
        return 0
    """, 1, PREFIX + "otp", challenge, "1" if valid else "0")
    return bool(valid and consumed)


def invalidate_otp(code: str) -> None:
    _call("eval", """
        if redis.call('HGET', KEYS[1], 'digest') == ARGV[1] then redis.call('DEL', KEYS[1]) end
        return 1
    """, 1, PREFIX + "otp", hashlib.sha256(code.encode()).hexdigest())


def retry_after(path: str, limit: int) -> int:
    result = _call("eval", """
        local t = redis.call('TIME'); local now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
        if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
            local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
            return math.max(1, math.ceil((tonumber(first[2]) + 60000 - now) / 1000))
        end
        redis.call('ZADD', KEYS[1], now, ARGV[2]); redis.call('EXPIRE', KEYS[1], 60)
        return 0
    """, 1, PREFIX + "limit:" + path, limit, secrets.token_hex(16))
    return int(result)


def create(token: str, role: str, csrf: str) -> None:
    _call("eval", """
        local now = tonumber(redis.call('TIME')[1])
        local epoch = redis.call('GET', KEYS[2]) or '0'
        redis.call('HSET', KEYS[1], 'role', ARGV[1], 'csrf', ARGV[2],
                   'expires', now + tonumber(ARGV[3]), 'fresh', 0, 'epoch', epoch)
        redis.call('EXPIRE', KEYS[1], ARGV[4]); return 1
    """, 2, _session_key(token), PREFIX + "epoch", role, csrf, SESSION_TTL, IDLE_TTL)


def read(token: str, *, touch: bool = True) -> dict[str, Any] | None:
    if not token or len(token) > 256:
        return None
    raw = _call("eval", """
        local role = redis.call('HGET', KEYS[1], 'role')
        if not role then return '' end
        local now = tonumber(redis.call('TIME')[1])
        local expires = tonumber(redis.call('HGET', KEYS[1], 'expires') or 0)
        if now >= expires or redis.call('HGET', KEYS[1], 'epoch') ~= (redis.call('GET', KEYS[2]) or '0') then
            redis.call('DEL', KEYS[1]); return ''
        end
        if ARGV[1] == '1' then redis.call('EXPIRE', KEYS[1], math.min(expires-now, tonumber(ARGV[2]))) end
        return cjson.encode({role=role, csrf=redis.call('HGET', KEYS[1], 'csrf'),
            fresh=tonumber(redis.call('HGET', KEYS[1], 'fresh') or 0), expires=expires, now=now})
    """, 2, _session_key(token), PREFIX + "epoch", "1" if touch else "0", IDLE_TTL)
    return json.loads(raw) if raw else None


def destroy(token: str) -> None:
    _call("delete", _session_key(token))


def revoke_all() -> None:
    _call("incr", PREFIX + "epoch")


def mark_fresh(token: str) -> bool:
    return bool(_call("eval", """
        if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
        redis.call('HSET', KEYS[1], 'fresh', redis.call('TIME')[1]); return 1
    """, 1, _session_key(token)))
