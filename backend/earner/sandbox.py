"""Isolated execution of an approved artifact's tests.

The broker is this module, inside the backend, talking to the existing
docker-socket-proxy (DOCKER_HOST). It holds no new privilege: the backend
already reaches that proxy for dev-run sandboxes. What it adds is a *fixed*
container spec — nothing in it comes from the model except the files, which
travel as data in one environment variable:

* runtime ``runsc`` (gVisor). If Docker has no such runtime, execution is
  unavailable. There is no runc fallback, and ``runc`` cannot be configured;
* ``network_mode=none``, no volumes or bind mounts, no devices, no GPU, no
  environment beyond the payload — none of the backend's secrets;
* uid 10001, all capabilities dropped, no-new-privileges, read-only rootfs,
  a small noexec tmpfs for the files;
* CPU, memory (swap included), PIDs, open files, file size, log size and
  wall-clock limits;
* a constant bootstrap (BOOTSTRAP below) runs ``python -I -m unittest
  discover`` — never a shell, never a command the model wrote;
* the container is always removed; orphans from a crash are removed at
  startup (label ``hermes.earner.sandbox``).

Known limit of the surrounding system (docs/autonomous-earner.md): the
docker-socket-proxy allows container create with any body, so anyone who
controls the backend process can already start a privileged container. This
module does not widen that, and agents never hold the proxy address' network.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.earner.sandbox")

LABEL = "hermes.earner.sandbox"
RESULT_MARKER = "@@HERMES_EARNER_RESULT@@"
MAX_PAYLOAD_BYTES = 96 * 1024
MAX_LOG_BYTES = 64 * 1024
_FORBIDDEN_RUNTIMES = {"", "runc", "io.containerd.runc.v2", "nvidia", "crun"}

_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def _runtime() -> str:
    return os.getenv("EARNER_SANDBOX_RUNTIME", "runsc").strip()


def _image() -> str:
    return os.getenv("EARNER_SANDBOX_IMAGE", "hermes-web-dev-runner:local").strip()


def _timeout() -> int:
    try:
        return max(5, min(int(os.getenv("EARNER_SANDBOX_TIMEOUT", "60")), 300))
    except ValueError:
        return 60


# The only code that runs in the container besides the artifact. It unpacks
# the files, runs the tests in a child process, then kills every other
# process in the container before printing the single result line — so a
# test that forks a straggler cannot print a forged result after it.
BOOTSTRAP = r'''
import base64, gzip, json, os, signal, subprocess, sys, time
LIMIT = int(sys.argv[1])
files = json.loads(gzip.decompress(base64.b64decode(os.environ.pop("EARNER_PAYLOAD"))))
os.makedirs("/tmp/work", mode=0o700)
for name, text in files.items():
    if "/" in name or name.startswith("."):
        raise SystemExit(3)
    with open("/tmp/work/" + name, "w", encoding="utf-8") as handle:
        handle.write(text)
started = time.monotonic()
result = {"timed_out": False}
try:
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "/tmp/work", "-t", "/tmp/work",
         "-p", "test_*.py", "-v"],
        cwd="/tmp/work", capture_output=True, text=True, timeout=LIMIT, start_new_session=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"})
    result.update(exit_code=done.returncode, stdout=done.stdout[-20000:], stderr=done.stderr[-20000:])
except subprocess.TimeoutExpired as exc:
    result.update(exit_code=None, timed_out=True,
                  stdout=(exc.stdout or b"")[-20000:].decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-20000:],
                  stderr=(exc.stderr or b"")[-20000:].decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")[-20000:])
result["duration_s"] = round(time.monotonic() - started, 2)
me = os.getpid()
for _ in range(5):
    others = [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me]
    if not others:
        break
    for pid in others:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.05)
sys.stdout.write("\n" + "@@HERMES_EARNER_RESULT@@" + json.dumps(result) + "\n")
sys.stdout.flush()
'''


class SandboxUnavailable(RuntimeError):
    pass


def public_spec() -> Dict[str, Any]:
    """What the operator is shown and approves (part of the approval hash)."""
    return {
        "runtime": _runtime(),
        "image": _image(),
        "network": "none",
        "user": "10001:10001",
        "cpus": 1,
        "memory": "512m",
        "pids": 64,
        "tmpfs": "64m noexec",
        "read_only_rootfs": True,
        "timeout_s": _timeout(),
        "command": "python -I -m unittest discover -p 'test_*.py'",
    }


def _client():
    import docker

    return docker.from_env(timeout=20)


def availability(refresh: bool = False) -> Dict[str, Any]:
    now = time.monotonic()
    if not refresh and _cache["value"] is not None and now - _cache["at"] < 30:
        return _cache["value"]
    value = _check()
    _cache.update(at=now, value=value)
    return value


def _check() -> Dict[str, Any]:
    runtime, image = _runtime(), _image()
    base = {"available": False, "runtime": runtime, "image": image}
    if os.getenv("EARNER_EXECUTION_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
        return {**base, "reason": "исполнение выключено (EARNER_EXECUTION_ENABLED=false)"}
    if runtime.lower() in _FORBIDDEN_RUNTIMES:
        return {**base, "reason": f"runtime «{runtime or 'по умолчанию'}» не изолирует недоверенный код; нужен gVisor runsc"}
    try:
        client = _client()
        info = client.info()
    except Exception as exc:  # docker SDK raises several unrelated types
        return {**base, "reason": f"Docker API недоступен ({type(exc).__name__})"}
    runtimes = sorted((info.get("Runtimes") or {}).keys())
    if runtime not in runtimes:
        return {**base, "runtimes": runtimes,
                "reason": f"в Docker нет runtime «{runtime}» (gVisor не установлен); доступны: {', '.join(runtimes)}"}
    try:
        client.images.get(image)
    except Exception:
        return {**base, "runtimes": runtimes,
                "reason": f"образ {image} не найден локально (автоматически не скачивается)"}
    return {**base, "runtimes": runtimes, "available": True, "reason": ""}


def encode_payload(files: List[Dict[str, str]]) -> str:
    mapping = {f["path"]: f["content"] for f in files}
    payload = base64.b64encode(gzip.compress(json.dumps(mapping).encode("utf-8"))).decode("ascii")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise SandboxUnavailable("артефакт слишком большой для передачи в sandbox")
    return payload


def container_kwargs(payload: str, opp_id: str) -> Dict[str, Any]:
    from docker.types import LogConfig, Ulimit

    spec = public_spec()
    if spec["runtime"].lower() in _FORBIDDEN_RUNTIMES:
        raise SandboxUnavailable("runc не используется для недоверенного кода")
    return {
        "image": spec["image"],
        "entrypoint": ["python", "-I", "-c", BOOTSTRAP],
        "command": [str(spec["timeout_s"])],
        "runtime": spec["runtime"],
        "network_mode": "none",
        "user": spec["user"],
        "working_dir": "/tmp",
        "environment": {"EARNER_PAYLOAD": payload},
        "read_only": True,
        "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"},
        "mem_limit": spec["memory"],
        "memswap_limit": spec["memory"],
        "nano_cpus": int(spec["cpus"] * 1_000_000_000),
        "pids_limit": spec["pids"],
        "ulimits": [Ulimit(name="nofile", soft=256, hard=256),
                    Ulimit(name="fsize", soft=16 * 1024 * 1024, hard=16 * 1024 * 1024)],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "ipc_mode": "none",
        "privileged": False,
        "volumes": {},
        "devices": [],
        "labels": {LABEL: "1", "hermes.earner.opportunity": opp_id},
        "log_config": LogConfig(type="json-file", config={"max-size": "256k", "max-file": "1"}),
        "hostname": "earner-sandbox",
        "stdin_open": False,
        "tty": False,
        "detach": True,
    }


def parse_output(logs: str) -> Dict[str, Any]:
    lines = [line for line in logs.splitlines() if line.strip()]
    markers = [line for line in lines if line.startswith(RESULT_MARKER)]
    if len(markers) != 1 or not lines or lines[-1] != markers[0]:
        return {"status": "inconclusive", "detail": "результат sandbox отсутствует или подделан",
                "log_tail": logs[-4000:]}
    try:
        data = json.loads(markers[0][len(RESULT_MARKER):])
    except ValueError:
        return {"status": "inconclusive", "detail": "результат sandbox не разобран", "log_tail": logs[-4000:]}
    if data.get("timed_out"):
        status = "timeout"
    elif data.get("exit_code") == 0:
        status = "passed"
    else:
        status = "failed"
    return {"status": status, "exit_code": data.get("exit_code"), "duration_s": data.get("duration_s"),
            "stdout": str(data.get("stdout") or "")[-20000:], "stderr": str(data.get("stderr") or "")[-20000:]}


def describe_exit(result: Dict[str, Any], state: Dict[str, Any], killed: bool) -> Dict[str, Any]:
    """Turn a container that died without a result line into a reason the
    operator can read. gVisor tears the whole sandbox down when the memory
    cgroup is exhausted (a 2 GiB allocation, or a fork bomb of Python
    processes), so the bootstrap never gets to print."""
    exit_code = state.get("ExitCode")
    update: Dict[str, Any] = {"container_exit_code": exit_code, "oom_killed": bool(state.get("OOMKilled"))}
    if killed:
        update.update(status="timeout", detail=f"превышено время ({_timeout()} с) — контейнер остановлен")
    elif result.get("status") == "inconclusive" and RESULT_MARKER not in (result.get("log_tail") or ""):
        update.update(status="killed",
                      detail="контейнер принудительно завершён до вывода результата "
                             f"(код {exit_code}{', OOM' if state.get('OOMKilled') else ''}) — "
                             "вероятно, превышен лимит памяти или процессов")
    return update


def run(files: List[Dict[str, str]], opp_id: str) -> Dict[str, Any]:
    """Blocking: create → start → wait (bounded) → read logs → always remove."""
    state = availability(refresh=True)
    if not state["available"]:
        raise SandboxUnavailable(state["reason"])
    payload = encode_payload(files)
    client = _client()
    container = client.containers.create(**container_kwargs(payload, opp_id))
    started = time.monotonic()
    killed = False
    try:
        container.start()
        try:
            container.wait(timeout=_timeout() + 20)
        except Exception:
            killed = True
            try:
                container.kill()
            except Exception:
                logger.warning("Could not kill sandbox %s", container.short_id)
        raw = container.logs(stdout=True, stderr=True) or b""
        logs = raw[-MAX_LOG_BYTES:].decode("utf-8", errors="replace")
        result = parse_output(logs)
        try:
            container.reload()
            state = container.attrs.get("State") or {}
        except Exception:
            state = {}
        result.update(describe_exit(result, state, killed))
        result["wall_s"] = round(time.monotonic() - started, 2)
        result["runtime"] = _runtime()
        return result
    finally:
        try:
            container.remove(force=True)
        except Exception:
            logger.warning("Could not remove sandbox container %s; startup cleanup will retry", container.short_id)


def cleanup_orphans() -> int:
    """Remove sandbox containers a crashed process left behind."""
    try:
        client = _client()
        leftovers = client.containers.list(all=True, filters={"label": LABEL})
    except Exception as exc:
        logger.info("Sandbox cleanup skipped: %s", type(exc).__name__)
        return 0
    removed = 0
    for container in leftovers:
        try:
            container.remove(force=True)
            removed += 1
        except Exception:
            logger.warning("Could not remove orphaned sandbox %s", container.short_id)
    return removed


# ── repository mode ──────────────────────────────────────────────────────────
#
# A real repository does not fit into an environment variable, and the
# docker-socket-proxy (deliberately) allows neither volumes nor builds. So the
# repository travels over the container's stdin: the backend attaches to the
# created container and writes one length-prefixed stream — the run plan as
# JSON, then the commit tarball. The bootstrap unpacks it into tmpfs inside
# gVisor, runs the repository's tests on the base commit (baseline) and on the
# patched tree, kills every other process, and prints the single result line.
# Still no network, no mounts, no environment, read-only rootfs.

REPO_MAX_STREAM_BYTES = 72 * 1024 * 1024


def _repo_image() -> str:
    return os.getenv("EARNER_REPO_SANDBOX_IMAGE", "").strip() or _image()


def _repo_timeout() -> int:
    try:
        return max(30, min(int(os.getenv("EARNER_REPO_SANDBOX_TIMEOUT", "240")), 900))
    except ValueError:
        return 240


def _repo_baseline() -> bool:
    return os.getenv("EARNER_REPO_BASELINE", "true").lower() in ("1", "true", "yes", "on")


def public_repo_spec() -> Dict[str, Any]:
    """What the operator is shown for a repository run (part of the hash)."""
    return {
        "runtime": _runtime(),
        "image": _repo_image(),
        "network": "none",
        "user": "10001:10001",
        "cpus": 2,
        "memory": "2g",
        "pids": 256,
        "tmpfs": "1g noexec",
        "read_only_rootfs": True,
        "timeout_s": _repo_timeout(),
        "baseline": _repo_baseline(),
        "transport": "stdin (план JSON + архив коммита)",
    }


def repo_availability() -> Dict[str, Any]:
    state = availability()
    if not state["available"]:
        return state
    image = _repo_image()
    if image == state.get("image"):
        return state
    try:
        _client().images.get(image)
    except Exception:
        return {**state, "available": False, "image": image,
                "reason": f"образ {image} для тестов репозитория не найден локально (см. backend/earner/runner)"}
    return {**state, "image": image}


REPO_BOOTSTRAP = r'''
import hashlib, io, json, os, shutil, signal, struct, subprocess, sys, tarfile, time
import xml.etree.ElementTree as ET
def read_exact(n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(min(1 << 20, n - len(buf)))
        if not chunk:
            raise SystemExit(4)
        buf.extend(chunk)
    return bytes(buf)
size = struct.unpack(">Q", read_exact(8))[0]
if size > (4 << 20):
    raise SystemExit(4)
meta = json.loads(read_exact(size))
size = struct.unpack(">Q", read_exact(8))[0]
if size > (64 << 20):
    raise SystemExit(4)
data = read_exact(size)
result = {"transport_ok": hashlib.sha256(data).hexdigest() == meta["tarball_sha256"], "runner": meta["runner"]}
def relative(name):
    parts = name.split("/", 1)
    if len(parts) < 2 or not parts[1]:
        return None
    rel = os.path.normpath(parts[1])
    if rel.startswith("..") or os.path.isabs(rel) or rel.split(os.sep)[0] == ".git":
        return None
    return rel
def unpack(dest):
    os.makedirs(dest, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            rel = relative(member.name) if member.isreg() else None
            if rel is None:
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.extractfile(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
def kill_others():
    me = os.getpid()
    for _ in range(5):
        others = [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me]
        if not others:
            return
        for pid in others:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)
def tail(value, limit):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return (value or "")[-limit:]
def junit(path):
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    total, skipped, failed, passed = 0, 0, [], set()
    for case in root.iter("testcase"):
        total += 1
        tags = {child.tag for child in case}
        if "skipped" in tags:
            skipped += 1
        if tags & {"failure", "error"}:
            failed.append((case.get("classname") or "") + "::" + (case.get("name") or ""))
        elif "skipped" not in tags:
            passed.add((case.get("classname") or "")[:200])
    return {"total": total, "skipped": skipped, "failed_count": len(failed), "failed": sorted(f[:240] for f in failed)[:150],
            "passed_classes": sorted(passed)[:200]}
def run(cwd, phase):
    scratch = "/tmp/scratch-" + phase
    os.makedirs(scratch, exist_ok=True)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": scratch, "TMPDIR": scratch, "CI": "1", "NO_COLOR": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
           "PYTHONPATH": os.pathsep.join(os.path.normpath(os.path.join(cwd, p)) for p in meta["pythonpath"])}
    report = "/tmp/junit-" + phase + ".xml"
    if meta["runner"] == "pytest":
        base = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no",
                "--continue-on-collection-errors", "--junitxml=" + report, "-o", "junit_family=xunit2"]
    else:
        base = [sys.executable, "-B", "-m", "unittest", "discover", "-v"]
    out = {"phase": phase, "timed_out": False, "addopts_cleared": False}
    started = time.monotonic()
    command = base
    for attempt in (1, 2):
        try:
            done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=meta["limit"],
                                  start_new_session=True, env=env)
            out.update(exit_code=done.returncode, stdout=tail(done.stdout, 8000), stderr=tail(done.stderr, 4000))
        except subprocess.TimeoutExpired as exc:
            out.update(exit_code=None, timed_out=True, stdout=tail(exc.stdout, 8000), stderr=tail(exc.stderr, 4000))
        kill_others()
        # pytest that stopped before writing its report never ran a test: almost
        # always addopts naming a plugin the image lacks (--cov → exit 4,
        # -p plugin → exit 1). One retry without the repository's addopts.
        if meta["runner"] == "pytest" and attempt == 1 and out.get("exit_code") not in (0, 5, None) \
                and not os.path.exists(report) and time.monotonic() - started < 60:
            command = base + ["-o", "addopts="]
            out["addopts_cleared"] = True
            continue
        break
    out["duration_s"] = round(time.monotonic() - started, 2)
    out["tests"] = junit(report) if meta["runner"] == "pytest" else None
    return out
missing = []
try:
    from importlib import metadata
    for name in meta.get("dependencies") or []:
        try:
            metadata.version(name)
        except Exception:
            missing.append(name)
except Exception:
    pass
result["deps_missing"] = missing
if result["transport_ok"]:
    if meta["baseline"]:
        unpack("/tmp/base")
        result["baseline"] = run("/tmp/base", "baseline")
        shutil.copytree("/tmp/base", "/tmp/work", symlinks=False)
    else:
        unpack("/tmp/work")
    for rel, text in meta["overlay"].items():
        target = os.path.normpath(os.path.join("/tmp/work", rel))
        if not target.startswith("/tmp/work/"):
            raise SystemExit(3)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    result["patched"] = run("/tmp/work", "patched")
kill_others()
sys.stdout.write("\n" + "@@HERMES_EARNER_RESULT@@" + json.dumps(result) + "\n")
sys.stdout.flush()
'''


def encode_repo_stream(meta: Dict[str, Any], tarball: bytes) -> bytes:
    import struct

    header = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    stream = struct.pack(">Q", len(header)) + header + struct.pack(">Q", len(tarball)) + tarball
    if len(stream) > REPO_MAX_STREAM_BYTES:
        raise SandboxUnavailable("репозиторий слишком большой для передачи в sandbox")
    return stream


def repo_meta(plan: Dict[str, Any], files: List[Dict[str, Any]], tarball_sha256: str) -> Dict[str, Any]:
    spec = public_repo_spec()
    return {
        "runner": "pytest" if plan.get("runner") == "pytest" else "unittest",
        "pythonpath": [p for p in plan.get("pythonpath") or ["."] if p in (".", "src")],
        "dependencies": [str(name)[:80] for name in plan.get("dependencies") or []][:60],
        "limit": spec["timeout_s"],
        "baseline": spec["baseline"],
        "overlay": {f["path"]: f["content"] for f in files},
        "touched_tests": [f["path"] for f in files
                          if re.search(r"(^|/)(tests?/[^/]*\.py$|test_[^/]*\.py$|[^/]*_test\.py$)", f["path"])],
        "tarball_sha256": tarball_sha256,
    }


def repo_container_kwargs(opp_id: str) -> Dict[str, Any]:
    from docker.types import LogConfig, Ulimit

    spec = public_repo_spec()
    if spec["runtime"].lower() in _FORBIDDEN_RUNTIMES:
        raise SandboxUnavailable("runc не используется для недоверенного кода")
    return {
        "image": spec["image"],
        "entrypoint": ["python", "-I", "-c", REPO_BOOTSTRAP],
        "command": [],
        "runtime": spec["runtime"],
        "network_mode": "none",
        "user": spec["user"],
        "working_dir": "/tmp",
        "environment": {},
        "read_only": True,
        "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=1g,mode=1777"},
        "mem_limit": spec["memory"],
        "memswap_limit": spec["memory"],
        "nano_cpus": int(spec["cpus"] * 1_000_000_000),
        "pids_limit": spec["pids"],
        "ulimits": [Ulimit(name="nofile", soft=1024, hard=1024),
                    Ulimit(name="fsize", soft=64 * 1024 * 1024, hard=64 * 1024 * 1024)],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "ipc_mode": "none",
        "privileged": False,
        "volumes": {},
        "devices": [],
        "labels": {LABEL: "1", "hermes.earner.opportunity": opp_id, "hermes.earner.mode": "repo"},
        "log_config": LogConfig(type="json-file", config={"max-size": "512k", "max-file": "1"}),
        "hostname": "earner-sandbox",
        "stdin_open": True,
        "tty": False,
        "detach": True,
    }


def _launcher_base() -> str:
    """The sandbox launcher's HTTP base when DOCKER_HOST points at it, else ""."""
    host = os.getenv("DOCKER_HOST", "")
    return "http://" + host.removeprefix("tcp://") if "sandbox-launcher" in host else ""


