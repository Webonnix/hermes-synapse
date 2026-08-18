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
    code: str

class AuthLoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str | None = None
    new_username: str
    new_password: str

class ConfigUpdate(BaseModel):
    system_prompt: str | None = None
    model: str | None = None
    fast_mode: bool | None = None
    max_history_len: int | None = None
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
    ollama_base_url: str | None = None
    openai_api_base: str | None = None
    ollama_num_ctx: int | None = None
    ollama_keep_alive: str | int | None = None
    ollama_think: bool | str | None = None

class OllamaModelRequest(BaseModel):
    model: str

class OllamaPullRequest(OllamaModelRequest):
    insecure: bool = False

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
    model_provider: str = "ollama"
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


async def _ensure_ollama_model_available(*, repair: bool) -> str:
    """Validate the active local model and optionally repair stale persisted settings."""
    from backend.ollama_client import (
        OllamaClient,
        installed_model_names,
        is_ollama_provider,
        resolve_installed_model,
        select_installed_model,
    )

    if not is_ollama_provider(agent_instance.api_base, agent_instance.provider):
        return agent_instance.model

    models = await OllamaClient(
        agent_instance.ollama_base_url,
        timeout=min(agent_instance.request_timeout, 15),
    ).list_models()
    available = installed_model_names(models)
    selected = resolve_installed_model(agent_instance.model, available)
    if selected:
        return selected
    if not repair:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ollama_model_not_installed",
                "message": f"Ollama model '{agent_instance.model}' is not installed.",
                "available_models": available,
            },
        )

    selected = select_installed_model(
        agent_instance.model,
        os.getenv("LLM_MODEL", ""),
        available,
    )
    if not selected:
        raise RuntimeError("Ollama is reachable but has no installed models.")

    stale_model = agent_instance.model
    agent_instance.model = selected
    from backend.database import save_app_settings
    save_app_settings(agent_instance.get_runtime_config())
    logger.warning(
        "Repaired stale Ollama model setting: %s -> %s",
        stale_model,
        selected,
    )
    return selected


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB, Qdrant/RAG and run the Telegram bot
    from backend.database import init_db
    init_db()
    
    from backend.rag import init_rag
    init_rag()

    try:
        await _ensure_ollama_model_available(repair=True)
    except Exception as exc:
        logger.error("Ollama startup model validation failed: %s", exc)

    if os.getenv("VOICE_STT_PRELOAD", "false").lower() in {"1", "true", "yes", "on"}:
        async def _preload_voice():
            try:
                from backend.voice import preload_voice_model
                status = await asyncio.to_thread(preload_voice_model)
                logger.info("Voice recognition ready: %s", status["model"])
            except Exception as exc:
                logger.warning("Voice recognition preload failed (will retry on demand): %s", exc)

        asyncio.create_task(_preload_voice())

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

        asyncio.create_task(_index_project_memory())
    
    # Start price alert monitor background task
    from backend.price_monitor import price_monitor
    price_monitor.start()

    # Durable dev-runs worker: resumes unfinished runs after restart.
    dev_runs_worker_task = None
    if os.getenv("DEV_RUNS_WORKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}:
        from backend import dev_runs
        dev_runs_worker_task = asyncio.create_task(dev_runs.worker_loop())

    bot_app = await init_bot()

    from backend import agent_bot
    await agent_bot.manager.start_all_active()

    from backend import agent_matrix_bot
    await agent_matrix_bot.manager.start_all_active()

    from backend import agent_discord_bot
    await agent_discord_bot.manager.start_all_active()

    from backend import agent_slack_bot
    await agent_slack_bot.manager.start_all_active()

    from backend import agent_email_channel
    await agent_email_channel.manager.start_all_active()

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
    asyncio.create_task(_obsidian_startup_sync())
    
    from backend.mcp_client import init_mcp_servers, shutdown_mcp_servers
    await init_mcp_servers()

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
    asyncio.create_task(_bcm_session_scheduler_task())

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
    asyncio.create_task(_subscription_expiry_task())

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
    asyncio.create_task(_channel_health_check_task())

    yield
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
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Allow public auth routes and plots (images)
    if path in (
        "/health/live",
        "/health/ready",
        "/api/auth/request-code",
        "/api/auth/verify-code",
        "/api/auth/login",
        # Crypto payment callback: the provider can't hold a dashboard session,
        # so this one route authenticates itself with an HMAC signature over the
        # raw body instead (backend/payments.handle_webhook). It grants nothing
        # on a bad signature.
        "/api/payments/webhook",
    ) or path.startswith("/api/plots/") or path.startswith("/api/generated-images/"):
        return await call_next(request)
        
    # Apply auth only to API routes
    if not path.startswith("/api/"):
        return await call_next(request)
        
    # Check authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized: Missing or invalid token"})
        
    token = auth_header.split(" ")[1]
    if not validate_session(token):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized: Session expired or invalid"})
        
    return await call_next(request)

@app.post("/api/auth/request-code")
async def request_code():
    from backend.auth import generate_otp
    import backend.bot
    import os
    
    code = generate_otp()
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        return {"status": "error", "message": "TELEGRAM_CHAT_ID is not configured on backend."}
        
    msg = (
        f"🏛️ **Hermes Authorization Request**\n\n"
        f"Albert, an entry request to the web dashboard was detected.\n"
        f"Your one-time access code is:\n\n"
        f"`{code}`\n\n"
        f"This code is valid for 5 minutes."
    )
    
    try:
        if backend.bot.telegram_app and backend.bot.telegram_app.bot:
            await backend.bot.telegram_app.bot.send_message(
                chat_id=int(chat_id),
                text=msg,
                parse_mode="Markdown"
            )
            return {"status": "success", "message": "Code sent to Telegram."}
        else:
            logger.error("Telegram bot is not initialized.")
            return {"status": "error", "message": "Telegram bot is not initialized."}
    except Exception as e:
        logger.error(f"Failed to send auth code to Telegram: {e}")
        return {"status": "error", "message": "Failed to send code. Check server logs."}

@app.post("/api/auth/verify-code")
async def verify_code(req: AuthVerifyRequest):
    from backend.auth import verify_otp, create_session
    if verify_otp(req.code):
        token = create_session()
        return {"status": "success", "token": token}
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

    if req.username.strip() != stored_username or not verify_password(req.password, stored_hash, stored_salt):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    token = create_session()
    return {"status": "success", "token": token}

@app.post("/api/auth/change-password")
async def change_password(req: ChangePasswordRequest):
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
    return {"status": "success"}

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
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            response = await client.get(f"{agent_instance.ollama_base_url}/api/version")
            checks["ollama"] = {"ok": response.status_code == 200}
        except Exception as exc:
            checks["ollama"] = {"ok": False, "error": type(exc).__name__}

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
    {"key_name": "ROUTER_API_KEY", "label": "AI Router (9Router)", "description": "Ключ из 9Router → Dashboard → API Keys. Используется в разделе «AI Router» только для проверки доступности и списка моделей (GET /v1/models) — квоты и usage смотрите в самом 9Router. Тот же ключ можно использовать и для провайдер-биндинга агентов через Админку → Providers.", "category": "LLM"},
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
    return {"status": "success"}

@app.delete("/api/settings/api-keys/{key_name}")
async def remove_api_key(key_name: str):
    from backend import database as db
    if key_name not in _KNOWN_API_KEY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown key: {key_name}")
    db.delete_api_key(key_name)
    return {"status": "success"}

@app.get("/api/config")
async def get_config():
    return agent_instance.get_runtime_config()

@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    selected_model = update.model
    prospective_provider = update.provider if update.provider is not None else agent_instance.provider
    prospective_api_base = update.api_base if update.api_base is not None else agent_instance.api_base
    from backend.ollama_client import (
        OllamaClient,
        installed_model_names,
        is_ollama_provider,
        resolve_installed_model,
    )
    if is_ollama_provider(prospective_api_base, prospective_provider) and (
        update.model is not None
        or update.provider is not None
        or update.ollama_base_url is not None
    ):
        ollama_base_url = update.ollama_base_url or agent_instance.ollama_base_url
        try:
            available = installed_model_names(
                await OllamaClient(
                    ollama_base_url,
                    timeout=min(agent_instance.request_timeout, 15),
                ).list_models()
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ollama_unavailable",
                    "message": f"Cannot validate the local model: {exc}",
                },
            ) from exc
        requested_model = update.model or agent_instance.model
        selected_model = resolve_installed_model(requested_model, available)
        if selected_model is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ollama_model_not_installed",
                    "message": f"Ollama model '{requested_model}' is not installed.",
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
        ollama_base_url=update.ollama_base_url,
        openai_api_base=update.openai_api_base,
        ollama_num_ctx=update.ollama_num_ctx,
        ollama_keep_alive=update.ollama_keep_alive,
        ollama_think=update.ollama_think,
        fast_mode=update.fast_mode,
        max_history_len=update.max_history_len,
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
    import shutil
    import os
    uploads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    file_path = os.path.join(uploads_dir, file.filename)
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"File uploaded successfully: {file.filename}")
        
        # Broadcast upload event over WS so the UI is notified
        await manager.broadcast({
            "type": "chat_message",
            "role": "system",
            "content": f"⚙️ [Orchestrator] Dataset: Data file '{file.filename}' loaded."
        })
        
        return {"status": "success", "filename": file.filename, "filepath": file_path}
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
            model_provider = "ollama"
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

