"""Make process death and background-task death visible.

The backend runs the scheduler, the messenger bots, dev-runs, the price monitor
and the model keeper as long-lived asyncio tasks inside the same process as the
API. Three ways that can fail without leaving a usable trace:

1. **A native crash** (segfault or abort inside a C extension — sqlite, ssl, a
   provider SDK) kills the interpreter with no Python-level traceback. Without
   ``faulthandler`` nothing at all is written.
2. **A background task raises and nobody awaits it.** asyncio reports this only
   when the task object is garbage-collected, which for a task held in a
   module-level variable can be "never". The worker is simply gone, and the only
   symptom is that scheduled jobs quietly stop happening.
3. **An uncaught exception on a thread** goes to ``threading.excepthook``, whose
   stock implementation prints to ``sys.stderr`` — lost if that stream is gone.

This module is diagnostics only: it changes no behaviour, it only guarantees a
recorded reason. Every hook is defensive, because a logging hook that raises
would itself become a new silent-failure mode.
"""

from __future__ import annotations

import asyncio
import atexit
import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Coroutine, Optional

logger = logging.getLogger("hermes.crash")

_installed = False
_fault_log_handle = None


def _fault_log_path() -> Optional[Path]:
    raw = os.getenv("HERMES_CRASH_LOG", "").strip()
    if raw:
        return Path(raw)
    for candidate in (Path("/app/logs"), Path("logs")):
        if candidate.is_dir():
            return candidate / "crash.log"
    return None


def _enable_faulthandler() -> None:
    """Dump native-crash tracebacks. Writes to a file when one is available so
    the trace survives a container whose stderr is not being collected."""
    global _fault_log_handle
    try:
        faulthandler.enable(all_threads=True)
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("faulthandler on stderr unavailable: %s", exc)
    path = _fault_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Held open for the life of the process on purpose: faulthandler writes
        # to the fd from a signal handler, where opening a file is not safe.
        _fault_log_handle = path.open("a", buffering=1)
        faulthandler.enable(file=_fault_log_handle, all_threads=True)
        logger.info("Native crash traces will be written to %s", path)
    except Exception as exc:  # pragma: no cover - filesystem dependent
        logger.debug("Could not open crash log %s: %s", path, exc)


