"""Watchdog for API requests that take too long.

The dashboard polls a lot of endpoints. When one of them starts hanging — a
blocking DB call on the event loop, a provider connection with no timeout, a
lock nobody releases — the visible symptom is a UI that half-loads, with nothing
in the log to say which request is stuck or where it is stuck.

This registers every in-flight request and, once one crosses the slow threshold,
logs it together with a stack dump of every thread.

One watchdog thread, not one timer per request
──────────────────────────────────────────────
A ``threading.Timer`` per request spawns an OS thread per request that lives
until the request finishes. Under sustained polling that exhausts the process
thread cap ("can't start new thread") and the server stops accepting
connections — trading a latency problem for an outage. A single daemon thread
scanning the in-flight table costs the same regardless of request rate.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
import uuid
from typing import Dict, Optional, Tuple

logger = logging.getLogger("hermes.slowreq")


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


SLOW_REQUEST_SECONDS = _float_env("HERMES_SLOW_REQUEST_SECONDS", 5.0)
WATCHDOG_TICK_SECONDS = _float_env("HERMES_SLOW_REQUEST_TICK_SECONDS", 1.0)
# Paths that are long-lived by design and must never be reported as stuck.
EXCLUDED_PATH_PREFIXES = ("/api/ws", "/api/stream", "/api/voice/stream")
MAX_STACK_CHARS = 20000
MAX_STACK_FRAMES = 25

_lock = threading.Lock()
# request_id -> (started_at, method, path, reported)
_in_flight: Dict[str, Tuple[float, str, str, bool]] = {}
_watchdog: Optional[threading.Thread] = None


def is_excluded(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES)


def begin(method: str, path: str) -> Optional[str]:
    """Register an in-flight request. Returns its id, or None when not tracked."""
    if is_excluded(path):
        return None
    request_id = uuid.uuid4().hex[:12]
    with _lock:
        _in_flight[request_id] = (time.monotonic(), method, path, False)
    _ensure_watchdog()
    return request_id


def finish(request_id: Optional[str]) -> float:
    """Deregister a request. Returns its duration in seconds (0 when untracked)."""
    if not request_id:
        return 0.0
    with _lock:
        entry = _in_flight.pop(request_id, None)
    if entry is None:
        return 0.0
    started_at, method, path, reported = entry
    duration = time.monotonic() - started_at
    if reported:
        logger.warning(
            "Slow request finished after %.1fs: %s %s [%s]", duration, method, path, request_id
        )
    return duration


def in_flight_snapshot() -> list:
    """Read-only view for the health endpoint."""
    now = time.monotonic()
    with _lock:
        return [
            {"request_id": rid, "method": method, "path": path,
             "elapsed_s": round(now - started, 2), "reported": reported}
            for rid, (started, method, path, reported) in _in_flight.items()
        ]


def _dump_all_thread_stacks() -> str:
    """Format every thread's stack.

    Uses ``sys._current_frames`` rather than ``faulthandler.dump_traceback``:
    faulthandler writes to a real file descriptor, so it cannot render into a log
    record, and the log is where this has to land.
    """
    try:
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        chunks = []
        for thread_id, frame in sys._current_frames().items():
            chunks.append(f"--- thread {names.get(thread_id, '?')} ({thread_id}) ---")
            chunks.extend(line.rstrip() for line in traceback.format_stack(frame)[-MAX_STACK_FRAMES:])
        return "\n".join(chunks)[:MAX_STACK_CHARS]
    except Exception as exc:  # pragma: no cover - defensive
        return f"<stack dump unavailable: {exc}>"


def _scan_once(now: Optional[float] = None) -> int:
    """Report every request past the threshold that has not been reported yet.

    Each request is reported once: a stuck request that stays stuck would
    otherwise dump every thread's stack on every tick and bury the log.
    """
    now = now if now is not None else time.monotonic()
    overdue = []
    with _lock:
        for request_id, (started_at, method, path, reported) in list(_in_flight.items()):
            if reported or now - started_at < SLOW_REQUEST_SECONDS:
                continue
            _in_flight[request_id] = (started_at, method, path, True)
            overdue.append((request_id, now - started_at, method, path))
    for request_id, elapsed, method, path in overdue:
        logger.warning(
            "Request still running after %.1fs: %s %s [%s]\n%s",
            elapsed, method, path, request_id, _dump_all_thread_stacks(),
        )
    return len(overdue)


def _watchdog_loop() -> None:
    while True:
        time.sleep(WATCHDOG_TICK_SECONDS)
        try:
            _scan_once()
        except Exception:  # pragma: no cover - the watchdog must never die
            try:
                logger.exception("Slow-request watchdog tick failed")
            except Exception:
                pass


def _ensure_watchdog() -> None:
    global _watchdog
    if _watchdog is not None and _watchdog.is_alive():
        return
    with _lock:
        if _watchdog is not None and _watchdog.is_alive():
            return
        thread = threading.Thread(
            target=_watchdog_loop, name="slow-request-watchdog", daemon=True
        )
        thread.start()
        _watchdog = thread