@app.post("/api/dev-runs")
async def create_dev_run_api(request: DevRunCreateRequest):
    from backend import dev_runs
    if not request.goal.strip():
        raise HTTPException(status_code=400, detail="Goal is required")
    kwargs: dict = {"assignee_agent_id": request.assignee_agent_id, "start": request.start,
                    "parent_run_id": request.parent_run_id}
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
async def approve_control_plane_task_api(task_id: str):
    from backend.control_plane import approve_task, get_task
    from backend.approval_dispatch import execute_if_ready
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

    from backend.ollama_client import OllamaClient, is_ollama_provider
    if is_ollama_provider(api_base, agent_instance.provider):
        try:
            raw_models = await OllamaClient(api_base).list_models()
            result = []
            for item in raw_models:
                model_id = item.get("name") or item.get("model")
                if model_id:
                    result.append({
                        "id": model_id,
                        "name": model_id,
                        "provider": "ollama",
                        "size": item.get("size"),
                        "digest": item.get("digest"),
                        "modified_at": item.get("modified_at"),
                        "details": item.get("details") or {},
                    })
            return result
        except Exception as exc:
            logger.warning("Failed to list Ollama models: %s", exc)
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

                # ── Format 2: Ollama native /api/tags ──────────────────────
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
                   for m in ("localhost", "127.0.0.1", "0.0.0.0", "ollama",
                             "host.docker.internal", "lmstudio", "vllm"))
    if is_local:
        # Return the currently configured model as the only available option.
        current_model = os.getenv("LLM_MODEL", "hermes-brain")
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


