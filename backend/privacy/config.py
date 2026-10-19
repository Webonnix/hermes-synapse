"""Env-driven knobs for the privacy gateway, read on every call so a changed
.env takes effect on the next container start without code changes."""

from __future__ import annotations

import os

FAIL_OPEN = "open"
FAIL_CLOSED = "closed"


def _int_env(key: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(key, "") or default))
    except ValueError:
        return default


def fail_mode() -> str:
    """What happens when the local-model pass cannot vouch for a message
    (model down, timeout, unparseable answer, text over the size cap).

    open   — send it with only the regex pass applied (logged as a warning).
    closed — do not send it at all; the call fails with PrivacyCheckFailed.

    Default stays "open" so that turning the gateway into a package does not
    change behaviour on its own: with llama.cpp at --parallel 1, a busy local
    model would otherwise fail every external call. Flip to "closed" once the
    failure rate in the logs ("privacy: local pass failed") is known.
    """
    mode = (os.getenv("PRIVACY_FAIL_MODE", FAIL_OPEN) or FAIL_OPEN).strip().lower()
    return FAIL_CLOSED if mode == FAIL_CLOSED else FAIL_OPEN


def chunk_chars() -> int:
    return _int_env("PRIVACY_LLM_CHUNK_CHARS", 3000, 500)


def chunk_overlap() -> int:
    # Overlap so a secret straddling a chunk boundary is still seen whole by
    # at least one chunk. Must stay well below chunk_chars().
    return min(_int_env("PRIVACY_LLM_CHUNK_OVERLAP", 200, 0), chunk_chars() // 4)


def max_chars() -> int:
    """Longest text the local pass scans in fail-open mode. Past it the rest
    is regex-only (and logged). Fail-closed mode ignores the cap: there an
    unchecked tail is exactly what must not leave."""
    return _int_env("PRIVACY_LLM_MAX_CHARS", 40000, 1000)


def local_timeout() -> float:
    return float(_int_env("PRIVACY_LLM_TIMEOUT", 15, 1))
