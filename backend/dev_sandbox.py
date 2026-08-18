"""Ephemeral, per-dev-run sandbox containers.

The static `dev-runner` container (backend/dev_runner_api.py) exposes a single
shared dev-repo checkout — fine for one run at a time, but a Kanban board with
several cards in flight needs several runs actually progressing at once, each
in its own isolated git working copy (same objects, separate index/worktree,
so two agents editing different files never race on the same `.git`).

This module gives every *active* dev-run its own container, cloned from the
same image as the static `dev-runner` service, bind-mounting its own subtree
of `backend/data/dev-repo-runs/<run_id>`. The security boundary in
tool_permissions.py is untouched: every tool call this sandbox serves still
goes through execute_governed_tool under principal=OWNER — only *where* the
sandbox lives changes, not *who* is allowed to drive it.

Docker-out-of-docker gotcha, and why this does NOT mount the raw host
/var/run/docker.sock into the backend: a raw socket mount hands whoever holds
it the entire Docker daemon — create a privileged container, bind-mount `/`
from the host, `exec` into any other running container — so one RCE in this
process (or in anything an LLM-driven tool call reaches) would be a full host
compromise, not a sandbox escape. Instead, docker-compose.yml puts a
`docker-socket-proxy` (github.com/Tecnativa/docker-socket-proxy) sidecar in
front of the real socket: it only holds the raw mount itself, exposes a
narrow allowlisted subset of the Docker API over HTTP (container/image/
network create-start-stop-remove; explicitly NOT exec, volumes, swarm,
secrets, plugins, or builds), and sits on its own internal network that only
`backend` can reach — the dev-run sandboxes themselves (which run
agent-controlled `dev_exec` shell commands) are on a *different* network and
can never talk to the proxy, so a malicious dev-run goal can't ask Docker to
create it a way out. `DOCKER_HOST=tcp://docker-proxy:2375` (set in
docker-compose.yml) is all `docker.from_env()` below needs to pick up the
proxy instead of a local socket — everything else about the SDK calls in this
module is unchanged. The proxy still can't stop this module from bind-
mounting an arbitrary host path if the module's own code were compromised, so
_safe_run_id() below pins the one bind-mount source this module will ever
construct to a run id shaped exactly like dev_runs.create_run() generates,
never an arbitrary caller-supplied string, as a second, independent layer.

DEV_SANDBOX_HOST_RUNS_ROOT is the *host* path matching RUNS_ROOT below (for
bind-mount sources — the SDK is talking to the host daemon regardless of
whether that's over a raw socket or the proxy, so it still needs a host path,
not a path inside this container), set explicitly in docker-compose.yml
rather than derived from a project-root guess: the local dev compose and the
production compose mount backend/data at different relative locations
(`./backend/data` vs a top-level `./data`), so "root + a fixed relative
suffix" does not generalize across them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("hermes.dev_sandbox")

MAX_CONCURRENT_SANDBOXES = max(1, int(os.getenv("DEV_SANDBOX_MAX_CONCURRENT", "3")))

# In-container (backend) paths — this container sees them directly because
# `./backend:/app/backend` is mounted into it (same as the dev-runner's own
# `./backend/data/dev-repo:/dev-repo` mount, just from the backend side).
BASE_REPO_PATH = Path(os.getenv("DEV_REPO_ROOT", "/app/backend/data/dev-repo"))
RUNS_ROOT = Path(os.getenv("DEV_REPO_RUNS_ROOT", "/app/backend/data/dev-repo-runs"))
# Published static demos, served by nginx's /demo/ location (read-only mount
# into the frontend container — see nginx.conf.template + docker-compose.yml).
PREVIEWS_ROOT = Path(os.getenv("DEV_PREVIEWS_ROOT", "/app/backend/data/dev-previews"))

# Host-side path matching RUNS_ROOT, for bind-mount sources handed to the
# sibling containers this module creates. Must be set explicitly per
# deployment — see the module docstring for why it can't be derived.
HOST_RUNS_ROOT = os.getenv("DEV_SANDBOX_HOST_RUNS_ROOT", "")

STATIC_RUNNER_CONTAINER = os.getenv("DEV_RUNNER_CONTAINER_NAME", "hermes-dev-runner")
RUNNER_PORT = 8600
HEALTH_TIMEOUT_S = 30.0

# Matches dev_runs.create_run()'s `f"run-{uuid.uuid4().hex[:12]}"` exactly.
# Every function below that turns a run_id into a filesystem path or a Docker
# container name validates against this first — see the module docstring.
_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{12}$")

# Must match `useradd --uid 10001 runner` in Dockerfile.dev-runner. This
# backend container runs as root, so `git clone` below leaves the run's repo
# copy root-owned — without the chown, the sandbox container (which drops to
# uid 10001 as soon as it starts) can list/read the bind-mounted /dev-repo
# but gets EACCES on every write (mkdir/create needs write on the containing
# directory, which stays root:root otherwise).
SANDBOX_UID = 10001
SANDBOX_GID = 10001


def _safe_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"Refusing to sandbox a malformed run id: {run_id!r}")
    return run_id


def _docker_client():
    import docker  # local import: only touched when sandboxing is actually used
    return docker.from_env()  # picks up DOCKER_HOST=tcp://docker-proxy:2375


def sandbox_name(run_id: str) -> str:
    return f"hermes-dev-runner-{_safe_run_id(run_id)}"


_sandbox_name = sandbox_name  # internal alias used throughout this module


# ── Published sites: one stable URL per chain, one snapshot per revision ────
# dev_publish_demo copies each revision's build into its own immutable
# PREVIEWS_ROOT/<run_id>/ directory. On top of those, every chain of runs gets
# ONE stable directory — PREVIEWS_ROOT/site-<root_run_id> — which is a symlink
# pointing at whichever revision is currently live. That is what makes a
# published site shareable: the URL an owner sends out never changes when the
# site is revised, and rolling back is a symlink swap rather than a re-copy.

SITE_ALIAS_PREFIX = "site-"


def site_dir_name(root_run_id: str) -> str:
    return f"{SITE_ALIAS_PREFIX}{_safe_run_id(root_run_id)}"


def site_alias_path(root_run_id: str) -> Path:
    return PREVIEWS_ROOT / site_dir_name(root_run_id)


def site_url(root_run_id: str) -> str:
    return f"/demo/{site_dir_name(root_run_id)}/"


def point_site_alias(root_run_id: str, run_id: str) -> str:
    """Points a chain's stable URL at one revision's snapshot and returns it.

    The symlink is created under a temp name and moved into place with
    os.replace, so a request arriving mid-publish is served either the old
    revision or the new one — never a half-written link. Relative target, so
    it still resolves inside the frontend container's read-only bind mount of
    this directory (a host-absolute path would dangle there)."""
    root_run_id = _safe_run_id(root_run_id)
    target = _safe_run_id(run_id)
    if not (PREVIEWS_ROOT / target).is_dir():
        raise FileNotFoundError(f"Revision {target} has no published snapshot to point at.")
    alias = site_alias_path(root_run_id)
    staging = PREVIEWS_ROOT / f".{site_dir_name(root_run_id)}.{os.getpid()}.tmp"
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    os.symlink(target, staging)
    os.replace(staging, alias)
    return site_url(root_run_id)


def current_site_revision(root_run_id: str) -> Optional[str]:
    """Run id the chain's stable URL currently serves, or None if unpublished."""
    try:
        return os.readlink(site_alias_path(root_run_id))
    except OSError:
        return None


