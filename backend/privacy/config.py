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
    (model down or busy past the queue wait, unparseable answer, more
    windows than the call budget).

    open   — send it with what the checks that did run found (logged).
    closed — do not send it at all; the call fails with PrivacyCheckFailed.

    Default stays "open"; flip to "closed" once the failure rate in the logs
    ("privacy: local pass failed") is known.
    """
    mode = (os.getenv("PRIVACY_FAIL_MODE", FAIL_OPEN) or FAIL_OPEN).strip().lower()
    return FAIL_CLOSED if mode == FAIL_CLOSED else FAIL_OPEN


def full_scan_chars() -> int:
    """Text up to this length goes to the local model whole (typed user
    messages, short tool output). Longer text — pasted logs, files, a 1M-token
    history — only sends the windows around trigger words."""
    return _int_env("PRIVACY_LLM_FULL_SCAN_CHARS", 2000, 0)


def full_scan_roles() -> frozenset:
    """Message roles whose short text is checked whole. Default: only what a
    person typed ("user") — casual secrets without a trigger word come from
    people; tool output and model replies only get trigger windows."""
    raw = os.getenv("PRIVACY_LLM_FULL_SCAN_ROLES", "user")
    return frozenset(r.strip() for r in raw.split(",") if r.strip())


def memo_bytes() -> int:
    """Memory for the whole-message memo (redacted text + mapping)."""
    return _int_env("PRIVACY_MEMO_MB", 256, 0) * 1024 * 1024


def window_chars() -> int:
    """Context kept on each side of a trigger word."""
    return _int_env("PRIVACY_LLM_WINDOW_CHARS", 300, 50)


def chunk_chars() -> int:
    """Longest single request to the local model; bigger windows are split."""
    return _int_env("PRIVACY_LLM_CHUNK_CHARS", 3000, 500)


def chunk_overlap() -> int:
    # Overlap so a secret straddling a chunk boundary is still seen whole by
    # at least one chunk. Must stay well below chunk_chars().
    return min(_int_env("PRIVACY_LLM_CHUNK_OVERLAP", 200, 0), chunk_chars() // 4)


def max_calls() -> int:
    """Local-model requests allowed per message (cache hits are free). Past
    it: fail-open skips the rest (logged), fail-closed blocks the call."""
    return _int_env("PRIVACY_LLM_MAX_CALLS", 8, 1)


def local_timeout() -> float:
    return float(_int_env("PRIVACY_LLM_TIMEOUT", 15, 1))


def queue_wait() -> float:
    """How long a check waits behind other local-model work (chat has
    priority) before it counts as a failed check."""
    return float(_int_env("PRIVACY_LLM_QUEUE_WAIT", 20, 1))