def _ollama_client():
    from backend.ollama_client import OllamaClient
    # Model management remains available even while the active chat provider is
    # temporarily switched to OpenRouter/OpenAI-compatible.
    return OllamaClient(agent_instance.ollama_base_url, timeout=agent_instance.request_timeout)


@app.get("/api/ollama/status")
async def get_ollama_status():
    return await _ollama_client().status()


@app.get("/api/ollama/models")
async def get_ollama_models():
    try:
        return {"models": await _ollama_client().list_models()}
    except Exception as exc:
        from backend.ollama_client import OllamaError
        if isinstance(exc, OllamaError):
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        raise


@app.get("/api/ollama/running")
async def get_ollama_running_models():
    try:
        return {"models": await _ollama_client().list_running()}
    except Exception as exc:
        from backend.ollama_client import OllamaError
        if isinstance(exc, OllamaError):
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        raise


@app.post("/api/ollama/models/show")
async def show_ollama_model(request: OllamaModelRequest):
    try:
        return await _ollama_client().show_model(request.model)
    except Exception as exc:
        from backend.ollama_client import OllamaError
        if isinstance(exc, OllamaError):
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        raise


@app.post("/api/ollama/models/pull")
async def pull_ollama_model(request: OllamaPullRequest):
    import json

    async def progress_stream():
        try:
            async for event in _ollama_client().pull_model(request.model, insecure=request.insecure):
                yield json.dumps(event, ensure_ascii=False) + "\n"
            _models_cache["data"] = None
            _models_cache["timestamp"] = 0
        except Exception as exc:
            from backend.ollama_client import OllamaError
            code = exc.code if isinstance(exc, OllamaError) else "pull_error"
            yield json.dumps({"error": str(exc), "code": code}, ensure_ascii=False) + "\n"

    return StreamingResponse(progress_stream(), media_type="application/x-ndjson")