def drop_site_alias(root_run_id: str) -> None:
    """Removes the stable URL entirely (last published revision deleted)."""
    alias = site_alias_path(root_run_id)
    try:
        alias.unlink()
    except OSError as exc:
        logger.debug("Site alias removal for %s: %s", root_run_id, exc)


# ── Per-run repo checkouts ──────────────────────────────────────────────────

def _git(args: list, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    # safe.directory=*: run checkouts are chowned to the sandbox uid while this
    # backend process is root, which git otherwise refuses to touch ("dubious
    # ownership") — every path here is one this module itself constructed.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        check=check, capture_output=True, text=True, timeout=timeout,
    )


def _checkpoint_parent(parent_path: Path) -> None:
    """Commits the parent run's working tree before a continuation clones it.

    `git clone` copies committed history only, so without this a continuation
    would silently start from before its parent's uncommitted work — i.e. the
    agent would be asked to refine a site that isn't there. Committing first
    makes the continuation start from exactly the state the owner reviewed and
    leaves an auditable revision boundary in history. Best-effort: a failure
    here falls back to a verbatim copy in _prepare_repo_copy."""
    status = _git(["-C", str(parent_path), "status", "--porcelain"], timeout=30, check=False)
    if status.returncode != 0 or not status.stdout.strip():
        return
    _git(["-C", str(parent_path), "add", "-A"], timeout=60)
    _git([
        "-c", "user.name=Hermes dev-run", "-c", "user.email=dev-runs@hermes.local",
        "-C", str(parent_path), "commit", "-m",
        "checkpoint: state handed to the next revision",
    ], timeout=60)


def _chown_to_sandbox(target: Path) -> None:
    """Hands the checkout to the sandbox uid — see SANDBOX_UID above for why."""
    subprocess.run(
        ["chown", "-R", f"{SANDBOX_UID}:{SANDBOX_GID}", str(target)],
        check=True, capture_output=True, text=True, timeout=30,
    )


