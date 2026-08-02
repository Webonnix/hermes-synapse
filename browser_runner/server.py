"""Browser-automation sidecar: runs a browser-use Agent loop against Hermes's
own configured LLM inside a headless Chromium session.

Deliberately does not import the rest of the Hermes backend — mirrors
dev_runner_api.py's isolation stance: a compromise of this container should
not expose anything beyond what browser-use itself already touches (the
browser session and the LLM credentials it was given).

Hermes drives this at the *task* level (one natural-language sub-task per
call), not the click/type primitive level — browser-use's own Agent already
runs the DOM-indexing perception/action loop internally, so re-exposing that
loop as granular HTTP endpoints would just duplicate it. See
src/backend/tools.py's browser_read/browser_task for the caller side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("hermes.browser_runner")

MAX_STEPS = max(1, int(os.getenv("BROWSER_AGENT_MAX_STEPS", "25")))
RUN_TIMEOUT_SECONDS = max(30.0, float(os.getenv("BROWSER_AGENT_TIMEOUT_SECONDS", "240")))

# Actions excluded in "read_only" mode so the agent is *structurally* unable to
# interact with page elements — not just instructed not to. Matches the action
# names registered in browser-use's tools/service.py (registry key == the
# implementing function's __name__): click, input, upload_file,
# select_dropdown, send_keys — plus `evaluate` (arbitrary in-page JS can click
# or submit a form just as effectively as the click/input actions).
READ_ONLY_EXCLUDED_ACTIONS = ["click", "input", "upload_file", "select_dropdown", "send_keys", "evaluate"]

# Docker-network service names + loopback aliases this container must never be
# steered into navigating to. `block_ip_addresses=True` (below) covers raw
# RFC1918/localhost IP literals; this list covers the same targets reached by
# Docker DNS name, which IP-literal blocking alone would not catch. This is
# the actual containment control here — the container needs open internet
# egress plus a route to `ollama` to do its job, so network topology alone
# can't isolate it the way dev-runner's egress-less network does.
INTERNAL_HOSTS = [
    "localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal",
    "ollama", "qdrant", "backend", "xtts", "valkey", "gitea",
    "dev-runner", "browser-runner", "frontend",
]

app = FastAPI(title="Hermes Browser Runner", docs_url=None, redoc_url=None, openapi_url=None)

_run_lock = asyncio.Lock()

# Latest step's screenshot + what the agent is doing with it, for the "watch
# the browser" viewer. Only one run is ever in flight (_run_lock), so a single
# module-level slot is enough — no per-task keying needed. Cleared at the
# start of every run so a finished/idle sidecar reports nothing to look at
# rather than a stale frame from the last task.
_last_frame: Dict[str, Any] = {"active": False}


def _require_token(request: Request) -> None:
    expected = os.getenv("BROWSER_RUNNER_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="BROWSER_RUNNER_TOKEN is not configured")
    header = request.headers.get("Authorization", "")
    if header != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid runner token")


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    mode: Literal["read_only", "interactive"] = "read_only"
    start_url: Optional[str] = None
    allowed_domains: Optional[List[str]] = None


def _build_llm():
    """Mirrors backend/subagents.py's _active_llm_runtime(): reuse whichever
    provider Hermes itself is configured for, so browser tasks cost/behave
    consistently with the rest of the stack. AGENT_MODEL_BROWSER overrides the
    model, same pattern as AGENT_MODEL_RESEARCH/AGENT_MODEL_CODE.
    """
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    model = os.getenv("AGENT_MODEL_BROWSER", "").strip() or os.getenv("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("No model configured (set AGENT_MODEL_BROWSER or LLM_MODEL)")

    if provider == "ollama":
        from browser_use import ChatOllama
        host = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        return ChatOllama(model=model, host=host)

    if provider == "openrouter":
        # Not re-exported from the top-level `browser_use` package in this
        # pinned release — only from its own submodule.
        from browser_use.llm.openrouter.chat import ChatOpenRouter
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        return ChatOpenRouter(model=model, api_key=api_key)

    # openai_compatible / vLLM / LM Studio / anything else exposing /v1
    from browser_use import ChatOpenAI
    base_url = os.getenv("LLM_API_BASE", "").rstrip("/") or None
    api_key = os.getenv("OPENROUTER_API_KEY", "") or os.getenv("OPENAI_API_KEY", "") or "not-needed"
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key)


def _build_browser_profile(requested_allowed_domains: Optional[List[str]]):
    from browser_use import BrowserProfile

    # `allowed_domains` takes precedence over `prohibited_domains` in
    # browser-use, so a caller-supplied allow-list must not be able to smuggle
    # an internal hostname past the deny-list below.
    safe_allowed = None
    if requested_allowed_domains:
        safe_allowed = [
            d for d in requested_allowed_domains
            if not any(host in d.lower() for host in INTERNAL_HOSTS)
        ]

    return BrowserProfile(
        headless=True,
        allowed_domains=safe_allowed,
        prohibited_domains=INTERNAL_HOSTS,
        block_ip_addresses=True,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "busy": _run_lock.locked(), "max_steps": MAX_STEPS}


@app.get("/live-frame", dependencies=[Depends(_require_token)])
async def live_frame() -> dict:
    """Latest step's screenshot + what the agent is currently doing, for the
    dashboard's "watch the browser" viewer. Meant to be polled every second or
    two while a run is active; {"active": False} once it finishes or before
    one has started."""
    return _last_frame


async def _on_step(state: Any, output: Any, step_number: int) -> None:
    screenshot = getattr(state, "screenshot", None)
    if not screenshot and hasattr(state, "get_screenshot"):
        try:
            screenshot = state.get_screenshot()
        except Exception:  # noqa: BLE001 — a missing screenshot must not fail the step
            screenshot = None
    _last_frame.update({
        "active": True,
        "step": step_number,
        "url": getattr(state, "url", None),
        "goal": getattr(output, "next_goal", None) or getattr(output, "thinking", None) or "",
        "screenshot_b64": screenshot,
        "ts": time.time(),
    })


@app.post("/run", dependencies=[Depends(_require_token)])
async def run(req: RunRequest) -> dict:
    if _run_lock.locked():
        raise HTTPException(status_code=429, detail="Another browser task is already running.")

    async with _run_lock:
        try:
            llm = _build_llm()
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear 503 to the backend
            raise HTTPException(status_code=503, detail=f"LLM not configured: {exc}") from exc

        from browser_use import Agent, Tools

        tools = Tools(exclude_actions=READ_ONLY_EXCLUDED_ACTIONS) if req.mode == "read_only" else Tools()
        profile = _build_browser_profile(req.allowed_domains)

        task_text = req.task.strip()
        if req.start_url:
            task_text = f"Start by navigating to {req.start_url}. {task_text}"

        _last_frame.clear()
        _last_frame.update({"active": True, "step": 0, "url": req.start_url, "goal": "Starting…", "screenshot_b64": None, "ts": time.time()})

        agent = Agent(
            task=task_text,
            llm=llm,
            tools=tools,
            browser_profile=profile,
            calculate_cost=True,
            use_vision=True,
            enable_signal_handler=False,
            register_new_step_callback=_on_step,
        )

        try:
            history = await asyncio.wait_for(agent.run(max_steps=MAX_STEPS), timeout=RUN_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("Browser task timed out after %.0fs: %s", RUN_TIMEOUT_SECONDS, task_text[:200])
            return {"error": f"Browser task timed out after {RUN_TIMEOUT_SECONDS:.0f}s.", "steps": 0, "cost_usd": 0.0}
        except Exception as exc:  # noqa: BLE001 — reported to the backend as structured JSON, not a 500
            logger.exception("Browser task failed")
            return {"error": f"{type(exc).__name__}: {exc}", "steps": 0, "cost_usd": 0.0}
        finally:
            try:
                await agent.close()
            except Exception:  # noqa: BLE001 — cleanup must never mask the real result/error
                logger.warning("Error while closing the browser session", exc_info=True)
            _last_frame.update({"active": False})

        errors = [e for e in history.errors() if e]
        return {
            "result": history.final_result(),
            "success": history.is_successful(),
            "steps": len(history),
            "errors": errors,
            "cost_usd": history.usage.total_cost if history.usage else 0.0,
        }
