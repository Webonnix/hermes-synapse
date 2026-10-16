"""External voice providers and the voice targets built on them.

A *provider* is a remote endpoint with credentials and a set of capabilities:

``tts``          text → audio
``stt``          audio → text
``voices``       a catalogue of voice ids
``voice_agent``  a remote conversational agent that answers a text turn itself

A *target* is what conversation routing (backend/inbox_routing.py) can point a
conversation at: one provider, one capability mode, a voice or remote agent id, and a
failure policy. Routing decides *whether* a turn goes to a target; the adapter here
owns the network call and nothing else.

Nothing in the core knows a vendor. Three adapters ship:

* ``openai_compatible`` — ``/audio/speech`` + ``/audio/transcriptions``. Covers OpenAI
  itself, and this deployment's own 9Router sidecar, which exposes exactly those.
* ``elevenlabs`` — its REST API (voices, text-to-speech, speech-to-text).
* ``voice_agent_http`` — a small documented JSON contract (``GET /health``,
  ``POST /respond``) for a self-hosted or third-party voice agent.

Security posture, deliberately written down
───────────────────────────────────────────
* Credentials never touch SQLite: they live in Valkey, like provider_governance's API
  keys, and the API only ever reports ``has_secret``.
* ``base_url`` passes the shared SSRF/HTTPS validator (mcp_governance._validate_url):
  HTTPS for public hosts; plain HTTP only for private/loopback/compose-service hosts,
  because self-hosted voice (the XTTS sidecar, 9Router) is the main use. Link-local,
  multicast and unspecified addresses are refused — and re-checked *after DNS
  resolution* on every call, so a public name pointing at 169.254.169.254 is refused
  too. Redirects are never followed.
* Every call has a connect and a read timeout. Retries apply only to idempotent GETs.
* Logs carry provider id, operation, status code and latency — never headers, keys,
  request bodies or audio.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("hermes.voice_providers")

CAPABILITIES = ("tts", "stt", "voices", "voice_agent")
KINDS: Dict[str, Dict[str, Any]] = {
    "openai_compatible": {"label": "OpenAI-совместимый (OpenAI, 9Router)", "capabilities": ("tts", "stt", "voices")},
    "elevenlabs": {"label": "ElevenLabs", "capabilities": ("tts", "stt", "voices")},
    "voice_agent_http": {"label": "Voice agent (HTTP JSON)", "capabilities": ("voice_agent", "voices")},
}
TARGET_MODES = ("voice_agent", "tts")
FAILURE_POLICIES = ("fallback_internal", "fail_closed", "human_takeover")
DEFAULT_FAILURE_POLICY = "fallback_internal"

_SECRET_KEY_HINTS = ("key", "secret", "token", "password", "authorization", "bearer", "credential")
# Names are display-only (never a path, a header or a key), so any printable text
# fits — «Голос „Nova“» included; only control characters and length are refused.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _valid_name(value: str) -> bool:
    return 1 <= len(value) <= 64 and not _CONTROL.search(value)
MAX_TTS_CHARS = 5000
MAX_AUDIO_BYTES = 25 * 1024 * 1024

_schema_ready_for: Optional[str] = None
_schema_lock = threading.Lock()


class VoiceProviderError(RuntimeError):
    """A provider call that could not be completed. Carries a message that is safe
    to show an operator: no URL query, no header, no body."""


# ── results ─────────────────────────────────────────────────────────────────
@dataclass
class ProviderHealth:
    ok: bool
    latency_ms: int = 0
    status_code: Optional[int] = None
    error: str = ""
    checked_at: str = ""


@dataclass
class VoiceInfo:
    id: str
    name: str
    language: str = ""


@dataclass
class VoiceResult:
    ok: bool
    audio: bytes = b""
    mime: str = ""
    latency_ms: int = 0
    error: str = ""


@dataclass
class TranscriptResult:
    ok: bool
    text: str = ""
    latency_ms: int = 0
    error: str = ""


@dataclass
class AgentReply:
    ok: bool
    text: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ── storage ─────────────────────────────────────────────────────────────────
def _db_path() -> str:
    from backend import database

    return database.DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def reset_schema_cache() -> None:
    global _schema_ready_for
    _schema_ready_for = None


def _init_schema() -> None:
    global _schema_ready_for
    path = _db_path()
    if _schema_ready_for == path:
        return
    with _schema_lock:
        if _schema_ready_for == path:
            return
        with _connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS voice_providers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    config TEXT NOT NULL DEFAULT '{}',
                    timeout_seconds REAL NOT NULL DEFAULT 20,
                    max_retries INTEGER NOT NULL DEFAULT 1,
                    last_health TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_targets (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    capability_mode TEXT NOT NULL,
                    voice_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    failure_policy TEXT NOT NULL DEFAULT 'fallback_internal',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_voice_targets_provider ON voice_targets (provider_id);
                CREATE TABLE IF NOT EXISTS voice_webhook_events (
                    provider_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (provider_id, event_id)
                );
                """
            )
        _schema_ready_for = path


