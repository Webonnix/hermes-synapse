import asyncio
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.logging_config import configure_logging

configure_logging()

from backend.agent import agent_instance, DECISION_LOGS
from backend.bot import init_bot, shutdown_bot
from backend.websocket_manager import manager

class AuthVerifyRequest(BaseModel):
    code: str = Field(max_length=64)

class AuthLoginRequest(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(max_length=4096)

class ChangePasswordRequest(BaseModel):
    current_password: str | None = Field(default=None, max_length=4096)
    new_username: str = Field(max_length=256)
    new_password: str = Field(max_length=4096)

class ReauthRequest(BaseModel):
    code: str = Field(max_length=64)
    password: str = Field(default="", max_length=4096)

class ConfigUpdate(BaseModel):
    system_prompt: str | None = None
    model: str | None = None
    fast_mode: bool | None = None
    max_history_len: int | None = None
    # The dashboard has had these two controls since the condenser landed, but
    # they were missing here (and from App.tsx's request body), so the checkbox
    # flipped, Save reported success, and the value was dropped on the floor —
    # the response then snapped the box back to its old state.
    condenser_enabled: bool | None = None
    condense_trigger_extra: int | None = None
    max_tokens: int | None = None
    tool_max_tokens: int | None = None
    temperature: float | None = None
    auto_rag: bool | None = None
    memory_enabled: bool | None = None
    memory_auto_save: bool | None = None
    memory_max_items: int | None = None
    telegram_reply_mode: str | None = None
    provider: str | None = None
    api_base: str | None = None
    local_base_url: str | None = None
    openai_api_base: str | None = None
    local_num_ctx: int | None = None
    local_think: bool | str | None = None

class PriceAlertRequest(BaseModel):
    symbol: str
    target_price: float
    condition: str

class SubagentUpdate(BaseModel):
    id: str
    name: str
    system_prompt: str
    model: str
    agent_type: str = "agent"
    parent_id: Optional[str] = None
    skills: str = ""
    x: int = 100
    y: int = 100
    temperature: float = 0.7
    role: str = "Specialist"
    status: str = "idle"
    is_enabled: bool = True
    model_provider: str = "local"
    model_type: str = "local"
    model_params: dict = {}
    budget_usd_limit: Optional[float] = None
    budget_period: str = "monthly"
    tier_id: Optional[str] = None
    # Additional provider_bindings ids (besides model_provider) this agent may
    # fall back to, in priority order — see backend/agent_provider_access.py.
    # Empty = unrestricted (legacy behavior).
    allowed_provider_ids: List[str] = []
    # When true, an exhausted budget degrades the agent to the free local
    # model instead of refusing the turn outright.
    budget_fallback_to_local: bool = False
    # Which named project (backend/projects.py) this agent belongs to.
    project_id: Optional[str] = None
    # backend/disciplines.py ids this agent is qualified for. Empty = generalist:
    # still assignable, but always outranked by a declared specialist.
    disciplines: List[str] = []

class SubagentPosition(BaseModel):
    id: str
    x: int
    y: int

class SubagentPositionsUpdate(BaseModel):
    positions: List[SubagentPosition]

class ScheduledTaskCreate(BaseModel):
    type: str  # "one-shot" | "alarm" | "recurring"
    label: str
    agent_id: str
    prompt: str
    duration_seconds: Optional[int] = None
    time_str: Optional[str] = None
    interval_hours: Optional[float] = None

class ControlPlaneAction(BaseModel):
    reason: str = ""

class AutonomyPlanRequest(BaseModel):
    goal: str
    root: Optional[str] = None

class ProjectIndexRequest(BaseModel):
    root: Optional[str] = None

class ProjectMemoryEntryRequest(BaseModel):
    kind: str
    title: str
    content: str
    files: List[str] = Field(default_factory=list)
    source: str = "owner"

class VoiceSynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: Optional[str] = Field(default=None, max_length=80)
    rate: float = Field(default=1.0, ge=0.6, le=1.6)

logger = logging.getLogger("hermes.main")

# In-flight chat tasks keyed by opaque client run id. This is process-local by
# design; a multi-worker deployment must move cancellation to a shared run queue.
ACTIVE_CHAT_RUNS: dict[str, asyncio.Task] = {}


async def _ensure_local_model_available(*, repair: bool) -> str:
    """Validate the configured model against what the llama.cpp server serves.

    llama-server serves exactly one model under its --alias, so the configured
    id must be that alias. With ``repair`` a stale id (an Ollama tag left over
    from before the 2026-09-13 migration, say) is replaced, and the context size
    the server really runs with is adopted so the context guards measure against
    what is allocated; either change is persisted.
    """
    from backend.llamacpp_client import LlamaCppClient, is_llamacpp_provider

    if not is_llamacpp_provider(agent_instance.api_base, agent_instance.provider):
        return agent_instance.model

    client = LlamaCppClient(agent_instance.local_base_url, timeout=min(agent_instance.request_timeout, 15))
    available = await client.model_ids()
    if agent_instance.model in available:
        selected = agent_instance.model
    elif not repair:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "local_model_not_served",
                "message": f"The llama.cpp server does not serve '{agent_instance.model}'.",
                "available_models": available,
            },
        )
    elif not available:
        raise RuntimeError("The llama.cpp server is reachable but serves no model.")
    else:
        selected = available[0]

    if not repair:
        return selected

    changed = False
    if selected != agent_instance.model:
        logger.warning("Repaired stale local model setting: %s -> %s", agent_instance.model, selected)
        agent_instance.model = selected
        changed = True
    try:
        props = await client.props()
        served_ctx = int((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx") or 0)
    except Exception:  # noqa: BLE001 - the context sync is best-effort
        served_ctx = 0
    if served_ctx and served_ctx != agent_instance.local_num_ctx:
        logger.info("Local context window: %s -> %s (from the llama.cpp server)", agent_instance.local_num_ctx, served_ctx)
        agent_instance.update_runtime_config(local_num_ctx=served_ctx)
        changed = True
    if changed:
        from backend.database import save_app_settings
        save_app_settings(agent_instance.get_runtime_config())
    return selected


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else: make a crash or a dying background worker leave a
    # recorded reason instead of a log that just stops.
    from backend import crash_visibility
    crash_visibility.install()
    # A loop that stops turning answers nothing, not even /health: exit instead,
    # and Docker's restart policy brings the backend back (HERMES_LOOP_STALL_SECONDS).
    crash_visibility.start_loop_watchdog(asyncio.get_running_loop())

    # Startup: Initialize DB, Qdrant/RAG and run the Telegram bot
    from backend.database import init_db
    init_db()

    # Any run still open in the journal belonged to a process that no longer
    # exists. Close them out so an interrupted turn is a recorded fact instead
    # of a browser waiting forever on a run nothing will finish.
    from backend import run_journal
    interrupted = run_journal.reconcile_interrupted_runs()
    if interrupted:
        logger.warning("Closed %d interrupted run(s) from the previous process", len(interrupted))
    
    from backend.rag import init_rag
    init_rag()

    # The inbox archive is written from worker threads (the access gate runs in
    # asyncio.to_thread); its live-update frames need this loop to be sent on.
    from backend import inbox
    inbox.bind_event_loop(asyncio.get_running_loop())

    # Telephony: calls left open by the previous process ended with it; then the
    # Asterisk driver (ARI events + per-call RTP media) starts in the background and
    # keeps retrying — a missing sidecar must never block the rest of the backend.
    from backend import phone_lines
    try:
        stale_calls = phone_lines.reconcile_stale_calls()
        if stale_calls:
            logger.warning("Closed %d phone call(s) left open by the previous process", stale_calls)
    except Exception:
        logger.exception("Could not reconcile phone calls")
    from backend.phone_asterisk import controller as phone_controller
    await crash_visibility.bounded_start(phone_controller.start(), "телефония")

    try:
        await _ensure_local_model_available(repair=True)
    except Exception as exc:
        logger.error("Local model startup validation failed: %s", exc)

    if os.getenv("VOICE_STT_PRELOAD", "false").lower() in {"1", "true", "yes", "on"}:
        async def _preload_voice():
            try:
                from backend.voice import preload_voice_model
                status = await asyncio.to_thread(preload_voice_model)
                logger.info("Voice recognition ready: %s", status["model"])
            except Exception as exc:
                logger.warning("Voice recognition preload failed (will retry on demand): %s", exc)

        crash_visibility.spawn(_preload_voice(), "_preload_voice", oneshot=True)

    if os.getenv("PROJECT_MEMORY_AUTO_INDEX", "true").lower() in {"1", "true", "yes", "on"}:
        async def _index_project_memory():
            try:
                from backend.autonomy import index_project
                result = await asyncio.to_thread(index_project)
                logger.info(
                    "Project memory indexed: %s files (%s changed)",
                    result["files"],
                    result["indexed"],
                )
            except Exception as exc:
                logger.warning("Project memory indexing failed (non-fatal): %s", exc)

        crash_visibility.spawn(_index_project_memory(), "_index_project_memory", oneshot=True)
    
    # Start price alert monitor background task
    from backend.price_monitor import price_monitor
    price_monitor.start()

    # Incognito chats must not outlive the process that served them.
    try:
        from backend import chat_settings
        purged = await asyncio.to_thread(chat_settings.purge_ephemeral_sessions)
        if purged:
            logger.info("Purged %d incognito chat(s) left from the previous run", purged)
    except Exception:
        logger.exception("Could not purge incognito chats at startup")

    # Durable dev-runs worker: resumes unfinished runs after restart.
    dev_runs_worker_task = None
    if os.getenv("DEV_RUNS_WORKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from backend import dev_runs
        dev_runs_worker_task = crash_visibility.spawn_supervised(dev_runs.worker_loop, "dev_runs.worker_loop")

    # Host metrics history for the «Метрики» charts: the host agent keeps only the
    # latest snapshot, this loop accumulates it (see backend/host_metrics.py).
    host_metrics_task = None
    if os.getenv("HOST_METRICS_HISTORY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from backend import host_metrics
        host_metrics_task = crash_visibility.spawn_supervised(host_metrics.sampler_loop, "host_metrics.sampler_loop")

    # Content Factory: additive schema, its agent roster, and the pipeline
    # worker that resumes interrupted runs. Every step is guarded — a failure
    # here disables content production, it must not stop the whole backend.
    content_factory_worker_task = None
    try:
        from backend.content_factory import agents as cf_agents
        from backend.content_factory.schema import ensure_schema as cf_ensure_schema
        cf_ensure_schema()
        seeded = cf_agents.seed_agents()
        if seeded:
            logger.info("Content Factory: seeded agents %s", ", ".join(seeded))
        if os.getenv("CONTENT_FACTORY_WORKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
            from backend.content_factory import execution as cf_execution
            content_factory_worker_task = crash_visibility.spawn_supervised(cf_execution.worker_loop, "content_factory.worker_loop")
    except Exception:
        logger.exception("Content Factory startup failed — the module is unavailable this run")

    # Every channel starts under a time limit: one hung bot must not hold up the rest.
    bot_app = await crash_visibility.bounded_start(init_bot(), "Telegram-бот", 60)

    from backend import agent_bot
    await crash_visibility.bounded_start(agent_bot.manager.start_all_active(), "каналы: agent_bot")

    from backend import agent_matrix_bot
    await crash_visibility.bounded_start(agent_matrix_bot.manager.start_all_active(), "каналы: agent_matrix_bot")

    from backend import agent_discord_bot
    await crash_visibility.bounded_start(agent_discord_bot.manager.start_all_active(), "каналы: agent_discord_bot")

    from backend import agent_slack_bot
    await crash_visibility.bounded_start(agent_slack_bot.manager.start_all_active(), "каналы: agent_slack_bot")

    from backend import agent_email_channel
    await crash_visibility.bounded_start(agent_email_channel.manager.start_all_active(), "каналы: agent_email_channel")

    # SMM Factory social channels. VK polls like the messengers above; the Meta
    # ones only register what is active — their messages arrive at
    # backend/social_webhooks.py, so there is nothing to start.
    from backend import agent_vk_bot
    await crash_visibility.bounded_start(agent_vk_bot.manager.start_all_active(), "каналы: agent_vk_bot")

    from backend import agent_meta_channel
    await crash_visibility.bounded_start(agent_meta_channel.manager.start_all_active(), "каналы: agent_meta_channel")

    from backend import agent_whatsapp_channel
    await crash_visibility.bounded_start(agent_whatsapp_channel.manager.start_all_active(), "каналы: agent_whatsapp_channel")

    # Background Obsidian vault sync (non-blocking)
    async def _obsidian_startup_sync():
        try:
            from backend.obsidian import is_reachable, sync_vault_to_rag
            if await is_reachable():
                logger.info("Obsidian is reachable — starting vault sync in background...")
                result = await sync_vault_to_rag()
                logger.info(f"Obsidian startup sync: {result.get('message', result)}")
            else:
                logger.info("Obsidian not reachable at startup (plugin not running or key not set — OK).")
        except Exception as e:
            logger.warning(f"Obsidian startup sync failed (non-fatal): {e}")
    crash_visibility.spawn(_obsidian_startup_sync(), "_obsidian_startup_sync", oneshot=True)
    
    from backend.mcp_client import init_mcp_servers, shutdown_mcp_servers
    await crash_visibility.bounded_start(init_mcp_servers(), "MCP-серверы", 60)

    # Start BCM Session Scheduler in background (non-blocking)
    async def _bcm_session_scheduler_task():
        import sys
        import subprocess
        logger.info("BCM Session Scheduler background loop started.")
        while True:
            try:
                # Runs the session_scheduler checking rules every minute
                subprocess.run([sys.executable, "/app/backend/bcm/session_scheduler.py"], capture_output=True)
            except Exception as e:
                logger.error(f"Error in BCM session scheduler task: {e}")
            await asyncio.sleep(60)
    crash_visibility.spawn_supervised(_bcm_session_scheduler_task, "_bcm_session_scheduler_task")

    # Sweep lapsed subscriptions hourly so the admin view and the token statuses
    # agree. Enforcement doesn't depend on this — bot_access.check_access catches
    # a lapse on the very next message — it just keeps the books tidy.
    async def _subscription_expiry_task():
        from backend.bot_access import expire_lapsed_subscriptions

        while True:
            try:
                await asyncio.to_thread(expire_lapsed_subscriptions)
            except Exception:
                logger.exception("Subscription expiry sweep failed")
            await asyncio.sleep(3600)
    crash_visibility.spawn_supervised(_subscription_expiry_task, "_subscription_expiry_task")

    # Recurring client invoices + overdue ageing. Idempotent per (услуга,
    # период), so an hourly sweep is safe and a missed run (backend restart)
    # simply catches up on the next tick — see backend/client_billing.py.
    client_billing_task = None
    if os.getenv("CLIENT_BILLING_SCHEDULER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from backend import client_billing
        client_billing_task = crash_visibility.spawn_supervised(client_billing.scheduler_loop, "client_billing.scheduler_loop")

    # SMM Factory: send scheduled posts when their slot arrives. Only auto
    # posts move — a manual one has no way out of here, so a timer must not
    # claim it went out (backend/smm_posts.py). Minute granularity is enough:
    # the schedule picker offers minutes, and a missed tick simply catches up.
    smm_scheduler_task = None
    if os.getenv("SMM_SCHEDULER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from backend import smm_posts
        smm_scheduler_task = crash_visibility.spawn_supervised(smm_posts.scheduler_loop, "smm_posts.scheduler_loop")

    # Agent self-development: each agent's learning track runs on its cadence,
    # inside the quiet window (AGENT_LEARNING_WINDOW), one run at a time — it
    # uses the same model as live chat. See backend/agent_learning.py.
    if os.getenv("AGENT_LEARNING_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from backend import agent_learning
        crash_visibility.spawn_supervised(agent_learning.scheduler_loop, "agent_learning.scheduler_loop")

    # Autonomous Earner: schema always (so the section renders), the worker only
    # while EARNER_ENABLED. The worker marks stages a previous process left
    # mid-way as interrupted and never resumes them (backend/earner/worker.py).
    try:
        from backend.earner import store as earner_store
        earner_store.init_schema()
        if os.getenv("EARNER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
            from backend.earner import worker as earner_worker
            crash_visibility.spawn_supervised(earner_worker.worker_loop, "earner.worker_loop")
    except Exception:
        logger.exception("Autonomous Earner disabled: startup failed")

    # Proactively probe every active messenger channel's stored credential so a
    # dead Matrix/Telegram/Discord/Slack/email token surfaces as a visible
    # "Ошибка" in the dashboard within minutes, not whenever someone happens to
    # notice a message never arrived.
    async def _channel_health_check_task():
        from backend.agent_messenger_governance import health_check_all_active

        while True:
            await asyncio.sleep(900)
            try:
                await health_check_all_active()
            except Exception:
                logger.exception("Messenger channel health check sweep failed")
    crash_visibility.spawn_supervised(_channel_health_check_task, "_channel_health_check_task")

    # CRM: one Telegram message a day listing overdue and due-today tasks —
    # the "reminder" half of the Задачи/Календарь section. A 30-minute poll
    # rather than sleeping until the exact hour: it survives a restart
    # mid-window without drifting, the same idiom every other sweep in this
    # file uses. `crm_reminder_last_sent` (app_settings) is the dedup key, so
    # a redeploy inside the same reminder hour cannot double-send.
    if os.getenv("CRM_REMINDER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        reminder_hour = int(os.getenv("CRM_REMINDER_HOUR_UTC", "6"))

        async def _crm_reminder_task():
            from datetime import datetime, timezone
            from backend import client_tasks
            from backend.database import get_setting, set_setting

            while True:
                await asyncio.sleep(1800)
                now = datetime.now(timezone.utc)
                if now.hour != reminder_hour:
                    continue
                today = now.date().isoformat()
                try:
                    if await asyncio.to_thread(get_setting, "crm_reminder_last_sent") == today:
                        continue
                    text = await asyncio.to_thread(client_tasks.daily_digest_text)
                    if text:
                        import backend.bot as bot_module
                        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
                        if chat_id and getattr(bot_module, "telegram_app", None) and bot_module.telegram_app.bot:
                            await bot_module.telegram_app.bot.send_message(chat_id=int(chat_id), text=text)
                    await asyncio.to_thread(set_setting, "crm_reminder_last_sent", today)
                except Exception:
                    logger.exception("CRM daily reminder failed")
        crash_visibility.spawn_supervised(_crm_reminder_task, "_crm_reminder_task")

    yield
    if smm_scheduler_task is not None:
        smm_scheduler_task.cancel()
        try:
            await smm_scheduler_task
        except asyncio.CancelledError:
            pass

    if client_billing_task is not None:
        client_billing_task.cancel()
        try:
            await client_billing_task
        except asyncio.CancelledError:
            pass

    # Content Factory worker stops before the dev-runs one: it holds provider
    # calls that cost money, so it should stop accepting new nodes first.
    if content_factory_worker_task is not None:
        content_factory_worker_task.cancel()
        try:
            await content_factory_worker_task
        except asyncio.CancelledError:
            pass

    if host_metrics_task is not None:
        host_metrics_task.cancel()
        try:
            await host_metrics_task
        except asyncio.CancelledError:
            pass

    # Shutdown: stop the dev-runs worker first so no new tool calls start.
    if dev_runs_worker_task is not None:
        dev_runs_worker_task.cancel()
        try:
            await dev_runs_worker_task
        except asyncio.CancelledError:
            pass

    # Shutdown: Stop Telegram bots
    from backend import agent_bot
    await agent_bot.manager.stop_all()
    from backend import agent_matrix_bot
    await agent_matrix_bot.manager.stop_all()
    from backend import agent_discord_bot
    await agent_discord_bot.manager.stop_all()
    from backend import agent_slack_bot
    await agent_slack_bot.manager.stop_all()
    from backend import agent_email_channel
    await agent_email_channel.manager.stop_all()

    from backend import agent_vk_bot
    await agent_vk_bot.manager.stop_all()

    from backend import agent_meta_channel
    await agent_meta_channel.manager.stop_all()

    from backend import agent_whatsapp_channel
    await agent_whatsapp_channel.manager.stop_all()

    from backend.phone_asterisk import controller as phone_controller
    await phone_controller.stop()
    await shutdown_bot()
    
    # Stop price alert monitor background task
    price_monitor.stop()

    # Stop timers, alarms and recurring reminders before the event loop closes.
    from backend.scheduler import shutdown_scheduler_tasks
    await shutdown_scheduler_tasks()

    # Shutdown MCP servers
    await shutdown_mcp_servers()


app = FastAPI(
    title="Hermes Vexa Backend",
    description="Backend services for the Vexa AI Personal Assistant",
    lifespan=lifespan
)

@app.post("/api/runs/{run_id}/cancel")
async def cancel_chat_run(run_id: str):
    task = ACTIVE_CHAT_RUNS.get(run_id)
    if task is None or task.done():
        return {"status": "not_running", "run_id": run_id}
    task.cancel()
    return {"status": "cancelling", "run_id": run_id}

from backend.auth import validate_session
from fastapi.responses import JSONResponse

@app.middleware("http")
async def slow_request_watchdog(request: Request, call_next):
    """Outermost middleware: a request that hangs inside auth or routing is
    exactly the kind this needs to see, so tracking starts before anything else.

    Registered after auth_middleware and therefore *outside* it — Starlette runs
    middleware in reverse registration order."""
    from backend import request_diagnostics
    request_id = request_diagnostics.begin(request.method, request.url.path)
    try:
        return await call_next(request)
    finally:
        request_diagnostics.finish(request_id)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/api/plots/", "/api/generated-images/")):
        from backend.artifact_links import valid
        if await asyncio.to_thread(valid, path, request.query_params.get("expires", ""), request.query_params.get("signature", "")):
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
    # Allow public auth routes and plots (images)
    if path in (
        "/health/live",
        "/health/ready",
        "/api/auth/request-code",
        "/api/auth/verify-code",
        "/api/auth/login",
        "/api/auth/session",
        # Crypto payment callback: the provider can't hold a dashboard session,
        # so this one route authenticates itself with an HMAC signature over the
        # raw body instead (backend/payments.handle_webhook). It grants nothing
        # on a bad signature.
        "/api/payments/webhook",
    ) or (
        # Public read-only conversation snapshots. The token IS the credential,
        # and what it unlocks was sanitized once at creation — there is nothing
        # session-scoped behind this path to protect.
        path.startswith("/share/")
    ) or (
        # Meta social webhooks (Instagram/Facebook/WhatsApp): the provider can't
        # hold a dashboard session either, so these routes authenticate each
        # delivery themselves — the verify token on the GET handshake and an
        # X-Hub-Signature-256 HMAC over the raw body on every POST
        # (backend/social_webhooks.py). They grant nothing without that proof.
        path.startswith("/api/social/webhook/")
    ) or (
        # External voice provider callbacks (backend/voice_providers.py): HMAC over
        # the raw body with a per-provider secret, idempotent by event id. Unknown
        # provider, no secret and a bad signature all answer the same 401.
        path.startswith("/api/voice/webhook/")
    ) or (
        # Phone provider webhooks (backend/phone_api.py): X-Twilio-Signature or
        # Telnyx Ed25519 on every request. Media WebSockets under the same
        # prefixes are not HTTP and carry a per-call HMAC token instead.
        path.startswith("/api/phone/twilio/") or path.startswith("/api/phone/telnyx/")
    ):
        return await call_next(request)
        
    # Apply auth only to API routes
    if not path.startswith("/api/"):
        return await call_next(request)
        
    from backend import auth, auth_state
    from backend.auth_http import connection_token, csrf_valid
    token = connection_token(request)
    try:
        data = await asyncio.to_thread(auth.session_data, token, touch=False)
    except auth_state.AuthUnavailable:
        return JSONResponse(status_code=503, content={"detail": "Authentication temporarily unavailable"})
    if not data:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized: Session expired or invalid"})
    request.state.auth = data
    request.state.auth_token = token
    role_scoped_prefixes = ("/api/clients", "/api/client-", "/api/inbox", "/api/crm", "/api/calendar",
                            "/api/phone/", "/api/currency", "/api/v1/content-factory/", "/api/agent-desktops/",
                            "/api/auth/logout", "/api/auth/activity")
    if data["role"] != "owner" and not path.startswith(role_scoped_prefixes) and path not in ("/api/status", "/api/settings"):
        return JSONResponse(status_code=403, content={"detail": "Owner access required"})
    if not request.headers.get("authorization", "").startswith("Bearer ") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not csrf_valid(request, data):
            return JSONResponse(status_code=403, content={"detail": "Invalid CSRF token or origin"})
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not await asyncio.to_thread(auth.session_data, token, touch=True):
            return JSONResponse(status_code=401, content={"detail": "Session expired"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response

@app.middleware("http")
async def auth_rate_limit_middleware(request: Request, call_next):
    from backend import auth_state
    from backend.auth_http import origin_allowed
    from backend.auth import auth_retry_after
    is_auth = request.url.path.startswith("/api/auth/")
    try:
        if is_auth and request.method not in ("GET", "HEAD", "OPTIONS") and not origin_allowed(request):
            return JSONResponse(status_code=403, content={"detail": "Invalid origin"})
        retry_after = 0
        if is_auth and request.method != "OPTIONS":
            retry_after = await asyncio.to_thread(auth_retry_after, request.url.path)
        if retry_after:
            response = JSONResponse(status_code=429,
                content={"detail": "Too many authentication attempts. Please wait.",
                         "message": "Too many authentication attempts. Please wait."},
                headers={"Retry-After": str(retry_after)})
        else:
            response = await call_next(request)
    except auth_state.AuthUnavailable:
        response = JSONResponse(status_code=503, content={"detail": "Authentication temporarily unavailable"})
    if is_auth:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/auth/request-code")
async def request_code():
    from backend.auth import generate_otp, invalidate_otp, OTPRequestThrottled
    import backend.bot
    import os
    
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        return {"status": "error", "message": "TELEGRAM_CHAT_ID is not configured on backend."}
    if not backend.bot.telegram_app or not backend.bot.telegram_app.bot:
        logger.error("Telegram bot is not initialized.")
        return {"status": "error", "message": "Telegram bot is not initialized."}
    try:
        code = await asyncio.to_thread(generate_otp)
    except OTPRequestThrottled as exc:
        return JSONResponse(
            status_code=429,
            content={"status": "error", "message": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        )
        
    msg = (
        f"🏛️ **Hermes Authorization Request**\n\n"
        f"Albert, an entry request to the web dashboard was detected.\n"
        f"Your one-time access code is:\n\n"
        f"`{code}`\n\n"
        f"This code is valid for 5 minutes."
    )
    
    delivered = False
    try:
        await backend.bot.telegram_app.bot.send_message(
            chat_id=int(chat_id), text=msg, parse_mode="Markdown"
        )
        delivered = True
        return {"status": "success", "message": "Code sent to Telegram."}
    except Exception as exc:
        logger.error("Failed to send auth code to Telegram (%s)", type(exc).__name__)
        return {"status": "error", "message": "Failed to send code. Check server logs."}
    finally:
        # Cancellation can happen while the network send is pending too.
        if not delivered:
            await asyncio.to_thread(invalidate_otp, code)

@app.post("/api/auth/verify-code")
async def verify_code(req: AuthVerifyRequest):
    from backend.auth import verify_otp, create_session
    from backend.auth_http import login_response
    if await asyncio.to_thread(verify_otp, req.code):
        token = await asyncio.to_thread(create_session)
        return await asyncio.to_thread(login_response, token)
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid or expired access code, Albert.")

@app.post("/api/auth/login")
async def login(req: AuthLoginRequest):
    from backend.auth import verify_password, create_session
    from backend import database as db

    stored_username = db.get_setting("admin_username")
    stored_hash = db.get_setting("admin_password_hash")
    stored_salt = db.get_setting("admin_password_salt")

    if not stored_username or not stored_hash or not stored_salt:
        raise HTTPException(status_code=401, detail="Password login is not set up yet. Sign in via Telegram and set a password in Config.")

    valid_password = await asyncio.to_thread(verify_password, req.password, stored_hash, stored_salt)
    if req.username.strip() != stored_username or not valid_password:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    from backend.auth_http import login_response
    token = await asyncio.to_thread(create_session)
    return await asyncio.to_thread(login_response, token)


@app.get("/api/auth/session")
async def auth_session(request: Request):
    from backend import auth
    from backend.auth_http import connection_token
    data = await asyncio.to_thread(auth.session_data, connection_token(request), touch=False)
    if not data:
        raise HTTPException(status_code=401, detail="Session expired")
    return {"authenticated": True, "role": data["role"], "csrf": data["csrf"]}


@app.post("/api/auth/logout")
async def logout(request: Request):
    from backend import auth
    from backend.auth_http import connection_token, SESSION_COOKIE, CSRF_COOKIE
    await asyncio.to_thread(auth.destroy_session, connection_token(request))
    response = JSONResponse({"status": "success"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@app.post("/api/auth/activity")
async def session_activity():
    # Auth/CSRF middleware already refreshed an actual user interaction.
    return {"status": "success"}


@app.post("/api/auth/reauth")
async def reauthenticate(req: ReauthRequest, request: Request):
    from backend import auth, database
    from backend.auth_http import connection_token
    token = connection_token(request)
    if auth.role_for_session(token) != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    stored_hash = database.get_setting("admin_password_hash")
    stored_salt = database.get_setting("admin_password_salt")
    password_ok = not stored_hash or bool(stored_salt and await asyncio.to_thread(
        auth.verify_password, req.password, stored_hash, stored_salt))
    code_ok = await asyncio.to_thread(auth.verify_otp, req.code)
    if not password_ok or not code_ok:
        raise HTTPException(status_code=401, detail="Invalid password or access code")
    if not await asyncio.to_thread(auth.mark_session_fresh, token):
        raise HTTPException(status_code=401, detail="Session expired")
    return {"status": "success"}


class ArtifactLinkRequest(BaseModel):
    path: str = Field(max_length=240)


@app.post("/api/artifacts/link")
async def share_artifact(payload: ArtifactLinkRequest, request: Request):
    from backend import permissions
    from backend.artifact_links import sign
    if permissions.role_from_request(request) != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    try:
        url = await asyncio.to_thread(sign, payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"url": url, "expires_in": 300}

@app.post("/api/auth/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    from backend.auth import hash_password, verify_password
    from backend import database as db

    if not req.new_username.strip() or not req.new_password:
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    stored_hash = db.get_setting("admin_password_hash")
    stored_salt = db.get_setting("admin_password_salt")
    if stored_hash and stored_salt:
        # A password is already configured — require the current one to change it.
        if not req.current_password or not verify_password(req.current_password, stored_hash, stored_salt):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")

    new_hash, new_salt = hash_password(req.new_password)
    db.set_setting("admin_username", req.new_username.strip())
    db.set_setting("admin_password_hash", new_hash)
    db.set_setting("admin_password_salt", new_salt)
    from backend.auth import revoke_all_sessions, create_session
    from backend.auth_http import login_response
    await asyncio.to_thread(revoke_all_sessions)
    return await asyncio.to_thread(login_response, await asyncio.to_thread(create_session))

from fastapi.staticfiles import StaticFiles
import os

plots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "plots")
os.makedirs(plots_dir, exist_ok=True)
app.mount("/api/plots", StaticFiles(directory=plots_dir), name="plots")

generated_images_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "generated_images")
os.makedirs(generated_images_dir, exist_ok=True)
app.mount("/api/generated-images", StaticFiles(directory=generated_images_dir), name="generated_images")


# Enable CORS for frontend dashboard. Origins come from HERMES_ALLOWED_ORIGINS
# (comma-separated); the default covers local dev and the known deployments.
_DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:9119,http://127.0.0.1:9119,"
    "http://192.168.0.200:9119,https://hermes.webonnix.net"
)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("HERMES_ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS).split(",")
    if origin.strip()
]
if "*" in ALLOWED_ORIGINS:
    raise ValueError("Credentialed CORS requires explicit origins")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# CONTENT FACTORY (§74). Mounted as a router rather than more @app routes: it is
# the one module here with a versioned API surface of its own, and the auth
# middleware above already covers every /api/ path it exposes.
try:
    from backend.content_factory.api import router as content_factory_router
    app.include_router(content_factory_router)
except Exception:
    logger.exception("Content Factory API not mounted")


# SMM FACTORY. Meta's webhook receiver, mounted as its own router: public by
# design (see the auth middleware's allow-list) because it authenticates each
# delivery by HMAC signature rather than by a dashboard session. Kept out of the
# Content Factory try/except above so a failure there cannot silently take the
# social channels' only inbound path down with it.
try:
    from backend.social_webhooks import router as social_webhooks_router
    app.include_router(social_webhooks_router)
except Exception:
    logger.exception("SMM Factory social webhooks not mounted")


# Global uncensored-mode switch for the local model and the /llm-open door nginx's /llm/ uses (backend/uncensored_mode.py).
try:
    from backend.uncensored_mode import router as uncensored_mode_router
    app.include_router(uncensored_mode_router)
except Exception:
    logger.exception("Uncensored-mode router not mounted")


# INBOX: conversation archive, takeover, routing rules, conversation routing and
# external voice providers (backend/inbox_api.py).
try:
    from backend.inbox_api import router as inbox_router
    app.include_router(inbox_router)
except Exception:
    logger.exception("Inbox API not mounted")


# AGENT SELF-DEVELOPMENT: learning tracks, runs and learned items
# (backend/agent_learning_api.py).
try:
    from backend.agent_learning_api import router as agent_learning_router
    app.include_router(agent_learning_router)
except Exception:
    logger.exception("Agent learning API not mounted")


# AUTONOMOUS EARNER: opportunity research, proposals, approvals, sandbox checks
# (backend/earner/api.py).
try:
    from backend.earner.api import router as earner_router
    app.include_router(earner_router)
except Exception:
    logger.exception("Earner API not mounted")


# TELEPHONY: lines, numbers, speech services, calls, provider webhooks and media
# streams (backend/phone_api.py).
try:
    from backend.phone_api import router as phone_router
    app.include_router(phone_router)
except Exception:
    logger.exception("Phone API not mounted")


# AGENT AUTONOMY: the owner's one switch for how many approvals every agent's
# tool calls need (backend/autonomy_mode.py).
try:
    from backend.autonomy_mode import router as autonomy_mode_router
    app.include_router(autonomy_mode_router)
except Exception:
    logger.exception("Agent autonomy API not mounted")


# SAVED BROWSER LOGINS: the owner signs in to a site in a remote login window and
# agents reuse the session (backend/browser_sessions.py, browser_runner/server.py).
try:
    from backend.browser_sessions import router as browser_sessions_router
    app.include_router(browser_sessions_router)
except Exception:
    logger.exception("Browser sessions API not mounted")


# AGENT DESKTOPS: CLI coding agents (Grok Build, OpenCode) in per-agent gVisor
# desktops the owner can watch live (backend/agent_runtimes/, agent_desktop/).
try:
    from backend.agent_runtimes.api import router as agent_desktops_router
    app.include_router(agent_desktops_router)
except Exception:
    logger.exception("Agent desktops API not mounted")


# Sandboxes on the dev network (dev-runner, agent desktops) may reach only the
# desktops' model door, never /api (backend/agent_runtimes/net_guard.py).
# Registered after every @app.middleware above, so it is the outermost layer.
from backend.agent_runtimes.net_guard import SandboxNetworkGuard
app.add_middleware(SandboxNetworkGuard)


@app.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    """Check dependencies without loading a model or running inference."""
    checks: dict[str, dict] = {}

    try:
        from backend.database import DB_PATH
        with sqlite3.connect(DB_PATH, timeout=2) as connection:
            connection.execute("SELECT 1").fetchone()
        checks["database"] = {"ok": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": type(exc).__name__}

    async with httpx.AsyncClient(timeout=3.0) as client:
        qdrant_url = (
            f"http://{os.getenv('QDRANT_HOST', 'qdrant')}:"
            f"{os.getenv('QDRANT_PORT', '6333')}/readyz"
        )
        try:
            response = await client.get(qdrant_url)
            checks["qdrant"] = {"ok": response.status_code == 200}
        except Exception as exc:
            checks["qdrant"] = {"ok": False, "error": type(exc).__name__}

        try:
            response = await client.get(f"{agent_instance.local_base_url}/health")
            checks["llm"] = {"ok": response.status_code == 200}
        except Exception as exc:
            checks["llm"] = {"ok": False, "error": type(exc).__name__}

    from backend.obsidian import _get_api_key, is_reachable
    if _get_api_key():
        try:
            checks["obsidian"] = {"ok": await is_reachable()}
        except Exception as exc:
            checks["obsidian"] = {"ok": False, "error": type(exc).__name__}

    ready = all(item["ok"] for item in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "degraded", "checks": checks},
    )

class SettingsUpdate(BaseModel):
    language: str | None = None  # e.g. 'ru', 'en', 'he'


@app.get("/api/status")
async def get_status():
    return {
        "status": "online",
        "agent": {
            "model": agent_instance.model,
            "provider": agent_instance.provider,
            "api_base": agent_instance.api_base,
            "max_history_len": agent_instance.max_history_len,
            "memory_enabled": agent_instance.memory_enabled,
        },
        "logs_count": len(DECISION_LOGS)
    }

# Whitelist of secrets the dashboard is allowed to store — everything used by
# tools.py's _env() calls, plus the main LLM fallback provider. Editing this
# list is the only way to expose a new key in the Settings → API Keys tab;
# POST /api/settings/api-keys rejects any key_name not in this set.
KNOWN_API_KEYS = [
    {"key_name": "STABILITY_API_KEY", "label": "Stability AI", "description": "Платная генерация изображений (tool generate_image).", "category": "Инструменты"},
    {"key_name": "SERPER_API_KEY", "label": "Serper", "description": "Веб-поиск, новости, футбольная аналитика.", "category": "Инструменты"},
    {"key_name": "OPENWEATHERMAP_API_KEY", "label": "OpenWeatherMap", "description": "Реальная погода вместо заглушки.", "category": "Инструменты"},
    {"key_name": "TODOIST_API_TOKEN", "label": "Todoist", "description": "Синхронизация задач для Daily Planner.", "category": "Инструменты"},
    {"key_name": "OBSIDIAN_API_KEY", "label": "Obsidian", "description": "Доступ к Obsidian Local REST API (skill obsidian_rag).", "category": "Интеграции"},
    {"key_name": "GITEA_TOKEN", "label": "Gitea", "description": "Доступ агентов к dev-репозиторию (skill git_dev).", "category": "Интеграции"},
    {"key_name": "OPENROUTER_API_KEY", "label": "OpenRouter", "description": "Облачный провайдер для основной модели, если выбран OpenRouter.", "category": "LLM"},
    {"key_name": "ROUTER_API_KEY", "label": "AI Router (OmniRoute)", "description": "Ключ из OmniRoute → Dashboard → API Keys. Используется в разделе «AI Router» только для проверки доступности и списка моделей (GET /v1/models) — квоты и usage смотрите в самом OmniRoute. Тот же ключ можно использовать и для провайдер-биндинга агентов через Админку → Providers.", "category": "LLM"},
    {"key_name": "OMNIROUTE_MCP_API_KEY", "label": "OmniRoute MCP", "description": "Отдельный ключ OmniRoute со scope mcp:connect (Dashboard → API Keys) для подключения его MCP-сервера: агенты получают инструменты роутинга, квот, стоимости и веб-поиска OmniRoute. Если не задан — используется ROUTER_API_KEY.", "category": "LLM"},
    {"key_name": "EARNER_GITHUB_TOKEN", "label": "GitHub (Autonomous Earner)", "description": "Classic-токен GitHub со scope «public_repo» (fine-grained токены не могут писать в чужие публичные репозитории). Earner публикует им комментарий или открывает PR из вашего форка — только после вашего подтверждения конкретного текста и diff.", "category": "Инструменты"},
    {"key_name": "EARNER_SUPERTEAM_API_KEY", "label": "Superteam Earn (Autonomous Earner)", "description": "Ключ агента Superteam Earn (sk_…, выдаётся при регистрации агента: POST https://superteam.fun/api/agents). Earner только читает листинги, открытые для ИИ-агентов.", "category": "Инструменты"},
    {"key_name": "NOWPAYMENTS_API_KEY", "label": "NOWPayments (API)", "description": "Выставление криптосчетов за подписки на ботов.", "category": "Биллинг"},
    {"key_name": "NOWPAYMENTS_IPN_SECRET", "label": "NOWPayments (IPN)", "description": "Секрет для проверки подписи callback-ов об оплате. Без него платежи не подтверждаются.", "category": "Биллинг"},
]
_KNOWN_API_KEY_NAMES = {entry["key_name"] for entry in KNOWN_API_KEYS}

class ApiKeyUpdate(BaseModel):
    key_name: str
    value: str

@app.get("/api/settings/api-keys")
async def list_api_keys():
    from backend import database as db
    configured = set(db.list_configured_api_keys())
    return {
        "keys": [
            {**entry, "configured": entry["key_name"] in configured or bool(os.getenv(entry["key_name"], "").strip())}
            for entry in KNOWN_API_KEYS
        ]
    }

@app.post("/api/settings/api-keys")
async def save_api_key(update: ApiKeyUpdate):
    from backend import database as db
    if update.key_name not in _KNOWN_API_KEY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown key: {update.key_name}")
    if not update.value.strip():
        raise HTTPException(status_code=400, detail="Value is required.")
    db.set_api_key(update.key_name, update.value.strip())
    await _reconnect_omniroute_mcp_if(update.key_name)
    return {"status": "success"}

@app.delete("/api/settings/api-keys/{key_name}")
async def remove_api_key(key_name: str):
    from backend import database as db
    if key_name not in _KNOWN_API_KEY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown key: {key_name}")
    db.delete_api_key(key_name)
    await _reconnect_omniroute_mcp_if(key_name)
    return {"status": "success"}


async def _reconnect_omniroute_mcp_if(key_name: str) -> None:
    """A changed OmniRoute key takes effect for its MCP tools right away."""
    if key_name in {"OMNIROUTE_MCP_API_KEY", "ROUTER_API_KEY"}:
        from backend import omniroute_mcp

        await omniroute_mcp.connect()

@app.get("/api/config")
async def get_config():
    return agent_instance.get_runtime_config()

@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    selected_model = update.model
    prospective_provider = update.provider if update.provider is not None else agent_instance.provider
    prospective_api_base = update.api_base if update.api_base is not None else agent_instance.api_base
    from backend.llamacpp_client import LlamaCppClient, is_llamacpp_provider
    if is_llamacpp_provider(prospective_api_base, prospective_provider) and (
        update.model is not None
        or update.provider is not None
        or update.local_base_url is not None
    ):
        local_base_url = update.local_base_url or agent_instance.local_base_url
        try:
            available = await LlamaCppClient(
                local_base_url,
                timeout=min(agent_instance.request_timeout, 15),
            ).model_ids()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "local_llm_unavailable",
                    "message": f"Cannot validate the local model: {exc}",
                },
            ) from exc
        requested_model = update.model or agent_instance.model
        selected_model = requested_model if requested_model in available else None
        if selected_model is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "local_model_not_served",
                    "message": f"The llama.cpp server does not serve '{requested_model}'.",
                    "available_models": available,
                },
            )

    if update.system_prompt is not None:
        agent_instance.update_system_prompt(update.system_prompt)
    if selected_model is not None:
        agent_instance.model = selected_model
    agent_instance.update_runtime_config(
        provider=update.provider,
        api_base=update.api_base,
        local_base_url=update.local_base_url,
        openai_api_base=update.openai_api_base,
        local_num_ctx=update.local_num_ctx,
        local_think=update.local_think,
        fast_mode=update.fast_mode,
        max_history_len=update.max_history_len,
        condenser_enabled=update.condenser_enabled,
        condense_trigger_extra=update.condense_trigger_extra,
        max_tokens=update.max_tokens,
        tool_max_tokens=update.tool_max_tokens,
        temperature=update.temperature,
        auto_rag=update.auto_rag,
        memory_enabled=update.memory_enabled,
        memory_auto_save=update.memory_auto_save,
        memory_max_items=update.memory_max_items,
        telegram_reply_mode=update.telegram_reply_mode,
    )
    config = agent_instance.get_runtime_config()
    _models_cache["data"] = None
    _models_cache["timestamp"] = 0
    try:
        from backend.database import save_app_settings
        save_app_settings(config)
    except Exception as e:
        logger.warning(f"Failed to persist runtime config: {e}")
        
    # Broadcast updated configuration to all websocket clients
    await manager.broadcast({
        "type": "config_update",
        **config
    })
    return {"status": "success", "config": config}

@app.get("/api/settings")
async def get_settings():
    from backend.database import get_setting
    return {"language": get_setting("language") or "ru"}

@app.post("/api/settings")
async def update_settings(update: SettingsUpdate):
    from backend.database import set_setting, get_setting
    if update.language is not None:
        set_setting("language", update.language)
    await manager.broadcast({"type": "settings_update", "language": get_setting("language") or "ru"})
    return {"status": "success", "language": get_setting("language") or "ru"}

@app.get("/api/logs")
async def get_logs():
    from backend.database import get_decision_logs
    return get_decision_logs(100)


# ── Run journal ────────────────────────────────────────────────────────────
# Durable per-run record: what was submitted, which model answered, what it
# cost, and — crucially — which runs were still in flight when the process died.
# ── Conversation sharing ───────────────────────────────────────────────────
@app.post("/api/share/{chat_id}")
async def create_conversation_share(chat_id: str, request: Request):
    """Publish an immutable, sanitized snapshot of a conversation."""
    from backend import shares
    from backend.auth_http import connection_token
    from backend.auth import role_for_session
    from backend.privacy.gateway import protect_messages, PrivacyCheckFailed
    if await asyncio.to_thread(role_for_session, connection_token(request)) != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    from backend.database import get_session_transcript, get_session_title
    # get_session_transcript, not get_chat_history: it carries timestamps and
    # deliberately leaves out the run metadata that only exists to feed prompts.
    messages = get_session_transcript(chat_id, limit=shares.MAX_MESSAGES)
    messages = [dict(message) for message in messages if message.get("role") in shares.PUBLISHABLE_ROLES]
    title_message = {"role": "user", "content": get_session_title(chat_id) or ""}
    try:
        await protect_messages([*messages, title_message], mark_key="_share_privacy")
    except PrivacyCheckFailed:
        raise HTTPException(status_code=503, detail="Privacy check unavailable; conversation was not published")
    return shares.create_share(chat_id, messages, title_message["content"])


@app.get("/api/share/{chat_id}")
async def get_conversation_share(chat_id: str):
    from backend import shares
    existing = shares.get_share_for_chat(chat_id)
    return existing or {"token": None}


@app.delete("/api/share/{chat_id}")
async def revoke_conversation_share(chat_id: str):
    from backend import shares
    return {"revoked": shares.revoke_share(chat_id)}


@app.get("/share/{token}", include_in_schema=False)
async def read_conversation_share(token: str):
    """Public page. Unauthenticated by design — see backend/shares.py."""
    from backend import shares
    snapshot = shares.load_share(token)
    if snapshot is None:
        # Same answer for "never existed" and "revoked": a distinguishable
        # response would let anyone probe which tokens were once valid.
        return Response(content="Not found", status_code=404, media_type="text/plain")
    return Response(
        content=shares.render_html(snapshot),
        media_type="text/html; charset=utf-8",
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/diagnostics")
async def get_diagnostics():
    """Operational snapshot: what is in flight right now and what the last
    process left behind. Deliberately free of request bodies, arguments and
    credentials — it answers "is something stuck?", not "what was it doing"."""
    from backend import request_diagnostics, run_journal
    from backend.websocket_manager import manager as ws_manager
    return {
        "in_flight_requests": request_diagnostics.in_flight_snapshot(),
        "slow_request_threshold_s": request_diagnostics.SLOW_REQUEST_SECONDS,
        "websocket_connections": len(ws_manager.active_connections),
        "open_runs": run_journal.list_open_runs(),
    }


@app.get("/api/runs")
async def list_runs(chat_id: str = "", limit: int = 50):
    from backend import run_journal
    return {"runs": run_journal.list_runs(chat_id or None, limit)}


@app.get("/api/runs/open")
async def list_open_runs():
    """Runs the journal still considers in flight. After a clean startup this is
    empty; anything here is either live right now or evidence of a crash."""
    from backend import run_journal
    return {"runs": run_journal.list_open_runs()}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    from backend import run_journal
    run = run_journal.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run["events"] = run_journal.get_run_events(run_id)
    # Live replay state, when the in-memory buffer still has this run.
    from backend.websocket_manager import manager as ws_manager
    run["stream"] = ws_manager.run_status(run_id)
    return run

@app.get("/api/metrics")
async def get_metrics():
    from backend.database import db_get_aggregated_metrics
    return db_get_aggregated_metrics()

class DocumentCreate(BaseModel):
    title: str
    content: str

@app.get("/api/documents")
async def get_documents():
    from backend import rag
    return rag.list_documents()

@app.get("/api/documents/search")
async def search_documents(q: str = ""):
    from backend import rag
    if not q.strip():
        return []
    return rag.search_memory(q, limit=5, threshold=0.3)


@app.post("/api/documents")
async def create_document(doc: DocumentCreate):
    from backend import rag
    import uuid
    doc_id = str(uuid.uuid4())
    success = rag.index_document(doc_id, doc.title, doc.content)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to index document in vector store.")
    return {"status": "success", "doc_id": doc_id, "title": doc.title}

@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str):
    from backend import rag
    success = rag.delete_document(doc_id)
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to delete document from vector store.")
    return {"status": "success", "doc_id": doc_id}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    import os
    from backend import media_snapshots
    uploads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
    try:
        # The client-supplied name is never used as a path directly: it is reduced
        # to a safe basename and re-checked after resolution, and whatever it
        # replaces is archived so an earlier chat reference does not silently
        # start showing different bytes.
        stored = media_snapshots.store_upload(uploads_dir, file.filename, file.file)
        file_path = stored["path"]
        logger.info(f"File uploaded successfully: {stored['filename']}")
        
        # Chat-scoped notice: it renders in the dashboard conversation, so it is
        # addressed there rather than fanned out to every open chat.
        await manager.send_chat_event("dashboard", {
            "type": "chat_message",
            "role": "system",
            "content": f"⚙️ [Orchestrator] Dataset: Data file '{file.filename}' loaded."
        })
        
        return {
            "status": "success",
            "filename": stored["filename"],
            "filepath": file_path,
            "replaced_snapshot": stored["replaced_snapshot"],
        }
    except ValueError as e:
        # A name that cannot be contained is a client error, not a server fault.
        logger.warning("Rejected upload filename %r: %s", file.filename, e)
        raise HTTPException(status_code=400, detail="Invalid file name.")
    except Exception as e:
        logger.exception(f"Error uploading file: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload file. Check server logs.")


@app.get("/api/voice/status")
async def voice_status_api():
    from backend.voice import get_voice_status
    return get_voice_status()

@app.get("/api/voice/tts/status")
async def voice_tts_status_api():
    from backend.tts import get_tts_status
    return get_tts_status()

@app.get("/api/browser/live-frame")
async def browser_live_frame_api():
    from backend.tools import get_browser_live_frame
    return await asyncio.to_thread(get_browser_live_frame)


@app.post("/api/voice/synthesize")
async def synthesize_voice_api(payload: VoiceSynthesisRequest):
    import tempfile
    from backend.tts import VoiceSynthesisError, synthesize_speech

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="hermes_tts_", suffix=".wav", delete=False) as tmp:
            temp_path = tmp.name

        result = await asyncio.to_thread(
            synthesize_speech,
            payload.text,
            temp_path,
            payload.voice,
            payload.rate,
        )
        with open(temp_path, "rb") as audio_file:
            audio = audio_file.read()
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={
                "X-Vexa-TTS-Provider": result["provider"],
                "X-Vexa-TTS-Voice": result["voice"],
                "Cache-Control": "no-store",
            },
        )
    except VoiceSynthesisError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.post("/api/voice/transcribe")
async def transcribe_voice_api(file: UploadFile = File(...), language: Optional[str] = None):
    import asyncio
    import os
    import shutil
    import tempfile
    from backend.voice import VoiceTranscriptionError, transcribe_audio_file

    max_mb = float(os.getenv("VOICE_MAX_UPLOAD_MB", "25"))
    voice_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "voice")
    os.makedirs(voice_dir, exist_ok=True)

    original_name = file.filename or "voice.webm"
    _, ext = os.path.splitext(original_name)
    if not ext:
        ext = ".webm"

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="hermes_voice_", suffix=ext, dir=voice_dir, delete=False) as tmp:
            temp_path = tmp.name
            shutil.copyfileobj(file.file, tmp)

        size_bytes = os.path.getsize(temp_path)
        if size_bytes == 0:
            raise HTTPException(status_code=400, detail="Uploaded audio is empty.")
        if size_bytes > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Audio is larger than {max_mb:g} MB.")

        result = await asyncio.to_thread(transcribe_audio_file, temp_path, language)
        text = (result.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="No speech detected in uploaded audio.")

        return {"status": "success", **result, "size_bytes": size_bytes}
    except HTTPException:
        raise
    except VoiceTranscriptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Voice transcription failed")
        raise HTTPException(status_code=500, detail=f"Voice transcription failed: {exc}") from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

@app.get("/api/uploads")
async def list_uploads():
    import os
    uploads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    try:
        files = os.listdir(uploads_dir)
        result = []
        for f in files:
            p = os.path.join(uploads_dir, f)
            # ponytail: skip hidden files like .gitkeep or .DS_Store
            if os.path.isfile(p) and not f.startswith('.'):
                result.append({
                    "name": f,
                    "size_bytes": os.path.getsize(p)
                })
        return result
    except Exception as e:
        logger.error(f"Error listing uploads: {e}")
        return []

@app.get("/api/timers")
async def get_timers_api():
    from backend.scheduler import get_all_timers
    return get_all_timers()

@app.get("/api/reminders")
async def get_reminders_api():
    from backend.scheduler import get_all_reminders
    return get_all_reminders()

@app.delete("/api/reminders/{reminder_id}")
async def cancel_reminder_api(reminder_id: str):
    from backend.scheduler import cancel_recurring_reminder
    ok = cancel_recurring_reminder(reminder_id)
    return {"status": "cancelled" if ok else "not_found", "reminder_id": reminder_id}

@app.post("/api/timers")
async def create_timer_api(task: ScheduledTaskCreate):
    from backend.scheduler import add_timer, add_alarm, add_recurring_reminder
    chat_id = "dashboard"
    try:
        if task.type == "one-shot":
            if task.duration_seconds is None:
                raise ValueError("duration_seconds is required for one-shot timer")
            timer_id = add_timer(task.label, task.duration_seconds, chat_id, task.agent_id, task.prompt)
            return {"status": "success", "id": timer_id}
        elif task.type == "alarm":
            if not task.time_str:
                raise ValueError("time_str is required for alarm timer")
            alarm_id = add_alarm(task.time_str, task.label, chat_id, task.agent_id, task.prompt)
            return {"status": "success", "id": alarm_id}
        elif task.type == "recurring":
            if task.interval_hours is None:
                raise ValueError("interval_hours is required for recurring timer")
            reminder_id = add_recurring_reminder(task.label, task.interval_hours, chat_id, task.agent_id, task.prompt)
            return {"status": "success", "id": reminder_id}
        else:
            return JSONResponse(status_code=400, content={"status": "failed", "error": f"Invalid type: {task.type}"})
    except ValueError as e:
        # Deliberate validation errors raised above — safe to show to the client.
        return JSONResponse(status_code=400, content={"status": "failed", "error": str(e)})
    except Exception as e:
        logger.exception(f"Failed to create timer: {e}")
        return JSONResponse(status_code=500, content={"status": "failed", "error": "Internal error creating timer. Check server logs."})

@app.delete("/api/timers/{timer_id}")
async def cancel_timer_api(timer_id: str):
    from backend.scheduler import cancel_timer_or_alarm, cancel_recurring_reminder
    ok = cancel_timer_or_alarm(timer_id)
    if not ok:
        ok = cancel_recurring_reminder(timer_id)
    return {"status": "cancelled" if ok else "not_found", "timer_id": timer_id}

@app.get("/api/subagents")
async def get_subagents_api():
    from backend.database import get_all_subagents
    return get_all_subagents()

@app.get("/api/agents")
async def get_agents_api():
    from backend.database import get_all_subagents
    return get_all_subagents()

@app.post("/api/subagents")
async def save_subagent_api(subagent: SubagentUpdate):
    from backend.database import save_subagent
    from backend import disciplines as disciplines_module
    # Basic slug validation for ID
    import re
    clean_id = re.sub(r'[^a-zA-Z0-9_-]', '', subagent.id).lower()
    # Tier ceiling: a tier that forbids external providers wins over whatever
    # the form submitted, so a save can't silently smuggle external access
    # back in for an agent assigned to a "local only" tier (defense in depth —
    # agent_provider_access.py enforces the same ceiling again at call time).
    allowed_provider_ids = subagent.allowed_provider_ids
    model_provider = subagent.model_provider
    if subagent.tier_id:
        from backend.agent_tiers import get_tier
        tier = get_tier(subagent.tier_id)
        if tier and not tier["allow_external_provider"]:
            allowed_provider_ids = []
            model_provider = "local"
    save_subagent(
        clean_id,
        subagent.name,
        subagent.system_prompt,
        subagent.model,
        subagent.agent_type,
        subagent.parent_id,
        subagent.skills,
        subagent.x,
        subagent.y,
        subagent.temperature,
        subagent.role,
        subagent.status,
        subagent.is_enabled,
        model_provider,
        subagent.model_type,
        subagent.model_params,
        subagent.budget_usd_limit,
        subagent.budget_period,
        subagent.tier_id,
        allowed_provider_ids,
        subagent.budget_fallback_to_local,
        subagent.project_id,
        [d for d in subagent.disciplines if disciplines_module.is_valid(d)],
    )
    return {"status": "success", "id": clean_id}

@app.post("/api/agents")
async def save_agent_api(subagent: SubagentUpdate):
    return await save_subagent_api(subagent)

@app.get("/api/agents/{agent_id}/events")
async def get_agent_events_api(agent_id: str, limit: int = 50):
    from backend.database import get_agent_events
    return get_agent_events(agent_id, limit=limit)

@app.get("/api/autonomy/summary")
async def get_autonomy_summary_api():
    from backend.autonomy import autonomy_summary
    return await asyncio.to_thread(autonomy_summary)

@app.get("/api/autonomy/capabilities")
async def get_autonomy_capabilities_api():
    from backend.autonomy import doctor_capabilities
    return await asyncio.to_thread(doctor_capabilities)

@app.post("/api/autonomy/capabilities/{capability_id}/propose")
async def propose_autonomy_capability_api(capability_id: str):
    from backend.autonomy import propose_capability
    try:
        return await asyncio.to_thread(propose_capability, capability_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown capability")

@app.post("/api/autonomy/index")
async def index_project_memory_api(request: ProjectIndexRequest):
    from backend.autonomy import index_project
    try:
        return await asyncio.to_thread(index_project, request.root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/autonomy/memory/search")
async def search_project_memory_api(q: str, limit: int = 8):
    from backend.autonomy import search_project_memory
    return await asyncio.to_thread(search_project_memory, q, None, limit)

@app.post("/api/autonomy/memory")
async def save_project_memory_api(request: ProjectMemoryEntryRequest):
    from backend.autonomy import remember_project_entry
    return await asyncio.to_thread(
        remember_project_entry,
        request.kind,
        request.title,
        request.content,
        request.files,
        request.source,
    )

@app.post("/api/autonomy/plans")
async def create_autonomy_plan_api(request: AutonomyPlanRequest):
    from backend.autonomy import build_plan
    if not request.goal.strip():
        raise HTTPException(status_code=400, detail="Goal is required")
    try:
        return await asyncio.to_thread(build_plan, request.goal, request.root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/autonomy/plans")
async def list_autonomy_plans_api(limit: int = 30):
    from backend.autonomy import list_plans
    return await asyncio.to_thread(list_plans, limit)

# ── Durable autonomous dev-runs ───────────────────────────────────────────────

class DevRunCreateRequest(BaseModel):
    goal: str
    iter_budget: int | None = None
    cost_budget: float | None = None
    wall_minutes: int | None = None
    assignee_agent_id: str | None = None
    # False = land in the Kanban "Backlog" column without starting execution;
    # the board promotes it to planned via /start once dragged to "To Do".
    start: bool = True
    # Set = this card continues that run: same product, next revision, cloned
    # working tree and shared published URL (see dev_runs.create_run).
    parent_run_id: str | None = None
    # backend/disciplines.py id, or None to infer it from the goal text.
    discipline: str | None = None
    # Human-facing product name for the board. Empty = fall back to the first
    # line of the goal, so naming stays optional.
    title: str = ""

@app.post("/api/dev-runs")
async def create_dev_run_api(request: DevRunCreateRequest):
    from backend import dev_runs
    if not request.goal.strip():
        raise HTTPException(status_code=400, detail="Goal is required")
    kwargs: dict = {"assignee_agent_id": request.assignee_agent_id, "start": request.start,
                    "parent_run_id": request.parent_run_id, "discipline": request.discipline,
                    "title": request.title}
    if request.iter_budget is not None:
        kwargs["iter_budget"] = request.iter_budget
    if request.cost_budget is not None:
        kwargs["cost_budget"] = request.cost_budget
    if request.wall_minutes is not None:
        kwargs["wall_minutes"] = request.wall_minutes
    try:
        return await asyncio.to_thread(dev_runs.create_run, request.goal, **kwargs)
    except KeyError:
        raise HTTPException(status_code=404, detail="Parent dev-run not found")

class DevRunRenameRequest(BaseModel):
    title: str

@app.post("/api/dev-runs/{run_id}/rename")
async def rename_dev_run_api(run_id: str, request: DevRunRenameRequest):
    """Renames the product — every revision in the chain, not just this card
    (see dev_runs.rename_chain)."""
    from backend import dev_runs
    try:
        renamed = await asyncio.to_thread(dev_runs.rename_chain, run_id, request.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    return {"status": "renamed", "cards_updated": renamed}

@app.get("/api/disciplines")
async def list_disciplines_api(language: str = "ru"):
    """The fixed discipline vocabulary the board's picker and the agent editor
    both render — see backend/disciplines.py for why it is a closed set."""
    from backend import disciplines
    return disciplines.catalog("en" if language == "en" else "ru")

@app.get("/api/dev-runs")
async def list_dev_runs_api(limit: int = 50):
    from backend import dev_runs
    return await asyncio.to_thread(dev_runs.list_runs, limit)

# Declared before /api/dev-runs/{run_id} so "metrics" is not swallowed as a run id.
@app.get("/api/dev-runs/metrics")
async def dev_runs_metrics_api(limit: int = 200):
    """Autonomy metrics: success/failure, autonomous completion, tool error and
    verification rates across recent runs (see dev_runs.metrics)."""
    from backend import dev_runs
    return await asyncio.to_thread(dev_runs.metrics, limit)

@app.get("/api/dev-runs/{run_id}")
async def get_dev_run_api(run_id: str):
    from backend import dev_runs
    run = await asyncio.to_thread(dev_runs.get_run, run_id, True)
    if not run:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    return run

@app.get("/api/dev-runs/{run_id}/lineage")
async def dev_run_lineage_api(run_id: str):
    """Every revision of the product this card belongs to, oldest first, each
    flagged with whether its snapshot survives and which one the stable URL
    currently serves (see dev_runs.lineage)."""
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.lineage, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")

@app.post("/api/dev-runs/{run_id}/promote")
async def promote_dev_run_revision_api(run_id: str):
    """Serves this revision from the product's stable /demo/site-<root>/ URL —
    the rollback (and roll-forward) path. Reversible: snapshots are immutable
    and this only moves a pointer."""
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.promote_revision, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

class DevRunFeedbackCreateRequest(BaseModel):
    comment: str
    page_path: str = ""
    selector: str = ""
    element_text: str = ""
    viewport: str = ""

@app.post("/api/dev-runs/{run_id}/feedback")
async def add_dev_run_feedback_api(run_id: str, request: DevRunFeedbackCreateRequest):
    """Records one click-to-comment remark left on a published demo. The
    overlay tools.py injects into every published page posts here, same-origin
    through nginx — see backend/dev_runs.add_feedback."""
    from backend import dev_runs
    if not request.comment.strip():
        raise HTTPException(status_code=400, detail="Comment is required")
    try:
        return await asyncio.to_thread(
            dev_runs.add_feedback, run_id, comment=request.comment,
            page_path=request.page_path, selector=request.selector,
            element_text=request.element_text, viewport=request.viewport,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/dev-runs/{run_id}/feedback")
async def list_dev_run_feedback_api(run_id: str, status: str | None = "open"):
    """Every comment on this product's whole chain, not just this revision —
    see dev_runs.list_feedback."""
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.list_feedback, run_id, status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")

@app.post("/api/dev-runs/feedback/{feedback_id}/dismiss")
async def dismiss_dev_run_feedback_api(feedback_id: str):
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.dismiss_feedback, feedback_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Feedback not found or already resolved")

class DevRunFeedbackApplyRequest(BaseModel):
    assignee_agent_id: str | None = None
    start: bool = True

@app.post("/api/dev-runs/{run_id}/feedback/apply")
async def apply_dev_run_feedback_api(run_id: str, request: DevRunFeedbackApplyRequest):
    """Folds every open comment on this product into one continuation card —
    see dev_runs.consume_feedback."""
    from backend import dev_runs
    try:
        return await asyncio.to_thread(
            dev_runs.consume_feedback, run_id,
            assignee_agent_id=request.assignee_agent_id, start=request.start,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@app.post("/api/dev-runs/{run_id}/pause")
async def pause_dev_run_api(run_id: str):
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.pause_run, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")

@app.post("/api/dev-runs/{run_id}/resume")
async def resume_dev_run_api(run_id: str):
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.resume_run, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@app.post("/api/dev-runs/{run_id}/cancel")
async def cancel_dev_run_api(run_id: str):
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.cancel_run, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")

@app.post("/api/dev-runs/{run_id}/start")
async def start_dev_run_api(run_id: str):
    """Promotes a Kanban card from Backlog to To Do (planned) — the worker
    then picks it up like any other planned dev-run."""
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.start_run, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

class DevRunAssignRequest(BaseModel):
    assignee_agent_id: str | None = None

@app.post("/api/dev-runs/{run_id}/assign")
async def assign_dev_run_api(run_id: str, request: DevRunAssignRequest):
    """Changes who a Kanban card is attributed to. Execution always runs
    under the owner principal (see tool_permissions.py) — this only changes
    who the card displays as responsible and who auto-assign counts as busy."""
    from backend import dev_runs
    try:
        return await asyncio.to_thread(dev_runs.reassign_run, run_id, request.assignee_agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Dev-run not found")

@app.delete("/api/dev-runs/{run_id}")
async def delete_dev_run_api(run_id: str):
    from backend import dev_runs, dev_sandbox
    run = await asyncio.to_thread(dev_runs.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Dev-run not found")
    if run["status"] in dev_runs.ACTIVE_STATUSES + ("paused", "awaiting_approval"):
        try:
            await dev_sandbox.release_sandbox(run_id)
        except Exception:
            logging.getLogger("hermes.main").warning("release_sandbox failed for %s during delete", run_id, exc_info=True)
    await asyncio.to_thread(dev_runs.delete_run, run_id)
    return {"status": "deleted"}

@app.get("/api/dev-runs/{run_id}/download")
async def download_dev_run_demo_api(run_id: str):
    """Zips the run's published /demo/<run_id>/ output for download. 404 if
    the run never published a demo (see tools.dev_publish_demo)."""
    import io
    import zipfile
    from backend import dev_sandbox

    demo_dir = dev_sandbox.PREVIEWS_ROOT / run_id
    if not demo_dir.is_dir():
        raise HTTPException(status_code=404, detail="No published demo for this run")

    def build_zip() -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in demo_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(demo_dir))
        return buffer.getvalue()

    zip_bytes = await asyncio.to_thread(build_zip)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-demo.zip"'},
    )

@app.get("/api/control-plane/summary")
async def get_control_plane_summary_api(limit: int = 100):
    from backend.control_plane import get_summary
    return get_summary(limit=limit)

@app.get("/api/control-plane/tasks")
async def get_control_plane_tasks_api(limit: int = 100, status: Optional[str] = None):
    from backend.control_plane import list_tasks
    return list_tasks(limit=limit, status=status)

@app.get("/api/control-plane/events")
async def get_control_plane_events_api(limit: int = 100):
    from backend.control_plane import list_events
    return list_events(limit=limit)

@app.post("/api/control-plane/tasks/{task_id}/approve")
async def approve_control_plane_task_api(task_id: str, request: Request):
    from backend.control_plane import approve_task, get_task
    from backend.approval_dispatch import execute_if_ready
    from backend.auth_http import require_recent_auth
    pending = await asyncio.to_thread(get_task, task_id)
    if pending and pending["risk_class"] == "R4":
        await asyncio.to_thread(require_recent_auth, request)
    try:
        task = approve_task(task_id, actor="owner:web")
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    execution = await execute_if_ready(task)
    if execution is not None:
        task = get_task(task_id) or task
    return {"status": task["status"], "task": task, "execution": execution}

@app.post("/api/control-plane/tasks/{task_id}/reject")
async def reject_control_plane_task_api(task_id: str, action: ControlPlaneAction):
    from backend.control_plane import reject_task
    try:
        task = reject_task(task_id, action.reason or "Rejected by owner", actor="owner:web")
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": task["status"], "task": task}

@app.post("/api/control-plane/kill")
async def kill_control_plane_api(action: ControlPlaneAction):
    from backend.control_plane import set_kill_switch
    state = set_kill_switch(True, action.reason or "Emergency stop requested by owner", actor="owner:web")
    cancelled_runs = 0
    for run in list(ACTIVE_CHAT_RUNS.values()):
        if not run.done():
            run.cancel()
            cancelled_runs += 1
    return {"status": "stopped", "state": state, "cancelled_runs": cancelled_runs}

@app.post("/api/control-plane/resume")
async def resume_control_plane_api(action: ControlPlaneAction):
    from backend.control_plane import set_kill_switch
    state = set_kill_switch(False, action.reason or "Resumed by owner", actor="owner:web")
    return {"status": "running", "state": state}


class ApprovalRuleRequest(BaseModel):
    tool_name: str = Field(..., max_length=120)
    feature_key: str = Field(..., max_length=40)
    feature_value: str = Field(..., max_length=200)


@app.get("/api/control-plane/decisions")
async def control_plane_decisions_api(limit: int = 200):
    from backend.control_plane import decision_history
    return {"decisions": await asyncio.to_thread(decision_history, limit)}


@app.get("/api/control-plane/approval-rules")
async def control_plane_rules_api():
    from backend.control_plane import list_approval_rules, policy_suggestions
    rules, suggestions = await asyncio.gather(asyncio.to_thread(list_approval_rules),
                                              asyncio.to_thread(policy_suggestions))
    return {"rules": rules, "suggestions": suggestions}


@app.post("/api/control-plane/approval-rules")
async def create_control_plane_rule_api(rule: ApprovalRuleRequest, request: Request):
    # A rule stops future approvals, so it needs a recent sign-in like a mode change.
    from backend.auth_http import require_recent_auth
    from backend.control_plane import create_approval_rule
    await asyncio.to_thread(require_recent_auth, request)
    try:
        return await asyncio.to_thread(create_approval_rule, rule.tool_name, rule.feature_key, rule.feature_value,
                                       "owner:web")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.delete("/api/control-plane/approval-rules/{rule_id}")
async def delete_control_plane_rule_api(rule_id: int):
    from backend.control_plane import delete_approval_rule
    try:
        await asyncio.to_thread(delete_approval_rule, rule_id, "owner:web")
    except KeyError:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "deleted"}

@app.post("/api/subagents/positions")
async def update_subagent_positions_api(update: SubagentPositionsUpdate):
    from backend.database import DB_PATH
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        for pos in update.positions:
            cursor.execute("UPDATE subagents SET x = ?, y = ? WHERE id = ?", (pos.x, pos.y, pos.id))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        logger.exception(f"Error updating positions: {e}")
        return {"status": "error", "message": "Failed to update positions. Check server logs."}

@app.delete("/api/subagents/{subagent_id}")
async def delete_subagent_api(subagent_id: str):
    from backend.database import delete_subagent
    ok = delete_subagent(subagent_id)
    return {"status": "success" if ok else "failed"}

@app.get("/api/skills")
async def get_skills_api():
    """Returns all available built-in skill names and which tools each unlocks."""
    skill_to_tools = {
        "web_search":       ["web_search", "get_current_time_israel", "get_weather", "get_rss_digest"],
        "market_monitor":   ["get_market_prices", "add_price_alert"],
        "obsidian_rag":     ["search_obsidian", "read_obsidian_note", "create_obsidian_note", "sync_obsidian_vault"],
        "todoist_sync":     ["get_todoist_tasks", "add_todoist_task", "delete_todoist_task"],
        "google_calendar":  ["get_calendar_events", "add_calendar_event"],
        "timers_alarms":    ["set_timer", "set_alarm", "cancel_timer_or_alarm"],
        "shell_execution":  ["get_system_stats", "execute_command"],
        "python_sandbox":   ["execute_command"],
        "bcm":              ["bcm tools (crypto trading)"],
        "mcp_all":          ["all connected MCP server tools"],
    }
    # Append any live MCP servers as selectable skills
    from backend.mcp_client import mcp_clients
    for name in mcp_clients:
        if name not in skill_to_tools:
            skill_to_tools[name] = [f"MCP: {name}"]
    return skill_to_tools

_models_cache = {"data": None, "timestamp": 0}

@app.get("/api/models")
async def get_models_api():
    """Returns all available models from OpenRouter (or user provider) using user keys."""
    import time, os, httpx
    now = time.time()
    if _models_cache["data"] and (now - _models_cache["timestamp"] < 3600):
        return _models_cache["data"]

    api_base = agent_instance.api_base
    api_key = os.getenv("OPENROUTER_API_KEY", "")

    from backend.llamacpp_client import LlamaCppClient, is_llamacpp_provider
    if is_llamacpp_provider(api_base, agent_instance.provider):
        try:
            raw_models = await LlamaCppClient(api_base).list_models()
            return [{
                "id": item["id"],
                "name": item["id"],
                "provider": "llamacpp",
                "size": (item.get("meta") or {}).get("size"),
                "details": item.get("meta") or {},
            } for item in raw_models]
        except Exception as exc:
            logger.warning("Failed to list llama.cpp models: %s", exc)
            return []

    url = f"{api_base}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                result: list = []

                # ── Format 1: OpenAI / OpenRouter ──────────────────────────
                if isinstance(data, dict) and "data" in data:
                    raw_models = data["data"]
                    for m in raw_models:
                        m_id = m.get("id") or m.get("name")
                        m_name = m.get("name") or m_id
                        if m_id:
                            result.append({"id": m_id, "name": m_name})

                # ── Format 2: {"models": [...]} listings ────────────────
                elif isinstance(data, dict) and "models" in data:
                    raw_models = data["models"]
                    for m in raw_models:
                        m_name = m.get("name") or m.get("model")
                        if m_name:
                            result.append({"id": m_name, "name": m_name})

                # ── Format 3: flat list ────────────────────────────────────
                elif isinstance(data, list):
                    for m in data:
                        m_id = m.get("id") or m.get("name") or str(m)
                        m_name = m.get("name") or m_id
                        if m_id:
                            result.append({"id": m_id, "name": m_name})

                if result:
                    # Sort local models alphabetically; no "recommended" bias.
                    result.sort(key=lambda x: x["id"].lower())
                    _models_cache["data"] = result
                    _models_cache["timestamp"] = now
                    return result
    except Exception as e:
        logger.error(f"Error fetching models: {e}")

    # ── Fallback: local provider vs OpenRouter ─────────────────────────────
    is_local = any(m in (api_base or "").lower()
                   for m in ("localhost", "127.0.0.1", "0.0.0.0", "hermes-llama",
                             "host.docker.internal", "lmstudio", "vllm"))
    if is_local:
        # Return the currently configured model as the only available option.
        current_model = os.getenv("LLM_MODEL", "qwen3.8-27b")
        return [{"id": current_model, "name": f"Local: {current_model}"}]

    # OpenRouter fallback list
    return [
        {"id": "google/gemini-2.5-flash", "name": "Google: Gemini 2.5 Flash (default)"},
        {"id": "google/gemini-2.5-pro", "name": "Google: Gemini 2.5 Pro"},
        {"id": "anthropic/claude-sonnet-4-5", "name": "Anthropic: Claude Sonnet 4.5"},
        {"id": "anthropic/claude-opus-4", "name": "Anthropic: Claude Opus 4"},
        {"id": "openai/gpt-4o", "name": "OpenAI: GPT-4o"},
        {"id": "openai/gpt-4o-mini", "name": "OpenAI: GPT-4o-Mini"},
        {"id": "deepseek/deepseek-r1", "name": "DeepSeek: R1"},
        {"id": "deepseek/deepseek-v3-0324", "name": "DeepSeek: V3"},
        {"id": "meta-llama/llama-3.3-70b-instruct", "name": "Meta Llama 3.3 70B"},
    ]


@app.get("/api/llm/status")
async def get_local_llm_status():
    """The local llama.cpp server as the dashboard shows it: health, the model it
    serves, context size, slots and build. Always 200 — an unreachable or still
    loading server is part of the payload, not an HTTP error."""
    from backend.llamacpp_client import LlamaCppClient

    status = await LlamaCppClient(agent_instance.local_base_url, timeout=10).status()
    status["active_model"] = agent_instance.model
    status["local_num_ctx"] = agent_instance.local_num_ctx
    return status

# ─── MCP CONFIG API ───────────────────────────────────────────────────────────

class MCPServerConfig(BaseModel):
    name: str
    command: str
    args: list = Field(default_factory=list)
    env: dict = Field(default_factory=dict)

class ProviderBindingRequest(BaseModel):
    name: str
    provider_type: str = "openai_compatible"
    api_base: str
    api_key: str
    # Optional billing-accuracy override, USD per 1M tokens — leave both null
    # to keep using cost.py's model-name-substring pricing guess.
    cost_per_1m_input: Optional[float] = None
    cost_per_1m_output: Optional[float] = None


class ProviderPricingRequest(BaseModel):
    cost_per_1m_input: Optional[float] = None
    cost_per_1m_output: Optional[float] = None

@app.get("/api/mcp/servers")
async def get_mcp_servers():
    """Returns current MCP server configs and live connection status."""
    import json, os
    from backend.mcp_client import mcp_clients
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mcp_config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)
    servers = config.get("mcpServers", {})
    result = []
    for name, cfg in servers.items():
        result.append({
            "name": name,
            "command": cfg.get("command", ""),
            "args": cfg.get("args", []),
            "env": {k: v for k, v in cfg.get("env", {}).items() if "key" not in k.lower() and "secret" not in k.lower() and "token" not in k.lower()},
            "connected": name in mcp_clients,
            "tools_count": len(mcp_clients[name].tools) if name in mcp_clients else 0,
        })
    from backend import omniroute_mcp
    omni = omniroute_mcp.status()
    result.append({
        "name": omniroute_mcp.SERVER_NAME,
        "command": "",
        "args": [],
        "env": {},
        "url": omni["url"],
        "transport": "streamable-http",
        "builtin": True,
        "connected": omni["connected"],
        "tools_count": len(omni["tools"]),
        "error": omni["error"],
    })
    return result

@app.post("/api/mcp/servers")
async def add_mcp_server(server: MCPServerConfig):
    """Validate an MCP config and create a double-confirmation connection proposal."""
    from backend.mcp_governance import create_connection_proposal
    try:
        proposal = await asyncio.to_thread(
            create_connection_proposal,
            server.name,
            {"command": server.command, "args": server.args, "env": server.env},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "proposal_id": proposal["id"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R4",
        "message": "Connection validated. Two explicit owner approvals are required before activation.",
    }

@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    """Removes an MCP server from config and disconnects it."""
    import json, os
    from backend.mcp_client import mcp_clients, unregister_client_tools
    from backend import omniroute_mcp
    if name == omniroute_mcp.SERVER_NAME:
        raise HTTPException(status_code=400, detail="OmniRoute MCP is built in — disable it with OMNIROUTE_MCP_ENABLED=false.")
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mcp_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)
        config.get("mcpServers", {}).pop(name, None)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
    if name in mcp_clients:
        await mcp_clients[name].shutdown()
        del mcp_clients[name]
    unregister_client_tools(name)
    return {"status": "success", "name": name}

@app.get("/api/providers")
async def list_providers_api():
    """Lists external provider bindings agents can be assigned to (never returns secrets)."""
    from backend.provider_governance import list_bindings
    return await asyncio.to_thread(list_bindings)

@app.post("/api/providers")
async def add_provider_api(binding: ProviderBindingRequest):
    """Validate a provider binding and create a single-confirmation (R3) approval proposal."""
    from backend.provider_governance import create_binding_proposal
    try:
        proposal = await asyncio.to_thread(
            create_binding_proposal,
            binding.name,
            binding.provider_type,
            binding.api_base,
            binding.api_key,
            binding.cost_per_1m_input,
            binding.cost_per_1m_output,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Binding validated. One owner approval is required before it can be used.",
    }

@app.delete("/api/providers/{binding_id}")
async def delete_provider_api(binding_id: str):
    """Permanently removes a provider binding and its stored API key."""
    from backend.provider_governance import delete_binding
    try:
        await asyncio.to_thread(delete_binding, binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}

@app.get("/api/providers/{binding_id}/models")
async def list_provider_models_api(binding_id: str):
    """What one binding says it can serve — `GET {api_base}/models`, the
    OpenAI-compatible contract OmniRoute, OpenRouter, DeepSeek and vLLM all
    answer. Generic on purpose: GET /api/router/models only ever describes the
    OmniRoute sidecar, so anything that pins a *binding* to a model (a channel, an
    agent, a Content Factory node) needs to ask that binding, not the sidecar.

    Never an error for an endpoint that simply does not list: a caller is always
    a picker, and "could not list" has to degrade to typing the name by hand."""
    from backend.provider_governance import resolve_binding_credentials

    resolved = await asyncio.to_thread(resolve_binding_credentials, binding_id)
    if not resolved:
        return {"models": [], "error": "Provider is not active"}
    api_base, api_key = resolved
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{api_base.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
    except httpx.HTTPError as exc:
        logger.warning("Model list for binding %s failed: %s", binding_id, exc)
        return {"models": [], "error": "Endpoint unreachable"}
    if resp.status_code != 200:
        detail = "invalid API key" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}"
        return {"models": [], "error": f"GET /models failed: {detail}"}
    try:
        entries = resp.json().get("data") or []
    except ValueError:
        return {"models": [], "error": "Endpoint returned a non-JSON model list"}
    models = sorted({str(e["id"]) for e in entries if isinstance(e, dict) and e.get("id")})
    return {"models": models, "error": None}


@app.put("/api/providers/{binding_id}/pricing")
async def update_provider_pricing_api(binding_id: str, payload: ProviderPricingRequest):
    """Sets/clears a provider's billing-accuracy pricing override — plain R0
    edit, no new secret or network surface (see provider_governance.update_binding_pricing)."""
    from backend.provider_governance import update_binding_pricing
    try:
        return await asyncio.to_thread(
            update_binding_pricing, binding_id, payload.cost_per_1m_input, payload.cost_per_1m_output,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/router/stats")
async def router_stats_api():
    """Reachability/catalog check for the optional OmniRoute sidecar (Admin -> AI Router).

    Deliberately does NOT try to read quota or usage numbers: OmniRoute's own
    docs advertise `GET /api/quota` and `GET /api/usage` as Bearer-key-
    authenticated, but against an actual running instance both 404 — they
    don't exist at that path/version. `/api/providers` and `/api/combos` do
    exist, but only accept the dashboard's own session cookie, not an API
    key, so a headless backend can't call them without a full login flow
    against an undocumented, versionless internal API — not worth building
    against. `GET /v1/models` (Bearer-key auth) is the one endpoint that is
    both documented and confirmed working, so that's what this checks:
    reachability, key validity, and the model catalog size. Real quota/usage/
    combo detail stays a dashboard-only concern — see `dashboard_url`.

    Best-effort and read-only: this never touches agent LLM routing (that's a
    normal provider_bindings entry, same as any other openai_compatible
    provider). Degrades gracefully — no container running or no key
    configured just report `reachable`/`available: false` instead of
    raising, since the sidecar is optional and off by default.
    """
    from backend import database as db

    router_api_key = db.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")
    router_api_base = os.getenv("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/")
    dashboard_url = os.getenv("ROUTER_PUBLIC_BASE_URL", "http://localhost:20128")

    if not router_api_key:
        return {
            "available": False,
            "reachable": None,
            "model_count": None,
            "provider_count": None,
            "sample_models": [],
            "dashboard_url": dashboard_url,
            "error": "ROUTER_API_KEY not configured (Settings -> API Keys -> AI Router).",
        }

    headers = {"Authorization": f"Bearer {router_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{router_api_base}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("AI Router reachability check failed: %s", exc)
        return {
            "available": True,
            "reachable": False,
            "model_count": None,
            "provider_count": None,
            "sample_models": [],
            "dashboard_url": dashboard_url,
            "error": "OmniRoute sidecar not reachable — is the 'omniroute' container running?",
        }

    if resp.status_code != 200:
        detail = "invalid ROUTER_API_KEY" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}"
        return {
            "available": True,
            "reachable": False,
            "model_count": None,
            "provider_count": None,
            "sample_models": [],
            "dashboard_url": dashboard_url,
            "error": f"GET /v1/models failed: {detail}",
        }

    try:
        models = resp.json().get("data") or []
    except ValueError:
        models = []
    owners = {m.get("owned_by") for m in models if isinstance(m, dict) and m.get("owned_by")}

    from backend.provider_governance import list_bindings
    bindings = await asyncio.to_thread(list_bindings)

    return {
        "available": True,
        "reachable": True,
        "model_count": len(models),
        "provider_count": len(owners),
        "sample_models": [m.get("id") for m in models[:8] if isinstance(m, dict) and m.get("id")],
        "providers": bindings,
        "dashboard_url": dashboard_url,
        "error": None,
    }


@app.get("/api/router/models")
async def router_models_api():
    """Full OmniRoute model catalog for the AI Router tab's "Добавить тир" form —
    GET /api/router/stats only returns an 8-item sample_models preview for the
    diagnostics panel, this is the complete list the tier form's model picker
    needs. Catalog entries are namespaced by provider (e.g. 'kimi/kimi-k3',
    'ds/deepseek-chat') — a tier's model_override must match one of these
    verbatim, which a free-text field made easy to get wrong (a user typed
    'deepseek-chat' instead of 'ds/deepseek-chat' and the tier would have
    silently 404'd against OmniRoute on first use)."""
    from backend import database as db

    router_api_key = db.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")
    router_api_base = os.getenv("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/")
    if not router_api_key:
        return {"available": False, "models": [], "error": "ROUTER_API_KEY not configured (Settings -> API Keys -> AI Router)."}

    headers = {"Authorization": f"Bearer {router_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{router_api_base}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("AI Router model catalog fetch failed: %s", exc)
        return {"available": True, "models": [], "error": "OmniRoute sidecar not reachable — is the 'omniroute' container running?"}

    if resp.status_code != 200:
        detail = "invalid ROUTER_API_KEY" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}"
        return {"available": True, "models": [], "error": f"GET /v1/models failed: {detail}"}

    try:
        raw = resp.json().get("data") or []
    except ValueError:
        raw = []
    seen: set[str] = set()
    models = []
    for entry in raw:
        model_id = isinstance(entry, dict) and entry.get("id")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        # capabilities drive the agent form's per-model settings panel (context
        # window, output ceiling, whether reasoning can be switched off).
        models.append({
            "id": model_id,
            "owned_by": entry.get("owned_by") or "",
            "capabilities": entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {},
        })
    models.sort(key=lambda m: m["id"])
    return {"available": True, "models": models, "error": None}


@app.get("/api/chat/models")
async def chat_models_api(refresh: bool = False):
    """The chat model picker's catalogue: the model the local llama.cpp server is
    serving plus the external OmniRoute catalogue, in one payload.

    /api/models answers "what can the *configured* provider serve" and
    /api/router/models answers only the OmniRoute half — the picker needs both
    at once, each labelled with its source, because that source is what the
    chat frame sends back and what backend/chat_models.py resolves an endpoint
    from. Neither half can fail the request: an unreachable source comes back
    empty with an `*_error` string the UI shows in place of its group."""
    from backend import chat_models

    return await chat_models.list_chat_models(
        local_base_url=agent_instance.local_base_url,
        default_model=agent_instance.model,
        default_provider=agent_instance.provider,
        force_refresh=refresh,
    )


@app.get("/api/router/mcp")
async def router_mcp_status_api():
    """OmniRoute's built-in MCP server as seen by Hermes (backend/omniroute_mcp.py):
    connected or not, which curated tools agents got, and why not if it failed."""
    from backend import omniroute_mcp

    return omniroute_mcp.status()


@app.post("/api/router/mcp/reconnect")
async def router_mcp_reconnect_api():
    """Re-run the OmniRoute MCP handshake — after saving a new key or enabling
    MCP in OmniRoute's own dashboard."""
    from backend import omniroute_mcp

    return await omniroute_mcp.connect()


@app.post("/api/router/mcp/enable")
async def router_mcp_enable_api():
    """Switch OmniRoute's MCP server on (streamable-http transport) through the
    governed dashboard session, then connect to it."""
    from backend import omniroute_mcp

    return await omniroute_mcp.enable_on_router()


class RouterSessionCredentialRequest(BaseModel):
    password: str


@app.get("/api/router/session-status")
async def router_session_status_api():
    """Whether an OmniRoute dashboard session is configured/pending/active —
    powers the combos/connections panels' setup state in the AI Router tab."""
    from backend.router_session import is_configured, has_pending_proposal

    return {
        "configured": is_configured(),
        "pending_task_id": has_pending_proposal(),
    }


@app.post("/api/router/session-credential")
async def router_session_credential_propose_api(body: RouterSessionCredentialRequest):
    """Stage the OmniRoute dashboard password behind one Telegram/dashboard
    R3 approval — see backend/router_session.py for why this needs the same
    governance as an external provider binding, not less."""
    from backend.router_session import propose_router_password

    try:
        proposal = await asyncio.to_thread(propose_router_password, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal


@app.delete("/api/router/session-credential")
async def router_session_credential_revoke_api():
    from backend.router_session import revoke_router_password

    await asyncio.to_thread(revoke_router_password)
    return {"status": "success"}


@app.get("/api/router/combos")
async def router_combos_api():
    """Proxies OmniRoute's own GET /api/combos through a backend-held dashboard
    session — this data has no Bearer-key API, only the session-cookie one.
    Degrades to {"available": false} rather than raising when no session is
    configured or OmniRoute can't be reached, since this whole panel is optional.
    """
    from backend.router_session import session_request, is_configured

    if not is_configured():
        return {"available": False, "combos": None, "error": "OmniRoute dashboard session not configured."}

    resp = await session_request("GET", "/api/combos")
    if resp is None:
        return {"available": True, "combos": None, "error": "OmniRoute session unavailable — check the password or container status."}
    if resp.status_code != 200:
        return {"available": True, "combos": None, "error": f"OmniRoute returned HTTP {resp.status_code} for /api/combos."}
    try:
        data = resp.json()
    except ValueError:
        return {"available": True, "combos": None, "error": "OmniRoute returned invalid JSON for /api/combos."}
    return {"available": True, "combos": data.get("combos") if isinstance(data, dict) else data, "error": None}


@app.get("/api/router/connections")
async def router_connections_api():
    """Proxies OmniRoute's own GET /api/providers (its connected upstream AI
    accounts — Claude subscription, GLM key, etc.) — deliberately named
    differently from this app's own GET /api/providers (agent provider
    bindings), which is an unrelated concept that happens to share a name
    with OmniRoute's endpoint."""
    from backend.router_session import session_request, is_configured

    if not is_configured():
        return {"available": False, "connections": None, "error": "OmniRoute dashboard session not configured."}

    resp = await session_request("GET", "/api/providers")
    if resp is None:
        return {"available": True, "connections": None, "error": "OmniRoute session unavailable — check the password or container status."}
    if resp.status_code != 200:
        return {"available": True, "connections": None, "error": f"OmniRoute returned HTTP {resp.status_code} for /api/providers."}
    try:
        data = resp.json()
    except ValueError:
        return {"available": True, "connections": None, "error": "OmniRoute returned invalid JSON for /api/providers."}
    return {"available": True, "connections": data.get("connections") if isinstance(data, dict) else data, "error": None}


@app.get("/api/router/catalog")
async def router_catalog_api():
    """Everything OmniRoute *could* serve, not just what it can serve today.

    GET /api/router/models (the Bearer-key /v1/models) only lists models a
    connected, active upstream account can answer with — 23 of them on this
    deployment. OmniRoute's session-cookie /api/models is the whole catalogue,
    including providers nobody has connected yet, which is the list to look at
    when the question is "what would connecting this account give me". Grouped
    by provider here because that is the unit an account is connected as.
    """
    from backend.router_session import session_request, is_configured

    if not await asyncio.to_thread(is_configured):
        return {"available": False, "providers": [], "error": "Dashboard password is not configured"}
    resp = await session_request("GET", "/api/models")
    if resp is None or resp.status_code != 200:
        detail = "session unavailable" if resp is None else f"HTTP {resp.status_code}"
        return {"available": False, "providers": [], "error": f"GET /api/models failed: {detail}"}
    try:
        entries = resp.json().get("models") or []
    except ValueError:
        return {"available": False, "providers": [], "error": "OmniRoute returned a non-JSON catalogue"}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "")
        if not provider:
            continue
        grouped.setdefault(provider, []).append({
            "id": entry.get("fullModel") or entry.get("routedModel") or entry.get("model"),
            "name": entry.get("name") or entry.get("model"),
            "capabilities": entry.get("caps") if isinstance(entry.get("caps"), dict) else {},
        })
    providers = [
        {"provider": provider, "models": models}
        for provider, models in sorted(grouped.items(), key=lambda item: item[0])
    ]
    return {"available": True, "providers": providers, "error": None}


@app.post("/api/router/bind-self")
async def router_bind_self_api():
    """Convenience: create a governed provider_binding pointing at the OmniRoute
    sidecar itself, reusing the already-configured ROUTER_API_KEY instead of
    making the user copy/paste it into the generic 'Add provider' form. This
    is the only way accounts connected inside OmniRoute's own dashboard (see
    /api/router/connections) become usable as a fallback-chain tier — a tier
    always routes through one provider_binding + a model_override, never
    directly at an OmniRoute-native connection. Still goes through the normal
    single-Telegram-approval flow, same as any other binding."""
    from backend import database as db
    from backend.provider_governance import create_binding_proposal, list_bindings

    router_api_key = db.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")
    if not router_api_key:
        raise HTTPException(status_code=400, detail="ROUTER_API_KEY not configured (Settings -> API Keys -> AI Router).")

    existing = await asyncio.to_thread(list_bindings)
    if any("omniroute" in (b.get("api_base") or "") for b in existing):
        raise HTTPException(status_code=409, detail="A provider binding for OmniRoute already exists.")

    router_api_base = os.getenv("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/") + "/v1"
    try:
        proposal = await asyncio.to_thread(
            create_binding_proposal, "OmniRoute", "openai_compatible", router_api_base, router_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Binding validated. One owner approval is required before it can be used.",
    }


class RouterTierRequest(BaseModel):
    label: str
    tier_rank: int
    kind: str = "binding"
    provider_binding_id: Optional[str] = None
    model_override: str = ""
    quota_limit: Optional[int] = None
    quota_window_hours: float = 24
    is_active: bool = True


@app.get("/api/router/tiers")
async def list_router_tiers_api():
    """The native fallback chain the local model escalates through — this is
    Hermes's own concept, distinct from (and not fed by) OmniRoute's own combos,
    which stay session-only-visible in the panel above. Each tier is annotated
    with its live quota usage from backend/router_usage.py so the AI Router tab
    never needs to leave this page to show a real number."""
    from backend import router_tiers, router_usage

    tiers = await asyncio.to_thread(router_tiers.list_tiers)
    for tier in tiers:
        tier["quota"] = router_usage.tier_quota_status(tier["id"], tier.get("quota_limit"), tier["quota_window_hours"])
    return tiers


@app.post("/api/router/tiers")
async def create_router_tier_api(payload: RouterTierRequest):
    from backend import router_tiers
    try:
        return await asyncio.to_thread(
            router_tiers.create_tier,
            payload.label, payload.tier_rank, payload.kind, payload.provider_binding_id,
            payload.model_override, payload.quota_limit, payload.quota_window_hours, payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/router/tiers/{tier_id}")
async def update_router_tier_api(tier_id: str, payload: RouterTierRequest):
    from backend import router_tiers
    try:
        return await asyncio.to_thread(
            router_tiers.update_tier, tier_id,
            label=payload.label, tier_rank=payload.tier_rank, kind=payload.kind,
            provider_binding_id=payload.provider_binding_id, model_override=payload.model_override,
            quota_limit=payload.quota_limit, quota_window_hours=payload.quota_window_hours,
            is_active=payload.is_active,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Tier not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/router/tiers/{tier_id}")
async def delete_router_tier_api(tier_id: str):
    from backend import router_tiers
    await asyncio.to_thread(router_tiers.delete_tier, tier_id)
    return {"status": "success", "id": tier_id}


@app.get("/api/router/overview")
async def router_overview_api():
    """Stat-card data for the AI Router tab's header row — active chain label,
    24h request volume, an honest 'economy' figure (share of the last 24h's
    traffic that did NOT need the top/most-expensive tier), and provider
    binding health. All computed from Hermes's own usage log, never from
    OmniRoute (which has no quota/usage API at all — see router_stats_api)."""
    from backend import router_usage

    return await asyncio.to_thread(router_usage.overview_stats)


# ── Agent budgets, tiers, and per-agent messenger bindings ──────────────────

class AgentBudgetRequest(BaseModel):
    budget_usd_limit: Optional[float] = None
    budget_period: str = "monthly"

class AgentTierRequest(BaseModel):
    name: str
    description: str = ""
    budget_usd_limit_default: Optional[float] = None
    budget_period_default: str = "monthly"
    allow_external_provider: bool = True
    allow_messenger: bool = True
    is_active: bool = True

class ProjectRequest(BaseModel):
    name: str
    description: str = ""
    is_active: bool = True
    parent_id: Optional[str] = None

class AgentTelegramBindingRequest(BaseModel):
    bot_token: str
    allowed_chat_ids: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    response_mode: str = "draft"

class AgentMatrixBindingRequest(BaseModel):
    homeserver_url: str
    user_id: str = ""
    password: str = ""
    access_token: str = ""
    refresh_token: str = ""
    device_flow_id: str = ""
    allowed_room_ids: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    response_mode: str = "draft"

class MessengerBindingUpdateRequest(BaseModel):
    system_prompt: Optional[str] = None
    response_mode: Optional[str] = None
    access_mode: Optional[str] = None
    default_plan_id: Optional[str] = None
    welcome_message: Optional[str] = None
    allowed_chat_ids: Optional[List[str]] = None
    auto_reply_disclosure: Optional[str] = None
    human_takeover_pause_minutes: Optional[int] = None
    escalation_enabled: Optional[bool] = None
    handling_menu_enabled: Optional[bool] = None
    # Per-channel model. '' on either clears the override and hands the channel
    # back to its agent's own provider/model.
    model: Optional[str] = None
    model_provider: Optional[str] = None

class MessengerBindingReconnectRequest(BaseModel):
    """One shape covering all 5 platforms' credential fields — the endpoint reads
    only the ones the binding's own platform needs. Lets a dead/rotated
    credential be swapped in place instead of deleting and recreating the whole
    binding (which would lose its prompt override, access mode and whitelist)."""
    bot_token: Optional[str] = None
    app_token: Optional[str] = None
    homeserver_url: Optional[str] = None
    user_id: Optional[str] = None
    password: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    device_flow_id: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    address: Optional[str] = None
    # Social platforms: the same free-form credential map the connect form uses.
    credentials: Optional[Dict[str, str]] = None

class AgentSocialBindingRequest(BaseModel):
    """One shape for all four social platforms (VK, Instagram, Facebook,
    WhatsApp). `credentials` carries exactly the fields that platform declares
    in agent_messenger_governance.SOCIAL_PLATFORMS — the frontend renders its
    form from that same declaration (GET /api/social-platforms), so the two
    cannot drift apart."""
    platform: str
    credentials: Dict[str, str] = Field(default_factory=dict)
    allowed_chat_ids: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    response_mode: str = "draft"

class SmmSettingsRequest(BaseModel):
    public_base_url: Optional[str] = None

class SmmPublicationRequest(BaseModel):
    """One outbound post. `binding_id`/`target_chat_id` are only meaningful for
    delivery_mode='auto' — that pair is what backend/smm_posts.py hands to the
    shared per-platform sender. A 'manual' post carries no credentials at all;
    it is a record of something the owner posts themselves."""
    title: str
    platform: str
    body: str = ""
    account: str = ""
    binding_id: str = ""
    target_chat_id: str = ""
    delivery_mode: str = "manual"
    status: str = "draft"
    tags: List[str] = Field(default_factory=list)
    media_url: str = ""
    scheduled_at: Optional[str] = None
    reach: Optional[int] = None
    reach_delta: Optional[float] = None

class SmmPublicationUpdateRequest(BaseModel):
    title: Optional[str] = None
    platform: Optional[str] = None
    body: Optional[str] = None
    account: Optional[str] = None
    binding_id: Optional[str] = None
    target_chat_id: Optional[str] = None
    delivery_mode: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[List[str]] = None
    media_url: Optional[str] = None
    scheduled_at: Optional[str] = None
    published_at: Optional[str] = None
    reach: Optional[int] = None
    reach_delta: Optional[float] = None

class SmmPublicationBulkRequest(BaseModel):
    ids: List[str] = Field(default_factory=list)

class PendingReplySendRequest(BaseModel):
    edited_text: Optional[str] = None

class AgentDiscordBindingRequest(BaseModel):
    bot_token: str
    allowed_channel_ids: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    response_mode: str = "draft"

class AgentSlackBindingRequest(BaseModel):
    bot_token: str
    app_token: str
    allowed_channel_ids: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    response_mode: str = "draft"

class AgentEmailBindingRequest(BaseModel):
    imap_host: str
    imap_port: int = 993
    smtp_host: str
    smtp_port: int = 587
    address: str
    password: str
    allowed_senders: List[str] = Field(default_factory=list)
    system_prompt: str = ""
    response_mode: str = "draft"


@app.get("/api/agents/{agent_id}/usage")
async def get_agent_usage_api(agent_id: str):
    """Current spend vs. configured budget for one agent (see billing plan)."""
    from backend.database import get_agent_budget_status
    return await asyncio.to_thread(get_agent_budget_status, agent_id)

@app.put("/api/agents/{agent_id}/budget")
async def set_agent_budget_api(agent_id: str, payload: AgentBudgetRequest):
    from backend.database import get_subagent, save_subagent
    subagent = await asyncio.to_thread(get_subagent, agent_id)
    if not subagent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if payload.budget_period not in ("monthly", "lifetime"):
        raise HTTPException(status_code=400, detail="budget_period must be 'monthly' or 'lifetime'")
    await asyncio.to_thread(
        save_subagent,
        subagent["id"], subagent["name"], subagent["system_prompt"], subagent["model"],
        subagent["agent_type"], subagent["parent_id"], subagent["skills"], subagent["x"], subagent["y"],
        subagent["temperature"], subagent["role"], subagent["status"], subagent["is_enabled"],
        subagent["model_provider"], subagent["model_type"], subagent["model_params"],
        payload.budget_usd_limit, payload.budget_period, subagent.get("tier_id"),
        subagent.get("allowed_provider_ids"), subagent.get("budget_fallback_to_local", False),
        subagent.get("project_id"),
    )
    return {"status": "success", "id": agent_id}


@app.get("/api/agent-tiers")
async def list_agent_tiers_api():
    from backend.agent_tiers import list_tiers
    return await asyncio.to_thread(list_tiers)

@app.post("/api/agent-tiers")
async def create_agent_tier_api(tier: AgentTierRequest):
    from backend.agent_tiers import create_tier
    try:
        return await asyncio.to_thread(
            create_tier, tier.name, tier.description, tier.budget_usd_limit_default,
            tier.budget_period_default, tier.allow_external_provider, tier.allow_messenger, tier.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.put("/api/agent-tiers/{tier_id}")
async def update_agent_tier_api(tier_id: str, tier: AgentTierRequest):
    from backend.agent_tiers import update_tier
    try:
        return await asyncio.to_thread(
            update_tier, tier_id,
            name=tier.name, description=tier.description,
            budget_usd_limit_default=tier.budget_usd_limit_default, budget_period_default=tier.budget_period_default,
            allow_external_provider=tier.allow_external_provider, allow_messenger=tier.allow_messenger,
            is_active=tier.is_active,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Tier not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.delete("/api/agent-tiers/{tier_id}")
async def delete_agent_tier_api(tier_id: str):
    from backend.agent_tiers import delete_tier
    await asyncio.to_thread(delete_tier, tier_id)
    return {"status": "success", "id": tier_id}


@app.get("/api/projects")
async def list_projects_api():
    from backend.projects import list_projects
    return await asyncio.to_thread(list_projects)

@app.post("/api/projects")
async def create_project_api(project: ProjectRequest):
    from backend.projects import create_project
    try:
        return await asyncio.to_thread(
            create_project, project.name, project.description, project.is_active, project.parent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.put("/api/projects/{project_id}")
async def update_project_api(project_id: str, project: ProjectRequest):
    from backend.projects import update_project
    try:
        return await asyncio.to_thread(
            update_project, project_id,
            name=project.name, description=project.description, is_active=project.is_active,
            parent_id=project.parent_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.delete("/api/projects/{project_id}")
async def delete_project_api(project_id: str):
    from backend.projects import delete_project
    await asyncio.to_thread(delete_project, project_id)
    return {"status": "success", "id": project_id}


@app.get("/api/agents/{agent_id}/telegram")
async def list_agent_telegram_bindings_api(agent_id: str):
    from backend.agent_messenger_governance import list_telegram_bindings
    return await asyncio.to_thread(list_telegram_bindings, agent_id)

@app.post("/api/agents/{agent_id}/telegram")
async def create_agent_telegram_binding_api(agent_id: str, payload: AgentTelegramBindingRequest):
    """Validates a BotFather token and creates a single-confirmation (R3) approval proposal."""
    from backend.agent_messenger_governance import create_telegram_binding_proposal
    try:
        proposal = await asyncio.to_thread(
            create_telegram_binding_proposal, agent_id, payload.bot_token, payload.allowed_chat_ids,
            payload.system_prompt, "", "", payload.response_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "bot_username": proposal["bot_username"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Bot token validated. One owner approval is required before the bot goes live.",
    }

@app.delete("/api/agents/telegram/{binding_id}")
async def delete_agent_telegram_binding_api(binding_id: str):
    from backend.agent_messenger_governance import disable_binding
    try:
        await disable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}


@app.get("/api/agents/{agent_id}/matrix")
async def list_agent_matrix_bindings_api(agent_id: str):
    from backend.agent_messenger_governance import list_matrix_bindings
    return await asyncio.to_thread(list_matrix_bindings, agent_id)

class MatrixDeviceLoginRequest(BaseModel):
    homeserver_url: str


@app.post("/api/matrix/device-login/start")
async def start_matrix_device_login_api(payload: MatrixDeviceLoginRequest):
    """Begins the browser (device-code) login used by MAS-backed homeservers —
    the only Matrix credential that renews itself instead of expiring minutes
    after it is pasted."""
    from backend.matrix_oauth import start_device_login
    try:
        return await asyncio.to_thread(start_device_login, payload.homeserver_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/matrix/device-login/{flow_id}/poll")
async def poll_matrix_device_login_api(flow_id: str):
    """Returns {'status': 'pending'} until the owner confirms the code in the
    browser, then {'status': 'complete', 'user_id': ...}. The tokens themselves
    stay server-side and are picked up by device_flow_id when the binding is created."""
    from backend.agent_messenger_governance import poll_matrix_device_login
    try:
        return await asyncio.to_thread(poll_matrix_device_login, flow_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agents/{agent_id}/matrix")
async def create_agent_matrix_binding_api(agent_id: str, payload: AgentMatrixBindingRequest):
    """Validates a Matrix login (or access token) and creates a single-confirmation (R3) approval proposal."""
    from backend.agent_messenger_governance import create_matrix_binding_proposal
    try:
        proposal = await asyncio.to_thread(
            create_matrix_binding_proposal,
            agent_id, payload.homeserver_url, payload.user_id,
            payload.password, payload.access_token, payload.refresh_token, payload.device_flow_id,
            payload.allowed_room_ids, payload.system_prompt, "", "", payload.response_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "matrix_user_id": proposal["bot_username"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Matrix credential validated. One owner approval is required before it goes live.",
    }

@app.delete("/api/agents/matrix/{binding_id}")
async def delete_agent_matrix_binding_api(binding_id: str):
    from backend.agent_messenger_governance import disable_binding
    try:
        await disable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}


@app.get("/api/agents/{agent_id}/discord")
async def list_agent_discord_bindings_api(agent_id: str):
    from backend.agent_messenger_governance import list_discord_bindings
    return await asyncio.to_thread(list_discord_bindings, agent_id)

@app.post("/api/agents/{agent_id}/discord")
async def create_agent_discord_binding_api(agent_id: str, payload: AgentDiscordBindingRequest):
    """Validates a Discord bot token and creates a single-confirmation (R3) approval proposal."""
    from backend.agent_messenger_governance import create_discord_binding_proposal
    try:
        proposal = await asyncio.to_thread(
            create_discord_binding_proposal, agent_id, payload.bot_token, payload.allowed_channel_ids,
            payload.system_prompt, "", "", payload.response_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "bot_username": proposal["bot_username"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Bot token validated. One owner approval is required before the bot goes live.",
    }

@app.delete("/api/agents/discord/{binding_id}")
async def delete_agent_discord_binding_api(binding_id: str):
    from backend.agent_messenger_governance import disable_binding
    try:
        await disable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}


@app.get("/api/agents/{agent_id}/slack")
async def list_agent_slack_bindings_api(agent_id: str):
    from backend.agent_messenger_governance import list_slack_bindings
    return await asyncio.to_thread(list_slack_bindings, agent_id)

@app.post("/api/agents/{agent_id}/slack")
async def create_agent_slack_binding_api(agent_id: str, payload: AgentSlackBindingRequest):
    """Validates Slack tokens and creates a single-confirmation (R3) approval proposal."""
    from backend.agent_messenger_governance import create_slack_binding_proposal
    try:
        proposal = await asyncio.to_thread(
            create_slack_binding_proposal, agent_id, payload.bot_token, payload.app_token,
            payload.allowed_channel_ids, payload.system_prompt, "", "", payload.response_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "slack_identity": proposal["bot_username"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Slack tokens validated. One owner approval is required before the bot goes live.",
    }

@app.delete("/api/agents/slack/{binding_id}")
async def delete_agent_slack_binding_api(binding_id: str):
    from backend.agent_messenger_governance import disable_binding
    try:
        await disable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}


@app.get("/api/agents/{agent_id}/email")
async def list_agent_email_bindings_api(agent_id: str):
    from backend.agent_messenger_governance import list_email_bindings
    return await asyncio.to_thread(list_email_bindings, agent_id)

@app.post("/api/agents/{agent_id}/email")
async def create_agent_email_binding_api(agent_id: str, payload: AgentEmailBindingRequest):
    """Logs into IMAP+SMTP with the given mailbox credentials and creates a
    single-confirmation (R3) approval proposal."""
    from backend.agent_messenger_governance import create_email_binding_proposal
    try:
        proposal = await asyncio.to_thread(
            create_email_binding_proposal, agent_id,
            payload.imap_host, payload.imap_port, payload.smtp_host, payload.smtp_port,
            payload.address, payload.password, payload.allowed_senders,
            payload.system_prompt, "", "", payload.response_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "mailbox": proposal["bot_username"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Mailbox credentials validated. One owner approval is required before it goes live.",
    }

@app.delete("/api/agents/email/{binding_id}")
async def delete_agent_email_binding_api(binding_id: str):
    from backend.agent_messenger_governance import disable_binding
    try:
        await disable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}


@app.get("/api/social-platforms")
async def list_social_platforms_api():
    """What each social platform needs to connect, straight from the backend's
    own declaration — the SMM Factory connect form renders its fields from this
    rather than hard-coding a second copy that could drift from the validator."""
    from backend.agent_messenger_governance import SOCIAL_PLATFORMS

    return [
        {
            "id": platform,
            "label": spec["label"],
            "delivery": spec["delivery"],
            "required": list(spec["required"]),
            "account_field": spec["account_field"],
        }
        for platform, spec in SOCIAL_PLATFORMS.items()
    ]


@app.get("/api/agents/{agent_id}/social")
async def list_agent_social_bindings_api(agent_id: str):
    from backend.agent_messenger_governance import list_social_bindings
    return await asyncio.to_thread(list_social_bindings, None, agent_id)


@app.post("/api/agents/{agent_id}/social")
async def create_agent_social_binding_api(agent_id: str, payload: AgentSocialBindingRequest):
    """Validates a social account credential against the provider itself and
    creates the same single-confirmation (R3) proposal every other channel
    goes through — nothing polls, and no webhook is honoured, until approval."""
    from backend.agent_messenger_governance import SOCIAL_PLATFORMS, create_social_binding_proposal

    if payload.platform not in SOCIAL_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown social platform: {payload.platform}")
    try:
        proposal = await asyncio.to_thread(
            create_social_binding_proposal, payload.platform, agent_id, payload.credentials,
            payload.allowed_chat_ids, payload.system_prompt, "", "", payload.response_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": proposal["status"],
        "binding_id": proposal["id"],
        "account": proposal["bot_username"],
        "task_id": proposal["control_task_id"],
        "risk_class": "R3",
        "message": "Учётные данные проверены. Канал заработает после одного подтверждения владельца.",
    }


@app.get("/api/messenger-bindings/{binding_id}/webhook")
async def get_social_webhook_details_api(binding_id: str):
    """The callback URL and verify token to paste into the Meta app console.
    Its own endpoint because the verify token is a credential and stays out of
    the general binding list."""
    from backend.agent_messenger_governance import social_webhook_details

    try:
        return await asyncio.to_thread(social_webhook_details, binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/smm/settings")
async def get_smm_settings_api():
    """SMM Factory-wide settings. Today just the public base URL the Meta
    webhooks are reachable at — the backend cannot discover that for itself
    behind a reverse proxy, and the connect form has to show the owner an
    exact URL to register."""
    from backend.database import get_setting

    return {"public_base_url": await asyncio.to_thread(get_setting, "smm_public_base_url") or ""}


@app.put("/api/smm/settings")
async def update_smm_settings_api(payload: SmmSettingsRequest):
    from backend.database import set_setting

    if payload.public_base_url is not None:
        await asyncio.to_thread(set_setting, "smm_public_base_url", payload.public_base_url.strip().rstrip("/"))
    return await get_smm_settings_api()


# ── SMM Factory: the publication register ──────────────────────────────────
# The outbound half of the section (backend/smm_posts.py). Everything else here
# answers people who wrote first; this is what the accounts say on their own.

@app.get("/api/smm/publications")
async def list_smm_publications_api(
    status: str = "all",
    platform: str = "all",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    per_page: int = 10,
    sort: str = "date",
    direction: str = "desc",
):
    from backend import smm_posts

    try:
        return await asyncio.to_thread(
            smm_posts.list_publications, status, platform, q, date_from or None,
            date_to or None, page, per_page, sort, direction,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/smm/publications/stats")
async def smm_publication_stats_api(platform: str = "all", date_from: str = "", date_to: str = ""):
    """KPI row above the table, computed over the same period/platform filter
    the table uses so the cards and the rows can never disagree."""
    from backend import smm_posts

    try:
        return await asyncio.to_thread(
            smm_posts.publication_stats, platform, date_from or None, date_to or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/smm/publications")
async def create_smm_publication_api(payload: SmmPublicationRequest):
    from backend import smm_posts

    try:
        return await asyncio.to_thread(
            smm_posts.create_publication,
            payload.title, payload.platform, payload.body, payload.account, payload.binding_id,
            payload.target_chat_id, payload.delivery_mode, payload.status, payload.tags,
            payload.media_url, payload.scheduled_at, None, payload.reach, payload.reach_delta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/smm/publications/{post_id}")
async def get_smm_publication_api(post_id: str):
    from backend import smm_posts

    post = await asyncio.to_thread(smm_posts.get_publication, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Публикация не найдена")
    return post


@app.put("/api/smm/publications/{post_id}")
async def update_smm_publication_api(post_id: str, payload: SmmPublicationUpdateRequest):
    from backend import smm_posts

    try:
        return await asyncio.to_thread(
            smm_posts.update_publication, post_id, **payload.model_dump(exclude_unset=True),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Публикация не найдена")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/smm/publications/{post_id}/publish")
async def publish_smm_publication_api(post_id: str):
    """Sends an auto post through its bound channel now, or records that a
    manual one went out. A provider refusal comes back as 409 with the
    provider's own wording — the row is left 'Ошибка', never 'Опубликовано'."""
    from backend import smm_posts

    try:
        return await smm_posts.publish_publication(post_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Публикация не найдена")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Не удалось опубликовать: {exc}") from exc


@app.delete("/api/smm/publications/{post_id}")
async def delete_smm_publication_api(post_id: str):
    from backend import smm_posts

    try:
        await asyncio.to_thread(smm_posts.delete_publication, post_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Публикация не найдена")
    return {"status": "success", "id": post_id}


@app.post("/api/smm/publications/bulk-delete")
async def bulk_delete_smm_publications_api(payload: SmmPublicationBulkRequest):
    from backend import smm_posts

    deleted = await asyncio.to_thread(smm_posts.delete_publications, payload.ids)
    return {"status": "success", "deleted": deleted}


@app.get("/api/messenger-bindings")
async def list_all_messenger_bindings_api():
    """Cross-agent, cross-platform view of every messenger binding — powers the
    central 'Каналы связи' admin page rather than each agent's own edit form."""
    from backend.agent_messenger_governance import (
        list_telegram_bindings, list_matrix_bindings, list_discord_bindings,
        list_slack_bindings, list_email_bindings, list_social_bindings,
    )
    from backend.database import get_all_subagents

    names = {a["id"]: a["name"] for a in await asyncio.to_thread(get_all_subagents)}
    telegram, matrix, discord_, slack, email_, social = await asyncio.gather(
        asyncio.to_thread(list_telegram_bindings),
        asyncio.to_thread(list_matrix_bindings),
        asyncio.to_thread(list_discord_bindings),
        asyncio.to_thread(list_slack_bindings),
        asyncio.to_thread(list_email_bindings),
        asyncio.to_thread(list_social_bindings),
    )
    bindings = telegram + matrix + discord_ + slack + email_ + social
    for binding in bindings:
        binding["agent_name"] = names.get(binding["subagent_id"], binding["subagent_id"])
    bindings.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return bindings


@app.get("/api/messenger-bindings/activity")
async def list_messenger_activity_api(binding_id: Optional[str] = None, limit: int = 200):
    """Live feed of what backend/bot_access_gate.py's authorize() has decided
    about incoming messages across every channel — lets the admin see whether
    a message actually reached a bound bot without reading server logs."""
    from backend import channel_activity

    return channel_activity.recent(binding_id, min(max(limit, 1), 500))


@app.patch("/api/messenger-bindings/{binding_id}")
async def update_messenger_binding_api(binding_id: str, payload: MessengerBindingUpdateRequest):
    """Edits an active binding's per-channel prompt and/or draft/auto-labeled
    response mode. Doesn't require a new owner approval — see
    agent_messenger_governance.update_binding_settings for why."""
    from backend.agent_messenger_governance import update_binding_settings
    try:
        return await update_binding_settings(
            binding_id,
            system_prompt_override=payload.system_prompt,
            response_mode=payload.response_mode,
            access_mode=payload.access_mode,
            default_plan_id=payload.default_plan_id,
            welcome_message=payload.welcome_message,
            allowed_chat_ids=payload.allowed_chat_ids,
            auto_reply_disclosure_text=payload.auto_reply_disclosure,
            human_takeover_pause_minutes=payload.human_takeover_pause_minutes,
            escalation_enabled=payload.escalation_enabled,
            handling_menu_enabled=payload.handling_menu_enabled,
            model_override=payload.model,
            model_provider_override=payload.model_provider,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Binding not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/messenger-bindings/{binding_id}/disable")
async def disable_messenger_binding_api(binding_id: str):
    """Stops the binding's bot but keeps its stored credential, so it can be
    re-enabled later — unlike DELETE below, this is reversible."""
    from backend.agent_messenger_governance import disable_binding
    try:
        return await disable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")


@app.post("/api/messenger-bindings/{binding_id}/enable")
async def enable_messenger_binding_api(binding_id: str):
    """Restarts a disabled binding's bot using its still-stored credential."""
    from backend.agent_messenger_governance import enable_binding
    try:
        return await enable_binding(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/messenger-bindings/{binding_id}/reconnect")
async def reconnect_messenger_binding_api(binding_id: str, payload: MessengerBindingReconnectRequest):
    """Swaps in a fresh credential for an existing binding and restarts its bot
    — the fix for a dead/expired token (e.g. a short-lived Matrix access token)
    without deleting and recreating the whole channel. The new credential is
    verified live before anything is touched, same bar as connecting fresh."""
    from backend.agent_messenger_governance import (
        SOCIAL_PLATFORMS, get_binding, reconnect_telegram_binding, reconnect_matrix_binding,
        reconnect_discord_binding, reconnect_slack_binding, reconnect_email_binding,
        reconnect_social_binding,
    )
    from backend.matrix_oauth import MatrixTransientError

    binding = await asyncio.to_thread(get_binding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    platform = binding["platform"]
    try:
        if platform == "telegram":
            if not payload.bot_token:
                raise HTTPException(status_code=400, detail="bot_token обязателен")
            return await reconnect_telegram_binding(binding_id, payload.bot_token)
        if platform == "discord":
            if not payload.bot_token:
                raise HTTPException(status_code=400, detail="bot_token обязателен")
            return await reconnect_discord_binding(binding_id, payload.bot_token)
        if platform == "matrix":
            if not payload.homeserver_url or not (payload.user_id or payload.device_flow_id):
                raise HTTPException(status_code=400, detail="homeserver_url и user_id обязательны")
            return await reconnect_matrix_binding(
                binding_id, payload.homeserver_url, payload.user_id or "",
                password=payload.password or "", access_token=payload.access_token or "",
                refresh_token=payload.refresh_token or "", device_flow_id=payload.device_flow_id or "",
            )
        if platform == "slack":
            if not payload.bot_token or not payload.app_token:
                raise HTTPException(status_code=400, detail="bot_token и app_token обязательны")
            return await reconnect_slack_binding(binding_id, payload.bot_token, payload.app_token)
        if platform == "email":
            if not payload.imap_host or not payload.smtp_host or not payload.address or not payload.password:
                raise HTTPException(status_code=400, detail="imap_host, smtp_host, address и password обязательны")
            return await reconnect_email_binding(
                binding_id, payload.imap_host, payload.imap_port or 993,
                payload.smtp_host, payload.smtp_port or 587, payload.address, payload.password,
            )
        if platform in SOCIAL_PLATFORMS:
            if not payload.credentials:
                raise HTTPException(status_code=400, detail="credentials обязательны")
            return await reconnect_social_binding(binding_id, payload.credentials)
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")
    except MatrixTransientError as exc:
        # Credentials are saved and the channel stays active; the health check
        # starts the bot once the homeserver answers again.
        raise HTTPException(
            status_code=503, detail=f"Данные сохранены, но Matrix сейчас недоступен — канал запустится сам: {exc}",
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Binding not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/messenger-bindings/conversations")
async def list_all_conversations_api(limit: int = 300):
    """Every conversation on every active channel, newest first — the SMM
    Factory "Диалоги" panel is cross-channel by design, and fanning this out
    into one request per binding from the browser would be a request storm on
    an owner with a dozen accounts."""
    from backend.conversation_state import list_states
    from backend.database import get_all_subagents

    async def collect() -> list[dict]:
        bindings = await list_all_messenger_bindings_api()
        names = {a["id"]: a["name"] for a in await asyncio.to_thread(get_all_subagents)}
        out: list[dict] = []
        for binding in bindings:
            if binding.get("status") not in ("active", "failed"):
                continue
            for state in await asyncio.to_thread(list_states, binding["id"]):
                state["binding_id"] = binding["id"]
                state["agent_name"] = names.get(binding["subagent_id"], binding["subagent_id"])
                state["account"] = binding.get("bot_username", "")
                state["response_mode"] = binding.get("response_mode", "draft")
                out.append(state)
        out.sort(key=lambda c: c.get("last_message_at") or c.get("updated_at") or "", reverse=True)
        return out[: max(1, min(int(limit), 1000))]

    return await collect()


@app.get("/api/messenger-bindings/{binding_id}/conversations")
async def list_binding_conversations_api(binding_id: str):
    """Per-chat AI/owner handling state for this binding — see
    backend/conversation_state.py and bot_access_gate.py's handling-mode
    hook. Powers MessengerChannelsTab's "Диалоги" panel and its manual
    take-over/release button."""
    from backend.conversation_state import list_states

    return await asyncio.to_thread(list_states, binding_id)


class ConversationTakeoverRequest(BaseModel):
    action: str  # "take_over" | "enable_ai" ("release" kept as its old alias) | "wait"


_CONVERSATION_ACTIONS = {
    "take_over": "owner_takeover",
    "enable_ai": "ai",
    "release": "ai",  # what the button was originally called
    "wait": "waiting",
}


@app.post("/api/messenger-bindings/{binding_id}/conversations/{platform}/{chat_id}/takeover")
async def takeover_conversation_api(binding_id: str, platform: str, chat_id: str, payload: ConversationTakeoverRequest):
    """Decides who answers one specific conversation, on any channel, at any
    moment — independent of that binding's response_mode/handling_menu_enabled.
    'take_over' silences the bot, 'enable_ai' hands it to the agent without
    waiting for the person to pick anything, 'wait' puts it back to silence."""
    from backend.conversation_state import set_handling_mode

    mode = _CONVERSATION_ACTIONS.get(payload.action)
    if not mode:
        raise HTTPException(
            status_code=400, detail=f"action must be one of {sorted(_CONVERSATION_ACTIONS)}"
        )
    return await asyncio.to_thread(set_handling_mode, binding_id, platform, chat_id, mode)


@app.get("/api/messenger-bindings/{binding_id}/conversations/{platform}/{chat_id}/history")
async def conversation_history_api(binding_id: str, platform: str, chat_id: str, limit: int = 200):
    """The stored conversation with one person — the same rows the agent reads
    as its context, so what is shown here is what it actually remembers."""
    from backend.conversation_state import history

    return await asyncio.to_thread(history, binding_id, platform, chat_id, limit)


@app.delete("/api/messenger-bindings/{binding_id}/conversations/{platform}/{chat_id}/history")
async def clear_conversation_history_api(binding_id: str, platform: str, chat_id: str):
    """Makes the agent forget this person: the next message starts from an empty
    context. Irreversible — the messages are deleted, not archived."""
    from backend.conversation_state import clear_history

    return await asyncio.to_thread(clear_history, binding_id, platform, chat_id)


@app.delete("/api/messenger-bindings/{binding_id}")
async def delete_messenger_binding_api(binding_id: str):
    """Permanently removes the binding: stops its bot, destroys the stored
    credential, and deletes the row. Not reversible — reconnecting the channel
    afterwards means entering fresh credentials."""
    from backend.agent_messenger_governance import delete_binding_permanently
    try:
        await delete_binding_permanently(binding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"status": "success", "id": binding_id}


@app.get("/api/channel-replies")
async def list_channel_replies_api(status: str = "pending"):
    """Drafts queued by 'draft' mode channels, awaiting the owner's review/send."""
    from backend.channel_replies import list_pending_replies
    return await asyncio.to_thread(list_pending_replies, status)


@app.post("/api/channel-replies/{reply_id}/send")
async def send_channel_reply_api(reply_id: str, payload: PendingReplySendRequest):
    from backend.channel_replies import send_pending_reply
    try:
        return await send_pending_reply(reply_id, payload.edited_text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/channel-replies/{reply_id}/discard")
async def discard_channel_reply_api(reply_id: str):
    from backend.channel_replies import discard_pending_reply
    try:
        await asyncio.to_thread(discard_pending_reply, reply_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "id": reply_id}


# ── Public bot access: plans, tokens, subscribers, usage ────────────────────
# The admin surface for token-gated social bots (backend/bot_access.py). Every
# route here sits behind the same dashboard session auth as the rest of /api,
# so "owner-only" needs no extra gate — but nothing in this block may ever
# return a token's plaintext except the issue endpoints, which return it once.

class AccessPlanRequest(BaseModel):
    name: str
    description: str = ""
    period: str = "monthly"
    price_usd: Optional[float] = None
    is_purchasable: bool = False
    duration_days: Optional[int] = None
    limit_usd: Optional[float] = None
    limit_tokens: Optional[int] = None
    limit_messages: Optional[int] = None
    rate_limit_per_min: int = 6
    max_message_chars: int = 2000
    allowed_tools: Optional[List[str]] = None
    system_prompt_suffix: str = ""
    welcome_message: str = ""
    is_active: bool = True
    subagent_id: Optional[str] = None


class AccessTokenRequest(BaseModel):
    binding_id: str
    subagent_id: str
    plan_id: Optional[str] = None
    label: str = ""
    max_chats: int = 1
    expires_at: Optional[str] = None
    limit_usd: Optional[float] = None
    limit_tokens: Optional[int] = None
    limit_messages: Optional[int] = None
    notes: str = ""
    count: int = 1


class AccessTokenUpdateRequest(BaseModel):
    label: Optional[str] = None
    plan_id: Optional[str] = None
    status: Optional[str] = None
    max_chats: Optional[int] = None
    expires_at: Optional[str] = None
    limit_usd: Optional[float] = None
    limit_tokens: Optional[int] = None
    limit_messages: Optional[int] = None
    notes: Optional[str] = None


class SubscriberUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    profile: Optional[Dict[str, Any]] = None


@app.get("/api/access/overview")
async def access_overview_api():
    from backend.bot_access import overview
    return await asyncio.to_thread(overview)


@app.get("/api/access/plans")
async def list_access_plans_api():
    from backend.bot_access import list_plans
    return await asyncio.to_thread(list_plans)


@app.post("/api/access/plans")
async def create_access_plan_api(payload: AccessPlanRequest):
    from backend.bot_access import create_plan
    try:
        return await asyncio.to_thread(create_plan, **payload.model_dump())
    except KeyError as exc:
        # Only raised here for an unresolvable subagent_id — "Plan not found"
        # would be nonsensical since no plan exists yet at creation time.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/access/plans/{plan_id}")
async def update_access_plan_api(plan_id: str, payload: AccessPlanRequest):
    from backend.bot_access import update_plan
    try:
        return await asyncio.to_thread(update_plan, plan_id, **payload.model_dump())
    except KeyError as exc:
        detail = "Plan not found" if str(exc).strip("'\"") == plan_id else str(exc)
        raise HTTPException(status_code=404, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/access/plans/{plan_id}")
async def delete_access_plan_api(plan_id: str):
    from backend.bot_access import delete_plan
    try:
        if not await asyncio.to_thread(delete_plan, plan_id):
            raise HTTPException(status_code=404, detail="Plan not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "success", "id": plan_id}


@app.get("/api/access/tokens")
async def list_access_tokens_api(
    binding_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    status: Optional[str] = None,
):
    from backend.bot_access import list_tokens
    return await asyncio.to_thread(list_tokens, binding_id, subagent_id, status)


@app.post("/api/access/tokens")
async def issue_access_tokens_api(payload: AccessTokenRequest):
    """Issues one token, or `count` of them for handing out in a batch. The
    plaintext in the response is the only copy that ever leaves the server —
    it is stored hashed and cannot be shown again."""
    from backend.bot_access import issue_token, issue_tokens_bulk
    fields = payload.model_dump()
    count = max(1, int(fields.pop("count", 1) or 1))
    try:
        if count == 1:
            return {"tokens": [await asyncio.to_thread(issue_token, **fields)]}
        return {"tokens": await asyncio.to_thread(issue_tokens_bulk, count, **fields)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/access/tokens/{token_id}")
async def get_access_token_api(token_id: str):
    from backend.bot_access import token_usage_summary
    try:
        return await asyncio.to_thread(token_usage_summary, token_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Token not found")


@app.patch("/api/access/tokens/{token_id}")
async def update_access_token_api(token_id: str, payload: AccessTokenUpdateRequest):
    from backend.bot_access import update_token
    try:
        return await asyncio.to_thread(update_token, token_id, **payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Token not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/access/tokens/{token_id}/revoke")
async def revoke_access_token_api(token_id: str):
    """Kills the token and blocks every chat that already redeemed it — an
    outstanding token is worthless the moment this returns."""
    from backend.bot_access import revoke_token
    try:
        return await asyncio.to_thread(revoke_token, token_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Token not found")


@app.post("/api/access/tokens/{token_id}/reset-usage")
async def reset_access_token_usage_api(token_id: str):
    from backend.bot_access import reset_token_usage
    try:
        return await asyncio.to_thread(reset_token_usage, token_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Token not found")


@app.delete("/api/access/tokens/{token_id}")
async def delete_access_token_api(token_id: str):
    from backend.bot_access import delete_token
    if not await asyncio.to_thread(delete_token, token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return {"status": "success", "id": token_id}


@app.get("/api/access/subscribers")
async def list_access_subscribers_api(
    binding_id: Optional[str] = None,
    token_id: Optional[str] = None,
    status: Optional[str] = None,
):
    from backend.bot_access import list_subscribers
    return await asyncio.to_thread(list_subscribers, binding_id, token_id, status)


@app.get("/api/access/subscribers/{subscriber_id}")
async def get_access_subscriber_api(subscriber_id: str, message_limit: int = 50):
    """The картотека: identity, token, plan, spend and the stored conversation."""
    from backend.bot_access import subscriber_card
    try:
        return await asyncio.to_thread(subscriber_card, subscriber_id, message_limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscriber not found")


@app.patch("/api/access/subscribers/{subscriber_id}")
async def update_access_subscriber_api(subscriber_id: str, payload: SubscriberUpdateRequest):
    from backend.bot_access import update_subscriber
    try:
        return await asyncio.to_thread(update_subscriber, subscriber_id, **payload.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/access/subscribers/{subscriber_id}")
async def delete_access_subscriber_api(subscriber_id: str):
    """Forgets a subscriber's account. Their stored conversation lives in the
    shared messages table under the same session id and is left alone — delete
    it from the chat view if that is also wanted."""
    from backend.bot_access import delete_subscriber
    if not await asyncio.to_thread(delete_subscriber, subscriber_id):
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return {"status": "success", "id": subscriber_id}


# ── Billing: subscriptions, crypto invoices, provider settings ──────────────
# The webhook below is the one route in this block that is NOT behind the
# dashboard session — it is authenticated by the provider's HMAC signature
# instead (backend/payments.py) and is listed in the auth middleware's public
# paths. Everything else is owner-only like the rest of /api.

class PaymentConfigRequest(BaseModel):
    provider: Optional[str] = None
    public_base_url: Optional[str] = None
    success_url: Optional[str] = None


class InvoiceRequest(BaseModel):
    plan_id: str
    binding_id: str
    subagent_id: str
    customer_ref: str = ""
    pay_currency: str = ""
    purpose: str = "new"
    subscription_id: Optional[str] = None


@app.get("/api/billing/overview")
async def billing_overview_api():
    from backend.payments import revenue_overview
    return await asyncio.to_thread(revenue_overview)


@app.get("/api/billing/config")
async def get_billing_config_api():
    """Provider settings. The two secrets are reported as booleans only — they
    are stored in the api_keys table and set from the «Ключи API» panel."""
    from backend.payments import get_config
    return await asyncio.to_thread(get_config)


@app.put("/api/billing/config")
async def set_billing_config_api(payload: PaymentConfigRequest):
    from backend.payments import set_config
    try:
        return await asyncio.to_thread(
            set_config, payload.provider, payload.public_base_url, payload.success_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/billing/subscriptions")
async def list_subscriptions_api(
    binding_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    status: Optional[str] = None,
):
    from backend.bot_access import list_subscriptions
    return await asyncio.to_thread(list_subscriptions, binding_id, plan_id, status)


@app.post("/api/billing/subscriptions/{subscription_id}/renew")
async def renew_subscription_api(subscription_id: str):
    """Grants another paid period without charging — for a payment settled
    outside the provider, or a goodwill extension."""
    from backend.bot_access import renew_subscription
    try:
        return await asyncio.to_thread(renew_subscription, subscription_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Subscription not found") from exc


@app.post("/api/billing/subscriptions/{subscription_id}/cancel")
async def cancel_subscription_api(subscription_id: str, suspend_token: bool = True):
    from backend.bot_access import cancel_subscription
    try:
        return await asyncio.to_thread(cancel_subscription, subscription_id, suspend_token)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscription not found")


@app.post("/api/billing/subscriptions/{subscription_id}/auto-renew")
async def set_auto_renew_api(subscription_id: str, enabled: bool = True):
    from backend.bot_access import set_subscription_auto_renew
    try:
        return await asyncio.to_thread(set_subscription_auto_renew, subscription_id, enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subscription not found")


@app.get("/api/billing/invoices")
async def list_invoices_api(status: Optional[str] = None, binding_id: Optional[str] = None):
    from backend.payments import list_invoices
    return await asyncio.to_thread(list_invoices, status, binding_id)


@app.post("/api/billing/invoices")
async def create_invoice_api(payload: InvoiceRequest):
    from backend.payments import create_invoice
    try:
        return await create_invoice(
            plan_id=payload.plan_id,
            binding_id=payload.binding_id,
            subagent_id=payload.subagent_id,
            customer_ref=payload.customer_ref,
            pay_currency=payload.pay_currency,
            origin="admin",
            purpose=payload.purpose,
            subscription_id=payload.subscription_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/billing/invoices/{invoice_id}/mark-paid")
async def mark_invoice_paid_api(invoice_id: str):
    """Owner confirming a transfer they verified themselves. Runs the same
    issuance path the webhook does, including the one-time plaintext token."""
    from backend.payments import mark_paid_manually
    try:
        result = await asyncio.to_thread(mark_paid_manually, invoice_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Invoice not found") from exc
    await _deliver_paid_access(result)
    return result


@app.post("/api/billing/invoices/{invoice_id}/cancel")
async def cancel_invoice_api(invoice_id: str):
    from backend.payments import cancel_invoice
    try:
        return await asyncio.to_thread(cancel_invoice, invoice_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Invoice not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _deliver_paid_access(result: dict) -> None:
    """Tells a buyer who paid from inside a bot chat that they are in.

    The token was already bound to their chat by apply_paid_invoice, so this is
    a courtesy message, not the credential handover — best-effort, and a failure
    here never un-does a completed payment.
    """
    deliver_to = result.get("deliver_to")
    if not deliver_to:
        return
    try:
        from backend.channel_replies import send_to_channel

        subscription = result.get("subscription") or {}
        until = subscription.get("current_period_end")
        suffix = f" Доступ действует до {until[:10]}." if until else ""
        await send_to_channel(
            deliver_to["binding_id"], deliver_to["platform"], deliver_to["chat_id"],
            f"Оплата получена — доступ открыт.{suffix} Можете задавать вопрос.",
        )
    except Exception:
        logger.exception("Could not confirm paid access to the buyer")


@app.post("/api/payments/webhook")
async def payments_webhook_api(request: Request):
    """Provider IPN callback. Public by necessity, authenticated by an HMAC
    signature over the raw body — see backend/payments.py. Always answers 200
    on a handled callback so the provider stops retrying; a rejected signature
    answers 400 without revealing why."""
    raw_body = await request.body()
    from backend.payments import handle_webhook

    outcome = await asyncio.to_thread(handle_webhook, raw_body, dict(request.headers))
    if not outcome["ok"]:
        raise HTTPException(status_code=400, detail=outcome["detail"])
    if outcome.get("result"):
        await _deliver_paid_access(outcome["result"])
    return {"status": "ok"}


@app.get("/api/system/stats")
async def get_system_stats_api():
    from backend.tools import get_system_stats
    import json
    return json.loads(get_system_stats())

@app.get("/api/system/metrics/history")
async def get_system_metrics_history_api(range: str = "24h"):
    """Downsampled host metrics plus threshold alerts for the «Метрики» tab."""
    from backend import host_metrics
    try:
        return await asyncio.to_thread(host_metrics.history, range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/api/market/prices")
async def get_market_prices_api(symbols: str):
    from backend.price_monitor import price_monitor
    parts = [s.strip() for s in symbols.split(",") if s.strip()]
    results = {}
    for s in parts:
        p = await price_monitor.get_market_price(s)
        results[s] = p if p is not None else "no data"
    return results

@app.get("/api/market/alerts")
async def get_market_alerts():
    from backend.price_monitor import price_monitor
    return price_monitor.get_alerts()

@app.post("/api/market/alerts")
async def create_market_alert(req: PriceAlertRequest):
    from backend.price_monitor import price_monitor
    alert = price_monitor.add_alert(req.symbol, req.target_price, req.condition, "dashboard")
    return {"status": "success", "alert": alert}

@app.delete("/api/market/alerts/{alert_id}")
async def cancel_market_alert(alert_id: str):
    from backend.price_monitor import price_monitor
    ok = price_monitor.cancel_alert(alert_id)
    return {"status": "cancelled" if ok else "not_found"}

@app.delete("/api/activity/logs")
async def clear_activity_logs_api():
    from backend.database import clear_activity_logs
    from backend.activity_logger import ACTIVITY_LOGS
    clear_activity_logs()
    ACTIVITY_LOGS.clear()
    return {"status": "success"}

@app.get("/api/history/sessions")
async def get_history_sessions():
    from backend.database import DB_PATH
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM subagents")
        subagent_ids = {r[0] for r in cursor.fetchall()}
        
        cursor.execute("SELECT session_id, MAX(timestamp) as last_time FROM messages GROUP BY session_id ORDER BY last_time DESC")
        session_rows = cursor.fetchall()
        sessions = [r[0] for r in session_rows]
        # The dashboard's chat-history card shows the last-activity time per session, so
        # surface the timestamp this query already computes instead of discarding it.
        last_time_map = {r[0]: r[1] for r in session_rows}
        
        # Fetch all custom titles, agent_ids and project_ids from session_metadata
        cursor.execute("SELECT session_id, title, agent_id, project_id, created_at FROM session_metadata")
        metadata_rows = cursor.fetchall()
        metadata_map = {r[0]: {"title": r[1], "agent_id": r[2], "project_id": r[3]} for r in metadata_rows}
        created_at_map = {r[0]: r[4] for r in metadata_rows}
        conn.close()

        # A session that exists but has not been spoken in yet. The list used to
        # be built from `messages` alone, so a chat the user had just created —
        # named, assigned an agent, and open on screen — was simply absent from
        # the sidebar until its first reply arrived, which reads as "my new chat
        # vanished". Its metadata row is proof enough that it exists.
        sessions.extend(s for s in created_at_map if s not in last_time_map)
        # No message means no last_time, so order those by when they were made;
        # a brand-new chat then appears at the top, where it was just created.
        last_time_map.update({
            s: created_at_map[s] for s in created_at_map if s not in last_time_map
        })

        # Incognito chats (backend/chat_settings.py) never appear in the list:
        # the only window onto one is the tab that opened it.
        from backend import chat_settings
        try:
            ephemeral_ids = set(chat_settings.ephemeral_session_ids())
        except Exception:
            logger.exception("Could not read incognito chats")
            ephemeral_ids = set()

        # Filter out subagents, and keep only "dashboard" and custom sessions
        user_sessions = [
            s for s in sessions
            if s not in subagent_ids and s != "dashboard" and not s.startswith("archive_")
            and s not in ephemeral_ids
        ]
        user_sessions.sort(key=lambda s: last_time_map.get(s) or "", reverse=True)

        sessions_response = []
        for s in ["dashboard"] + user_sessions:
            meta = metadata_map.get(s, {})
            title = meta.get("title")
            agent_id = meta.get("agent_id")
            project_id = meta.get("project_id")
            if not title:
                if s == "dashboard":
                    title = "Main Terminal"
                else:
                    title = s
            sessions_response.append({
                "id": s,
                "title": title,
                "agent_id": agent_id,
                "project_id": project_id,
                "updated_at": last_time_map.get(s)
            })
        return sessions_response
    except Exception as e:
        return [{"id": "dashboard", "title": "Main Terminal", "agent_id": None}]

class SessionAgentPayload(BaseModel):
    agent_id: str

@app.post("/api/history/{session_id}/agent")
async def set_session_agent(session_id: str, payload: SessionAgentPayload):
    """Updates the target agent/orchestrator ID for a session in the DB."""
    from backend.database import save_session_metadata, get_session_title
    try:
        title = get_session_title(session_id) or session_id
        save_session_metadata(session_id, title, agent_id=payload.agent_id)
        return {"status": "success", "message": f"Session {session_id} target agent set to {payload.agent_id}"}
    except Exception as e:
        logger.exception(f"Failed to set session agent for {session_id}: {e}")
        return {"status": "error", "message": "Failed to set session agent. Check server logs."}

class SessionProjectPayload(BaseModel):
    project_id: Optional[str] = None

@app.post("/api/history/{session_id}/project")
async def set_session_project(session_id: str, payload: SessionProjectPayload):
    """Updates which project (backend/projects.py) a session belongs to. A
    null project_id clears the assignment (conversation goes back to
    'no project')."""
    from backend.database import save_session_metadata, get_session_title
    try:
        title = get_session_title(session_id) or session_id
        # save_session_metadata treats agent_id=None as "leave unchanged" — an
        # explicit two-step (agent unchanged, project set) call keeps that
        # contract intact for this endpoint's one job.
        save_session_metadata(session_id, title, project_id=payload.project_id or "")
        return {"status": "success", "message": f"Session {session_id} project set to {payload.project_id}"}
    except Exception as e:
        logger.exception(f"Failed to set session project for {session_id}: {e}")
        return {"status": "error", "message": "Failed to set session project. Check server logs."}

@app.get("/api/history/{chat_id}")
async def get_history_api(chat_id: str, limit: int = 40):
    from backend.database import get_chat_history
    return get_chat_history(chat_id, limit=limit)

@app.delete("/api/history/{chat_id}")
async def delete_history_api(chat_id: str):
    from backend.database import clear_chat_history, delete_session_title
    from backend import chat_settings
    clear_chat_history(chat_id)
    delete_session_title(chat_id)
    chat_settings.delete_settings(chat_id)
    # Also clear from agent's in-memory last costs or messages if needed
    if chat_id in agent_instance.last_costs:
        agent_instance.last_costs[chat_id] = 0.0
    return {"status": "success"}

@app.delete("/api/history/{chat_id}/messages/{message_id}")
async def delete_history_message_api(chat_id: str, message_id: int):
    """Drops a single message from a conversation — the chat UI's per-message
    delete. The whole-session DELETE above is a different, much blunter thing."""
    from backend.database import delete_message

    if not delete_message(chat_id, message_id):
        raise HTTPException(status_code=404, detail="Message not found in this session.")
    return {"status": "success"}


@app.post("/api/history/{session_id}/archive")
async def archive_history_session(session_id: str):
    """Archives a session by renaming its session_id in the DB."""
    from backend.database import DB_PATH
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE messages SET session_id = ? WHERE session_id = ?", (f"archive_{session_id}", session_id))
        cursor.execute("UPDATE session_metadata SET session_id = ? WHERE session_id = ?", (f"archive_{session_id}", session_id))
        conn.commit()
        conn.close()
        return {"status": "success", "message": f"Session {session_id} archived"}
    except Exception as e:
        logger.exception(f"Failed to archive session {session_id}: {e}")
        return {"status": "error", "message": "Failed to archive session. Check server logs."}

class ForkSessionPayload(BaseModel):
    # Branch from this message: it and everything before it is copied, nothing
    # after. Omitted, the whole conversation is copied, as before.
    up_to_message_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=200)


@app.post("/api/history/{session_id}/fork")
async def fork_history_session(session_id: str, payload: Optional[ForkSessionPayload] = None):
    """Forks a session by duplicating its messages to a new session_id.

    The branch keeps the source chat's agent, project and chat settings, so it
    continues the same conversation rather than starting a differently
    configured one; only the messages after the branch point are left out."""
    from backend.database import (
        DB_PATH, get_session_agent_id, get_session_project_id, get_session_title,
        save_session_metadata,
    )
    from backend import chat_settings
    import sqlite3
    import time
    payload = payload or ForkSessionPayload()
    new_session_id = f"{session_id}_fork_{int(time.time())}"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if payload.up_to_message_id is not None:
            cursor.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND id = ?",
                (session_id, payload.up_to_message_id),
            )
            if cursor.fetchone() is None:
                conn.close()
                raise HTTPException(status_code=404, detail="Message not found in this session.")
            cursor.execute("""
                INSERT INTO messages (session_id, role, content, cost_usd, run_meta)
                SELECT ?, role, content, cost_usd, run_meta FROM messages
                WHERE session_id = ? AND id <= ? ORDER BY id ASC
            """, (new_session_id, session_id, payload.up_to_message_id))
        else:
            cursor.execute("""
                INSERT INTO messages (session_id, role, content, cost_usd, run_meta)
                SELECT ?, role, content, cost_usd, run_meta FROM messages WHERE session_id = ? ORDER BY id ASC
            """, (new_session_id, session_id))
        conn.commit()
        conn.close()

        old_title = get_session_title(session_id) or session_id
        save_session_metadata(
            new_session_id,
            (payload.title or "").strip() or f"Fork of {old_title}",
            agent_id=get_session_agent_id(session_id),
            project_id=get_session_project_id(session_id),
        )
        chat_settings.copy_settings(session_id, new_session_id)

        return {"status": "success", "new_session_id": new_session_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to fork session {session_id}: {e}")
        return {"status": "error", "message": "Failed to fork session. Check server logs."}


class ArenaContender(BaseModel):
    model: str = ""
    model_source: str = ""


class ArenaPayload(BaseModel):
    prompt: str
    contenders: List[ArenaContender]
    system_prompt: str = Field(default="", max_length=8000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=16384)


@app.post("/api/chat/arena")
async def chat_arena_api(payload: ArenaPayload):
    """Same prompt to two models, answers side by side (see backend/chat_arena.py)."""
    from backend import chat_arena
    contenders = [c.model_dump() for c in payload.contenders]
    error = chat_arena.validate_request(payload.prompt, contenders)
    if error:
        raise HTTPException(status_code=400, detail=error)
    results = await chat_arena.run_arena(
        payload.prompt,
        contenders,
        local_base_url=agent_instance.local_base_url,
        api_key=agent_instance.api_key,
        default_llm={
            "api_base": agent_instance.api_base, "api_key": agent_instance.api_key,
            "model": agent_instance.model, "provider": agent_instance.provider,
        },
        system_prompt=payload.system_prompt,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )
    return {"results": results}


class ChatSettingsPayload(BaseModel):
    system_prompt: str = ""
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    think: Optional[str] = None
    ephemeral: bool = False
    uncensored: bool = False


@app.get("/api/history/{session_id}/settings")
async def get_chat_settings_api(session_id: str):
    from backend import chat_settings
    return await asyncio.to_thread(chat_settings.get_settings, session_id)


@app.put("/api/history/{session_id}/settings")
async def put_chat_settings_api(session_id: str, payload: ChatSettingsPayload):
    from backend import chat_settings
    try:
        return await asyncio.to_thread(chat_settings.save_settings, session_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class RenameSessionPayload(BaseModel):
    title: str

@app.post("/api/history/{session_id}/rename")
async def rename_history_session(session_id: str, payload: RenameSessionPayload):
    """Updates the custom title for a session in the DB."""
    from backend.database import save_session_title
    try:
        save_session_title(session_id, payload.title)
        return {"status": "success", "message": f"Session {session_id} renamed to {payload.title}"}
    except Exception as e:
        logger.exception(f"Failed to rename session {session_id}: {e}")
        return {"status": "error", "message": "Failed to rename session. Check server logs."}



# ─── Obsidian API Endpoints ─────────────────────────────────────────────────

class ObsidianNoteCreate(BaseModel):
    title: str
    content: str
    folder: str = "Vexa"

@app.get("/api/obsidian/status")
async def obsidian_status():
    """Check if the Obsidian Local REST API plugin is reachable."""
    from backend.obsidian import is_reachable, _get_api_key
    reachable = await is_reachable()
    return {
        "reachable": reachable,
        "api_key_configured": bool(_get_api_key()),
        "message": "✅ Obsidian connected" if reachable else "❌ Obsidian is unavailable. Start Obsidian and enable the Local REST API plugin."
    }

@app.get("/api/obsidian/notes")
async def obsidian_list_notes(folder: str = ""):
    """List all markdown notes in the vault (or a specific folder)."""
    from backend.obsidian import list_notes
    from backend.rag import list_documents
    notes = await list_notes(folder)
    indexed = {d["note_path"] for d in list_documents(source_filter="obsidian") if d.get("note_path")}
    return {
        "notes": notes,
        "total": len(notes),
        "indexed_count": len(indexed),
        "indexed_paths": list(indexed)
    }

@app.post("/api/obsidian/sync")
async def obsidian_sync():
    """Trigger full Obsidian vault → Qdrant RAG sync."""
    from backend.obsidian import sync_vault_to_rag
    result = await sync_vault_to_rag()
    return result

@app.get("/api/obsidian/search")
async def obsidian_search(q: str = ""):
    """Semantic search across indexed Obsidian notes."""
    if not q.strip():
        return []
    from backend.rag import search_memory
    return search_memory(q, limit=8, threshold=0.35, source_filter="obsidian")

@app.post("/api/obsidian/notes")
async def obsidian_create_note(note: ObsidianNoteCreate):
    """Create a new note in the Obsidian vault."""
    from backend.tools import create_obsidian_note
    result_str = create_obsidian_note(
        title=note.title,
        content=note.content,
        folder=note.folder
    )
    import json
    return json.loads(result_str)

@app.get("/api/obsidian/note")
async def obsidian_read_note(path: str):
    """Read the raw markdown content of a note by its vault-relative path."""
    from backend.obsidian import read_note
    content = await read_note(path)
    if content is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Note not found: {path}")
    return {"path": path, "content": content}

@app.delete("/api/obsidian/note")
async def obsidian_delete_note(path: str):
    """Delete a note in the Obsidian vault by path and remove from Qdrant RAG."""
    from backend.obsidian import delete_note
    from backend.rag import delete_document
    import hashlib
    
    ok = await delete_note(path)
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Failed to delete note: {path}")
        
    # Also delete from Qdrant RAG
    doc_id = "obsidian_" + hashlib.sha1(path.encode()).hexdigest()
    delete_document(doc_id)
    
    return {"status": "success", "message": f"Note {path} deleted successfully."}

# ── Commercial clients: клиенты, услуги, подключения агентов, счета, платежи ──
# The revenue side of the house. Deliberately separate from the agent billing
# block above: that one answers "what does running this agent cost us", this one
# answers "what does this client pay us". Both stay queryable side by side so a
# gross-margin view can subtract one from the other later.
#
# Every route here is permission-checked (backend/permissions.py) rather than
# relying on the session alone, because the clients module is the first part of
# the dashboard where "logged in" and "allowed to see the money" are different
# questions.

class CurrencySettingsRequest(BaseModel):
    displayCurrency: Optional[str] = None
    rates: Optional[Dict[str, Any]] = None


class ClientRequest(BaseModel):
    # Free-form on purpose: the create drawer submits a client plus optional
    # nested contact / service / billing / agent in one payload, and
    # backend/clients.py owns the validation of each nested shape.
    model_config = {"extra": "allow"}


class ClientServiceRequest(BaseModel):
    model_config = {"extra": "allow"}


class CountryTaxRateRequest(BaseModel):
    taxRatePercent: Any


class AgentConnectionRequest(BaseModel):
    agentId: str
    clientServiceId: Optional[str] = None
    connectionType: str = "INTERNAL"
    channel: str = ""


class ClientInvoiceRequest(BaseModel):
    clientId: str
    amount: Any
    sourceCurrency: str = "USD"
    clientServiceId: Optional[str] = None
    invoiceDate: Optional[str] = None
    dueDate: Optional[str] = None
    billingPeriod: Optional[str] = None
    status: str = "PLANNED"
    comment: str = ""


class ClientPaymentRequest(BaseModel):
    clientId: str
    amount: Any
    currency: str = "USD"
    invoiceId: Optional[str] = None
    paymentDate: Optional[str] = None
    paymentMethod: str = ""
    reference: str = ""
    comment: str = ""


class DealRequest(BaseModel):
    # Free-form like ClientRequest: backend/deals.py owns field validation, and
    # the convert-to-client action forwards a client-shaped payload through the
    # very same "extra: allow" pattern ClientRequest uses.
    model_config = {"extra": "allow"}


class ClientTaskRequest(BaseModel):
    model_config = {"extra": "allow"}


class ClientNoteRequest(BaseModel):
    body: str
    author: str = ""
    pinned: bool = False


class ClientNoteUpdateRequest(BaseModel):
    body: Optional[str] = None
    pinned: Optional[bool] = None


class CalendarEventRequest(BaseModel):
    title: str
    date: str
    time: str = "10:00"
    endTime: Optional[str] = None
    durationMinutes: Optional[int] = None
    description: str = ""
    eventType: str = "OTHER"
    location: str = ""


class CalendarEventUpdateRequest(BaseModel):
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    endTime: Optional[str] = None
    durationMinutes: Optional[int] = None
    description: Optional[str] = None
    eventType: Optional[str] = None
    location: Optional[str] = None


def _client_error(exc: Exception) -> HTTPException:
    """Maps the modules' domain errors onto status codes. Bad input is 400 and
    a missing row is 404 — neither is a server fault, and returning 500 for them
    would bury a typo in a stack trace."""
    from backend.currency import CurrencyError
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"Не найдено: {exc.args[0] if exc.args else ''}")
    if isinstance(exc, (CurrencyError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Clients API failure")
    return HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


def _strip_financials(payload: Any) -> Any:
    """Removes every money-bearing field from a client payload (§63).

    Applied server-side, after the query, for roles without
    `clients.financials.view`: the response must not carry the number at all,
    since a frontend that merely hides it still shipped it to the browser.
    """
    money_keys = {
        "amount", "primaryAmountUsd", "primaryAmountDisplay", "paymentStatus",
        "sourceAmount", "normalizedUsdAmount", "displayAmount", "outstandingAmount",
        "paidAmount", "exchangeRateSnapshot",
    }
    if isinstance(payload, list):
        return [_strip_financials(item) for item in payload]
    if isinstance(payload, dict):
        return {
            key: _strip_financials(value)
            for key, value in payload.items()
            if key not in money_keys
        }
    return payload


# ── currency settings ────────────────────────────────────────────────────────

@app.get("/api/settings/currency")
async def get_currency_settings_api(request: Request):
    """Base currency, current rates and the display currency in one payload —
    the frontend caches this and never fetches a rate per table row."""
    from backend import currency, permissions
    permissions.require(request, permissions.SETTINGS_CURRENCY_VIEW)
    return await asyncio.to_thread(currency.get_settings)


@app.patch("/api/settings/currency")
async def update_currency_settings_api(payload: CurrencySettingsRequest, request: Request):
    from backend import currency, permissions
    actor = permissions.require(request, permissions.SETTINGS_CURRENCY_EDIT)
    try:
        return await asyncio.to_thread(
            currency.update_settings,
            display_currency=payload.displayCurrency,
            rates=payload.rates,
            actor_id=actor,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/settings/currency/history")
async def currency_rate_history_api(request: Request, currency_code: Optional[str] = None, limit: int = 100):
    from backend import currency, permissions
    permissions.require(request, permissions.SETTINGS_CURRENCY_VIEW)
    try:
        return await asyncio.to_thread(currency.rate_history, currency_code, limit)
    except Exception as exc:
        raise _client_error(exc)


# ── clients ──────────────────────────────────────────────────────────────────

@app.get("/api/clients/dashboard")
async def clients_dashboard_api(request: Request, displayCurrency: Optional[str] = None):
    """KPI row. Returns each figure in both USD and the display currency, so the
    frontend renders numbers instead of computing them."""
    from backend import client_billing, permissions
    role = permissions.require(request, permissions.CLIENTS_VIEW)
    if not permissions.can_view_financials(role):
        raise HTTPException(status_code=403, detail="Недостаточно прав: требуется «clients.financials.view»")
    try:
        return await asyncio.to_thread(client_billing.dashboard, displayCurrency)
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/clients")
async def list_clients_api(
    request: Request,
    search: str = "",
    status: Optional[str] = None,
    serviceType: Optional[str] = None,
    serviceId: Optional[str] = None,
    agentStatus: Optional[str] = None,
    agentId: Optional[str] = None,
    billingFrom: Optional[str] = None,
    billingTo: Optional[str] = None,
    paymentStatus: Optional[str] = None,
    responsibleUserId: Optional[str] = None,
    includeArchived: bool = False,
    page: int = 1,
    limit: int = 50,
    sort: str = "name",
    order: str = "asc",
    displayCurrency: Optional[str] = None,
):
    from backend import clients, permissions
    role = permissions.require(request, permissions.CLIENTS_VIEW)
    try:
        result = await asyncio.to_thread(
            clients.list_clients,
            search=search, status=status, service_type=serviceType, service_id=serviceId,
            agent_status=agentStatus, agent_id=agentId, billing_from=billingFrom,
            billing_to=billingTo, payment_status=paymentStatus,
            responsible_user_id=responsibleUserId, include_archived=includeArchived,
            page=page, limit=limit, sort=sort, order=order, display_currency=displayCurrency,
        )
    except Exception as exc:
        raise _client_error(exc)
    if not permissions.can_view_financials(role):
        result["items"] = _strip_financials(result["items"])
    return result


@app.post("/api/clients")
async def create_client_api(payload: ClientRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_CREATE)
    try:
        return await asyncio.to_thread(clients.create_client, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/clients/agents")
async def clients_agent_picker_api(request: Request):
    """Existing VEXA agents available to link. This module never creates
    agents — the agent admin remains the only place they are born."""
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_AGENTS_VIEW)
    return await asyncio.to_thread(clients.list_agents_for_picker)


@app.get("/api/clients/services")
async def list_services_api(request: Request):
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_VIEW)
    return await asyncio.to_thread(clients.list_services)


@app.post("/api/clients/services")
async def create_service_api(payload: ClientServiceRequest, request: Request):
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_CREATE)
    data = payload.model_dump()
    try:
        return await asyncio.to_thread(
            clients.create_service, data.get("name", ""), data.get("description", ""),
            bool(data.get("isActive", True)),
        )
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/clients/countries")
async def list_country_settings_api(request: Request):
    """One row per country in use, each with its tax-deduction %, for the
    «Клиенты» list's country group headers. Financial-view gated like the
    dashboard KPIs — a tax rate is a revenue figure, not a locale label."""
    from backend import clients, permissions
    role = permissions.require(request, permissions.CLIENTS_VIEW)
    if not permissions.can_view_financials(role):
        raise HTTPException(status_code=403, detail="Недостаточно прав: требуется «clients.financials.view»")
    return await asyncio.to_thread(clients.list_country_settings)


@app.patch("/api/clients/countries/{country}")
async def set_country_tax_rate_api(country: str, payload: CountryTaxRateRequest, request: Request):
    from backend import clients, permissions
    role = permissions.require(request, permissions.CLIENTS_EDIT)
    if not permissions.can_view_financials(role):
        raise HTTPException(status_code=403, detail="Недостаточно прав: требуется «clients.financials.view»")
    try:
        return await asyncio.to_thread(clients.set_country_tax_rate, country, payload.taxRatePercent)
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/clients/{client_id}")
async def get_client_api(client_id: str, request: Request):
    from backend import clients, permissions
    role = permissions.require(request, permissions.CLIENTS_VIEW)
    client = await asyncio.to_thread(clients.get_client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    if not permissions.can_view_financials(role):
        return _strip_financials(client)
    return client


@app.patch("/api/clients/{client_id}")
async def update_client_api(client_id: str, payload: ClientRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_EDIT)
    try:
        return await asyncio.to_thread(clients.update_client, client_id, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.delete("/api/clients/{client_id}")
async def archive_client_api(client_id: str, request: Request):
    """Archive, not delete: invoices and payments point at this row and are
    financial records that must not be orphaned."""
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_ARCHIVE)
    try:
        return await asyncio.to_thread(clients.archive_client, client_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/clients/{client_id}/activity")
async def client_activity_api(client_id: str, request: Request, limit: int = 100):
    from backend import client_activity, permissions
    permissions.require(request, permissions.CLIENTS_VIEW)
    return await asyncio.to_thread(client_activity.list_events, client_id=client_id, limit=limit)


@app.get("/api/clients/{client_id}/billing")
async def client_billing_summary_api(client_id: str, request: Request, displayCurrency: Optional[str] = None):
    from backend import client_billing, permissions
    permissions.require(
        request, permissions.CLIENTS_BILLING_VIEW, permissions.CLIENTS_FINANCIALS_VIEW
    )
    try:
        return await asyncio.to_thread(client_billing.client_billing_summary, client_id, displayCurrency)
    except Exception as exc:
        raise _client_error(exc)


# ── client services ──────────────────────────────────────────────────────────

@app.post("/api/clients/{client_id}/services")
async def add_client_service_api(client_id: str, payload: ClientServiceRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_EDIT, permissions.CLIENTS_BILLING_EDIT)
    try:
        return await asyncio.to_thread(clients.add_client_service, client_id, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.patch("/api/client-services/{client_service_id}")
async def update_client_service_api(client_service_id: str, payload: ClientServiceRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_EDIT, permissions.CLIENTS_BILLING_EDIT)
    try:
        return await asyncio.to_thread(
            clients.update_client_service, client_service_id, payload.model_dump(), actor
        )
    except Exception as exc:
        raise _client_error(exc)


# ── contacts ─────────────────────────────────────────────────────────────────

@app.get("/api/clients/{client_id}/contacts")
async def list_contacts_api(client_id: str, request: Request):
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_VIEW)
    return await asyncio.to_thread(clients.list_contacts, client_id)


@app.post("/api/clients/{client_id}/contacts")
async def add_contact_api(client_id: str, payload: ClientServiceRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_EDIT)
    try:
        return await asyncio.to_thread(clients.add_contact, client_id, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.patch("/api/client-contacts/{contact_id}")
async def update_contact_api(contact_id: str, payload: ClientServiceRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_EDIT)
    try:
        return await asyncio.to_thread(clients.update_contact, contact_id, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.delete("/api/client-contacts/{contact_id}")
async def delete_contact_api(contact_id: str, request: Request):
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_EDIT)
    removed = await asyncio.to_thread(clients.delete_contact, contact_id)
    return {"status": "ok" if removed else "not_found"}


# ── agent connections ────────────────────────────────────────────────────────

@app.get("/api/client-agent-connections")
async def list_agent_connections_api(
    request: Request, clientId: Optional[str] = None, agentId: Optional[str] = None,
    status: Optional[str] = None,
):
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_AGENTS_VIEW)
    try:
        return await asyncio.to_thread(
            clients.list_connections, client_id=clientId, agent_id=agentId, status=status
        )
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/clients/{client_id}/agents")
async def list_client_agents_api(client_id: str, request: Request):
    from backend import clients, permissions
    permissions.require(request, permissions.CLIENTS_AGENTS_VIEW)
    return await asyncio.to_thread(clients.list_connections, client_id=client_id)


@app.post("/api/clients/{client_id}/agents")
async def connect_client_agent_api(client_id: str, payload: AgentConnectionRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_AGENTS_CONNECT)
    try:
        return await asyncio.to_thread(
            clients.connect_agent, client_id, payload.agentId,
            client_service_id=payload.clientServiceId, connection_type=payload.connectionType,
            channel=payload.channel, actor_id=actor,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.patch("/api/client-agent-connections/{connection_id}")
async def update_agent_connection_api(connection_id: str, payload: ClientServiceRequest, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_AGENTS_CONNECT)
    try:
        return await asyncio.to_thread(
            clients.update_connection, connection_id, payload.model_dump(), actor
        )
    except Exception as exc:
        raise _client_error(exc)


@app.delete("/api/client-agent-connections/{connection_id}")
async def delete_agent_connection_api(connection_id: str, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_AGENTS_DISCONNECT)
    removed = await asyncio.to_thread(clients.delete_connection, connection_id, actor)
    return {"status": "ok" if removed else "not_found"}


@app.post("/api/client-agent-connections/{connection_id}/check")
async def health_check_agent_connection_api(connection_id: str, request: Request):
    """Re-probes the linked agent and re-derives the status. The backend stays
    the only thing that decides whether a connection is healthy (§29)."""
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_AGENTS_VIEW)
    try:
        return await asyncio.to_thread(clients.health_check, connection_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-agent-connections/{connection_id}/reconnect")
async def reconnect_agent_connection_api(connection_id: str, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_AGENTS_CONNECT)
    try:
        return await asyncio.to_thread(clients.reconnect_agent, connection_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-agent-connections/{connection_id}/disconnect")
async def disconnect_agent_connection_api(connection_id: str, request: Request):
    from backend import clients, permissions
    actor = permissions.require(request, permissions.CLIENTS_AGENTS_DISCONNECT)
    try:
        return await asyncio.to_thread(clients.disconnect_agent, connection_id, actor)
    except Exception as exc:
        raise _client_error(exc)


# ── client invoices ──────────────────────────────────────────────────────────

@app.get("/api/client-invoices")
async def list_client_invoices_api(
    request: Request, clientId: Optional[str] = None, clientServiceId: Optional[str] = None,
    status: Optional[str] = None, dateFrom: Optional[str] = None, dateTo: Optional[str] = None,
    limit: int = 200,
):
    from backend import client_billing, permissions
    permissions.require(
        request, permissions.CLIENTS_INVOICES_VIEW, permissions.CLIENTS_FINANCIALS_VIEW
    )
    try:
        return await asyncio.to_thread(
            client_billing.list_invoices, client_id=clientId, client_service_id=clientServiceId,
            status=status, date_from=dateFrom, date_to=dateTo, limit=limit,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-invoices")
async def create_client_invoice_api(payload: ClientInvoiceRequest, request: Request):
    from backend import client_billing, permissions
    actor = permissions.require(request, permissions.CLIENTS_INVOICES_CREATE)
    try:
        return await asyncio.to_thread(
            client_billing.create_invoice,
            client_id=payload.clientId, amount=payload.amount,
            source_currency=payload.sourceCurrency, client_service_id=payload.clientServiceId,
            invoice_date=payload.invoiceDate, due_date=payload.dueDate,
            billing_period=payload.billingPeriod, status=payload.status,
            comment=payload.comment, actor_id=actor,
        )
    except client_billing.DuplicateInvoice as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/client-invoices/{invoice_id}")
async def get_client_invoice_api(invoice_id: str, request: Request):
    from backend import client_billing, permissions
    permissions.require(
        request, permissions.CLIENTS_INVOICES_VIEW, permissions.CLIENTS_FINANCIALS_VIEW
    )
    invoice = await asyncio.to_thread(client_billing.get_invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    return invoice


@app.patch("/api/client-invoices/{invoice_id}")
async def update_client_invoice_api(invoice_id: str, payload: ClientServiceRequest, request: Request):
    from backend import client_billing, permissions
    actor = permissions.require(request, permissions.CLIENTS_INVOICES_EDIT)
    try:
        return await asyncio.to_thread(
            client_billing.update_invoice, invoice_id, payload.model_dump(), actor
        )
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-invoices/{invoice_id}/issue")
async def issue_client_invoice_api(invoice_id: str, request: Request):
    from backend import client_billing, permissions
    actor = permissions.require(request, permissions.CLIENTS_INVOICES_EDIT)
    try:
        return await asyncio.to_thread(client_billing.issue_invoice, invoice_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-invoices/{invoice_id}/mark-paid")
async def mark_client_invoice_paid_api(invoice_id: str, payload: ClientServiceRequest, request: Request):
    from backend import client_billing, permissions
    actor = permissions.require(request, permissions.CLIENTS_INVOICES_MARK_PAID)
    data = payload.model_dump()
    try:
        return await asyncio.to_thread(
            client_billing.mark_invoice_paid, invoice_id,
            payment_method=data.get("paymentMethod", ""), reference=data.get("reference", ""),
            comment=data.get("comment", ""), actor_id=actor,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-invoices/{invoice_id}/cancel")
async def cancel_client_invoice_api(invoice_id: str, request: Request):
    from backend import client_billing, permissions
    actor = permissions.require(request, permissions.CLIENTS_INVOICES_EDIT)
    try:
        return await asyncio.to_thread(client_billing.cancel_invoice, invoice_id, actor)
    except Exception as exc:
        raise _client_error(exc)


# ── client payments ──────────────────────────────────────────────────────────

@app.get("/api/client-payments")
async def list_client_payments_api(
    request: Request, clientId: Optional[str] = None, invoiceId: Optional[str] = None,
    dateFrom: Optional[str] = None, dateTo: Optional[str] = None, limit: int = 200,
):
    from backend import client_billing, permissions
    permissions.require(
        request, permissions.CLIENTS_PAYMENTS_VIEW, permissions.CLIENTS_FINANCIALS_VIEW
    )
    try:
        return await asyncio.to_thread(
            client_billing.list_payments, client_id=clientId, invoice_id=invoiceId,
            date_from=dateFrom, date_to=dateTo, limit=limit,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-payments")
async def create_client_payment_api(payload: ClientPaymentRequest, request: Request):
    from backend import client_billing, permissions
    actor = permissions.require(request, permissions.CLIENTS_PAYMENTS_CREATE)
    try:
        return await asyncio.to_thread(
            client_billing.create_payment,
            client_id=payload.clientId, amount=payload.amount,
            payment_currency=payload.currency, invoice_id=payload.invoiceId,
            payment_date=payload.paymentDate, payment_method=payload.paymentMethod,
            reference=payload.reference, comment=payload.comment, actor_id=actor,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/client-payments/{payment_id}")
async def get_client_payment_api(payment_id: str, request: Request):
    from backend import client_billing, permissions
    permissions.require(
        request, permissions.CLIENTS_PAYMENTS_VIEW, permissions.CLIENTS_FINANCIALS_VIEW
    )
    payment = await asyncio.to_thread(client_billing.get_payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    return payment


@app.post("/api/client-billing/run-scheduler")
async def run_client_billing_scheduler_api(request: Request):
    """Manual trigger for the recurring-invoice sweep. Safe to press twice: the
    sweep is idempotent per (услуга, период)."""
    from backend import client_billing, permissions
    permissions.require(request, permissions.CLIENTS_INVOICES_CREATE)
    generated = await asyncio.to_thread(client_billing.generate_due_invoices)
    aged = await asyncio.to_thread(client_billing.sweep_overdue_invoices)
    return {**generated, "markedOverdue": aged}


# ── CRM: sales pipeline (deals) ──────────────────────────────────────────────

@app.get("/api/deals/pipeline")
async def deals_pipeline_api(request: Request, displayCurrency: Optional[str] = None):
    from backend import deals, permissions
    permissions.require(request, permissions.CLIENTS_DEALS_VIEW)
    try:
        return await asyncio.to_thread(deals.pipeline_summary, displayCurrency)
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/deals")
async def list_deals_api(
    request: Request, stage: Optional[str] = None, search: str = "",
    responsibleUserId: Optional[str] = None, includeClosed: bool = True,
):
    from backend import deals, permissions
    permissions.require(request, permissions.CLIENTS_DEALS_VIEW)
    try:
        return await asyncio.to_thread(
            deals.list_deals, stage=stage, search=search,
            responsible_user_id=responsibleUserId, include_closed=includeClosed,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/deals")
async def create_deal_api(payload: DealRequest, request: Request):
    from backend import deals, permissions
    actor = permissions.require(request, permissions.CLIENTS_DEALS_EDIT)
    try:
        return await asyncio.to_thread(deals.create_deal, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/deals/{deal_id}")
async def get_deal_api(deal_id: str, request: Request):
    from backend import deals, permissions
    permissions.require(request, permissions.CLIENTS_DEALS_VIEW)
    deal = await asyncio.to_thread(deals.get_deal, deal_id)
    if not deal:
        raise HTTPException(status_code=404, detail="Сделка не найдена")
    return deal


@app.patch("/api/deals/{deal_id}")
async def update_deal_api(deal_id: str, payload: DealRequest, request: Request):
    """One route for every deal edit — a plain field change or a kanban card
    dragged to a new stage (see backend/deals.py:update_deal)."""
    from backend import deals, permissions
    actor = permissions.require(request, permissions.CLIENTS_DEALS_EDIT)
    try:
        return await asyncio.to_thread(deals.update_deal, deal_id, payload.model_dump(exclude_unset=True), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.delete("/api/deals/{deal_id}")
async def delete_deal_api(deal_id: str, request: Request):
    from backend import deals, permissions
    permissions.require(request, permissions.CLIENTS_DEALS_EDIT)
    try:
        await asyncio.to_thread(deals.delete_deal, deal_id)
    except Exception as exc:
        raise _client_error(exc)
    return {"status": "success", "id": deal_id}


@app.post("/api/deals/{deal_id}/convert-to-client")
async def convert_deal_api(deal_id: str, payload: ClientRequest, request: Request):
    """Turns a won deal into a real client — the payload is exactly what the
    "Добавить клиента" drawer would submit (name, contact, service, agent),
    pre-filled by the frontend from the deal. Requires both deals-edit (this
    changes the deal) and clients-create (it creates a client record)."""
    from backend import deals, permissions
    actor = permissions.require(request, permissions.CLIENTS_DEALS_EDIT, permissions.CLIENTS_CREATE)
    try:
        deal, client = await asyncio.to_thread(deals.convert_to_client, deal_id, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)
    return {"deal": deal, "client": client}


# ── CRM: tasks and follow-ups ────────────────────────────────────────────────

@app.get("/api/client-tasks")
async def list_client_tasks_api(
    request: Request, clientId: Optional[str] = None, dealId: Optional[str] = None,
    status: Optional[str] = None, overdueOnly: bool = False,
):
    from backend import client_tasks, permissions
    permissions.require(request, permissions.CLIENTS_TASKS_VIEW)
    try:
        return await asyncio.to_thread(
            client_tasks.list_tasks, client_id=clientId, deal_id=dealId, status=status, overdue_only=overdueOnly,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.get("/api/client-tasks/stats")
async def client_task_stats_api(request: Request):
    from backend import client_tasks, permissions
    permissions.require(request, permissions.CLIENTS_TASKS_VIEW)
    return await asyncio.to_thread(client_tasks.task_stats)


@app.post("/api/client-tasks")
async def create_client_task_api(payload: ClientTaskRequest, request: Request):
    from backend import client_tasks, permissions
    actor = permissions.require(request, permissions.CLIENTS_TASKS_EDIT)
    try:
        return await asyncio.to_thread(client_tasks.create_task, payload.model_dump(), actor)
    except Exception as exc:
        raise _client_error(exc)


@app.patch("/api/client-tasks/{task_id}")
async def update_client_task_api(task_id: str, payload: ClientTaskRequest, request: Request):
    from backend import client_tasks, permissions
    actor = permissions.require(request, permissions.CLIENTS_TASKS_EDIT)
    try:
        return await asyncio.to_thread(
            client_tasks.update_task, task_id, payload.model_dump(exclude_unset=True), actor,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-tasks/{task_id}/complete")
async def complete_client_task_api(task_id: str, request: Request):
    from backend import client_tasks, permissions
    actor = permissions.require(request, permissions.CLIENTS_TASKS_EDIT)
    try:
        return await asyncio.to_thread(client_tasks.complete_task, task_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-tasks/{task_id}/reopen")
async def reopen_client_task_api(task_id: str, request: Request):
    from backend import client_tasks, permissions
    actor = permissions.require(request, permissions.CLIENTS_TASKS_EDIT)
    try:
        return await asyncio.to_thread(client_tasks.reopen_task, task_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.post("/api/client-tasks/{task_id}/cancel")
async def cancel_client_task_api(task_id: str, request: Request):
    from backend import client_tasks, permissions
    actor = permissions.require(request, permissions.CLIENTS_TASKS_EDIT)
    try:
        return await asyncio.to_thread(client_tasks.cancel_task, task_id, actor)
    except Exception as exc:
        raise _client_error(exc)


@app.delete("/api/client-tasks/{task_id}")
async def delete_client_task_api(task_id: str, request: Request):
    from backend import client_tasks, permissions
    permissions.require(request, permissions.CLIENTS_TASKS_EDIT)
    try:
        await asyncio.to_thread(client_tasks.delete_task, task_id)
    except Exception as exc:
        raise _client_error(exc)
    return {"status": "success", "id": task_id}


# ── CRM: client notes ────────────────────────────────────────────────────────

@app.get("/api/clients/{client_id}/notes")
async def list_client_notes_api(client_id: str, request: Request):
    from backend import client_notes, permissions
    permissions.require(request, permissions.CLIENTS_NOTES_VIEW)
    return await asyncio.to_thread(client_notes.list_notes, client_id)


@app.post("/api/clients/{client_id}/notes")
async def create_client_note_api(client_id: str, payload: ClientNoteRequest, request: Request):
    from backend import client_notes, permissions
    actor = permissions.require(request, permissions.CLIENTS_NOTES_EDIT)
    try:
        return await asyncio.to_thread(
            client_notes.create_note, client_id, payload.body, payload.author, payload.pinned, actor,
        )
    except Exception as exc:
        raise _client_error(exc)


@app.patch("/api/client-notes/{note_id}")
async def update_client_note_api(note_id: str, payload: ClientNoteUpdateRequest, request: Request):
    from backend import client_notes, permissions
    permissions.require(request, permissions.CLIENTS_NOTES_EDIT)
    try:
        return await asyncio.to_thread(client_notes.update_note, note_id, payload.body, payload.pinned)
    except Exception as exc:
        raise _client_error(exc)


@app.delete("/api/client-notes/{note_id}")
async def delete_client_note_api(note_id: str, request: Request):
    from backend import client_notes, permissions
    permissions.require(request, permissions.CLIENTS_NOTES_EDIT)
    try:
        await asyncio.to_thread(client_notes.delete_note, note_id)
    except Exception as exc:
        raise _client_error(exc)
    return {"status": "success", "id": note_id}


# ── CRM: Календарь (Google Calendar) ────────────────────────────────────────
# Thin wrappers over backend/tools.py's *dict*-returning internals
# (`_list_events`/`_create_event`/`_update_event`/`_delete_event`) — the same
# functions the chat agent's `get_calendar_events`/`add_calendar_event`/
# `update_calendar_event`/`delete_calendar_event` tools call, so a dashboard
# action and an agent action against the same event are one code path, not
# two that could drift. Deliberately outside control_plane's R3/R4 approval
# gate: that gate exists for the *agent* deciding on its own to touch the
# owner's calendar, not for the owner's own dashboard button click.

def _calendar_error(exc_or_message) -> HTTPException:
    """404 means Google itself said the event is gone — its errors carry an
    HTTP status/reason in English ("404", "Not Found"). Everything else
    (missing credentials, an expired token, a network failure) is a 502: the
    integration is not currently usable, which is a different situation from
    "that specific event doesn't exist". The two must not be conflated — an
    earlier version of this matched the *Russian* "не найден" substring,
    which the credentials-missing message also contains ("...не найден."),
    so a disconnected calendar was misreported as a 404."""
    detail = str(exc_or_message)
    if "404" in detail or "Not Found" in detail:
        return HTTPException(status_code=404, detail=detail)
    return HTTPException(status_code=502, detail=detail)


@app.get("/api/calendar/status")
async def calendar_status_api(request: Request):
    from backend import permissions, tools
    permissions.require(request, permissions.CLIENTS_CALENDAR_VIEW)
    return await asyncio.to_thread(tools.calendar_connection_status)


@app.get("/api/calendar/events")
async def list_calendar_events_api(request: Request, dateFrom: str, dateTo: str):
    """`dateFrom`/`dateTo` are YYYY-MM-DD, inclusive, interpreted in the same
    Asia/Jerusalem local day boundaries the chat tools use — a month grid on
    screen and "сегодня" in chat must agree on where a day starts."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from backend import permissions, tools
    permissions.require(request, permissions.CLIENTS_CALENDAR_VIEW)
    try:
        tz = ZoneInfo(tools.CALENDAR_LOCAL_TZ_NAME)
        start = datetime.strptime(dateFrom, "%Y-%m-%d").replace(tzinfo=tz)
        end = datetime.strptime(dateTo, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {exc}")
    events, err = await asyncio.to_thread(tools._list_events, start, end, 250)
    if err:
        raise _calendar_error(err)
    return events


@app.post("/api/calendar/events")
async def create_calendar_event_api(payload: CalendarEventRequest, request: Request):
    from backend import permissions, tools
    permissions.require(request, permissions.CLIENTS_CALENDAR_EDIT)
    event_type = payload.eventType if payload.eventType in tools.CALENDAR_EVENT_TYPES else "OTHER"
    event, err = await asyncio.to_thread(
        tools._create_event, payload.title, payload.date, payload.time, payload.durationMinutes,
        payload.endTime, payload.description, event_type, payload.location,
    )
    if err:
        raise _calendar_error(err)
    return event


@app.patch("/api/calendar/events/{event_id}")
async def update_calendar_event_api(event_id: str, payload: CalendarEventUpdateRequest, request: Request):
    from backend import permissions, tools
    permissions.require(request, permissions.CLIENTS_CALENDAR_EDIT)
    event_type = payload.eventType if payload.eventType in tools.CALENDAR_EVENT_TYPES else None
    event, err = await asyncio.to_thread(
        tools._update_event, event_id, payload.title, payload.date, payload.time, payload.endTime,
        payload.durationMinutes, payload.description, event_type, payload.location,
    )
    if err:
        raise _calendar_error(err)
    return event


@app.delete("/api/calendar/events/{event_id}")
async def delete_calendar_event_api(event_id: str, request: Request):
    from backend import permissions, tools
    permissions.require(request, permissions.CLIENTS_CALENDAR_EDIT)
    ok, err = await asyncio.to_thread(tools._delete_event, event_id)
    if not ok:
        raise _calendar_error(err)
    return {"status": "success", "id": event_id}


def _resolve_turn_agent(raw: Any) -> Optional[str]:
    """The @mentioned agent for one chat turn, or None for the session's own."""
    agent_id = str(raw or "").strip()[:128]
    if not agent_id:
        return None
    if agent_id == "vexa":
        return agent_id
    from backend.database import get_subagent
    try:
        return agent_id if get_subagent(agent_id) else None
    except Exception:
        logger.exception("Could not resolve @mentioned agent %s", agent_id)
        return None


@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    from backend.auth_http import websocket_session, watch_websocket_session
    offered_protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    token, session = await websocket_session(websocket)
    # Prefer the addressed/resumable protocol. A client still on hermes-v1 (or a
    # stale cached bundle offering neither) keeps working on the legacy firehose.
    from backend.websocket_manager import PROTOCOL_V1, PROTOCOL_V2
    if PROTOCOL_V2 in offered_protocols:
        response_protocol = PROTOCOL_V2
    elif PROTOCOL_V1 in offered_protocols:
        response_protocol = PROTOCOL_V1
    else:
        response_protocol = None
    from backend.auth import validate_session
    if not session:
        await websocket.accept(subprotocol=response_protocol)
        await websocket.close(code=1008)
        return

    owner = session["role"] == "owner"
    await manager.connect(websocket, subprotocol=response_protocol, owner=owner)
    session_watch = asyncio.create_task(watch_websocket_session(websocket, token))
    try:
        # Send initial setup on connection
        from backend.database import get_chat_history
        from backend.activity_logger import ACTIVITY_LOGS
        history = get_chat_history("dashboard") if owner else []
        await websocket.send_json({
            "type": "init",
            "config": agent_instance.get_runtime_config() if owner else {},
            "logs": DECISION_LOGS[:20] if owner else [],
            "history": history,
            "activity_logs": ACTIVITY_LOGS if owner else []
        })
        
        while True:
            # Maintain connection alive, process incoming messages if any
            data = await websocket.receive_text()
            if not await asyncio.to_thread(validate_session, token, touch=False):
                await websocket.close(code=1008)
                break
            # Declared out here so the error handler below can always close out
            # the run the browser is waiting on, even for a frame that failed
            # before these were read.
            chat_id, run_id = "dashboard", ""
            try:
                import json
                msg = json.loads(data)
                frame_type = msg.get("type")
                if frame_type in ("subscribe", "unsubscribe"):
                    raw_ids = msg.get("chat_ids")
                    if not isinstance(raw_ids, list):
                        raw_ids = [msg.get("chat_id")]
                    chat_ids = [str(item)[:128] for item in raw_ids if item]
                    if frame_type == "subscribe" and any(c.startswith("inbox") for c in chat_ids):
                        # The inbox feed is the only subscription behind a
                        # permission: a role without inbox.view must not even
                        # learn which conversations changed.
                        from backend import permissions as _permissions
                        from backend.auth import role_for_session
                        if not _permissions.has_permission(role_for_session(token), _permissions.INBOX_VIEW):
                            chat_ids = [c for c in chat_ids if not c.startswith("inbox")]
                    if frame_type == "subscribe":
                        current = manager.subscribe(websocket, chat_ids)
                    else:
                        current = manager.unsubscribe(websocket, chat_ids)
                    await websocket.send_json({
                        "type": "subscribed",
                        "chat_ids": sorted(current),
                    })
                    continue
                if frame_type == "resume":
                    # A reconnecting client asks for the tail of each run it was
                    # watching. Replay is per run so one expired cursor does not
                    # invalidate the others.
                    requests = msg.get("runs")
                    if not isinstance(requests, list):
                        requests = [{"run_id": msg.get("run_id"), "after_seq": msg.get("after_seq", 0)}]
                    for item in requests[:16]:
                        if not isinstance(item, dict):
                            continue
                        resume_run_id = str(item.get("run_id") or "")[:128]
                        if not resume_run_id:
                            continue
                        try:
                            after_seq = int(item.get("after_seq") or 0)
                        except (TypeError, ValueError):
                            after_seq = 0
                        await manager.resume(websocket, resume_run_id, after_seq)
                    continue
                if frame_type == "chat_message":
                    if not owner:
                        await websocket.send_json({"type": "error", "error": "Owner access required"})
                        continue
                    await asyncio.to_thread(validate_session, token, touch=True)
                    user_text = msg.get("content", "")
                    chat_id = msg.get("chat_id", "dashboard")
                    run_id = str(msg.get("run_id") or "")[:128]
                    # Per-turn model choice from the chat UI's model picker. The
                    # frame carries the catalogue id and its source, never an
                    # endpoint — resolve_chat_model looks the pair up in the live
                    # catalogue and returns None (i.e. "use the default") for
                    # anything it doesn't recognise.
                    from backend import chat_models
                    llm_override = await chat_models.resolve_chat_model(
                        str(msg.get("model_source") or ""),
                        str(msg.get("model") or "")[:256],
                        local_base_url=agent_instance.local_base_url,
                        api_key=agent_instance.api_key,
                    )
                    # Per-turn agent from the chat UI's @mention: this one turn
                    # goes to that agent inside the same chat. Only a known agent
                    # id is honoured; anything else falls back to the session's
                    # own agent rather than failing the turn.
                    agent_override = _resolve_turn_agent(msg.get("agent_id"))
                    # Write-ahead: the turn is durable BEFORE any provider work
                    # starts, so a crash mid-turn leaves a record instead of a guess.
                    from backend import run_journal
                    run_journal.record_submitted(
                        run_id, chat_id,
                        user_text=user_text,
                        initiator="web",
                        model=(llm_override or {}).get("model") or agent_instance.model,
                        provider=(llm_override or {}).get("provider") or agent_instance.provider,
                    )
                    # The sender is by definition watching this chat; subscribing
                    # here closes the race between connecting and the first frame.
                    manager.subscribe(websocket, [chat_id])
                    await manager.send_chat_event(chat_id, {
                        "type": "chat_message",
                        "role": "user",
                        "content": user_text,
                    })
                    # Call agent
                    current_task = asyncio.current_task()
                    if run_id and current_task:
                        ACTIVE_CHAT_RUNS[run_id] = current_task
                    if run_id:
                        run_journal.record_started(
                            run_id,
                            model=(llm_override or {}).get("model") or agent_instance.model,
                            provider=(llm_override or {}).get("provider") or agent_instance.provider,
                        )
                        await manager.send_run_event(run_id, chat_id, {
                            "type": "chat_stream_start",
                            "model": (llm_override or {}).get("model") or agent_instance.model,
                            "provider": (llm_override or {}).get("provider") or agent_instance.provider,
                        })

                    async def emit_stream_chunk(chunk):
                        if not run_id:
                            return
                        await manager.send_run_event(run_id, chat_id, {
                            "type": "chat_stream_chunk",
                            "content": chunk.get("content") or "",
                            "thinking": chunk.get("thinking") or "",
                            "tool_calls": chunk.get("tool_calls") or [],
                            "done": bool(chunk.get("done")),
                        })
                    try:
                        response_text = await agent_instance.respond(
                            user_text,
                            session_id=chat_id,
                            stream_callback=emit_stream_chunk,
                            llm_override=llm_override,
                            agent_override=agent_override,
                        )
                    except asyncio.CancelledError:
                        await manager.send_run_event(run_id, chat_id, {
                            "type": "chat_cancelled",
                        })
                        manager.close_run(run_id)
                        run_journal.record_finished(run_id, run_journal.STATUS_CANCELLED)
                        # The WebSocket handler itself is the registered task. Clear
                        # cancellation so this connection can keep serving messages.
                        if current_task and hasattr(current_task, "uncancel"):
                            current_task.uncancel()
                        continue
                    finally:
                        if run_id:
                            ACTIVE_CHAT_RUNS.pop(run_id, None)
                    cost_usd = agent_instance.last_costs.get(chat_id, 0.0)
                    suppress_tts = agent_instance.check_and_clear_suppress_tts(chat_id)
                    
                    saved_ids = agent_instance.last_saved_ids.get(chat_id, {})
                    user_msg_id = saved_ids.get("user")
                    assistant_msg_id = saved_ids.get("assistant")

                    # Secret-free run diagnostics for the UI tech-details panel.
                    run_meta = agent_instance.last_run_metadata.get(chat_id, {}) or {}
                    meta = {
                        "status": run_meta.get("status", "success"),
                        "model": run_meta.get("model"),
                        "provider": run_meta.get("provider"),
                        "finish_reason": run_meta.get("finish_reason"),
                        "request_id": run_meta.get("request_id"),
                        "latency_ms": run_meta.get("latency_ms"),
                        # LLM-only time, so the UI can show a generation speed that
                        # isn't diluted by however long a tool took, and the pure
                        # decode time the token rate is actually computed from.
                        "generation_ms": run_meta.get("generation_ms"),
                        "decode_ms": run_meta.get("decode_ms"),
                        "prompt_ms": run_meta.get("prompt_ms"),
                        "input_tokens": run_meta.get("input_tokens"),
                        "output_tokens": run_meta.get("output_tokens"),
                        "tool_iterations": run_meta.get("tool_iterations"),
                        # Set only for an @mention turn: who answered it.
                        "agent_id": run_meta.get("agent_id"),
                        "agent_name": run_meta.get("agent_name"),
                    }

                    # The terminal frame carries the FULL answer, so replaying it
                    # repairs a client whose delta chunks had a gap.
                    await manager.send_run_event(run_id, chat_id, {
                        "type": "chat_message",
                        "role": "assistant",
                        "content": response_text,
                        "cost_usd": cost_usd,
                        "suppress_tts": suppress_tts,
                        "id": assistant_msg_id,
                        "meta": meta,
                    })
                    if run_id:
                        await manager.send_run_event(run_id, chat_id, {
                            "type": "chat_stream_end",
                            "message_id": assistant_msg_id,
                            "meta": meta,
                        })
                        # Keep the buffer replayable for a short TTL: a client that
                        # reconnects AFTER the answer finished still needs it.
                        manager.close_run(run_id)
                        # Named apart from meta["status"] one line down — same word,
                        # entirely different thing.
                        stream_status = manager.run_status(run_id) or {}
                        run_journal.record_finished(
                            run_id,
                            run_journal.STATUS_ERROR if meta.get("status") == "error"
                            else run_journal.STATUS_COMPLETED,
                            meta=meta,
                            cost_usd=cost_usd,
                            error_code=None if meta.get("status") != "error" else "agent_error",
                            last_seq=stream_status.get("last_seq", 0),
                        )
                    
                    # Broadcast user message ID update
                    if user_msg_id:
                        await manager.send_chat_event(chat_id, {
                            "type": "user_message_id_update",
                            "content": user_text,
                            "id": user_msg_id
                        })
                    # Broadcast updated logs
                    await manager.broadcast({
                        "type": "logs_update",
                        "logs": DECISION_LOGS[:20]
                    })
            except Exception as e:
                logger.error(f"Error processing websocket frame: {e}")
                # The composer only stops spinning on a frame carrying this
                # run_id: a handler that dies silently leaves the UI generating
                # forever, which is how an UnboundLocalError in the decision log
                # managed to look like "the agent never answered".
                if run_id:
                    try:
                        await manager.send_run_event(run_id, chat_id, {
                            "type": "chat_message",
                            "role": "assistant",
                            "content": "Простите, Альберт. Сбой при обработке ответа — подробности в логах сервера.",
                            "meta": {"status": "error"},
                        })
                        await manager.send_run_event(run_id, chat_id, {
                            "type": "chat_stream_end",
                            "meta": {"status": "error"},
                        })
                        manager.close_run(run_id)
                        from backend import run_journal as _run_journal
                        _run_journal.record_finished(
                            run_id, _run_journal.STATUS_ERROR, error_code="handler_exception"
                        )
                    except Exception:
                        logger.exception("Could not notify the UI about a failed chat frame")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.exception("WebSocket connection error")
        manager.disconnect(websocket)
    finally:
        session_watch.cancel()
        await asyncio.gather(session_watch, return_exceptions=True)
        manager.disconnect(websocket)
