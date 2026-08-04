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

Docker-out-of-docker gotcha: this module talks to the *host* Docker daemon
over the mounted /var/run/docker.sock (sibling-container pattern), so any
bind-mount source path handed to the SDK must be a HOST path, not a path
inside this (backend) container. DEV_SANDBOX_HOST_RUNS_ROOT is that host path
for RUNS_ROOT below, set explicitly in docker-compose.yml rather than derived
from a project-root guess — the local dev compose and the production compose
mount backend/data at different relative locations (`./backend/data` vs a
top-level `./data`), so "root + a fixed relative suffix" does not generalize
across them; a direct, deployment-specific env var does.
"""

from __future__ import annotations

import asyncio
import logging
import os
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


def _docker_client():
    import docker  # local import: only touched when sandboxing is actually used
    return docker.from_env()


def sandbox_name(run_id: str) -> str:
    return f"hermes-dev-runner-{run_id}"


_sandbox_name = sandbox_name  # internal alias used throughout this module


def _prepare_repo_copy(run_id: str) -> Path:
    """Local `git clone` of the shared dev-repo into its own run directory,
    preserving the `origin` remote (Gitea) so git_push still works from the
    clone. `--local` hardlinks objects instead of copying them — fast and
    disk-cheap for same-filesystem clones."""
    target = RUNS_ROOT / run_id
    if target.exists():
        return target
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    origin_url = subprocess.run(
        ["git", "-C", str(BASE_REPO_PATH), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    subprocess.run(
        ["git", "clone", "--local", "--", str(BASE_REPO_PATH), str(target)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    if origin_url:
        subprocess.run(
            ["git", "-C", str(target), "remote", "set-url", "origin", origin_url],
            check=True, capture_output=True, text=True, timeout=15,
        )
    return target


def _blocking_ensure_container(run_id: str) -> str:
    if not HOST_RUNS_ROOT:
        raise RuntimeError(
            "DEV_SANDBOX_HOST_ROOT is not configured on the backend — cannot bind-mount "
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

    _prepare_repo_copy(run_id)
    base = client.containers.get(STATIC_RUNNER_CONTAINER)
    image = base.image.tags[0] if base.image.tags else base.image.id
    networks = list(base.attrs.get("NetworkSettings", {}).get("Networks", {}).keys()) or ["dev-net"]
    token = os.getenv("DEV_RUNNER_TOKEN", "")

    container = client.containers.run(
        image,
        name=name,
        detach=True,
        environment={"DEV_RUNNER_TOKEN": token, "DEV_RUNNER_REPO_ROOT": "/dev-repo"},
        volumes={f"{HOST_RUNS_ROOT}/{run_id}": {"bind": "/dev-repo", "mode": "rw"}},
        network=networks[0],
        mem_limit="2g",
        nano_cpus=2_000_000_000,
        restart_policy={"Name": "no"},
    )
    for extra_network in networks[1:]:
        client.networks.get(extra_network).connect(container)
    return name


async def ensure_sandbox(run_id: str) -> str:
    """Creates (or reuses) the run's sandbox container and returns its base URL."""
    name = await asyncio.to_thread(_blocking_ensure_container, run_id)
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
        container = client.containers.get(_sandbox_name(run_id))
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
    return RUNS_ROOT / run_id