def _prepare_repo_copy(run_id: str, parent_run_id: Optional[str] = None) -> Path:
    """Local `git clone` into the run's own directory, preserving the `origin`
    remote (Gitea) so git_push still works from the clone. `--local` hardlinks
    objects instead of copying them — fast and disk-cheap for same-filesystem
    clones.

    A continuation run clones its PARENT's checkout instead of the shared
    dev-repo, which is what lets a second card refine the site the first one
    built (the shared dev-repo never receives a run's work — pushes go to
    Gitea). If the parent's checkout is gone (deleted, or pruned), this falls
    back to the base repo rather than failing the run."""
    run_id = _safe_run_id(run_id)
    target = RUNS_ROOT / run_id
    if target.exists():
        return target
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)

    source = BASE_REPO_PATH
    if parent_run_id:
        parent_path = RUNS_ROOT / _safe_run_id(parent_run_id)
        if (parent_path / ".git").is_dir():
            try:
                _checkpoint_parent(parent_path)
            except (subprocess.SubprocessError, OSError) as exc:
                logger.warning("Checkpoint commit of parent %s failed: %s", parent_run_id, exc)
            source = parent_path
        else:
            logger.warning(
                "Continuation %s: parent checkout %s is gone — starting from the base dev-repo.",
                run_id, parent_run_id,
            )

    origin_url = _git(["-C", str(source), "remote", "get-url", "origin"],
                      timeout=15, check=False).stdout.strip()
    try:
        _git(["clone", "--local", "--", str(source), str(target)], timeout=120)
    except subprocess.SubprocessError as exc:
        if source == BASE_REPO_PATH:
            raise
        # The parent's history is unusable (shallow, corrupt, mid-rebase) but
        # its files are what matter for a continuation — copy them verbatim
        # rather than dropping the owner back to an empty repo.
        logger.warning("Clone from parent %s failed (%s) — copying its tree verbatim.",
                       parent_run_id, exc)
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, symlinks=True)
    if origin_url:
        _git(["-C", str(target), "remote", "set-url", "origin", origin_url], timeout=15)
    _chown_to_sandbox(target)
    return target


def _blocking_ensure_container(run_id: str, parent_run_id: Optional[str] = None) -> str:
    run_id = _safe_run_id(run_id)
    if not HOST_RUNS_ROOT:
        raise RuntimeError(
            "DEV_SANDBOX_HOST_RUNS_ROOT is not configured on the backend — cannot bind-mount "
            "a per-run sandbox on the host Docker daemon. Set it in docker-compose.yml."
        )
    client = _docker_client()
    name = _sandbox_name(run_id)
    try:
        existing = client.containers.get(name)
        if existing.status != "running":
            existing.start()
        return name
    except Exception:
        pass  # docker.errors.NotFound or a stopped/removed container — (re)create below

    _prepare_repo_copy(run_id, parent_run_id)
    base = client.containers.get(STATIC_RUNNER_CONTAINER)
    image = base.image.tags[0] if base.image.tags else base.image.id
    # Only ever the base dev-runner's own network(s) — never derived from
    # anything caller-supplied, so a sandbox can't be started on, say, the
    # docker-proxy's network by mistake.
    networks = list(base.attrs.get("NetworkSettings", {}).get("Networks", {}).keys()) or ["dev-net"]
    token = os.getenv("DEV_RUNNER_TOKEN", "")

    container = client.containers.run(
        image,
        name=name,
        detach=True,
        environment={"DEV_RUNNER_TOKEN": token, "DEV_RUNNER_REPO_ROOT": "/dev-repo"},
        # Bind-mount source is built from nothing but the fixed host root and
        # a run id already validated by _safe_run_id() above — never a
        # caller-supplied path.
        volumes={f"{HOST_RUNS_ROOT}/{run_id}": {"bind": "/dev-repo", "mode": "rw"}},
        network=networks[0],
        mem_limit="2g",
        nano_cpus=2_000_000_000,
        pids_limit=512,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        restart_policy={"Name": "no"},
    )
    for extra_network in networks[1:]:
        client.networks.get(extra_network).connect(container)
    return name


async def ensure_sandbox(run_id: str, parent_run_id: Optional[str] = None) -> str:
    """Creates (or reuses) the run's sandbox container and returns its base URL.

    `parent_run_id` makes this a continuation: the checkout is cloned from that
    run's working tree, so the agent opens the previous revision's code rather
    than an empty repo."""
    name = await asyncio.to_thread(_blocking_ensure_container, run_id, parent_run_id)
    base_url = f"http://{name}:{RUNNER_PORT}"
    deadline = asyncio.get_event_loop().time() + HEALTH_TIMEOUT_S
    async with httpx.AsyncClient(timeout=3.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                response = await client.get(f"{base_url}/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0)
    return base_url


def _blocking_release(run_id: str) -> None:
    try:
        client = _docker_client()
        container = client.containers.get(_sandbox_name(_safe_run_id(run_id)))
        container.remove(force=True)
    except Exception as exc:
        logger.debug("Sandbox release for %s: %s", run_id, exc)


async def release_sandbox(run_id: str) -> None:
    """Stops and removes the run's ephemeral container. The per-run repo
    directory is left on disk (retry/debugging), not deleted here."""
    await asyncio.to_thread(_blocking_release, run_id)


def repo_run_path(run_id: str) -> Path:
    """In-container filesystem path to a run's own dev-repo clone, used by
    dev_publish_demo to copy a built static site out without a network hop."""
    return RUNS_ROOT / _safe_run_id(run_id)