@app.delete("/api/ollama/models/{model:path}")
async def delete_ollama_model(model: str):
    try:
        await _ollama_client().delete_model(model)
        _models_cache["data"] = None
        _models_cache["timestamp"] = 0
        return {"status": "deleted", "model": model}
    except Exception as exc:
        from backend.ollama_client import OllamaError
        if isinstance(exc, OllamaError):
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        raise


@app.post("/api/ollama/models/unload")
async def unload_ollama_model(request: OllamaModelRequest):
    try:
        await _ollama_client().unload_model(request.model)
        return {"status": "unloaded", "model": request.model}
    except Exception as exc:
        from backend.ollama_client import OllamaError
        if isinstance(exc, OllamaError):
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        raise

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
    from backend.mcp_client import mcp_clients, mcp_tool_to_server
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
        # Remove its tools from registry
        dead = [t for t, s in mcp_tool_to_server.items() if s == name]
        for t in dead:
            mcp_tool_to_server.pop(t, None)
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
    """Reachability/catalog check for the optional 9Router sidecar (Admin -> AI Router).

    Deliberately does NOT try to read quota or usage numbers: 9Router's own
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
    router_api_base = os.getenv("ROUTER_API_BASE", "http://9router:20128").rstrip("/")
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
            "error": "9Router sidecar not reachable — is the '9router' container running?",
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
    """Full 9Router model catalog for the AI Router tab's "Добавить тир" form —
    GET /api/router/stats only returns an 8-item sample_models preview for the
    diagnostics panel, this is the complete list the tier form's model picker
    needs. Catalog entries are namespaced by provider (e.g. 'kimi/kimi-k3',
    'ds/deepseek-chat') — a tier's model_override must match one of these
    verbatim, which a free-text field made easy to get wrong (a user typed
    'deepseek-chat' instead of 'ds/deepseek-chat' and the tier would have
    silently 404'd against 9Router on first use)."""
    from backend import database as db

    router_api_key = db.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")
    router_api_base = os.getenv("ROUTER_API_BASE", "http://9router:20128").rstrip("/")
    if not router_api_key:
        return {"available": False, "models": [], "error": "ROUTER_API_KEY not configured (Settings -> API Keys -> AI Router)."}

    headers = {"Authorization": f"Bearer {router_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{router_api_base}/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("AI Router model catalog fetch failed: %s", exc)
        return {"available": True, "models": [], "error": "9Router sidecar not reachable — is the '9router' container running?"}

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


class RouterSessionCredentialRequest(BaseModel):
    password: str


@app.get("/api/router/session-status")
async def router_session_status_api():
    """Whether a 9Router dashboard session is configured/pending/active —
    powers the combos/connections panels' setup state in the AI Router tab."""
    from backend.router_session import is_configured, has_pending_proposal

    return {
        "configured": is_configured(),
        "pending_task_id": has_pending_proposal(),
    }


@app.post("/api/router/session-credential")
async def router_session_credential_propose_api(body: RouterSessionCredentialRequest):
    """Stage the 9Router dashboard password behind one Telegram/dashboard
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
    """Proxies 9Router's own GET /api/combos through a backend-held dashboard
    session — this data has no Bearer-key API, only the session-cookie one.
    Degrades to {"available": false} rather than raising when no session is
    configured or 9Router can't be reached, since this whole panel is optional.
    """
    from backend.router_session import session_request, is_configured

    if not is_configured():
        return {"available": False, "combos": None, "error": "9Router dashboard session not configured."}

    resp = await session_request("GET", "/api/combos")
    if resp is None:
        return {"available": True, "combos": None, "error": "9Router session unavailable — check the password or container status."}
    if resp.status_code != 200:
        return {"available": True, "combos": None, "error": f"9Router returned HTTP {resp.status_code} for /api/combos."}
    try:
        data = resp.json()
    except ValueError:
        return {"available": True, "combos": None, "error": "9Router returned invalid JSON for /api/combos."}
    return {"available": True, "combos": data.get("combos") if isinstance(data, dict) else data, "error": None}


@app.get("/api/router/connections")
async def router_connections_api():
    """Proxies 9Router's own GET /api/providers (its connected upstream AI
    accounts — Claude subscription, GLM key, etc.) — deliberately named
    differently from this app's own GET /api/providers (agent provider
    bindings), which is an unrelated concept that happens to share a name
    with 9Router's endpoint."""
    from backend.router_session import session_request, is_configured

    if not is_configured():
        return {"available": False, "connections": None, "error": "9Router dashboard session not configured."}

    resp = await session_request("GET", "/api/providers")
    if resp is None:
        return {"available": True, "connections": None, "error": "9Router session unavailable — check the password or container status."}
    if resp.status_code != 200:
        return {"available": True, "connections": None, "error": f"9Router returned HTTP {resp.status_code} for /api/providers."}
    try:
        data = resp.json()
    except ValueError:
        return {"available": True, "connections": None, "error": "9Router returned invalid JSON for /api/providers."}
    return {"available": True, "connections": data.get("connections") if isinstance(data, dict) else data, "error": None}


