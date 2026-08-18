"""Tests for tools.dev_publish_demo — publishes a dev-run's build output to
nginx's unauthenticated /demo/<run_id>/ location. Security-critical: a
dev-run's sandbox is a live git clone, and the publish source is often "."
(no dist/ subfolder for a plain static site), so anything not explicitly
filtered out here becomes a world-readable file the moment it's copied."""

import json

import pytest

from backend import dev_sandbox, dev_runs, tools


@pytest.fixture()
def publish_env(tmp_path, monkeypatch):
    run_id = "run-000000000001"
    runs_root = tmp_path / "dev-repo-runs"
    previews_root = tmp_path / "dev-previews"
    (runs_root / run_id).mkdir(parents=True)
    monkeypatch.setattr(dev_sandbox, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(dev_sandbox, "PREVIEWS_ROOT", previews_root)
    recorded = {}
    monkeypatch.setattr(dev_runs, "update_run",
                        lambda rid, **fields: recorded.update(fields))
    # A root run: its own chain, so the stable URL is /demo/site-<run_id>/.
    monkeypatch.setattr(dev_runs, "get_run",
                        lambda rid, *a, **kw: {"id": rid, "root_run_id": rid})
    token = tools.CURRENT_DEV_RUN_ID.set(run_id)
    yield run_id, runs_root / run_id, previews_root, recorded
    tools.CURRENT_DEV_RUN_ID.reset(token)


def _write(path, content: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_publish_copies_visible_files(publish_env):
    run_id, checkout, previews_root, recorded = publish_env
    _write(checkout / "index.html", "<html>hi</html>")
    _write(checkout / "assets" / "app.js", "console.log(1)")

    result = json.loads(tools.dev_publish_demo("."))
    # Two URLs come out of one publish: the chain's stable one (what an owner
    # shares) and this revision's immutable snapshot (what rollback needs).
    assert result["demo_url"] == f"/demo/site-{run_id}/"
    assert result["snapshot_url"] == f"/demo/{run_id}/"
    assert recorded == {"demo_url": f"/demo/site-{run_id}/",
                        "demo_snapshot_url": f"/demo/{run_id}/"}
    published = previews_root / run_id
    assert (published / "index.html").read_text() == "<html>hi</html>"
    assert (published / "assets" / "app.js").exists()
    # The stable URL is a pointer at the snapshot, not a second copy.
    assert (previews_root / f"site-{run_id}" / "index.html").read_text() == "<html>hi</html>"


def test_publish_never_ships_git_directory(publish_env):
    """Regression test: publishing '.' (the whole checkout, not a dist/
    subfolder) must never leak .git — it holds the Gitea remote URL and full
    commit history, and /demo/ has no auth in front of it."""
    run_id, checkout, previews_root, _ = publish_env
    _write(checkout / "index.html", "<html>hi</html>")
    _write(checkout / ".git" / "config", "[remote \"origin\"]\n\turl = https://gitea.internal/secret-org/app.git\n")
    _write(checkout / ".git" / "objects" / "ab" / "cdef1234", "binary-ish")

    result = json.loads(tools.dev_publish_demo("."))
    assert result["files_published"] == 1  # only index.html counted
    published = previews_root / run_id
    assert not (published / ".git").exists()
    assert (published / "index.html").exists()


def test_publish_excludes_all_dotfiles_not_just_git(publish_env):
    run_id, checkout, previews_root, _ = publish_env
    _write(checkout / "index.html", "<html>hi</html>")
    _write(checkout / ".env", "SECRET_KEY=abc123")
    _write(checkout / ".github" / "workflows" / "ci.yml", "name: ci")

    json.loads(tools.dev_publish_demo("."))
    published = previews_root / run_id
    assert not (published / ".env").exists()
    assert not (published / ".github").exists()
    assert (published / "index.html").exists()


def test_publish_dist_subfolder_excludes_nested_dotdirs_too(publish_env):
    run_id, checkout, previews_root, _ = publish_env
    _write(checkout / "dist" / "index.html", "<html>built</html>")
    _write(checkout / "dist" / ".vite" / "manifest.json", "{}")
    _write(checkout / ".git" / "config", "irrelevant here since build_dir=dist")

    json.loads(tools.dev_publish_demo("dist"))
    published = previews_root / run_id
    assert (published / "index.html").exists()
    assert not (published / ".vite").exists()
    assert not (published / ".git").exists()  # never even a candidate — outside build_dir


def test_publish_rejects_path_traversal(publish_env):
    result = json.loads(tools.dev_publish_demo("../../etc"))
    assert "escapes" in result["error"]


def test_publish_rejects_nonexistent_build_dir(publish_env):
    result = json.loads(tools.dev_publish_demo("dist"))
    assert "is not a directory" in result["error"]


def test_publish_requires_active_dev_run():
    tools.CURRENT_DEV_RUN_ID.set(None)
    result = json.loads(tools.dev_publish_demo("."))
    assert "only be called from within a dev-run" in result["error"]


def test_publish_replaces_a_prior_publish(publish_env):
    run_id, checkout, previews_root, _ = publish_env
    _write(checkout / "index.html", "v1")
    tools.dev_publish_demo(".")
    _write(checkout / "index.html", "v2")
    (checkout / "old-page.html").unlink(missing_ok=True)
    tools.dev_publish_demo(".")
    assert (previews_root / run_id / "index.html").read_text() == "v2"
    assert (previews_root / f"site-{run_id}" / "index.html").read_text() == "v2"


def test_a_continuation_republishes_the_same_stable_url(publish_env, monkeypatch):
    """The point of the stable URL: revision 2 takes over the link revision 1
    handed out, and revision 1's snapshot stays intact for rollback."""
    root_id, checkout, previews_root, _ = publish_env
    _write(checkout / "index.html", "v1")
    tools.dev_publish_demo(".")

    child_id = "run-000000000002"
    child_checkout = dev_sandbox.RUNS_ROOT / child_id
    _write(child_checkout / "index.html", "v2")
    monkeypatch.setattr(dev_runs, "get_run",
                        lambda rid, *a, **kw: {"id": rid, "root_run_id": root_id})
    token = tools.CURRENT_DEV_RUN_ID.set(child_id)
    try:
        result = json.loads(tools.dev_publish_demo("."))
    finally:
        tools.CURRENT_DEV_RUN_ID.reset(token)

    assert result["demo_url"] == f"/demo/site-{root_id}/"       # unchanged link
    assert result["snapshot_url"] == f"/demo/{child_id}/"
    assert (previews_root / f"site-{root_id}" / "index.html").read_text() == "v2"
    assert (previews_root / root_id / "index.html").read_text() == "v1"


def test_publish_survives_a_missing_run_row(publish_env, monkeypatch):
    """Bookkeeping must never cost the owner a finished build: an unreadable
    dev_runs row degrades to the snapshot URL instead of failing the publish."""
    run_id, checkout, previews_root, _ = publish_env
    _write(checkout / "index.html", "built")

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dev_runs, "get_run", boom)
    monkeypatch.setattr(dev_runs, "update_run", boom)
    result = json.loads(tools.dev_publish_demo("."))
    assert result["files_published"] == 1
    assert (previews_root / run_id / "index.html").read_text() == "built"