def send_stdin(container: Any, data: bytes) -> None:
    """Write the stream to the container's stdin over an attach connection."""
    import socket as _socket

    base = _launcher_base()
    if base:
        # The launcher owns the Docker socket and attaches on our behalf: a
        # hijacked attach connection cannot pass through its HTTP broker.
        import httpx

        response = httpx.post(f"{base}/hermes/earner/{container.id}/stdin", content=data, timeout=180)
        if response.status_code != 200:
            raise SandboxUnavailable(f"launcher refused sandbox input ({response.status_code})")
        return

    attached = container.attach_socket(params={"stdin": 1, "stream": 1})
    raw = getattr(attached, "_sock", attached)
    try:
        raw.settimeout(120)
        raw.sendall(data)
    finally:
        try:
            raw.shutdown(_socket.SHUT_WR)
        except OSError:
            pass
        try:
            attached.close()
        except Exception:
            pass


def _phase_summary(phase: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(phase, dict):
        return None
    tests = phase.get("tests") if isinstance(phase.get("tests"), dict) else None
    return {
        "exit_code": phase.get("exit_code"), "timed_out": bool(phase.get("timed_out")),
        "duration_s": phase.get("duration_s"), "addopts_cleared": bool(phase.get("addopts_cleared")),
        "tests": {"total": int(tests.get("total") or 0), "skipped": int(tests.get("skipped") or 0),
                  "failed_count": int(tests.get("failed_count") or 0),
                  "failed": [str(t)[:300] for t in (tests.get("failed") or [])][:300],
                  "passed_classes": [str(t)[:200] for t in (tests.get("passed_classes") or [])][:200]}
        if tests else None,
        "stdout": str(phase.get("stdout") or "")[-12000:], "stderr": str(phase.get("stderr") or "")[-6000:],
    }


def _module_of(path: str) -> str:
    return path[:-3].replace("/", ".") if path.endswith(".py") else ""


def touched_tests_passed(patched: Dict[str, Any], touched: List[str]) -> List[str]:
    """Test files the patch added or changed that have at least one passing
    test in the patched run (junit classname is the dotted module path,
    optionally followed by a class name)."""
    classes = (patched.get("tests") or {}).get("passed_classes") or []
    found = []
    for path in touched:
        module = _module_of(path)
        if module and any(name == module or name.startswith(module + ".") or name.endswith("." + module)
                          or ("." + module + ".") in name for name in classes):
            found.append(path)
    return found


def parse_repo_output(logs: str, touched_tests: Optional[List[str]] = None) -> Dict[str, Any]:
    """Same single-marker rule as parse_output, then baseline vs patched.

    ``passed``: the patched tree's suite exits 0. ``no_regressions``: it does
    not, but every failing test also fails on the base commit, no test
    disappeared, and the tests the patch itself added or changed ran and
    passed — the usual state of a real repository whose optional dependencies
    are missing. Anything else is ``failed``."""
    lines = [line for line in logs.splitlines() if line.strip()]
    markers = [line for line in lines if line.startswith(RESULT_MARKER)]
    if len(markers) != 1 or not lines or lines[-1] != markers[0]:
        return {"status": "inconclusive", "mode": "repo", "detail": "результат sandbox отсутствует или подделан",
                "log_tail": logs[-4000:]}
    try:
        data = json.loads(markers[0][len(RESULT_MARKER):])
    except ValueError:
        return {"status": "inconclusive", "mode": "repo", "detail": "результат sandbox не разобран",
                "log_tail": logs[-4000:]}
    if not isinstance(data, dict) or not data.get("transport_ok"):
        return {"status": "inconclusive", "mode": "repo", "detail": "архив повреждён при передаче в sandbox"}
    baseline = _phase_summary(data.get("baseline"))
    patched = _phase_summary(data.get("patched"))
    if patched is None:
        return {"status": "inconclusive", "mode": "repo", "detail": "тесты не запускались"}
    new_failures: List[str] = []
    fixed: List[str] = []
    if patched["timed_out"]:
        status = "timeout"
    elif patched["exit_code"] == 0:
        status = "passed"
    else:
        status = "failed"
        if baseline and baseline["tests"] and patched["tests"]:
            before, after = set(baseline["tests"]["failed"]), set(patched["tests"]["failed"])
            new_failures = sorted(after - before)
            fixed = sorted(before - after)
            if (not new_failures and baseline["tests"]["failed_count"] <= 150
                    and patched["tests"]["total"] >= baseline["tests"]["total"]
                    and patched["tests"]["failed_count"] <= baseline["tests"]["failed_count"]
                    and touched_tests and touched_tests_passed(patched, touched_tests)):
                status = "no_regressions"
    if status in ("passed", "timeout") and baseline and baseline["tests"] and patched["tests"]:
        fixed = sorted(set(baseline["tests"]["failed"]) - set(patched["tests"]["failed"]))
    return {
        "status": status, "mode": "repo", "runner": str(data.get("runner") or ""),
        "exit_code": patched["exit_code"], "duration_s": patched["duration_s"],
        "stdout": patched["stdout"], "stderr": patched["stderr"],
        "baseline": baseline, "patched": patched, "new_failures": new_failures[:100], "fixed": fixed[:100],
        "deps_missing": [str(name)[:80] for name in data.get("deps_missing") or []][:60],
        "touched_tests_passed": touched_tests_passed(patched, touched_tests or []),
    }


def run_repo(tarball: bytes, meta: Dict[str, Any], opp_id: str) -> Dict[str, Any]:
    """Blocking: create → start → stream stdin → wait → logs → always remove."""
    state = repo_availability()
    if not state["available"]:
        raise SandboxUnavailable(state["reason"])
    stream = encode_repo_stream(meta, tarball)
    phases = 2 if meta.get("baseline") else 1
    budget_s = int(meta["limit"]) * phases + 120
    client = _client()
    container = client.containers.create(**repo_container_kwargs(opp_id))
    started = time.monotonic()
    killed = False
    try:
        container.start()
        send_stdin(container, stream)
        try:
            container.wait(timeout=budget_s)
        except Exception:
            killed = True
            try:
                container.kill()
            except Exception:
                logger.warning("Could not kill sandbox %s", container.short_id)
        raw = container.logs(stdout=True, stderr=True) or b""
        logs = raw[-(4 * MAX_LOG_BYTES):].decode("utf-8", errors="replace")
        result = parse_repo_output(logs, meta.get("touched_tests"))
        try:
            container.reload()
            state = container.attrs.get("State") or {}
        except Exception:
            state = {}
        update = describe_exit(result, state, killed)
        if killed:
            update["detail"] = f"превышено время ({budget_s} с) — контейнер остановлен"
        result.update(update)
        result["wall_s"] = round(time.monotonic() - started, 2)
        result["runtime"] = _runtime()
        return result
    finally:
        try:
            container.remove(force=True)
        except Exception:
            logger.warning("Could not remove sandbox container %s; startup cleanup will retry", container.short_id)