def _install_excepthooks() -> None:
    previous_sys_hook = sys.excepthook
    previous_thread_hook = getattr(threading, "excepthook", None)

    def sys_hook(exc_type, exc_value, exc_tb):
        try:
            logger.critical(
                "Uncaught exception on the main thread", exc_info=(exc_type, exc_value, exc_tb)
            )
        except Exception:
            pass
        try:
            previous_sys_hook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    def thread_hook(args):
        try:
            logger.critical(
                "Uncaught exception in thread %s",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            pass
        if previous_thread_hook is not None:
            try:
                previous_thread_hook(args)
            except Exception:
                pass

    sys.excepthook = sys_hook
    if previous_thread_hook is not None:
        threading.excepthook = thread_hook


def _install_atexit_audit() -> None:
    def audit():
        # A log that simply stops mid-request is ambiguous: crash, OOM kill, or
        # an orderly stop? This line is the difference.
        try:
            logger.info("Process %s exiting (orderly shutdown path ran)", os.getpid())
        except Exception:
            pass

    atexit.register(audit)


def _loop_exception_handler(loop, context: dict) -> None:
    """Zero-false-positive chokepoint: asyncio only reaches here once it has
    decided nobody retrieved the exception."""
    exception = context.get("exception")
    message = context.get("message") or "asyncio error"
    task = context.get("task") or context.get("future")
    name = None
    try:
        name = task.get_name() if task is not None and hasattr(task, "get_name") else None
    except Exception:
        name = None
    try:
        logger.critical(
            "Unhandled asyncio error%s: %s",
            f" in task {name}" if name else "",
            message,
            exc_info=exception if isinstance(exception, BaseException) else None,
        )
    except Exception:
        pass


def install_loop_handler(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:  # no running loop yet
        return
    try:
        loop.set_exception_handler(_loop_exception_handler)
    except Exception as exc:  # pragma: no cover
        logger.debug("Could not install asyncio exception handler: %s", exc)


def install(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Idempotent. Safe to call from startup before or after the loop exists."""
    global _installed
    if not _installed:
        _enable_faulthandler()
        _install_excepthooks()
        _install_atexit_audit()
        _installed = True
    install_loop_handler(loop)


def _make_death_reporter(oneshot: bool):
    def report(task: "asyncio.Task") -> None:
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except Exception:  # pragma: no cover - task not done / already consumed
            return
        if exception is None:
            if oneshot:
                # A one-shot job finishing is the expected outcome, not news. A
                # warning here would be a false alarm on every boot, and a
                # channel full of false alarms is how a real one gets ignored.
                logger.debug("Startup job %r completed.", task.get_name())
            else:
                # A loop that falls out of its `while` is just as dead as one
                # that raised, and quieter — there is no exception to notice.
                logger.warning(
                    "Background worker %r returned; it is no longer running.",
                    task.get_name(),
                )
            return
        logger.critical(
            "Background task %r died: %s", task.get_name(), exception, exc_info=exception
        )

    return report


def supervise(
    task: "asyncio.Task", name: Optional[str] = None, *, oneshot: bool = False
) -> "asyncio.Task":
    """Log if a task nobody awaits stops unexpectedly.

    ``oneshot=True`` for a job that is *supposed* to finish (a startup sync, a
    preload): only a raised exception is then worth reporting. The default is a
    long-lived worker, where returning at all is a failure.

    Only use this for tasks nobody awaits. A task whose result *is* awaited
    would be reported here as well as by its awaiter, so the caller owns it.
    """
    if name:
        try:
            task.set_name(name)
        except Exception:
            pass
    task.add_done_callback(_make_death_reporter(oneshot))
    return task


def spawn(
    coro: "Coroutine[Any, Any, Any]", name: str, *, oneshot: bool = False
) -> "asyncio.Task":
    """``asyncio.create_task`` for a fire-and-forget task, with supervision.

    A worker started with bare ``create_task`` that raises leaves no trace until
    the task object is garbage-collected — which for one held in a variable for
    the process lifetime is effectively never.
    """
    return supervise(asyncio.create_task(coro), name, oneshot=oneshot)


# ── Self-healing: supervised restarts, owner alerts, event-loop watchdog ─────
# The backend runs its workers inside the API process (see the module
# docstring). Instead of splitting processes, a worker that dies is restarted
# here with backoff and the owner hears about it; a loop that stops turning
# altogether ends the process so Docker's restart policy brings it back.

_ALERT_INTERVAL = 600
_last_alert: dict = {}


def alert_owner(key: str, text: str) -> None:
    """Best-effort Telegram note to the owner, at most once per key per 10 min."""
    import time

    now = time.monotonic()
    if now - _last_alert.get(key, -_ALERT_INTERVAL) < _ALERT_INTERVAL:
        return
    _last_alert[key] = now
    try:
        import backend.bot as bot

        chat = (os.getenv("TELEGRAM_CHAT_ID") or "").split(",")[0].strip()
        app = getattr(bot, "telegram_app", None)
        if not chat or not app or not app.bot:
            return

        async def send() -> None:
            try:
                await app.bot.send_message(chat_id=int(chat), text=text)
            except Exception as exc:
                logger.warning("Owner alert not delivered: %s", type(exc).__name__)

        asyncio.get_running_loop().create_task(send())
    except Exception:
        logger.debug("Owner alert skipped", exc_info=True)


async def _supervised(factory, name: str, max_restarts: int) -> None:
    import time
    from collections import deque

    crashes: deque = deque()
    while True:
        try:
            await factory()
            reason = "вышел из цикла"
            logger.warning("Background worker %r returned; restarting it.", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.critical("Background worker %r died: %s", name, exc, exc_info=exc)
        now = time.monotonic()
        crashes.append(now)
        while crashes and now - crashes[0] > 3600:
            crashes.popleft()
        if len(crashes) > max_restarts:
            logger.critical("Background worker %r keeps dying; giving up until the next restart.", name)
            alert_owner(f"gave-up:{name}", f"⚠️ Hermes: фоновая задача {name} падает снова и снова "
                        f"({reason}); остановлена до перезапуска бэкенда.")
            return
        delay = min(300, 5 * 2 ** (len(crashes) - 1))
        alert_owner(f"restart:{name}", f"⚠️ Hermes: фоновая задача {name} упала ({reason}); перезапуск через {delay} с.")
        await asyncio.sleep(delay)


def spawn_supervised(factory, name: str, *, max_restarts: int = 5) -> "asyncio.Task":
    """Run ``factory()`` (a coroutine function) as a long-lived worker that is
    restarted with backoff when it raises or returns, and reported to the owner.
    More than ``max_restarts`` deaths within an hour stop it for good."""
    return spawn(_supervised(factory, name, max_restarts), name)


async def bounded_start(coro: "Coroutine[Any, Any, Any]", name: str, timeout: float = 45.0) -> Any:
    """Await a startup step without letting one hung channel block the rest."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        logger.critical("Startup step %s did not finish in %ss; continuing without it.", name, timeout)
        alert_owner(f"start:{name}", f"⚠️ Hermes: при запуске «{name}» не ответил за {int(timeout)} с и пропущен.")
    except Exception as exc:
        logger.critical("Startup step %s failed: %s", name, exc, exc_info=exc)
    return None


def start_loop_watchdog(loop: asyncio.AbstractEventLoop, stall_seconds: Optional[float] = None,
                        *, exit_process=os._exit) -> Optional[threading.Thread]:
    """A thread that ends the process when the event loop stops turning for
    ``stall_seconds`` (HERMES_LOOP_STALL_SECONDS, default 180; 0 disables).
    A blocked loop answers nothing, not even the health check, and Docker does
    not restart an unhealthy container — but it does restart one that exited."""
    import time

    if stall_seconds is None:
        stall_seconds = float(os.getenv("HERMES_LOOP_STALL_SECONDS", "180") or 0)
    if stall_seconds <= 0:
        return None
    state = {"beat": time.monotonic()}

    def beat() -> None:
        state["beat"] = time.monotonic()

    def watch() -> None:
        while not loop.is_closed():
            try:
                loop.call_soon_threadsafe(beat)
            except RuntimeError:
                return  # loop closed: orderly shutdown
            time.sleep(min(10.0, stall_seconds / 3))
            stalled = time.monotonic() - state["beat"]
            if stalled > stall_seconds:
                logger.critical("Event loop stalled for %.0fs; exiting so Docker restarts the backend.", stalled)
                try:
                    faulthandler.dump_traceback(file=_fault_log_handle or sys.stderr, all_threads=True)
                except Exception:
                    pass
                exit_process(70)
                return

    thread = threading.Thread(target=watch, name="loop-watchdog", daemon=True)
    thread.start()
    return thread
