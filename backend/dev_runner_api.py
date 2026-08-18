"""Standalone FastAPI app served inside the dev-runner sandbox container.

Exposes a minimal filesystem + exec surface over the mounted dev-repo volume.
Every path is canonicalized and must stay inside DEV_RUNNER_REPO_ROOT, every
exec call is a plain argv subprocess (never shell=True), output is capped, and
all requests must carry the internal bearer token from DEV_RUNNER_TOKEN.

This module deliberately does not import the rest of the Hermes backend: the
sandbox container runs only this file plus the standard library / FastAPI, so
a compromise of the runner exposes nothing but the dev-repo itself.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

MAX_OUTPUT_BYTES = 64 * 1024
MAX_EXEC_TIMEOUT_S = 600
MAX_FILE_BYTES = 2 * 1024 * 1024

REPO_ROOT = Path(os.getenv("DEV_RUNNER_REPO_ROOT", "/dev-repo")).resolve()


def _require_token(request: Request) -> None:
    expected = os.getenv("DEV_RUNNER_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="DEV_RUNNER_TOKEN is not configured")
    header = request.headers.get("Authorization", "")
    if header != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid runner token")


app = FastAPI(title="Hermes Dev Runner", docs_url=None, redoc_url=None, openapi_url=None)


def _resolve_inside_repo(raw_path: str) -> Path:
    """Canonicalize a client path and require it to stay inside the dev-repo."""
    candidate = (REPO_ROOT / (raw_path or ".")).resolve()
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path escapes the dev repository")
    return candidate


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="replace") + "\n…[output truncated]"


class PathRequest(BaseModel):
    path: str


class WriteRequest(BaseModel):
    path: str
    content: str


class PatchRequest(BaseModel):
    path: str
    unified_diff: str


class ExecRequest(BaseModel):
    argv: List[str] = Field(min_length=1)
    timeout_s: int = Field(default=120, ge=1, le=MAX_EXEC_TIMEOUT_S)
    cwd: Optional[str] = None


class TestRequest(BaseModel):
    runner: str = "auto"
    timeout_s: int = Field(default=MAX_EXEC_TIMEOUT_S, ge=1, le=MAX_EXEC_TIMEOUT_S)


class CommitRequest(BaseModel):
    message: str = "Agent commit"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "repo": str(REPO_ROOT), "repo_exists": REPO_ROOT.is_dir()}


@app.post("/fs/read", dependencies=[Depends(_require_token)])
async def fs_read(req: PathRequest) -> dict:
    target = _resolve_inside_repo(req.path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large to read via the runner")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Read failed: {type(exc).__name__}")
    return {"path": str(target.relative_to(REPO_ROOT)), "content": _truncate(content, MAX_FILE_BYTES)}


@app.post("/fs/write", dependencies=[Depends(_require_token)])
async def fs_write(req: WriteRequest) -> dict:
    target = _resolve_inside_repo(req.path)
    if len(req.content.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Content too large")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(req.content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Write failed: {type(exc).__name__}")
    return {"path": str(target.relative_to(REPO_ROOT)), "bytes": len(req.content.encode("utf-8"))}


@app.post("/fs/patch", dependencies=[Depends(_require_token)])
async def fs_patch(req: PatchRequest) -> dict:
    # Path is validated even though git apply works repo-wide: the declared path
    # documents intent and rejects traversal attempts early.
    _resolve_inside_repo(req.path)
    if len(req.unified_diff.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Diff too large")
    result = await _run_argv(
        ["git", "apply", "--whitespace=nowarn", "-"],
        timeout_s=60,
        stdin_text=req.unified_diff,
    )
    if result["exit_code"] != 0:
        raise HTTPException(status_code=422, detail=f"Patch failed: {result['stderr'][:500]}")
    return {"path": req.path, "applied": True}


@app.post("/fs/list", dependencies=[Depends(_require_token)])
async def fs_list(req: PathRequest) -> dict:
    target = _resolve_inside_repo(req.path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    entries = []
    for child in sorted(target.iterdir()):
        if child.name == ".git":
            continue
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": child.stat().st_size if child.is_file() else None,
        })
        if len(entries) >= 500:
            break
    return {"path": str(target.relative_to(REPO_ROOT)), "entries": entries}


async def _run_argv(argv: List[str], timeout_s: int, cwd: Optional[Path] = None,
                    stdin_text: Optional[str] = None) -> dict:
    def _blocking() -> dict:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd or REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                shell=False,
                input=stdin_text,
                env={
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/tmp"),
                    "LANG": "C.UTF-8",
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
            return {
                "exit_code": completed.returncode,
                "stdout": _truncate(completed.stdout or ""),
                "stderr": _truncate(completed.stderr or ""),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": f"Timed out after {timeout_s}s", "timed_out": True}
        except FileNotFoundError:
            return {"exit_code": -1, "stdout": "", "stderr": f"Executable not found: {argv[0]}", "timed_out": False}

    return await asyncio.to_thread(_blocking)


@app.post("/exec", dependencies=[Depends(_require_token)])
async def exec_argv(req: ExecRequest) -> dict:
    cwd = _resolve_inside_repo(req.cwd) if req.cwd else REPO_ROOT
    return await _run_argv(req.argv, req.timeout_s, cwd=cwd)


def _detect_test_runner() -> List[str]:
    if (REPO_ROOT / "pytest.ini").exists() or (REPO_ROOT / "pyproject.toml").exists() \
            or list(REPO_ROOT.glob("tests/test_*.py")) or list(REPO_ROOT.glob("test_*.py")):
        return ["python3", "-m", "pytest", "-q"]
    if (REPO_ROOT / "package.json").exists():
        return ["npm", "test", "--", "--run"]
    return []


@app.post("/test", dependencies=[Depends(_require_token)])
async def run_tests(req: TestRequest) -> dict:
    if req.runner == "pytest":
        argv = ["python3", "-m", "pytest", "-q"]
    elif req.runner == "npm":
        argv = ["npm", "test", "--", "--run"]
    elif req.runner == "auto":
        argv = _detect_test_runner()
        if not argv:
            return {"exit_code": 0, "stdout": "", "stderr": "No recognizable test suite in dev-repo.",
                    "timed_out": False, "runner": "none"}
    else:
        raise HTTPException(status_code=422, detail="runner must be auto, pytest or npm")
    result = await _run_argv(argv, req.timeout_s)
    result["runner"] = argv[0]
    return result


# ── Git, scoped to this sandbox's own clone ─────────────────────────────────
# Every dev-run gets its own /dev-repo (see dev_sandbox.py's per-run bind
# mount) — these must stay inside the sandbox, same as /fs/* and /exec, so an
# agent's git_status/git_commit/git_push actually see the files it wrote via
# dev_write_file instead of some other run's (or the shared base repo's)
# working tree.

@app.post("/git/status", dependencies=[Depends(_require_token)])
async def git_status() -> dict:
    return await _run_argv(["git", "status", "--short", "--branch"], 30)


@app.post("/git/diff", dependencies=[Depends(_require_token)])
async def git_diff() -> dict:
    return await _run_argv(["git", "diff", "HEAD"], 30)


@app.post("/git/commit", dependencies=[Depends(_require_token)])
async def git_commit(req: CommitRequest) -> dict:
    add_result = await _run_argv(["git", "add", "-A"], 30)
    if add_result["exit_code"] != 0:
        return {"error": "git add failed", "detail": add_result}
    return await _run_argv(["git", "commit", "-m", (req.message or "Agent commit")[:500]], 30)


@app.post("/git/push", dependencies=[Depends(_require_token)])
async def git_push() -> dict:
    return await _run_argv(["git", "push", "origin", "HEAD"], 60)
