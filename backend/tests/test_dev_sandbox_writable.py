"""Tests for the one sandbox failure an agent can never work around: a
/dev-repo it is allowed to read but not write.

A bind mount takes its ownership from the host directory, masking the
`chown runner /dev-repo` in Dockerfile.dev-runner — so any checkout the Docker
daemon had to create itself arrives root-owned, and the sandbox (uid 10001, no
CAP_CHOWN) fails every write with PermissionError. In production that surfaced
as a card that spent hundreds of steps retrying writes before reporting
BLOCKED, and a second one killed by the consecutive-failure breaker. These
cover the two halves of the fix: make the directory writable up front, and
stop a run immediately when it still isn't.
"""

import asyncio
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend import dev_runner_api, dev_runs, dev_sandbox


# ── The runner reports whether it can actually write its repo ────────────────

@pytest.fixture()
def runner_repo(tmp_path, monkeypatch):
    repo = tmp_path / "dev-repo"
    repo.mkdir()
    monkeypatch.setattr(dev_runner_api, "REPO_ROOT", repo.resolve())
    return repo


def test_health_reports_a_writable_repo(runner_repo):
    body = TestClient(dev_runner_api.app).get("/health").json()
    assert body["repo_exists"] is True
    assert body["writable"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_health_reports_an_unwritable_repo(runner_repo):
    runner_repo.chmod(0o555)
    try:
        body = TestClient(dev_runner_api.app).get("/health").json()
    finally:
        runner_repo.chmod(0o755)
    assert body["repo_exists"] is True
    assert body["writable"] is False


# ── dev_sandbox: preparing checkouts and reading that signal back ────────────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient so repo_writable can be driven without
    a real runner container."""

    payload = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if isinstance(self.payload, Exception):
            raise self.payload
        return _FakeResponse(self.payload)


@pytest.fixture()
def fake_health(monkeypatch):
    def _set(payload):
        _FakeAsyncClient.payload = payload
        monkeypatch.setattr(dev_sandbox.httpx, "AsyncClient", _FakeAsyncClient)
    return _set


def test_repo_writable_reads_the_health_flag(fake_health):
    fake_health({"status": "ok", "writable": False})
    assert asyncio.run(dev_sandbox.repo_writable("http://sandbox:8600")) is False
    fake_health({"status": "ok", "writable": True})
    assert asyncio.run(dev_sandbox.repo_writable("http://sandbox:8600")) is True


def test_repo_writable_is_unknown_for_an_old_or_unreachable_runner(fake_health):
    """None, not False: an unanswerable question must not fail a run — only a
    runner that explicitly says "not writable" does."""
    fake_health({"status": "ok", "repo_exists": True})          # pre-`writable` image
    assert asyncio.run(dev_sandbox.repo_writable("http://sandbox:8600")) is None
    fake_health(dev_sandbox.httpx.ConnectError("no route"))
    assert asyncio.run(dev_sandbox.repo_writable("http://sandbox:8600")) is None


def test_a_reused_checkout_is_chowned_again(tmp_path, monkeypatch):
    """The regression itself: _prepare_repo_copy used to return an existing
    directory untouched, so a run resumed on a checkout that was created
    root-owned (by the daemon, or before the chown existed) stayed unwritable
    forever."""
    runs_root = tmp_path / "runs"
    monkeypatch.setattr(dev_sandbox, "RUNS_ROOT", runs_root)
    chowned = []
    monkeypatch.setattr(dev_sandbox, "_chown_to_sandbox",
                        lambda target, recursive=True: chowned.append(target))
    existing = runs_root / "run-aaaaaaaaaaaa"
    existing.mkdir(parents=True)

    assert dev_sandbox._prepare_repo_copy("run-aaaaaaaaaaaa") == existing
    assert chowned == [existing]


def test_the_shared_repo_is_created_seeded_and_chowned(tmp_path, monkeypatch):
    """The static dev-runner mounts this path straight from the host: if it is
    missing at container start the daemon creates it root:root, and an empty
    directory is not clonable either."""
    base = tmp_path / "dev-repo"
    monkeypatch.setattr(dev_sandbox, "BASE_REPO_PATH", base)
    chowned = []
    monkeypatch.setattr(dev_sandbox, "_chown_to_sandbox",
                        lambda target, recursive=True: chowned.append(target))

    dev_sandbox.ensure_shared_repo_writable()

    assert (base / ".git").is_dir()
    assert chowned == [base]
    # Idempotent: a second call must not re-init over real history.
    (base / "index.html").write_text("<h1>site</h1>")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(base),
                    "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(base),
                    "commit", "-qm", "site"], check=True, capture_output=True)
    dev_sandbox.ensure_shared_repo_writable()
    log = subprocess.run(["git", "-C", str(base), "log", "--oneline"],
                         capture_output=True, text=True, check=True).stdout
    assert "site" in log


def test_ensure_sandbox_refuses_a_sandbox_it_cannot_make_writable(monkeypatch):
    """Better to fall back to the shared runner (dev_runs catches this) than to
    hand an agent a checkout where every write fails."""
    monkeypatch.setattr(dev_sandbox, "_blocking_ensure_container",
                        lambda run_id, parent=None: dev_sandbox.sandbox_name(run_id))
    monkeypatch.setattr(dev_sandbox, "_chown_to_sandbox", lambda target, recursive=True: None)
    released = []
    monkeypatch.setattr(dev_sandbox, "_blocking_release", released.append)

    async def always_healthy(base_url):
        return True

    async def never_writable(base_url):
        return False

    monkeypatch.setattr(dev_sandbox, "_await_health", always_healthy)
    monkeypatch.setattr(dev_sandbox, "repo_writable", never_writable)

    with pytest.raises(dev_sandbox.SandboxNotWritable):
        asyncio.run(dev_sandbox.ensure_sandbox("run-aaaaaaaaaaaa"))
    assert released == ["run-aaaaaaaaaaaa"]      # no container left behind


# ── dev_runs: the run stops with an actionable reason, not 500 dead steps ────

@pytest.fixture()
def runs_db(tmp_path, monkeypatch):
    from backend import autonomy, control_plane, database
    db_path = str(tmp_path / "dev-runs.db")
    for module in (database, control_plane, dev_runs, autonomy):
        monkeypatch.setattr(module, "DB_PATH", db_path)
    database.init_db()
    return db_path


def test_an_unwritable_sandbox_fails_the_run_before_it_spends_a_step(runs_db, monkeypatch, fake_health):
    fake_health({"status": "ok", "writable": False})
    run = dev_runs.create_run("Build a landing page", start=False)

    stopped = asyncio.run(dev_runs._fail_if_sandbox_unusable(run["id"], "http://sandbox:8600", True))

    assert stopped is True
    failed = dev_runs.get_run(run["id"])
    assert failed["status"] == "failed"
    assert "not writable" in failed["status_reason"]
    assert str(dev_sandbox.SANDBOX_UID) in failed["status_reason"]
    # The owner is told the fix, in the run's own ledger.
    steps = dev_runs.get_steps(run["id"])
    assert any("chown" in step["summary"] for step in steps)


def test_a_writable_or_unknown_sandbox_starts_normally(runs_db, fake_health):
    run = dev_runs.create_run("Build a landing page", start=False)
    fake_health({"status": "ok", "writable": True})
    assert asyncio.run(dev_runs._fail_if_sandbox_unusable(run["id"], "http://sandbox:8600", True)) is False
    fake_health({"status": "ok"})       # older runner image: unknown, not blocked
    assert asyncio.run(dev_runs._fail_if_sandbox_unusable(run["id"], "http://sandbox:8600", True)) is False
    assert dev_runs.get_run(run["id"])["status"] != "failed"
