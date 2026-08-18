"""Tests for the per-run checkout dev_sandbox hands a dev-run.

The behaviour under test is what makes iterating on a site possible at all: a
continuation must open its parent's working tree, including work the parent
never committed, and must degrade to the shared dev-repo rather than fail when
that tree is gone. The published-site alias is covered here too — it is the
one piece of the previews directory that is not a plain copy.
"""

import subprocess

import pytest

from backend import dev_sandbox


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    """A base dev-repo with one commit, and an empty per-run root."""
    base = tmp_path / "dev-repo"
    base.mkdir()
    _git("init", "-q", "-b", "main", cwd=base)
    (base / "README.md").write_text("base")
    _git("add", "-A", cwd=base)
    _git("commit", "-qm", "base", cwd=base)

    runs_root = tmp_path / "dev-repo-runs"
    monkeypatch.setattr(dev_sandbox, "BASE_REPO_PATH", base)
    monkeypatch.setattr(dev_sandbox, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(dev_sandbox, "PREVIEWS_ROOT", tmp_path / "dev-previews")
    # chown to the sandbox uid needs root; irrelevant to what these assert.
    monkeypatch.setattr(dev_sandbox, "_chown_to_sandbox", lambda target: None)
    return base, runs_root


ROOT_ID = "run-aaaaaaaaaaaa"
CHILD_ID = "run-bbbbbbbbbbbb"


def test_a_root_run_starts_from_the_base_repo(repos):
    base, runs_root = repos
    checkout = dev_sandbox._prepare_repo_copy(ROOT_ID)
    assert checkout == runs_root / ROOT_ID
    assert (checkout / "README.md").read_text() == "base"


def test_a_continuation_inherits_its_parents_uncommitted_work(repos):
    """The trap this guards: `git clone` copies committed history only, so a
    continuation would open a checkout without the site its parent just built
    and cheerfully rebuild it from scratch."""
    base, runs_root = repos
    parent = dev_sandbox._prepare_repo_copy(ROOT_ID)
    (parent / "index.html").write_text("<h1>landing</h1>")   # never committed
    (parent / "assets").mkdir()
    (parent / "assets" / "app.css").write_text("body{}")

    child = dev_sandbox._prepare_repo_copy(CHILD_ID, ROOT_ID)
    assert (child / "index.html").read_text() == "<h1>landing</h1>"
    assert (child / "assets" / "app.css").exists()
    assert (child / "README.md").exists()
    # The parent's tree was checkpointed, so the inherited state is a real
    # commit rather than a floating copy.
    log = subprocess.run(["git", "-C", str(child), "log", "--oneline"],
                         capture_output=True, text=True, check=True).stdout
    assert "checkpoint" in log


def test_a_continuation_keeps_the_gitea_origin(repos):
    base, runs_root = repos
    _git("remote", "add", "origin", "https://gitea.internal/org/app.git", cwd=base)
    dev_sandbox._prepare_repo_copy(ROOT_ID)
    child = dev_sandbox._prepare_repo_copy(CHILD_ID, ROOT_ID)
    origin = subprocess.run(["git", "-C", str(child), "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert origin == "https://gitea.internal/org/app.git"


def test_a_continuation_falls_back_when_the_parent_checkout_is_gone(repos):
    """Parent checkouts are prunable; losing one must cost the continuation its
    starting point, not the whole run."""
    base, runs_root = repos
    child = dev_sandbox._prepare_repo_copy(CHILD_ID, ROOT_ID)
    assert (child / "README.md").read_text() == "base"


def test_an_existing_checkout_is_reused_not_recloned(repos):
    base, runs_root = repos
    checkout = dev_sandbox._prepare_repo_copy(ROOT_ID)
    (checkout / "work-in-progress.txt").write_text("keep me")
    again = dev_sandbox._prepare_repo_copy(ROOT_ID)
    assert (again / "work-in-progress.txt").read_text() == "keep me"


def test_malformed_ids_never_reach_the_filesystem(repos):
    with pytest.raises(ValueError):
        dev_sandbox._prepare_repo_copy("../../etc")
    with pytest.raises(ValueError):
        dev_sandbox._prepare_repo_copy(ROOT_ID, "../../etc")
    with pytest.raises(ValueError):
        dev_sandbox.site_alias_path("run-not-hex")


def test_site_alias_swaps_atomically_between_revisions(repos):
    previews = dev_sandbox.PREVIEWS_ROOT
    for run_id, body in ((ROOT_ID, "v1"), (CHILD_ID, "v2")):
        (previews / run_id).mkdir(parents=True)
        (previews / run_id / "index.html").write_text(body)

    url = dev_sandbox.point_site_alias(ROOT_ID, ROOT_ID)
    assert url == f"/demo/site-{ROOT_ID}/"
    assert dev_sandbox.current_site_revision(ROOT_ID) == ROOT_ID

    dev_sandbox.point_site_alias(ROOT_ID, CHILD_ID)
    assert dev_sandbox.current_site_revision(ROOT_ID) == CHILD_ID
    assert (dev_sandbox.site_alias_path(ROOT_ID) / "index.html").read_text() == "v2"
    # Relative target, so the link still resolves inside the frontend
    # container's bind mount of this directory.
    assert "/" not in dev_sandbox.current_site_revision(ROOT_ID)
    # No stray staging links left behind.
    assert not [p for p in previews.iterdir() if p.name.startswith(".")]

    dev_sandbox.drop_site_alias(ROOT_ID)
    assert dev_sandbox.current_site_revision(ROOT_ID) is None


def test_pointing_a_site_at_an_unpublished_revision_is_refused(repos):
    dev_sandbox.PREVIEWS_ROOT.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError):
        dev_sandbox.point_site_alias(ROOT_ID, CHILD_ID)