def _secret_key(provider_id: str) -> str:
    return f"voice_provider_secret:{provider_id}"


def _webhook_secret_key(provider_id: str) -> str:
    return f"voice_provider_webhook_secret:{provider_id}"


def _get_secret(key: str) -> Optional[str]:
    try:
        from backend import valkey_client

        return valkey_client.get_value(key)
    except Exception:
        logger.warning("Secret store unavailable while reading %s", key.split(":", 1)[0])
        return None


def _set_secret(key: str, value: str) -> None:
    from backend import valkey_client

    valkey_client.set_value(key, value)


def _delete_secret(key: str) -> None:
    try:
        from backend import valkey_client

        valkey_client.delete_value(key)
    except Exception:
        logger.warning("Secret store unavailable while deleting %s", key.split(":", 1)[0])


def _json(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _public_provider(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["capabilities"] = _json(item.get("capabilities"), [])
    item["config"] = _json(item.get("config"), {})
    item["last_health"] = _json(item.get("last_health"), {})
    item["has_secret"] = bool(_get_secret(_secret_key(item["id"])))
    item["has_webhook_secret"] = bool(_get_secret(_webhook_secret_key(item["id"])))
    item["kind_label"] = KINDS.get(item["kind"], {}).get("label", item["kind"])
    item["webhook_path"] = f"/api/voice/webhook/{item['id']}"
    return item


# ── validation ──────────────────────────────────────────────────────────────
def validate_base_url(value: str) -> str:
    from backend.mcp_governance import _validate_url

    url = (value or "").strip().rstrip("/")
    if not url:
        raise ValueError("base_url обязателен")
    parsed = urlparse(url)
    if parsed.query or parsed.fragment:
        raise ValueError("base_url не должен содержать query или fragment")
    return _validate_url(url, "base_url")


def _validate_config(config: Any) -> Dict[str, Any]:
    if config in (None, ""):
        return {}
    if not isinstance(config, dict):
        raise ValueError("config должен быть объектом")
    if len(json.dumps(config)) > 4000:
        raise ValueError("config слишком большой")
    for key in config:
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            # A secret pasted into config would be stored in plain SQLite and
            # returned to the browser. The one place for it is the api_key field.
            raise ValueError(f"Поле «{key}» похоже на секрет — укажите его в api_key, а не в config")
    return config


def _validate_provider_payload(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(current or {})
    merged.update({k: v for k, v in payload.items() if v is not None})
    name = str(merged.get("name") or "").strip()
    if not _valid_name(name):
        raise ValueError("Имя провайдера: от 1 до 64 символов без управляющих символов")
    kind = str(merged.get("kind") or "")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {tuple(KINDS)}")
    supported = KINDS[kind]["capabilities"]
    capabilities = merged.get("capabilities")
    if capabilities in (None, []):
        capabilities = list(supported)
    if not isinstance(capabilities, list) or any(cap not in supported for cap in capabilities):
        raise ValueError(f"Провайдер «{kind}» поддерживает только {supported}")
    try:
        timeout = float(merged.get("timeout_seconds") or 20)
        retries = int(merged.get("max_retries") if merged.get("max_retries") is not None else 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds и max_retries должны быть числами") from exc
    if not 1 <= timeout <= 120:
        raise ValueError("timeout_seconds: от 1 до 120")
    if not 0 <= retries <= 3:
        raise ValueError("max_retries: от 0 до 3")
    return {
        "name": name,
        "kind": kind,
        "base_url": validate_base_url(str(merged.get("base_url") or "")),
        "enabled": bool(merged.get("enabled", True)),
        "capabilities": sorted(set(capabilities)),
        "config": _validate_config(merged.get("config")),
        "timeout_seconds": timeout,
        "max_retries": retries,
    }


# ── providers CRUD ──────────────────────────────────────────────────────────
def list_providers() -> List[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM voice_providers ORDER BY created_at DESC").fetchall()
    return [_public_provider(row) for row in rows]


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM voice_providers WHERE id = ?", (provider_id,)).fetchone()
    return _public_provider(row) if row else None


def create_provider(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = _validate_provider_payload(payload)
    _init_schema()
    provider_id = f"vp-{uuid.uuid4().hex[:12]}"
    now = _now()
    api_key = str(payload.get("api_key") or "").strip()
    if api_key:
        _set_secret(_secret_key(provider_id), api_key)
    webhook_secret = str(payload.get("webhook_secret") or "").strip()
    if webhook_secret:
        if len(webhook_secret) < 16:
            raise ValueError("webhook_secret должен быть не короче 16 символов")
        _set_secret(_webhook_secret_key(provider_id), webhook_secret)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO voice_providers
                (id, name, kind, base_url, enabled, capabilities, config, timeout_seconds, max_retries,
                 last_health, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (provider_id, fields["name"], fields["kind"], fields["base_url"], int(fields["enabled"]),
             json.dumps(fields["capabilities"]), json.dumps(fields["config"], ensure_ascii=False),
             fields["timeout_seconds"], fields["max_retries"], now, now),
        )
    return get_provider(provider_id)  # type: ignore[return-value]


def update_provider(provider_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    current = get_provider(provider_id)
    if current is None:
        raise KeyError(provider_id)
    fields = _validate_provider_payload(payload, current)
    api_key = payload.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        _set_secret(_secret_key(provider_id), api_key.strip())
    elif payload.get("clear_api_key"):
        _delete_secret(_secret_key(provider_id))
    webhook_secret = payload.get("webhook_secret")
    if isinstance(webhook_secret, str) and webhook_secret.strip():
        if len(webhook_secret.strip()) < 16:
            raise ValueError("webhook_secret должен быть не короче 16 символов")
        _set_secret(_webhook_secret_key(provider_id), webhook_secret.strip())
    with _connect() as connection:
        connection.execute(
            """
            UPDATE voice_providers SET name = ?, kind = ?, base_url = ?, enabled = ?, capabilities = ?, config = ?,
                   timeout_seconds = ?, max_retries = ?, updated_at = ?
             WHERE id = ?
            """,
            (fields["name"], fields["kind"], fields["base_url"], int(fields["enabled"]),
             json.dumps(fields["capabilities"]), json.dumps(fields["config"], ensure_ascii=False),
             fields["timeout_seconds"], fields["max_retries"], _now(), provider_id),
        )
    return get_provider(provider_id)  # type: ignore[return-value]


def generate_webhook_secret(provider_id: str) -> str:
    """Returns a new webhook secret exactly once; afterwards only has_webhook_secret."""
    if get_provider(provider_id) is None:
        raise KeyError(provider_id)
    value = secrets.token_urlsafe(32)
    _set_secret(_webhook_secret_key(provider_id), value)
    return value


def delete_provider(provider_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        deleted = connection.execute("DELETE FROM voice_providers WHERE id = ?", (provider_id,)).rowcount > 0
        connection.execute("DELETE FROM voice_targets WHERE provider_id = ?", (provider_id,))
    if deleted:
        _delete_secret(_secret_key(provider_id))
        _delete_secret(_webhook_secret_key(provider_id))
    return deleted


def _store_health(provider_id: str, health: ProviderHealth) -> None:
    try:
        with _connect() as connection:
            connection.execute(
                "UPDATE voice_providers SET last_health = ? WHERE id = ?",
                (json.dumps(asdict(health)), provider_id),
            )
    except Exception:
        logger.exception("Could not store health for voice provider %s", provider_id)


# ── targets CRUD ────────────────────────────────────────────────────────────
def _public_target(row: sqlite3.Row, providers: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    provider = (providers or {}).get(item["provider_id"]) if providers is not None else get_provider(item["provider_id"])
    item["provider_name"] = (provider or {}).get("name", "")
    item["provider_kind"] = (provider or {}).get("kind", "")
    item["provider_enabled"] = bool((provider or {}).get("enabled"))
    item["health"] = (provider or {}).get("last_health") or {}
    item["available"] = bool(item["enabled"] and provider and provider.get("enabled"))
    return item


def list_targets() -> List[Dict[str, Any]]:
    _init_schema()
    providers = {p["id"]: p for p in list_providers()}
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM voice_targets ORDER BY created_at DESC").fetchall()
    return [_public_target(row, providers) for row in rows]


def get_target(target_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM voice_targets WHERE id = ?", (target_id,)).fetchone()
    return _public_target(row) if row else None


def _validate_target_payload(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(current or {})
    merged.update({k: v for k, v in payload.items() if v is not None})
    provider = get_provider(str(merged.get("provider_id") or ""))
    if provider is None:
        raise ValueError("Провайдер не найден")
    mode = str(merged.get("capability_mode") or "")
    if mode not in TARGET_MODES:
        raise ValueError(f"capability_mode must be one of {TARGET_MODES}")
    if mode not in provider["capabilities"]:
        raise ValueError(f"Провайдер «{provider['name']}» не умеет «{mode}»")
    name = str(merged.get("name") or "").strip()
    if not _valid_name(name):
        raise ValueError("Имя цели: от 1 до 64 символов без управляющих символов")
    policy = str(merged.get("failure_policy") or DEFAULT_FAILURE_POLICY)
    if policy not in FAILURE_POLICIES:
        raise ValueError(f"failure_policy must be one of {FAILURE_POLICIES}")
    voice_id = str(merged.get("voice_id") or "").strip()[:128]
    agent_id = str(merged.get("agent_id") or "").strip()[:128]
    if mode == "tts" and not voice_id:
        raise ValueError("Для режима tts нужен voice_id")
    return {
        "provider_id": provider["id"], "name": name, "capability_mode": mode, "voice_id": voice_id,
        "agent_id": agent_id, "failure_policy": policy, "enabled": bool(merged.get("enabled", True)),
    }


def create_target(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = _validate_target_payload(payload)
    target_id = f"vt-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO voice_targets (id, provider_id, name, capability_mode, voice_id, agent_id, failure_policy,
                                       enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (target_id, fields["provider_id"], fields["name"], fields["capability_mode"], fields["voice_id"],
             fields["agent_id"], fields["failure_policy"], int(fields["enabled"]), now, now),
        )
    return get_target(target_id)  # type: ignore[return-value]


def update_target(target_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    current = get_target(target_id)
    if current is None:
        raise KeyError(target_id)
    fields = _validate_target_payload(payload, current)
    with _connect() as connection:
        connection.execute(
            """
            UPDATE voice_targets SET provider_id = ?, name = ?, capability_mode = ?, voice_id = ?, agent_id = ?,
                   failure_policy = ?, enabled = ?, updated_at = ?
             WHERE id = ?
            """,
            (fields["provider_id"], fields["name"], fields["capability_mode"], fields["voice_id"], fields["agent_id"],
             fields["failure_policy"], int(fields["enabled"]), _now(), target_id),
        )
    return get_target(target_id)  # type: ignore[return-value]


def delete_target(target_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        return connection.execute("DELETE FROM voice_targets WHERE id = ?", (target_id,)).rowcount > 0


# ── HTTP adapters ───────────────────────────────────────────────────────────
_BLOCKED_REASON = "адрес провайдера разрешается в запрещённый диапазон (link-local/multicast)"


def _address_blocked(host: str) -> bool:
    """Re-validates the resolved addresses on every call. A literal check at save
    time cannot see what a DNS name points to *now*."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # unresolvable: the HTTP client fails on its own, nothing to protect
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
            return True
    return False


class _HttpAdapter:
    def __init__(self, provider: Dict[str, Any], secret: Optional[str]):
        self.provider = provider
        self.secret = secret or ""
        self.base_url = provider["base_url"].rstrip("/")
        self.config = provider.get("config") or {}
        self.timeout = float(provider.get("timeout_seconds") or 20)
        self.max_retries = int(provider.get("max_retries") or 0)

    def _headers(self) -> Dict[str, str]:
        return {}

    def supports_instructions(self) -> bool:
        return False

    async def _request(self, method: str, path: str, *, idempotent: bool, **kwargs: Any):
        import httpx

        host = urlparse(self.base_url).hostname or ""
        if await asyncio.to_thread(_address_blocked, host):
            raise VoiceProviderError(_BLOCKED_REASON)
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        attempts = 1 + (self.max_retries if idempotent else 0)
        last_error = "нет ответа"
        timeout = httpx.Timeout(self.timeout, connect=min(5.0, self.timeout))
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                    response = await client.request(method, self.base_url + path, headers=headers, **kwargs)
            except httpx.TimeoutException:
                last_error = "таймаут"
                logger.warning("Voice provider %s %s %s: timeout", self.provider["id"], method, path)
                continue
            except httpx.HTTPError as exc:
                last_error = f"сетевая ошибка ({type(exc).__name__})"
                logger.warning("Voice provider %s %s %s: %s", self.provider["id"], method, path, type(exc).__name__)
                continue
            latency = int((time.monotonic() - started) * 1000)
            logger.info("Voice provider %s %s %s -> %s in %dms", self.provider["id"], method, path, response.status_code, latency)
            if 300 <= response.status_code < 400:
                raise VoiceProviderError("провайдер ответил редиректом — редиректы не выполняются")
            if response.status_code >= 500 and attempt < attempts - 1:
                last_error = f"HTTP {response.status_code}"
                continue
            return response
        raise VoiceProviderError(last_error)

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        try:
            response = await self._request("GET", self._health_path(), idempotent=True)
        except VoiceProviderError as exc:
            return ProviderHealth(ok=False, latency_ms=self._ms(started), error=str(exc), checked_at=_now())
        ok = 200 <= response.status_code < 300
        return ProviderHealth(
            ok=ok, latency_ms=self._ms(started), status_code=response.status_code,
            error="" if ok else f"HTTP {response.status_code}", checked_at=_now(),
        )

    def _health_path(self) -> str:
        return "/health"

    async def list_voices(self) -> List[VoiceInfo]:
        return []

    async def synthesize(self, *, text: str, voice_id: str, options: Optional[Dict[str, Any]] = None) -> VoiceResult:
        return VoiceResult(ok=False, error="провайдер не поддерживает tts")

    async def transcribe(self, *, audio: bytes, mime: str, options: Optional[Dict[str, Any]] = None) -> TranscriptResult:
        return TranscriptResult(ok=False, error="провайдер не поддерживает stt")

    async def respond(self, *, conversation_id: str, text: str, instructions: str = "",
                      agent_id: str = "", context: Optional[Dict[str, Any]] = None) -> AgentReply:
        return AgentReply(ok=False, error="провайдер не поддерживает voice_agent")


def _voice_list(items: Any, id_key: str, name_key: str, language_key: str = "") -> List[VoiceInfo]:
    """Tolerates whatever a provider sends: a malformed catalogue becomes an empty or
    shorter list, never an exception that would take the settings page down."""
    out: List[VoiceInfo] = []
    if not isinstance(items, list):
        return out
    for item in items[:500]:
        if not isinstance(item, dict):
            continue
        voice_id = item.get(id_key)
        if not isinstance(voice_id, (str, int)) or not str(voice_id).strip():
            continue
        name = item.get(name_key)
        language = item.get(language_key) if language_key else ""
        out.append(VoiceInfo(
            id=str(voice_id)[:128], name=str(name if isinstance(name, (str, int)) else voice_id)[:128],
            language=str(language)[:32] if isinstance(language, str) else "",
        ))
    return out


class OpenAICompatibleAdapter(_HttpAdapter):
    DEFAULT_VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}

    def _health_path(self) -> str:
        return "/models"

    async def list_voices(self) -> List[VoiceInfo]:
        configured = self.config.get("voices")
        if isinstance(configured, list) and configured:
            return [VoiceInfo(id=str(v)[:128], name=str(v)[:128]) for v in configured if isinstance(v, (str, int))]
        return [VoiceInfo(id=v, name=v) for v in self.DEFAULT_VOICES]

    async def synthesize(self, *, text: str, voice_id: str, options: Optional[Dict[str, Any]] = None) -> VoiceResult:
        started = time.monotonic()
        body = {
            "model": str(self.config.get("tts_model") or "tts-1"),
            "input": (text or "")[:MAX_TTS_CHARS],
            "voice": voice_id,
            "response_format": "mp3",
        }
        try:
            response = await self._request("POST", "/audio/speech", idempotent=False, json=body)
        except VoiceProviderError as exc:
            return VoiceResult(ok=False, latency_ms=self._ms(started), error=str(exc))
        if response.status_code != 200 or not response.content:
            return VoiceResult(ok=False, latency_ms=self._ms(started), error=f"HTTP {response.status_code}")
        if len(response.content) > MAX_AUDIO_BYTES:
            return VoiceResult(ok=False, latency_ms=self._ms(started), error="аудио слишком большое")
        mime = (response.headers.get("content-type") or "audio/mpeg").split(";")[0]
        return VoiceResult(ok=True, audio=response.content, mime=mime, latency_ms=self._ms(started))

    async def transcribe(self, *, audio: bytes, mime: str, options: Optional[Dict[str, Any]] = None) -> TranscriptResult:
        started = time.monotonic()
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            return TranscriptResult(ok=False, error="пустое или слишком большое аудио")
        try:
            response = await self._request(
                "POST", "/audio/transcriptions", idempotent=False,
                data={"model": str(self.config.get("stt_model") or "whisper-1")},
                files={"file": ("audio", audio, mime or "application/octet-stream")},
            )
        except VoiceProviderError as exc:
            return TranscriptResult(ok=False, latency_ms=self._ms(started), error=str(exc))
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        text = payload.get("text") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(text, str):
            return TranscriptResult(ok=False, latency_ms=self._ms(started), error=f"HTTP {response.status_code}")
        return TranscriptResult(ok=True, text=text, latency_ms=self._ms(started))


class ElevenLabsAdapter(_HttpAdapter):
    def _headers(self) -> Dict[str, str]:
        return {"xi-api-key": self.secret} if self.secret else {}

    def _health_path(self) -> str:
        return "/v1/user"

    async def list_voices(self) -> List[VoiceInfo]:
        try:
            response = await self._request("GET", "/v1/voices", idempotent=True)
            payload = response.json() if response.status_code == 200 else {}
        except (VoiceProviderError, ValueError):
            return []
        items = payload.get("voices") if isinstance(payload, dict) else None
        voices = _voice_list(items, "voice_id", "name")
        for voice, raw in zip(voices, items or []):
            labels = raw.get("labels") if isinstance(raw, dict) else None
            if isinstance(labels, dict) and isinstance(labels.get("language"), str):
                voice.language = labels["language"][:32]
        return voices

    async def synthesize(self, *, text: str, voice_id: str, options: Optional[Dict[str, Any]] = None) -> VoiceResult:
        started = time.monotonic()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", voice_id or ""):
            return VoiceResult(ok=False, error="некорректный voice_id")
        try:
            response = await self._request(
                "POST", f"/v1/text-to-speech/{voice_id}", idempotent=False,
                headers={"Accept": "audio/mpeg"},
                json={"text": (text or "")[:MAX_TTS_CHARS],
                      "model_id": str(self.config.get("tts_model") or "eleven_multilingual_v2")},
            )
        except VoiceProviderError as exc:
            return VoiceResult(ok=False, latency_ms=self._ms(started), error=str(exc))
        if response.status_code != 200 or not response.content:
            return VoiceResult(ok=False, latency_ms=self._ms(started), error=f"HTTP {response.status_code}")
        if len(response.content) > MAX_AUDIO_BYTES:
            return VoiceResult(ok=False, latency_ms=self._ms(started), error="аудио слишком большое")
        return VoiceResult(ok=True, audio=response.content, mime="audio/mpeg", latency_ms=self._ms(started))

    async def transcribe(self, *, audio: bytes, mime: str, options: Optional[Dict[str, Any]] = None) -> TranscriptResult:
        started = time.monotonic()
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            return TranscriptResult(ok=False, error="пустое или слишком большое аудио")
        try:
            response = await self._request(
                "POST", "/v1/speech-to-text", idempotent=False,
                data={"model_id": str(self.config.get("stt_model") or "scribe_v1")},
                files={"file": ("audio", audio, mime or "application/octet-stream")},
            )
            payload = response.json() if response.status_code == 200 else {}
        except (VoiceProviderError, ValueError) as exc:
            return TranscriptResult(ok=False, latency_ms=self._ms(started), error=str(exc) or "некорректный ответ")
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            return TranscriptResult(ok=False, latency_ms=self._ms(started), error="некорректный ответ")
        return TranscriptResult(ok=True, text=text, latency_ms=self._ms(started))


class VoiceAgentHttpAdapter(_HttpAdapter):
    """Contract for a remote voice agent:

    ``GET  {base}/health``   → 2xx when ready
    ``GET  {base}/voices``   → ``{"voices": [{"id", "name", "language"}]}`` (optional)
    ``POST {base}/respond``  ← ``{"conversation_id", "text", "agent_id", "instructions"?, "context"}``
                             → ``{"text": "..."}``

    ``instructions`` is sent only when the provider declares a separate instruction
    channel (``config.supports_instructions: true``); otherwise an operator whisper is
    never sent to it at all — there is no safe place to put it."""

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}

    def supports_instructions(self) -> bool:
        return bool(self.config.get("supports_instructions"))

    async def list_voices(self) -> List[VoiceInfo]:
        try:
            response = await self._request("GET", "/voices", idempotent=True)
            payload = response.json() if response.status_code == 200 else {}
        except (VoiceProviderError, ValueError):
            return []
        return _voice_list(payload.get("voices") if isinstance(payload, dict) else None, "id", "name", "language")

    async def respond(self, *, conversation_id: str, text: str, instructions: str = "",
                      agent_id: str = "", context: Optional[Dict[str, Any]] = None) -> AgentReply:
        started = time.monotonic()
        body: Dict[str, Any] = {
            "conversation_id": conversation_id, "text": (text or "")[:8000],
            "agent_id": agent_id, "context": context or {},
        }
        if instructions and self.supports_instructions():
            body["instructions"] = instructions[:4000]
        try:
            response = await self._request("POST", "/respond", idempotent=False, json=body)
        except VoiceProviderError as exc:
            return AgentReply(ok=False, latency_ms=self._ms(started), error=str(exc))
        try:
            payload = response.json()
        except ValueError:
            payload = None
        reply = payload.get("text") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(reply, str) or not reply.strip():
            return AgentReply(ok=False, latency_ms=self._ms(started), error=f"HTTP {response.status_code}")
        return AgentReply(ok=True, text=reply[:8000], latency_ms=self._ms(started))


_ADAPTERS = {
    "openai_compatible": OpenAICompatibleAdapter,
    "elevenlabs": ElevenLabsAdapter,
    "voice_agent_http": VoiceAgentHttpAdapter,
}


def adapter_for(provider: Dict[str, Any]) -> _HttpAdapter:
    cls = _ADAPTERS.get(provider.get("kind", ""))
    if cls is None:
        raise VoiceProviderError(f"Неизвестный тип провайдера: {provider.get('kind')}")
    return cls(provider, _get_secret(_secret_key(provider["id"])))


async def test_provider(provider_id: str) -> Dict[str, Any]:
    provider = await asyncio.to_thread(get_provider, provider_id)
    if provider is None:
        raise KeyError(provider_id)
    try:
        health = await adapter_for(provider).health()
    except Exception as exc:  # a broken adapter must still produce a result
        logger.warning("Voice provider %s health check crashed: %s", provider_id, type(exc).__name__)
        health = ProviderHealth(ok=False, error="проверка завершилась ошибкой", checked_at=_now())
    await asyncio.to_thread(_store_health, provider_id, health)
    return asdict(health)


async def list_provider_voices(provider_id: str) -> List[Dict[str, Any]]:
    provider = await asyncio.to_thread(get_provider, provider_id)
    if provider is None:
        raise KeyError(provider_id)
    if "voices" not in provider["capabilities"]:
        return []
    try:
        voices = await adapter_for(provider).list_voices()
    except Exception as exc:
        logger.warning("Voice provider %s voice list failed: %s", provider_id, type(exc).__name__)
        return []
    return [asdict(voice) for voice in voices]


# ── webhooks ────────────────────────────────────────────────────────────────
_WEBHOOK_TYPES = ("transcript", "agent_reply", "status")
MAX_WEBHOOK_BYTES = 256 * 1024


def verify_signature(secret: str, raw_body: bytes, header: str) -> bool:
    if not secret or not header:
        return False
    provided = header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


def handle_webhook(provider_id: str, raw_body: bytes, headers: Dict[str, str]) -> tuple:
    """Returns ``(status_code, body)``. An unknown provider, a missing secret and a
    bad signature all answer the same 401, so the endpoint cannot be used to probe
    which provider ids exist."""
    denied = (401, {"detail": "invalid signature"})
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        return 413, {"detail": "payload too large"}
    provider = get_provider(provider_id)
    secret = _get_secret(_webhook_secret_key(provider_id)) if provider else None
    lowered = {k.lower(): v for k, v in headers.items()}
    if not provider or not secret or not verify_signature(secret, raw_body, lowered.get("x-hermes-signature", "")):
        return denied
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 400, {"detail": "body must be JSON"}
    if not isinstance(payload, dict):
        return 400, {"detail": "body must be a JSON object"}
    event_id = str(lowered.get("x-hermes-event-id") or payload.get("event_id") or "").strip()[:128]
    if not event_id:
        return 400, {"detail": "event_id is required"}
    event_type = str(payload.get("type") or "")
    _init_schema()
    try:
        with _connect() as connection:
            connection.execute(
                "INSERT INTO voice_webhook_events (provider_id, event_id, event_type, received_at) VALUES (?, ?, ?, ?)",
                (provider_id, event_id, event_type[:32], _now()),
            )
    except sqlite3.IntegrityError:
        return 200, {"status": "duplicate"}
    if event_type in ("transcript", "agent_reply"):
        conversation_id = str(payload.get("conversation_id") or "")
        text = payload.get("text")
        if conversation_id and isinstance(text, str) and text.strip():
            from backend import inbox

            if inbox.get_conversation(conversation_id):
                label = "🎙 Расшифровка" if event_type == "transcript" else "🔊 Ответ голосового агента"
                inbox.add_system_note(
                    conversation_id, f"{label} ({provider['name']}): {text.strip()[:4000]}",
                    {"source": "voice_webhook", "provider_id": provider_id, "event_id": event_id},
                )
    return 200, {"status": "ok"}