@app.post("/api/router/bind-self")
async def router_bind_self_api():
    """Convenience: create a governed provider_binding pointing at the 9Router
    sidecar itself, reusing the already-configured ROUTER_API_KEY instead of
    making the user copy/paste it into the generic 'Add provider' form. This
    is the only way accounts connected inside 9Router's own dashboard (see
    /api/router/connections) become usable as a fallback-chain tier — a tier
    always routes through one provider_binding + a model_override, never
    directly at a 9Router-native connection. Still goes through the normal
    single-Telegram-approval flow, same as any other binding."""
    from backend import database as db
    from backend.provider_governance import create_binding_proposal, list_bindings

    router_api_key = db.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")
    if not router_api_key:
        raise HTTPException(status_code=400, detail="ROUTER_API_KEY not configured (Settings -> API Keys -> AI Router).")

    existing = await asyncio.to_thread(list_bindings)
    if any("9router" in (b.get("api_base") or "") for b in existing):
        raise HTTPException(status_code=409, detail="A provider binding for 9Router already exists.")

    router_api_base = os.getenv("ROUTER_API_BASE", "http://9router:20128").rstrip("/") + "/v1"
    try:
        proposal = await asyncio.to_thread(
            create_binding_proposal, "9Router", "openai_compatible", router_api_base, router_api_key,
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
    Hermes's own concept, distinct from (and not fed by) 9Router's own combos,
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
    9Router (which has no quota/usage API at all — see router_stats_api)."""
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
        return await asyncio.to_thread(create_project, project.name, project.description, project.is_active)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.put("/api/projects/{project_id}")
async def update_project_api(project_id: str, project: ProjectRequest):
    from backend.projects import update_project
    try:
        return await asyncio.to_thread(
            update_project, project_id,
            name=project.name, description=project.description, is_active=project.is_active,
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


@app.get("/api/messenger-bindings")
async def list_all_messenger_bindings_api():
    """Cross-agent, cross-platform view of every messenger binding — powers the
    central 'Каналы связи' admin page rather than each agent's own edit form."""
    from backend.agent_messenger_governance import (
        list_telegram_bindings, list_matrix_bindings, list_discord_bindings,
        list_slack_bindings, list_email_bindings,
    )
    from backend.database import get_all_subagents

    names = {a["id"]: a["name"] for a in await asyncio.to_thread(get_all_subagents)}
    telegram, matrix, discord_, slack, email_ = await asyncio.gather(
        asyncio.to_thread(list_telegram_bindings),
        asyncio.to_thread(list_matrix_bindings),
        asyncio.to_thread(list_discord_bindings),
        asyncio.to_thread(list_slack_bindings),
        asyncio.to_thread(list_email_bindings),
    )
    bindings = telegram + matrix + discord_ + slack + email_
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
        get_binding, reconnect_telegram_binding, reconnect_matrix_binding,
        reconnect_discord_binding, reconnect_slack_binding, reconnect_email_binding,
    )

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
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Binding not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        cursor.execute("SELECT session_id, title, agent_id, project_id FROM session_metadata")
        metadata_map = {r[0]: {"title": r[1], "agent_id": r[2], "project_id": r[3]} for r in cursor.fetchall()}
        conn.close()

        # Filter out subagents, and keep only "dashboard" and custom sessions
        user_sessions = [s for s in sessions if s not in subagent_ids and s != "dashboard" and not s.startswith("archive_")]

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
    clear_chat_history(chat_id)
    delete_session_title(chat_id)
    # Also clear from agent's in-memory last costs or messages if needed
    if chat_id in agent_instance.last_costs:
        agent_instance.last_costs[chat_id] = 0.0
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

