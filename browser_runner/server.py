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
import json
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
# egress plus a route to the local LLM server (`hermes-llama`) to do its job, so network topology alone
# can't isolate it the way dev-runner's egress-less network does.
INTERNAL_HOSTS = [
    "localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal",
    "hermes-llama", "llama", "qdrant", "backend", "xtts", "valkey", "gitea",
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
    # A saved login (see "Saved logins" below). Absent = a throwaway profile, as before.
    profile: Optional[str] = Field(default=None, max_length=32)


def _build_llm():
    """Mirrors backend/subagents.py's _active_llm_runtime(): reuse whichever
    provider Hermes itself is configured for, so browser tasks cost/behave
    consistently with the rest of the stack. AGENT_MODEL_BROWSER overrides the
    model, same pattern as AGENT_MODEL_RESEARCH/AGENT_MODEL_CODE.
    """
    provider = os.getenv("LLM_PROVIDER", "llamacpp").strip().lower()
    model = os.getenv("AGENT_MODEL_BROWSER", "").strip() or os.getenv("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("No model configured (set AGENT_MODEL_BROWSER or LLM_MODEL)")

    if provider in ("llamacpp", "local"):
        # The local llama.cpp server speaks the OpenAI API under /v1.
        from browser_use import ChatOpenAI
        base_url = os.getenv("LLAMACPP_BASE_URL", "http://hermes-llama:8080").rstrip("/")
        from backend.provider_trust import trusted_local_endpoint
        if not trusted_local_endpoint(base_url):
            raise RuntimeError("Local browser model endpoint is outside the trusted host policy")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        # Strata's default is "projection ON" for a request that omits the field; this agent must stay on the stock model,
        # so every request body gets an explicit false.
        import httpx

        async def _force_projection_off(request: "httpx.Request") -> None:
            try:
                body = json.loads(request.content or b"{}")
            except ValueError:
                return
            if isinstance(body, dict):
                body["experimental_speed_projection"] = False
                raw = json.dumps(body).encode()
                request._content = raw
                request.headers["content-length"] = str(len(raw))
                request.stream = httpx.ByteStream(raw)

        http_client = httpx.AsyncClient(event_hooks={"request": [_force_projection_off]}, timeout=httpx.Timeout(120.0))
        return ChatOpenAI(model=model, base_url=base_url, api_key=os.getenv("LLAMACPP_API_KEY", "") or "not-needed",
                          http_client=http_client)

    # Screenshots and logged-in DOM have no privacy pass in browser-use.
    # Refuse remote providers instead of silently sending these raw payloads.
    raise RuntimeError("Browser automation requires the local model; external screenshots/DOM are blocked")


def _build_browser_profile(requested_allowed_domains: Optional[List[str]], profile: Optional[str] = None):
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

    extra: Dict[str, Any] = {}
    if profile:
        # The owner's own login lives in this directory. Same user agent and
        # window as the login window, so the site sees the same browser.
        extra = {
            "user_data_dir": _profile_dir(profile),
            "user_agent": _read_meta(profile).get("user_agent") or _user_agent(),
            "viewport": dict(LOGIN_VIEWPORT),
            "args": list(STEALTH_ARGS),
        }
    return BrowserProfile(
        headless=True,
        allowed_domains=safe_allowed,
        prohibited_domains=INTERNAL_HOSTS,
        block_ip_addresses=True,
        **extra,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "busy": _run_lock.locked(), "login_open": bool(_login), "max_steps": MAX_STEPS}


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
    if _login:
        raise HTTPException(status_code=409, detail="The owner's login window is open; close it before running a task.")
    if req.profile:
        _check_profile_name(req.profile)
        if not os.path.isdir(_profile_dir(req.profile)):
            raise HTTPException(status_code=404, detail=f"No saved login named '{req.profile}'. The owner has to sign in first.")
        _touch_profile(req.profile, "last_used_at")

    async with _run_lock:
        try:
            llm = _build_llm()
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear 503 to the backend
            raise HTTPException(status_code=503, detail=f"LLM not configured: {exc}") from exc

        from browser_use import Agent, Tools

        tools = Tools(exclude_actions=READ_ONLY_EXCLUDED_ACTIONS) if req.mode == "read_only" else Tools()
        profile = _build_browser_profile(req.allowed_domains, req.profile)

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


# ── Saved logins ─────────────────────────────────────────────────────────────
# The owner signs in to a site *themselves* in a remote-controlled browser
# window (the dashboard shows screenshots and forwards clicks and keystrokes),
# and the cookies stay in a persistent Chromium profile on a volume. Agent runs
# that name the profile reuse that session. No password ever reaches the LLM,
# the chat or a log: typed text goes straight to the page and is never logged.
#
# One browser at a time: a login window and an agent run never share a profile
# directory (Chromium locks it), so /run refuses while a window is open and the
# window refuses while a run is in flight.

PROFILES_ROOT = os.getenv("BROWSER_PROFILES_ROOT", "/profiles")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_META_FILE = ".hermes-profile.json"
LOGIN_VIEWPORT = {"width": 1280, "height": 800}
LOGIN_IDLE_SECONDS = max(60.0, float(os.getenv("BROWSER_LOGIN_IDLE_SECONDS", "600")))
# Without it every page can read navigator.webdriver = true and many sign-in
# pages refuse outright.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
_ALLOWED_KEYS = {
    "Enter", "Tab", "Backspace", "Delete", "Escape", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End",
}

_login: Dict[str, Any] = {}
_login_guard = asyncio.Lock()
_ua_cache: Dict[str, str] = {}


def _check_profile_name(name: str) -> str:
    if not _PROFILE_RE.match(name or ""):
        raise HTTPException(status_code=422, detail="Profile name: 1-32 chars, a-z 0-9 _ -, starting with a letter or digit.")
    return name


def _profile_dir(name: str) -> str:
    return os.path.join(PROFILES_ROOT, _check_profile_name(name))


def _read_meta(name: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(_profile_dir(name), _META_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(name: str, data: Dict[str, Any]) -> None:
    path = os.path.join(_profile_dir(name), _META_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def _touch_profile(name: str, field: str) -> None:
    try:
        meta = _read_meta(name)
        meta[field] = time.time()
        _write_meta(name, meta)
    except OSError:
        logger.warning("Could not update metadata of profile %s", name)


def _user_agent() -> str:
    """A normal desktop Chrome UA for the bundled Chromium's real version:
    headless Chromium announces itself as "HeadlessChrome", which sign-in
    pages reject. BROWSER_USER_AGENT overrides it."""
    configured = os.getenv("BROWSER_USER_AGENT", "").strip()
    if configured:
        return configured
    if "ua" not in _ua_cache:
        _ua_cache["ua"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"
    return _ua_cache["ua"].format(v=_ua_cache.get("version", "141.0.0.0"))


async def _detect_chrome_version(pw: Any) -> None:
    if "version" in _ua_cache:
        return
    try:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            _ua_cache["version"] = browser.version
        finally:
            await browser.close()
    except Exception:  # noqa: BLE001 — the fallback version string still works
        logger.warning("Could not read the bundled Chromium version", exc_info=True)


def _blocked_url(url: str) -> bool:
    """True for anything inside the Docker network or on a private address —
    the same boundary INTERNAL_HOSTS/block_ip_addresses draw for agent runs."""
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return False
    if parts.scheme not in ("http", "https", "ws", "wss"):
        return True
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host in INTERNAL_HOSTS or host.endswith((".internal", ".local", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


async def _guard_route(route: Any) -> None:
    if _blocked_url(route.request.url):
        await route.abort()
    else:
        await route.continue_()


def _current_page() -> Any:
    pages = [page for page in _login["context"].pages if not page.is_closed()]
    if not pages:
        return None
    page = _login.get("page")
    return page if page in pages else pages[-1]


async def _close_login() -> None:
    session = dict(_login)
    _login.clear()
    watchdog = session.get("watchdog")
    if watchdog and watchdog is not asyncio.current_task():
        watchdog.cancel()
    try:
        if session.get("context") is not None:
            await session["context"].close()  # flushes cookies to the profile directory
    except Exception:  # noqa: BLE001 — cleanup only
        logger.warning("Error while closing the login window", exc_info=True)
    try:
        if session.get("pw") is not None:
            await session["pw"].stop()
    except Exception:  # noqa: BLE001 — cleanup only
        logger.warning("Error while stopping Playwright", exc_info=True)
    if session.get("profile"):
        _touch_profile(session["profile"], "last_login_at")


async def _login_watchdog() -> None:
    while _login:
        await asyncio.sleep(15)
        if _login and time.time() - _login.get("last_active", 0) > LOGIN_IDLE_SECONDS:
            logger.info("Closing an idle login window (profile %s)", _login.get("profile"))
            async with _login_guard:
                await _close_login()


class LoginStart(BaseModel):
    profile: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=2000)
    label: Optional[str] = Field(default=None, max_length=80)


class LoginInput(BaseModel):
    type: Literal["click", "type", "key", "scroll", "navigate", "back"]
    x: Optional[float] = Field(default=None, ge=0, le=1)
    y: Optional[float] = Field(default=None, ge=0, le=1)
    text: Optional[str] = Field(default=None, max_length=500)
    key: Optional[str] = Field(default=None, max_length=20)
    dy: Optional[float] = Field(default=None, ge=-5000, le=5000)
    url: Optional[str] = Field(default=None, max_length=2000)


@app.get("/profiles", dependencies=[Depends(_require_token)])
async def list_profiles() -> dict:
    items = []
    if os.path.isdir(PROFILES_ROOT):
        for name in sorted(os.listdir(PROFILES_ROOT)):
            if not _PROFILE_RE.match(name) or not os.path.isdir(os.path.join(PROFILES_ROOT, name)):
                continue
            meta = _read_meta(name)
            items.append({
                "name": name,
                "label": meta.get("label") or name,
                "start_url": meta.get("start_url") or "",
                "created_at": meta.get("created_at"),
                "last_login_at": meta.get("last_login_at"),
                "last_used_at": meta.get("last_used_at"),
            })
    return {"profiles": items, "login_open": _login.get("profile")}


@app.delete("/profiles/{name}", dependencies=[Depends(_require_token)])
async def delete_profile(name: str) -> dict:
    import shutil

    path = _profile_dir(name)
    if _login.get("profile") == name or _run_lock.locked():
        raise HTTPException(status_code=409, detail="The profile is in use.")
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="No such profile.")
    shutil.rmtree(path)
    return {"deleted": name}


@app.post("/login/start", dependencies=[Depends(_require_token)])
async def login_start(req: LoginStart) -> dict:
    if _run_lock.locked():
        raise HTTPException(status_code=429, detail="A browser task is running; try again when it finishes.")
    if not req.url.startswith(("http://", "https://")) or _blocked_url(req.url):
        raise HTTPException(status_code=422, detail="Only public http(s) addresses can be opened.")
    path = _profile_dir(req.profile)
    async with _login_guard:
        if _login:
            await _close_login()
        from playwright.async_api import async_playwright

        os.makedirs(path, exist_ok=True)
        meta = _read_meta(req.profile)
        meta.setdefault("created_at", time.time())
        meta["start_url"] = req.url
        meta["label"] = req.label or meta.get("label") or req.profile
        _write_meta(req.profile, meta)

        pw = await async_playwright().start()
        try:
            await _detect_chrome_version(pw)
            meta["user_agent"] = _user_agent()
            _write_meta(req.profile, meta)
            context = await pw.chromium.launch_persistent_context(
                path,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", *STEALTH_ARGS],
                viewport=dict(LOGIN_VIEWPORT),
                user_agent=_user_agent(),
                locale="ru-RU",
            )
        except Exception as exc:
            await pw.stop()
            logger.exception("Could not open the login window")
            raise HTTPException(status_code=500, detail=f"Could not start the browser: {type(exc).__name__}") from exc
        await context.route("**/*", _guard_route)

        def _on_page(page: Any) -> None:
            # Sign-in popups ("Continue with Facebook") become the visible page.
            _login["page"] = page

        context.on("page", _on_page)
        page = context.pages[0] if context.pages else await context.new_page()
        _login.update({"pw": pw, "context": context, "page": page, "profile": req.profile, "last_active": time.time()})
        _login["watchdog"] = asyncio.create_task(_login_watchdog())
        try:
            await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001 — the owner sees whatever loaded and can retry
            logger.warning("Initial navigation of the login window failed", exc_info=True)
    return {"profile": req.profile, "open": True}


@app.get("/login/frame", dependencies=[Depends(_require_token)])
async def login_frame() -> dict:
    if not _login:
        return {"open": False}
    page = _current_page()
    if page is None:
        return {"open": True, "profile": _login.get("profile"), "screenshot_b64": None, "url": "", "title": ""}
    try:
        shot = await page.screenshot(type="jpeg", quality=70, timeout=10000)
        title = await page.title()
    except Exception:  # noqa: BLE001 — mid-navigation; the next poll gets a frame
        return {"open": True, "profile": _login.get("profile"), "screenshot_b64": None, "url": page.url, "title": ""}
    return {
        "open": True,
        "profile": _login.get("profile"),
        "url": page.url,
        "title": title,
        "width": LOGIN_VIEWPORT["width"],
        "height": LOGIN_VIEWPORT["height"],
        "screenshot_b64": base64.b64encode(shot).decode("ascii"),
    }


@app.post("/login/input", dependencies=[Depends(_require_token)])
async def login_input(event: LoginInput) -> dict:
    if not _login:
        raise HTTPException(status_code=409, detail="No login window is open.")
    page = _current_page()
    if page is None:
        raise HTTPException(status_code=409, detail="The login window has no open page.")
    _login["last_active"] = time.time()
    try:
        if event.type == "click":
            if event.x is None or event.y is None:
                raise HTTPException(status_code=422, detail="click needs x and y")
            await page.mouse.click(event.x * LOGIN_VIEWPORT["width"], event.y * LOGIN_VIEWPORT["height"])
        elif event.type == "type":
            # Never logged: this is where the owner types passwords and codes.
            await page.keyboard.type(event.text or "", delay=25)
        elif event.type == "key":
            if event.key not in _ALLOWED_KEYS:
                raise HTTPException(status_code=422, detail="Unsupported key")
            await page.keyboard.press(" " if event.key == "Space" else event.key)
        elif event.type == "scroll":
            await page.mouse.move(LOGIN_VIEWPORT["width"] / 2, LOGIN_VIEWPORT["height"] / 2)
            await page.mouse.wheel(0, event.dy or 0)
        elif event.type == "navigate":
            url = (event.url or "").strip()
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            if _blocked_url(url):
                raise HTTPException(status_code=422, detail="Only public http(s) addresses can be opened.")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        elif event.type == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — a failed click must not kill the window
        logger.warning("Login window input %s failed: %s", event.type, type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True}


@app.post("/login/stop", dependencies=[Depends(_require_token)])
async def login_stop() -> dict:
    async with _login_guard:
        profile = _login.get("profile")
        if _login:
            await _close_login()
    return {"open": False, "saved": profile}


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
