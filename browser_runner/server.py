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
import base64
import logging
import os
import re
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


# ── Deterministic static-site audit ──────────────────────────────────────────
# Separate from /run on purpose. /run is an LLM-driven browser-use Agent, which
# is the wrong instrument for "did the site the dev-run just built actually
# render?": that question has factual answers (console errors, 404s on assets,
# a page that overflows horizontally on a phone) and paying for a model to
# eyeball them would be slower, costlier and less reliable than measuring them.
#
# The audited site is never fetched over the network. This container serves the
# read-only /previews mount to itself on a loopback-bound ephemeral port, so
# the page loads with real absolute-path asset resolution (which file:// would
# break) while the endpoint keeps no ability to reach anything else: the URL is
# built here from a run id validated against the same pattern the backend
# generates, never from a caller-supplied address. INTERNAL_HOSTS above stays
# untouched — it governs the LLM agent's navigation, which this does not use.

PREVIEWS_ROOT = os.getenv("DEV_PREVIEWS_ROOT", "/previews")
_AUDIT_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{12}$")
AUDIT_TIMEOUT_SECONDS = max(20.0, float(os.getenv("AUDIT_TIMEOUT_SECONDS", "120")))
# Enough to catch a mobile layout break, a tablet break and the desktop the
# agent implicitly designed for. Labels travel with the findings so the model
# is told "mobile", not "375".
DEFAULT_VIEWPORTS = [
    {"label": "mobile", "width": 375, "height": 812},
    {"label": "tablet", "width": 768, "height": 1024},
    {"label": "desktop", "width": 1280, "height": 800},
]
# Screenshots are for the owner, not the executor — a text model gains nothing
# from base64 pixels and they would crowd out its context. Kept small.
SCREENSHOT_MAX_B64 = 400_000
MAX_FINDINGS_PER_KIND = 25


class AuditRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    viewports: Optional[List[Dict[str, Any]]] = None


def _start_preview_server(directory: str):
    """Loopback-only static server over the published previews directory."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 — the audit reports, not the server
            pass

    handler = functools.partial(QuietHandler, directory=directory)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


async def _audit_one_viewport(browser, base_url: str, viewport: Dict[str, Any]) -> Dict[str, Any]:
    console_errors: List[str] = []
    failed_requests: List[str] = []

    context = await browser.new_context(
        viewport={"width": int(viewport["width"]), "height": int(viewport["height"])},
        ignore_https_errors=True,
    )
    page = await context.new_page()
    page.on("console", lambda msg: (
        console_errors.append(f"{msg.type}: {msg.text}"[:400])
        if msg.type in ("error", "warning") and len(console_errors) < MAX_FINDINGS_PER_KIND else None
    ))
    page.on("pageerror", lambda exc: (
        console_errors.append(f"pageerror: {exc}"[:400])
        if len(console_errors) < MAX_FINDINGS_PER_KIND else None
    ))
    page.on("requestfailed", lambda req: (
        failed_requests.append(f"{req.method} {req.url} — {(req.failure or 'failed')}"[:400])
        if len(failed_requests) < MAX_FINDINGS_PER_KIND else None
    ))

    def _on_response(response):
        if response.status >= 400 and len(failed_requests) < MAX_FINDINGS_PER_KIND:
            failed_requests.append(f"HTTP {response.status} {response.url}"[:400])

    page.on("response", _on_response)

    result: Dict[str, Any] = {"viewport": viewport["label"],
                              "size": f"{viewport['width']}x{viewport['height']}"}
    try:
        await page.goto(base_url, wait_until="networkidle", timeout=30_000)
    except Exception as exc:  # noqa: BLE001 — a page that will not load IS the finding
        result["load_error"] = f"{type(exc).__name__}: {exc}"[:300]
        await context.close()
        result.update({"console_errors": console_errors, "failed_requests": failed_requests})
        return result

    metrics = await page.evaluate(
        """() => ({
            title: document.title || '',
            textLength: (document.body ? document.body.innerText : '').trim().length,
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
            images: Array.from(document.images).filter(i => !i.complete || i.naturalWidth === 0)
                        .map(i => i.getAttribute('src') || '(inline)').slice(0, 25),
            links: Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.getAttribute('href')).slice(0, 200),
            headings: document.querySelectorAll('h1, h2').length,
        })"""
    )
    # A page wider than its own viewport is the single most common way an
    # agent-built site breaks on a phone, and it is invisible on desktop.
    overflow = int(metrics["scrollWidth"]) - int(metrics["clientWidth"])
    result.update({
        "title": metrics["title"][:200],
        "text_length": metrics["textLength"],
        "headings": metrics["headings"],
        "broken_images": metrics["images"],
        "horizontal_overflow_px": overflow if overflow > 1 else 0,
        "links": metrics["links"],
        "console_errors": console_errors,
        "failed_requests": failed_requests,
    })

    try:
        shot = await page.screenshot(type="jpeg", quality=60, full_page=True)
        if len(shot) * 4 // 3 > SCREENSHOT_MAX_B64:
            shot = await page.screenshot(type="jpeg", quality=45, full_page=False)
        result["screenshot_b64"] = base64.b64encode(shot).decode("ascii")
    except Exception:  # noqa: BLE001 — a missing screenshot must not fail the audit
        logger.warning("Screenshot failed for %s", viewport["label"], exc_info=True)

    await context.close()
    return result


@app.post("/audit", dependencies=[Depends(_require_token)])
async def audit(req: AuditRequest) -> dict:
    """Renders one published dev-run demo at several viewports and reports what
    is factually wrong with it. No LLM, no network egress, no caller-supplied
    URL — see the section comment above."""
    if not _AUDIT_RUN_ID_RE.match(req.run_id):
        raise HTTPException(status_code=400, detail="Malformed run id")
    site_dir = os.path.join(PREVIEWS_ROOT, req.run_id)
    if not os.path.isdir(site_dir):
        raise HTTPException(status_code=404, detail=f"No published snapshot for {req.run_id}")
    if _run_lock.locked():
        raise HTTPException(status_code=429, detail="Another browser task is already running.")

    viewports = req.viewports or DEFAULT_VIEWPORTS
    async with _run_lock:
        httpd, port = _start_preview_server(PREVIEWS_ROOT)
        base_url = f"http://127.0.0.1:{port}/{req.run_id}/"
        try:
            from playwright.async_api import async_playwright

            async def _run_all():
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(
                        args=["--no-sandbox", "--disable-dev-shm-usage"])
                    try:
                        return [await _audit_one_viewport(browser, base_url, vp) for vp in viewports]
                    finally:
                        await browser.close()

            pages = await asyncio.wait_for(_run_all(), timeout=AUDIT_TIMEOUT_SECONDS)
        except TimeoutError:
            return {"error": f"Audit timed out after {AUDIT_TIMEOUT_SECONDS:.0f}s."}
        except Exception as exc:  # noqa: BLE001 — structured error, same contract as /run
            logger.exception("Site audit failed")
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            httpd.shutdown()
            httpd.server_close()

    return {"run_id": req.run_id, "pages": pages}
