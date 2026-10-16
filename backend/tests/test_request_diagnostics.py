"""Slow-request watchdog."""

import logging
import threading

import pytest

from backend import request_diagnostics as rd


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(rd, "_in_flight", {})
    monkeypatch.setattr(rd, "_watchdog", None)
    yield


def test_a_fast_request_is_never_reported(caplog):
    request_id = rd.begin("GET", "/api/sessions")
    with caplog.at_level(logging.WARNING, logger="hermes.slowreq"):
        assert rd._scan_once(now=0.0) == 0
        rd.finish(request_id)
    assert caplog.records == []
    assert rd.in_flight_snapshot() == []


def test_a_stuck_request_is_reported_with_its_path_and_a_stack_dump(caplog, monkeypatch):
    monkeypatch.setattr(rd, "SLOW_REQUEST_SECONDS", 0.0001)
    rd.begin("GET", "/api/metrics")
    with caplog.at_level(logging.WARNING, logger="hermes.slowreq"):
        assert rd._scan_once() == 1
    message = caplog.records[0].getMessage()
    assert "/api/metrics" in message
    assert "Traceback" in message or "File " in message


def test_a_stuck_request_is_reported_once_not_every_tick(caplog, monkeypatch):
    """A request that stays stuck would otherwise dump every thread's stack on
    every tick and bury the log it is supposed to help read."""
    monkeypatch.setattr(rd, "SLOW_REQUEST_SECONDS", 0.0001)
    rd.begin("POST", "/api/chat")
    with caplog.at_level(logging.WARNING, logger="hermes.slowreq"):
        assert rd._scan_once() == 1
        assert rd._scan_once() == 0
        assert rd._scan_once() == 0
    assert len(caplog.records) == 1


def test_a_reported_request_logs_its_total_duration_when_it_finishes(caplog, monkeypatch):
    monkeypatch.setattr(rd, "SLOW_REQUEST_SECONDS", 0.0001)
    request_id = rd.begin("GET", "/api/models")
    rd._scan_once()
    with caplog.at_level(logging.WARNING, logger="hermes.slowreq"):
        duration = rd.finish(request_id)
    assert duration > 0
    assert any("Slow request finished" in r.getMessage() for r in caplog.records)


def test_long_lived_endpoints_are_never_tracked():
    """The websocket is long-lived by design; reporting it as stuck would make
    the signal worthless."""
    assert rd.begin("GET", "/api/ws") is None
    assert rd.in_flight_snapshot() == []
    assert rd.finish(None) == 0.0


def test_the_watchdog_is_one_thread_regardless_of_request_count():
    """One timer per request exhausts the process thread cap under polling load
    and stops the server accepting connections."""
    before = threading.active_count()
    for index in range(200):
        rd.begin("GET", f"/api/thing/{index}")
    assert threading.active_count() <= before + 1
    assert len(rd.in_flight_snapshot()) == 200


def test_finish_is_safe_for_an_unknown_id():
    assert rd.finish("does-not-exist") == 0.0
