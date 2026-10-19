from datetime import datetime, timezone

import pytest

from backend import host_metrics


@pytest.fixture(autouse=True)
def metrics_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_METRICS_DB_PATH", str(tmp_path / "host-metrics.db"))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot(ts: float, *, gpu_util=50.0, gpu_temp=60.0, cpu=20.0, ram=40.0):
    return {
        "schema_version": 1,
        "collected_at": iso(ts),
        "host": {
            "cpu": {"usage_percent": cpu},
            "memory": {"usage_percent": ram, "used_bytes": 10 * 2**30},
            "disks": [
                {"mountpoint": "/", "total_bytes": 1000, "used_bytes": 200},
                {"mountpoint": "/mnt/fast_storage", "total_bytes": 1000, "used_bytes": 100},
            ],
            "network": {"rx_bytes_per_second": 1000, "tx_bytes_per_second": 500},
            "disk_io": {"read_bytes_per_second": 4096, "write_bytes_per_second": 8192},
            "temperatures": {"cpu_celsius": 45.0, "nvme": [{"celsius": 30.0}, {"celsius": 41.0}]},
            "gpus": [
                {"index": 0, "utilization_percent": gpu_util, "temperature_celsius": gpu_temp,
                 "memory_used_bytes": 2**30, "memory_total_bytes": 24 * 2**30,
                 "power_draw_watts": 200.0, "power_limit_watts": 300.0},
                {"index": 1, "utilization_percent": 10.0, "temperature_celsius": 40.0},
            ],
        },
    }


def test_compact_sample_sums_disks_and_takes_hottest_nvme():
    sample = host_metrics.compact_sample(snapshot(0)["host"])
    assert sample["disk_used"] == 300
    assert sample["disk_total"] == 2000
    assert sample["nvme_temp"] == 41.0
    assert sample["io_w"] == 8192
    assert sample["gpus"][0] == {
        "i": 0, "util": 50.0, "mem": float(2**30), "mem_total": float(24 * 2**30),
        "temp": 60.0, "power": 200.0, "limit": 300.0,
    }


def test_compact_sample_tolerates_an_old_agent_without_new_fields():
    host = snapshot(0)["host"]
    del host["disk_io"], host["temperatures"]
    sample = host_metrics.compact_sample(host)
    assert sample["io_r"] is None and sample["cpu_temp"] is None and sample["nvme_temp"] is None


def test_stale_snapshot_is_not_recorded():
    """An agent that stopped leaves its last snapshot behind; replaying it would
    draw a flat line straight across the outage."""
    now = 1_000_000
    assert host_metrics.record_snapshot(snapshot(now - 120), now=now) is False
    assert host_metrics.history("1h", now=now)["samples"] == 0


def test_same_snapshot_read_twice_is_stored_once():
    now = 1_000_000
    assert host_metrics.record_snapshot(snapshot(now - 2), now=now)
    assert host_metrics.record_snapshot(snapshot(now - 2), now=now)
    assert host_metrics.history("1h", now=now)["samples"] == 1


def test_samples_older_than_retention_are_pruned():
    now = 2_000_000
    host_metrics.record_snapshot(snapshot(now - host_metrics.RETENTION_S - 100), now=now - host_metrics.RETENTION_S - 90)
    host_metrics.record_snapshot(snapshot(now - 5), now=now)
    assert host_metrics.history("7d", now=now + 10**6)["samples"] == 0
    assert host_metrics.history("7d", now=now)["samples"] == 1


def test_history_downsamples_and_keeps_gpu_peaks():
    now = 1_000_200
    for offset, util in ((0, 20.0), (30, 100.0)):
        ts = now - 100 + offset
        host_metrics.record_snapshot(snapshot(ts, gpu_util=util), now=ts + 1)
    result = host_metrics.history("24h", now=now)
    assert result["bucket_seconds"] == 360
    assert len(result["points"]) == 1
    gpu0 = result["points"][0]["gpus"][0]
    assert gpu0["util"] == 60.0
    assert gpu0["util_peak"] == 100.0


def test_unknown_range_is_rejected():
    with pytest.raises(ValueError):
        host_metrics.history("3y")


def rows(values, field="temp", start=0):
    return [
        (start + i * host_metrics.SAMPLE_INTERVAL_S, {"gpus": [{"i": 0, field: v}]})
        for i, v in enumerate(values)
    ]


def test_gpu_temperature_episode_reports_peak_and_severity():
    alerts = host_metrics.detect_alerts(rows([70, 82, 88, 81, 70]), now=10**6)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["kind"] == "gpu_temp" and alert["gpu"] == 0
    assert alert["peak"] == 88 and alert["severity"] == "critical"
    assert alert["start"] == 30 and alert["end"] == 90
    assert alert["ongoing"] is False


def test_alert_still_above_threshold_is_ongoing():
    data = rows([70, 82, 83])
    alerts = host_metrics.detect_alerts(data, now=data[-1][0] + 5)
    assert alerts[0]["ongoing"] is True


def test_gap_splits_an_episode_and_is_reported():
    """Nobody measured the stretch between the samples, so the condition is not
    claimed to have held through it."""
    data = rows([85, 85]) + rows([85, 85], start=3600)
    alerts = host_metrics.detect_alerts(data, now=10**6)
    kinds = sorted(a["kind"] for a in alerts)
    assert kinds == ["gpu_temp", "gpu_temp", "telemetry_gap"]


def test_short_full_load_is_not_a_peak_alert():
    short = rows([99] * 5, field="util")
    long = rows([99] * 12, field="util")
    assert not [a for a in host_metrics.detect_alerts(short, now=10**6) if a["kind"] == "gpu_peak"]
    assert [a for a in host_metrics.detect_alerts(long, now=10**6) if a["kind"] == "gpu_peak"]


def test_history_endpoint_serves_recorded_samples_and_rejects_unknown_ranges():
    from fastapi.testclient import TestClient
    from backend.auth import active_sessions
    from backend.main import app

    active_sessions.add("host-metrics-token")
    client = TestClient(app)
    client.headers = {"Authorization": "Bearer host-metrics-token"}
    import time
    now = time.time()
    host_metrics.record_snapshot(snapshot(now - 3), now=now)

    response = client.get("/api/system/metrics/history?range=1h")
    assert response.status_code == 200
    body = response.json()
    assert body["samples"] == 1
    assert body["points"][0]["gpus"][0]["util"] == 50.0

    assert client.get("/api/system/metrics/history?range=forever").status_code == 400
