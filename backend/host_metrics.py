"""History of physical-host metrics for the «Метрики» dashboard.

The host agent (deploy/hermes-host-telemetry.py) only keeps the *latest*
snapshot, so charts over hours need somewhere to accumulate. A background loop
here copies a compact sample out of that snapshot every SAMPLE_INTERVAL_S into
its own small SQLite file, and the history endpoint serves it downsampled.

Why a separate database file rather than hermes.db: the samples are disposable
(a week of them, rebuilt by simply waiting), they would bloat the nightly backup
of the database that actually matters, and a write every 30 s should never sit
in the same WAL as a user's chat turn.

Nothing is interpolated or invented. A stretch with no fresh snapshot (agent
stopped, backend down) is simply absent, and the UI draws it as a gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 30
RETENTION_S = 7 * 24 * 3600
# A snapshot older than this is the agent's last word before it stopped, not a
# measurement of now — recording it again would draw a flat line over an outage.
MAX_SNAPSHOT_AGE_S = 30
TARGET_POINTS = 240

RANGES: Dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
}

# Alert thresholds. Kept together so the UI and these rules cannot drift apart:
# the endpoint returns the threshold it used alongside each alert.
GPU_TEMP_WARN_C = 80.0
GPU_TEMP_CRIT_C = 87.0
CPU_TEMP_WARN_C = 85.0
GPU_PEAK_UTIL = 98.0
GPU_PEAK_MIN_S = 5 * 60
RAM_WARN_PERCENT = 90.0
DISK_WARN_PERCENT = 85.0
# A hole in the samples longer than this is reported: the collector or the
# backend was down, which is exactly what someone reading the charts wants to know.
GAP_ALERT_S = 5 * 60


def _db_path() -> str:
    return os.getenv(
        "HOST_METRICS_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "host-metrics.db"),
    )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS host_metric_samples ("
        " ts INTEGER PRIMARY KEY,"
        " sample TEXT NOT NULL)"
    )
    return conn


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def compact_sample(host: Dict[str, Any]) -> Dict[str, Any]:
    """The handful of numbers the charts need, out of a full host snapshot.

    Keys are short on purpose: a week at 30 s is ~20k rows.
    """
    cpu = host.get("cpu") if isinstance(host.get("cpu"), dict) else {}
    memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
    network = host.get("network") if isinstance(host.get("network"), dict) else {}
    disk_io = host.get("disk_io") if isinstance(host.get("disk_io"), dict) else {}
    temps = host.get("temperatures") if isinstance(host.get("temperatures"), dict) else {}
    disks = [d for d in host.get("disks") or [] if isinstance(d, dict)]
    gpus = [g for g in host.get("gpus") or [] if isinstance(g, dict)]

    nvme_temps = [
        _num(item.get("celsius")) for item in temps.get("nvme") or [] if isinstance(item, dict)
    ]
    nvme_temps = [t for t in nvme_temps if t is not None]
    disk_total = sum(_num(d.get("total_bytes")) or 0 for d in disks)
    disk_used = sum(_num(d.get("used_bytes")) or 0 for d in disks)

    return {
        "cpu": _num(cpu.get("usage_percent")),
        "ram": _num(memory.get("usage_percent")),
        "ram_used": _num(memory.get("used_bytes")),
        "disk_used": disk_used if disks else None,
        "disk_total": disk_total if disks else None,
        "rx": _num(network.get("rx_bytes_per_second")),
        "tx": _num(network.get("tx_bytes_per_second")),
        "io_r": _num(disk_io.get("read_bytes_per_second")),
        "io_w": _num(disk_io.get("write_bytes_per_second")),
        "cpu_temp": _num(temps.get("cpu_celsius")),
        "nvme_temp": max(nvme_temps) if nvme_temps else None,
        "gpus": [
            {
                "i": int(_num(g.get("index")) or 0),
                "util": _num(g.get("utilization_percent")),
                "mem": _num(g.get("memory_used_bytes")),
                "mem_total": _num(g.get("memory_total_bytes")),
                "temp": _num(g.get("temperature_celsius")),
                "power": _num(g.get("power_draw_watts")),
                "limit": _num(g.get("power_limit_watts")),
            }
            for g in gpus
        ],
    }


def record_snapshot(snapshot: Dict[str, Any], now: Optional[float] = None) -> bool:
    """Store one sample from a host snapshot. Returns False when it was skipped.

    Keyed by the snapshot's own collection time, so reading the same snapshot
    twice (the agent writes every 5 s, a slow loop may see it again) is a no-op.
    """
    from backend.tools import _parse_collected_at

    now = time.time() if now is None else now
    collected = _parse_collected_at(snapshot.get("collected_at"))
    host = snapshot.get("host")
    if collected is None or not isinstance(host, dict):
        return False
    ts = int(collected.timestamp())
    if now - ts > MAX_SNAPSHOT_AGE_S:
        return False
    payload = json.dumps(compact_sample(host), separators=(",", ":"))
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO host_metric_samples (ts, sample) VALUES (?, ?)",
            (ts, payload),
        )
        conn.execute("DELETE FROM host_metric_samples WHERE ts < ?", (int(now) - RETENTION_S,))
    return True


def record_once() -> bool:
    from backend.tools import _read_host_telemetry

    snapshot, _error = _read_host_telemetry()
    if not snapshot:
        return False
    return record_snapshot(snapshot)


async def sampler_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(record_once)
        except Exception:
            # A full disk or a locked file costs a few chart points, never the backend.
            logger.exception("Could not record a host metrics sample")
        await asyncio.sleep(SAMPLE_INTERVAL_S)


def _load(since: int) -> List[tuple[int, Dict[str, Any]]]:
    rows: List[tuple[int, Dict[str, Any]]] = []
    with closing(_connect()) as conn:
        cursor = conn.execute(
            "SELECT ts, sample FROM host_metric_samples WHERE ts >= ? ORDER BY ts", (since,)
        )
        for ts, raw in cursor:
            try:
                sample = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(sample, dict):
                rows.append((int(ts), sample))
    return rows


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 2)


def _peak(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return max(present) if present else None


SCALAR_FIELDS = ("cpu", "ram", "ram_used", "disk_used", "disk_total", "rx", "tx", "io_r", "io_w", "cpu_temp", "nvme_temp")
GPU_FIELDS = ("util", "mem", "mem_total", "temp", "power", "limit")


def downsample(rows: List[tuple[int, Dict[str, Any]]], bucket_s: int) -> List[Dict[str, Any]]:
    """Average samples into fixed time buckets aligned to the epoch.

    Averages hide short spikes, so each bucket also carries the peak GPU
    utilisation and temperature — the values an alert would have fired on.
    """
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for ts, sample in rows:
        buckets.setdefault(ts - ts % bucket_s, []).append(sample)

    points = []
    for start in sorted(buckets):
        samples = buckets[start]
        point: Dict[str, Any] = {"t": start}
        for field in SCALAR_FIELDS:
            point[field] = _mean(s.get(field) for s in samples)
        by_index: Dict[int, List[Dict[str, Any]]] = {}
        for s in samples:
            for gpu in s.get("gpus") or []:
                if isinstance(gpu, dict):
                    by_index.setdefault(int(gpu.get("i") or 0), []).append(gpu)
        point["gpus"] = []
        for index in sorted(by_index):
            gpu_samples = by_index[index]
            gpu_point: Dict[str, Any] = {"i": index}
            for field in GPU_FIELDS:
                gpu_point[field] = _mean(g.get(field) for g in gpu_samples)
            gpu_point["util_peak"] = _peak(g.get("util") for g in gpu_samples)
            gpu_point["temp_peak"] = _peak(g.get("temp") for g in gpu_samples)
            point["gpus"].append(gpu_point)
        points.append(point)
    return points


def _episodes(rows: List[tuple[int, Dict[str, Any]]], value_of, threshold: float, min_duration_s: int = 0):
    """Contiguous runs of samples whose value is >= threshold.

    A missing sample (gap) ends an episode: we cannot claim the condition held
    through a stretch nobody measured.
    """
    episodes = []
    current = None
    previous_ts = None
    for ts, sample in rows:
        value = value_of(sample)
        broken = previous_ts is not None and ts - previous_ts > 2 * SAMPLE_INTERVAL_S
        if current is not None and (broken or value is None or value < threshold):
            episodes.append(current)
            current = None
        if value is not None and value >= threshold:
            if current is None:
                current = {"start": ts, "end": ts, "peak": value}
            else:
                current["end"] = ts
                current["peak"] = max(current["peak"], value)
        previous_ts = ts
    if current is not None:
        episodes.append(current)
    return [e for e in episodes if e["end"] - e["start"] >= min_duration_s]


def _gpu_value(index: int, field: str):
    def read(sample: Dict[str, Any]) -> Optional[float]:
        for gpu in sample.get("gpus") or []:
            if isinstance(gpu, dict) and gpu.get("i") == index:
                return _num(gpu.get(field))
        return None
    return read


def detect_alerts(rows: List[tuple[int, Dict[str, Any]]], now: float) -> List[Dict[str, Any]]:
    """Threshold crossings in the window, newest first.

    Alerts carry structured fields (kind, gpu, peak, threshold) rather than
    prose so the dashboard can word them in the user's language.
    """
    alerts: List[Dict[str, Any]] = []
    latest_ts = rows[-1][0] if rows else None

    def add(kind: str, episode: Dict[str, Any], threshold: float, severity: str, **extra):
        ongoing = latest_ts is not None and episode["end"] == latest_ts and now - latest_ts <= 2 * SAMPLE_INTERVAL_S
        alerts.append({
            "kind": kind,
            "severity": severity,
            "start": episode["start"],
            "end": episode["end"],
            "ongoing": ongoing,
            "peak": round(episode["peak"], 1),
            "threshold": threshold,
            **extra,
        })

    gpu_indexes = sorted({
        int(g.get("i") or 0)
        for _, s in rows for g in s.get("gpus") or [] if isinstance(g, dict)
    })
    for index in gpu_indexes:
        for episode in _episodes(rows, _gpu_value(index, "temp"), GPU_TEMP_WARN_C):
            severity = "critical" if episode["peak"] >= GPU_TEMP_CRIT_C else "warning"
            add("gpu_temp", episode, GPU_TEMP_WARN_C, severity, gpu=index)
        for episode in _episodes(rows, _gpu_value(index, "util"), GPU_PEAK_UTIL, GPU_PEAK_MIN_S):
            add("gpu_peak", episode, GPU_PEAK_UTIL, "info", gpu=index)

    for episode in _episodes(rows, lambda s: _num(s.get("cpu_temp")), CPU_TEMP_WARN_C):
        add("cpu_temp", episode, CPU_TEMP_WARN_C, "warning")
    for episode in _episodes(rows, lambda s: _num(s.get("ram")), RAM_WARN_PERCENT):
        add("ram", episode, RAM_WARN_PERCENT, "warning")

    if rows:
        disk_used = _num(rows[-1][1].get("disk_used"))
        disk_total = _num(rows[-1][1].get("disk_total"))
        if disk_used is not None and disk_total:
            percent = disk_used / disk_total * 100
            if percent >= DISK_WARN_PERCENT:
                alerts.append({
                    "kind": "disk", "severity": "warning", "start": rows[-1][0], "end": rows[-1][0],
                    "ongoing": True, "peak": round(percent, 1), "threshold": DISK_WARN_PERCENT,
                })

    for (previous_ts, _), (ts, _) in zip(rows, rows[1:]):
        if ts - previous_ts > GAP_ALERT_S:
            alerts.append({
                "kind": "telemetry_gap", "severity": "info", "start": previous_ts, "end": ts,
                "ongoing": False, "peak": None, "threshold": None,
            })

    alerts.sort(key=lambda a: a["end"], reverse=True)
    return alerts


def history(range_key: str, now: Optional[float] = None) -> Dict[str, Any]:
    if range_key not in RANGES:
        raise ValueError(f"Unknown range '{range_key}', expected one of {', '.join(RANGES)}")
    now = time.time() if now is None else now
    span = RANGES[range_key]
    since = int(now) - span
    rows = _load(since)
    bucket_s = max(SAMPLE_INTERVAL_S, int(math.ceil(span / TARGET_POINTS / SAMPLE_INTERVAL_S)) * SAMPLE_INTERVAL_S)
    return {
        "range": range_key,
        "from": since,
        "to": int(now),
        "bucket_seconds": bucket_s,
        "sample_interval_seconds": SAMPLE_INTERVAL_S,
        "samples": len(rows),
        "first_sample_at": rows[0][0] if rows else None,
        "points": downsample(rows, bucket_s),
        "alerts": detect_alerts(rows, now)[:20],
        "thresholds": {
            "gpu_temp_warn": GPU_TEMP_WARN_C,
            "gpu_temp_crit": GPU_TEMP_CRIT_C,
            "cpu_temp_warn": CPU_TEMP_WARN_C,
            "ram_warn": RAM_WARN_PERCENT,
            "disk_warn": DISK_WARN_PERCENT,
        },
    }