@app.post("/api/history/{session_id}/fork")
async def fork_history_session(session_id: str):
    """Forks a session by duplicating its messages to a new session_id."""
    from backend.database import DB_PATH, get_session_title, save_session_title
    import sqlite3
    import time
    new_session_id = f"{session_id}_fork_{int(time.time())}"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO messages (session_id, role, content, cost_usd) 
            SELECT ?, role, content, cost_usd FROM messages WHERE session_id = ? ORDER BY id ASC
        """, (new_session_id, session_id))
        conn.commit()
        conn.close()
        
        # Fork custom title metadata
        old_title = get_session_title(session_id)
        if not old_title:
            old_title = session_id
        save_session_title(new_session_id, f"Fork of {old_title}")
        
        return {"status": "success", "new_session_id": new_session_id}
    except Exception as e:
        logger.exception(f"Failed to fork session {session_id}: {e}")
        return {"status": "error", "message": "Failed to fork session. Check server logs."}

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

@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    offered_protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    auth_protocol = next(
        (item for item in offered_protocols if item.startswith("hermes-auth.")),
        "",
    )
    token = auth_protocol.removeprefix("hermes-auth.")
    response_protocol = "hermes-v1" if "hermes-v1" in offered_protocols else None
    from backend.auth import validate_session
    if not token or not validate_session(token):
        await websocket.accept(subprotocol=response_protocol)
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, subprotocol=response_protocol)
    try:
        # Send initial setup on connection
        from backend.database import get_chat_history
        from backend.activity_logger import ACTIVITY_LOGS
        history = get_chat_history("dashboard")
        await websocket.send_json({
            "type": "init",
            "config": agent_instance.get_runtime_config(),
            "logs": DECISION_LOGS[:20],
            "history": history,
            "activity_logs": ACTIVITY_LOGS
        })
        
        while True:
            # Maintain connection alive, process incoming messages if any
            data = await websocket.receive_text()
            try:
                import json
                msg = json.loads(data)
                if msg.get("type") == "chat_message":
                    user_text = msg.get("content", "")
                    chat_id = msg.get("chat_id", "dashboard")
                    run_id = str(msg.get("run_id") or "")[:128]
                    # Broadcast user message to all dashboard connections
                    await manager.broadcast({
                        "type": "chat_message",
                        "role": "user",
                        "content": user_text,
                        "chat_id": chat_id
                    })
                    # Call agent
                    current_task = asyncio.current_task()
                    if run_id and current_task:
                        ACTIVE_CHAT_RUNS[run_id] = current_task
                    if run_id:
                        await manager.broadcast({
                            "type": "chat_stream_start",
                            "chat_id": chat_id,
                            "run_id": run_id,
                            "model": agent_instance.model,
                            "provider": agent_instance.provider,
                        })

                    async def emit_stream_chunk(chunk):
                        if not run_id:
                            return
                        await manager.broadcast({
                            "type": "chat_stream_chunk",
                            "chat_id": chat_id,
                            "run_id": run_id,
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
                        )
                    except asyncio.CancelledError:
                        await manager.broadcast({
                            "type": "chat_cancelled", "chat_id": chat_id,
                            "run_id": run_id,
                        })
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
                    }

                    # Broadcast agent response
                    await manager.broadcast({
                        "type": "chat_message",
                        "role": "assistant",
                        "content": response_text,
                        "chat_id": chat_id,
                        "cost_usd": cost_usd,
                        "suppress_tts": suppress_tts,
                        "id": assistant_msg_id,
                        "meta": meta,
                        "run_id": run_id,
                    })
                    if run_id:
                        await manager.broadcast({
                            "type": "chat_stream_end",
                            "chat_id": chat_id,
                            "run_id": run_id,
                            "message_id": assistant_msg_id,
                            "meta": meta,
                        })
                    
                    # Broadcast user message ID update
                    if user_msg_id:
                        await manager.broadcast({
                            "type": "user_message_id_update",
                            "chat_id": chat_id,
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
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.exception("WebSocket connection error")
        manager.disconnect(websocket)
