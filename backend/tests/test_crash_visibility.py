"""Background-task and process-death visibility.

The failure being guarded: a long-lived worker raises, asyncio reports nothing
until the task object is garbage-collected (never, for one held in a variable),
and the only symptom is that scheduled work quietly stops.
"""

import asyncio
import logging

import pytest

from backend import crash_visibility


@pytest.mark.asyncio
async def test_a_dying_worker_is_logged_with_its_traceback(caplog):
    async def worker():
        raise RuntimeError("provider client exploded")

    with caplog.at_level(logging.CRITICAL, logger="hermes.crash"):
        task = crash_visibility.spawn(worker(), "price_monitor")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert task.done()
    records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert records, "a worker death must never be silent"
    assert "price_monitor" in records[0].getMessage()
    assert "provider client exploded" in records[0].getMessage()
    assert records[0].exc_info is not None


@pytest.mark.asyncio
async def test_a_worker_that_simply_returns_is_also_reported(caplog):
    """A loop that falls out of its while is just as dead as one that raised,
    and used to be even quieter — there is no exception to notice."""
    async def worker():
        return None

    with caplog.at_level(logging.WARNING, logger="hermes.crash"):
        crash_visibility.spawn(worker(), "scheduler_loop")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert any("scheduler_loop" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_one_shot_startup_job_completing_is_not_an_alarm(caplog):
    """A startup sync finishing is the expected outcome. Warning about it on
    every boot is a false alarm, and a channel full of those is how a real
    failure gets ignored."""
    async def startup_job():
        return "done"

    with caplog.at_level(logging.WARNING, logger="hermes.crash"):
        crash_visibility.spawn(startup_job(), "_obsidian_startup_sync", oneshot=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_one_shot_job_that_raises_is_still_reported(caplog):
    async def startup_job():
        raise RuntimeError("vault unreachable")

    with caplog.at_level(logging.CRITICAL, logger="hermes.crash"):
        crash_visibility.spawn(startup_job(), "_obsidian_startup_sync", oneshot=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert any("vault unreachable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cancellation_at_shutdown_is_not_reported_as_a_crash(caplog):
    async def worker():
        await asyncio.sleep(60)

    with caplog.at_level(logging.WARNING, logger="hermes.crash"):
        task = crash_visibility.spawn(worker(), "model_keeper")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert not caplog.records, "an orderly shutdown must not look like a failure"


@pytest.mark.asyncio
async def test_loop_handler_reports_unretrieved_exceptions(caplog):
    loop = asyncio.get_running_loop()
    crash_visibility.install_loop_handler(loop)

    with caplog.at_level(logging.CRITICAL, logger="hermes.crash"):
        loop.call_exception_handler({
            "message": "Task exception was never retrieved",
            "exception": ValueError("boom"),
        })

    assert any("boom" in (r.exc_text or "") or "never retrieved" in r.getMessage()
               for r in caplog.records)


def test_install_is_idempotent_and_never_raises():
    crash_visibility.install()
    crash_visibility.install()
    assert crash_visibility._installed is True


def test_thread_excepthook_logs_and_still_chains(caplog, monkeypatch):
    import sys, threading

    monkeypatch.setattr(crash_visibility, "_installed", False)
    original_sys_hook = sys.excepthook
    original_thread_hook = threading.excepthook
    try:
        crash_visibility.install()

        def explode():
            raise RuntimeError("thread died")

        with caplog.at_level(logging.CRITICAL, logger="hermes.crash"):
            thread = threading.Thread(target=explode, name="worker-1")
            thread.start()
            thread.join()

        assert any("worker-1" in r.getMessage() for r in caplog.records)
    finally:
        sys.excepthook = original_sys_hook
        threading.excepthook = original_thread_hook


def test_a_failing_hook_never_becomes_a_new_silent_failure(caplog, monkeypatch):
    """A logging hook that raises would itself be a new way to lose a crash
    report, so every hook swallows its own errors."""
    def explode(*args, **kwargs):
        raise RuntimeError("logger is gone")

    monkeypatch.setattr(crash_visibility.logger, "critical", explode)
    # Must not propagate.
    crash_visibility._loop_exception_handler(None, {"message": "x", "exception": ValueError("y")})
