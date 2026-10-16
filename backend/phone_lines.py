"""Phone lines and calls: storage, provider accounts, numbers, signatures.

A *line* is one phone number with its provider, the agent that answers it and how it
behaves (greeting, language, transfer target, limits). A *call* is one ringing, talking
or finished call on a line; its transcript lives in the inbox conversation it points at.

Providers
─────────
``zadarma``  SIP registration through the Asterisk sidecar. Works from behind NAT
             without port forwarding, which is why it is the recommended one for this
             server. Numbers are bought in the Zadarma cabinet and entered here.
``twilio``   Programmable Voice with a bidirectional Media Stream to this backend.
             Needs a public HTTPS URL (webhooks + wss). Numbers can be searched and
             bought from the dashboard.
``telnyx``   Call Control with bidirectional media streaming, same public-URL need;
             numbers searchable and orderable from the dashboard.

Secrets (SIP password, Twilio auth token, Telnyx API key) are stored only in Valkey
(``phone_line_secret:<line>``) and never returned by the API. The Asterisk trunk file
necessarily contains the SIP password — Asterisk reads it from disk — so it is written
0600 into the state directory shared with the sidecar and nowhere else.

Buying a number spends the owner's money on the provider account: the API route calls
:func:`buy_number` only with an explicit confirmation flag from a person clicking the
button, never from an agent tool.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("hermes.phone_lines")

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "zadarma": {
        "label": "Zadarma", "recommended": True, "transport": "sip", "needs_public_url": False,
        "number_purchase": False, "cabinet_url": "https://my.zadarma.com/dirnum/",
        "note": "SIP через Asterisk на вашем сервере: работает за NAT без проброса портов, номера 100+ стран, "
                "дёшево. Номер покупается в кабинете Zadarma, сюда вводятся SIP-логин и пароль.",
        "fields": [
            {"key": "sip_login", "label": "SIP-логин (например 123456 или 123456-100)", "secret": False, "required": True},
            {"key": "sip_password", "label": "Пароль SIP", "secret": True, "required": True},
            {"key": "sip_server", "label": "SIP-сервер", "secret": False, "required": False, "default": "sip.zadarma.com"},
        ],
    },
    "twilio": {
        "label": "Twilio", "recommended": False, "transport": "media_stream", "needs_public_url": True,
        "number_purchase": True, "cabinet_url": "https://console.twilio.com/",
        "note": "Самая известная платформа, номера многих стран с поиском и покупкой прямо отсюда. Нужен "
                "публичный HTTPS-адрес для вебхуков. Номера РФ недоступны.",
        "fields": [
            {"key": "account_sid", "label": "Account SID (AC…)", "secret": False, "required": True},
            {"key": "auth_token", "label": "Auth Token", "secret": True, "required": True},
        ],
    },
    "telnyx": {
        "label": "Telnyx", "recommended": False, "transport": "media_stream", "needs_public_url": True,
        "number_purchase": True, "cabinet_url": "https://portal.telnyx.com/",
        "note": "Дешевле Twilio, поиск и заказ номеров отсюда. Нужны API-ключ, Call Control App ID, "
                "публичный ключ для проверки вебхуков и публичный HTTPS-адрес.",
        "fields": [
            {"key": "api_key", "label": "API Key (KEY…)", "secret": True, "required": True},
            {"key": "connection_id", "label": "Call Control App ID", "secret": False, "required": True},
            {"key": "public_key", "label": "Public Key (для проверки вебхуков)", "secret": False, "required": True},
        ],
    },
}

LANGUAGES = ("auto", "ru", "en")
CALL_STATUSES = ("ringing", "active", "ended", "failed", "rejected")
DEFAULT_GREETING = "Здравствуйте! Я ИИ-ассистент. Чем могу помочь?"
MAX_CONCURRENT_CALLS_DEFAULT = 4

_schema_ready_for: Optional[str] = None
_schema_lock = threading.Lock()
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{2,5})?$")
_SIP_LOGIN = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")


class PhoneProviderError(RuntimeError):
    """A provider API refused or failed. The message is safe to show."""


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
                CREATE TABLE IF NOT EXISTS phone_lines (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    number TEXT NOT NULL DEFAULT '',
                    subagent_id TEXT NOT NULL DEFAULT '',
                    greeting TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'auto',
                    owner_number TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    inbound_enabled INTEGER NOT NULL DEFAULT 1,
                    outbound_enabled INTEGER NOT NULL DEFAULT 1,
                    auto_book INTEGER NOT NULL DEFAULT 0,
                    max_call_minutes INTEGER NOT NULL DEFAULT 20,
                    config TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'new',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS phone_calls (
                    id TEXT PRIMARY KEY,
                    line_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_call_id TEXT NOT NULL DEFAULT '',
                    remote_number TEXT NOT NULL DEFAULT '',
                    local_number TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ringing',
                    purpose TEXT NOT NULL DEFAULT '',
                    client_id TEXT,
                    conversation_id TEXT,
                    started_at TEXT NOT NULL,
                    answered_at TEXT,
                    ended_at TEXT,
                    duration_s INTEGER,
                    end_reason TEXT NOT NULL DEFAULT '',
                    turns INTEGER NOT NULL DEFAULT 0,
                    interruptions INTEGER NOT NULL DEFAULT 0,
                    first_audio_ms_avg INTEGER,
                    first_audio_ms_p90 INTEGER,
                    transferred_to TEXT NOT NULL DEFAULT '',
                    degraded TEXT NOT NULL DEFAULT '[]',
                    initiated_by TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_phone_calls_line ON phone_calls (line_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_phone_calls_provider ON phone_calls (provider, provider_call_id);
                CREATE INDEX IF NOT EXISTS idx_phone_calls_conversation ON phone_calls (conversation_id);
                CREATE TABLE IF NOT EXISTS phone_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        _schema_ready_for = path


def get_setting(key: str, default: Any = None) -> Any:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT value FROM phone_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_setting(key: str, value: Any) -> None:
    _init_schema()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO phone_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), _now()),
        )


def get_settings() -> Dict[str, Any]:
    return {
        "public_base_url": get_setting("public_base_url", ""),
        "max_concurrent_calls": int(get_setting("max_concurrent_calls", MAX_CONCURRENT_CALLS_DEFAULT)),
    }


def update_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "public_base_url" in payload:
        url = str(payload.get("public_base_url") or "").strip().rstrip("/")
        if url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query:
                raise ValueError("Публичный адрес: https://домен без пути (например https://hermes.example.com)")
        set_setting("public_base_url", url)
    if "max_concurrent_calls" in payload:
        value = int(payload.get("max_concurrent_calls") or MAX_CONCURRENT_CALLS_DEFAULT)
        if not 1 <= value <= 50:
            raise ValueError("Одновременных звонков: от 1 до 50")
        set_setting("max_concurrent_calls", value)
    return get_settings()


# ── secrets ─────────────────────────────────────────────────────────────────
def _secret_key(line_id: str) -> str:
    return f"phone_line_secret:{line_id}"


def line_secrets(line_id: str) -> Dict[str, str]:
    try:
        from backend import valkey_client

        raw = valkey_client.get_value(_secret_key(line_id))
    except Exception:
        logger.warning("Secret store unavailable for phone line %s", line_id)
        return {}
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _store_secrets(line_id: str, updates: Dict[str, str]) -> None:
    if not updates:
        return
    from backend import valkey_client

    current = line_secrets(line_id)
    current.update(updates)
    valkey_client.set_value(_secret_key(line_id), json.dumps(current))


def _delete_secrets(line_id: str) -> None:
    try:
        from backend import valkey_client

        valkey_client.delete_value(_secret_key(line_id))
    except Exception:
        logger.warning("Could not delete secrets of phone line %s", line_id)


def media_signing_key() -> bytes:
    from backend import valkey_client

    key = valkey_client.get_value("phone_media_signing_key")
    if not key:
        key = secrets.token_hex(32)
        valkey_client.set_value("phone_media_signing_key", key)
    return key.encode()


def media_token(call_id: str) -> str:
    return hmac.new(media_signing_key(), f"media:{call_id}".encode(), hashlib.sha256).hexdigest()[:40]


def check_media_token(call_id: str, token: str) -> bool:
    try:
        return hmac.compare_digest(media_token(call_id), str(token or ""))
    except Exception:
        return False


# ── lines ───────────────────────────────────────────────────────────────────
def _json(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _public_line(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    for key in ("enabled", "inbound_enabled", "outbound_enabled", "auto_book"):
        item[key] = bool(item[key])
    item["config"] = _json(item.get("config"), {})
    stored = line_secrets(item["id"])
    provider = PROVIDERS.get(item["provider"], {})
    item["secrets_set"] = {
        field["key"]: bool(stored.get(field["key"])) for field in provider.get("fields", []) if field["secret"]
    }
    item["provider_label"] = provider.get("label", item["provider"])
    return item


def list_lines() -> List[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM phone_lines ORDER BY created_at ASC").fetchall()
    return [_public_line(row) for row in rows]


def get_line(line_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM phone_lines WHERE id = ?", (line_id,)).fetchone()
    return _public_line(row) if row else None


def find_line_by_number(number: str) -> Optional[Dict[str, Any]]:
    from backend.phone_audio import normalize_e164

    target = normalize_e164(number)
    if not target:
        return None
    return next((line for line in list_lines() if line["number"] == target), None)


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r", " ").strip()
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ValueError("Недопустимые символы в тексте")
    return text[:limit]


def _validate_line(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from backend.phone_audio import normalize_e164

    merged = dict(current or {})
    for key, value in (payload or {}).items():
        if value is not None:
            merged[key] = value
    provider = str(merged.get("provider") or "")
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {tuple(PROVIDERS)}")
    if current and provider != current["provider"]:
        raise ValueError("Провайдера линии нельзя сменить — создайте новую линию")
    name = _clean_text(merged.get("name"), 80)
    if not name:
        raise ValueError("Название линии обязательно")
    number = str(merged.get("number") or "").strip()
    if number:
        number = normalize_e164(number)
        if not _E164.match(number):
            raise ValueError("Номер линии в международном формате, например +972501234567")
    owner_number = str(merged.get("owner_number") or "").strip()
    if owner_number:
        owner_number = normalize_e164(owner_number)
        if not _E164.match(owner_number):
            raise ValueError("Номер для перевода в международном формате")
    language = str(merged.get("language") or "auto")
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES}")
    minutes = int(merged.get("max_call_minutes") or 20)
    if not 1 <= minutes <= 120:
        raise ValueError("Максимальная длительность звонка: от 1 до 120 минут")
    subagent_id = str(merged.get("subagent_id") or "").strip()
    if subagent_id:
        from backend.database import get_subagent
        from backend.tool_permissions import MAIN_AGENT_IDS

        if subagent_id in MAIN_AGENT_IDS:
            raise ValueError("Главный агент (Vexa) не отвечает посторонним — выберите другого агента")
        if not get_subagent(subagent_id):
            raise ValueError("Агент не найден")
    config = dict(_json(json.dumps(merged.get("config") or {}), {}))
    for field in PROVIDERS[provider]["fields"]:
        if field["secret"]:
            config.pop(field["key"], None)
            continue
        key = field["key"]
        value = str(payload.get(key) if payload.get(key) is not None else config.get(key) or field.get("default") or "").strip()
        config[key] = value
    if provider == "zadarma":
        if config.get("sip_login") and not _SIP_LOGIN.match(config["sip_login"]):
            raise ValueError("SIP-логин: латиница, цифры и . _ @ + -")
        if not _HOST.match(config.get("sip_server") or "sip.zadarma.com"):
            raise ValueError("SIP-сервер: имя хоста без схемы, например sip.zadarma.com")
    if provider == "twilio" and config.get("account_sid") and not re.match(r"^AC[0-9a-fA-F]{32}$", config["account_sid"]):
        raise ValueError("Account SID выглядит как AC и 32 шестнадцатеричных символа")
    if provider == "telnyx" and config.get("public_key"):
        try:
            if len(base64.b64decode(config["public_key"])) != 32:
                raise ValueError
        except Exception:
            raise ValueError("Public Key Telnyx: base64 от 32 байт (из Portal → Keys & Credentials)")
    return {
        "name": name, "provider": provider, "number": number, "subagent_id": subagent_id,
        "greeting": _clean_text(merged.get("greeting") if merged.get("greeting") is not None else DEFAULT_GREETING, 500),
        "language": language, "owner_number": owner_number,
        "enabled": bool(merged.get("enabled", True)), "inbound_enabled": bool(merged.get("inbound_enabled", True)),
        "outbound_enabled": bool(merged.get("outbound_enabled", True)), "auto_book": bool(merged.get("auto_book", False)),
        "max_call_minutes": minutes, "config": config,
    }


def _secret_updates(provider: str, payload: Dict[str, Any]) -> Dict[str, str]:
    updates = {}
    for field in PROVIDERS[provider]["fields"]:
        if field["secret"] and isinstance(payload.get(field["key"]), str) and payload[field["key"]].strip():
            value = payload[field["key"]].strip()
            if len(value) > 512 or any(ord(ch) < 32 for ch in value):
                raise ValueError(f"{field['label']}: недопустимое значение")
            updates[field["key"]] = value
    return updates


def create_line(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = _validate_line(payload)
    secrets_update = _secret_updates(fields["provider"], payload)
    _init_schema()
    line_id = f"ln{uuid.uuid4().hex[:10]}"
    now = _now()
    if secrets_update:
        _store_secrets(line_id, secrets_update)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO phone_lines (id, name, provider, number, subagent_id, greeting, language, owner_number, enabled,
                inbound_enabled, outbound_enabled, auto_book, max_call_minutes, config, status, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', '', ?, ?)
            """,
            (line_id, fields["name"], fields["provider"], fields["number"], fields["subagent_id"], fields["greeting"],
             fields["language"], fields["owner_number"], int(fields["enabled"]), int(fields["inbound_enabled"]),
             int(fields["outbound_enabled"]), int(fields["auto_book"]), fields["max_call_minutes"],
             json.dumps(fields["config"]), now, now),
        )
    return get_line(line_id)  # type: ignore[return-value]


def update_line(line_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    current = get_line(line_id)
    if current is None:
        raise KeyError(line_id)
    fields = _validate_line(payload, current)
    _store_secrets(line_id, _secret_updates(fields["provider"], payload))
    with _connect() as connection:
        connection.execute(
            """
            UPDATE phone_lines SET name = ?, number = ?, subagent_id = ?, greeting = ?, language = ?, owner_number = ?,
                enabled = ?, inbound_enabled = ?, outbound_enabled = ?, auto_book = ?, max_call_minutes = ?, config = ?,
                updated_at = ?
             WHERE id = ?
            """,
            (fields["name"], fields["number"], fields["subagent_id"], fields["greeting"], fields["language"],
             fields["owner_number"], int(fields["enabled"]), int(fields["inbound_enabled"]), int(fields["outbound_enabled"]),
             int(fields["auto_book"]), fields["max_call_minutes"], json.dumps(fields["config"]), _now(), line_id),
        )
    return get_line(line_id)  # type: ignore[return-value]


def set_line_status(line_id: str, status: str, error: str = "") -> None:
    _init_schema()
    with _connect() as connection:
        connection.execute("UPDATE phone_lines SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                           (status[:32], error[:300], _now(), line_id))


def delete_line(line_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        deleted = connection.execute("DELETE FROM phone_lines WHERE id = ?", (line_id,)).rowcount > 0
    if deleted:
        _delete_secrets(line_id)
    return deleted


# ── calls ───────────────────────────────────────────────────────────────────
def create_call(*, line: Dict[str, Any], direction: str, remote_number: str, provider_call_id: str = "",
                purpose: str = "", client_id: Optional[str] = None, initiated_by: str = "", status: str = "ringing") -> Dict[str, Any]:
    _init_schema()
    call_id = f"call{uuid.uuid4().hex[:14]}"
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO phone_calls (id, line_id, direction, provider, provider_call_id, remote_number, local_number, status,
                purpose, client_id, started_at, initiated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (call_id, line["id"], direction, line["provider"], provider_call_id, remote_number, line.get("number") or "",
             status, purpose[:500], client_id, _now(), initiated_by[:64]),
        )
    return get_call(call_id)  # type: ignore[return-value]


_CALL_FIELDS = {
    "provider_call_id", "status", "answered_at", "ended_at", "duration_s", "end_reason", "turns", "interruptions",
    "first_audio_ms_avg", "first_audio_ms_p90", "transferred_to", "degraded", "conversation_id", "client_id",
}


def update_call(call_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    updates = {key: value for key, value in fields.items() if key in _CALL_FIELDS}
    if not updates:
        return get_call(call_id)
    if "degraded" in updates and not isinstance(updates["degraded"], str):
        updates["degraded"] = json.dumps(updates["degraded"])
    _init_schema()
    with _connect() as connection:
        connection.execute(
            f"UPDATE phone_calls SET {', '.join(f'{key} = ?' for key in updates)} WHERE id = ?",
            (*updates.values(), call_id),
        )
    return get_call(call_id)


def _call_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["degraded"] = _json(item.get("degraded"), [])
    return item


def get_call(call_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM phone_calls WHERE id = ?", (call_id,)).fetchone()
    return _call_dict(row) if row else None


def find_call_by_provider(provider: str, provider_call_id: str) -> Optional[Dict[str, Any]]:
    if not provider_call_id:
        return None
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM phone_calls WHERE provider = ? AND provider_call_id = ? ORDER BY started_at DESC LIMIT 1",
            (provider, provider_call_id),
        ).fetchone()
    return _call_dict(row) if row else None


def list_calls(*, line_id: Optional[str] = None, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    _init_schema()
    where, params = [], []
    if line_id:
        where.append("line_id = ?")
        params.append(line_id)
    if conversation_id:
        where.append("conversation_id = ?")
        params.append(conversation_id)
    sql = "SELECT * FROM phone_calls" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY started_at DESC LIMIT ?"
    with _connect() as connection:
        rows = connection.execute(sql, (*params, max(1, min(int(limit), 500)))).fetchall()
    return [_call_dict(row) for row in rows]


def count_active_calls() -> int:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM phone_calls WHERE status IN ('ringing', 'active')").fetchone()
    return int(row[0] or 0)


def reconcile_stale_calls() -> int:
    """Calls left 'ringing'/'active' by a previous process ended with it."""
    _init_schema()
    with _connect() as connection:
        return connection.execute(
            "UPDATE phone_calls SET status = 'ended', end_reason = 'backend_restart', ended_at = ? "
            "WHERE status IN ('ringing', 'active')", (_now(),),
        ).rowcount


# ── Asterisk trunks ─────────────────────────────────────────────────────────
def asterisk_state_dir() -> Path:
    from backend import database

    path = Path(os.getenv("PHONE_ASTERISK_STATE_DIR") or Path(database.DB_DIR) / "asterisk")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _conf_value(value: str) -> str:
    if any(ch in value for ch in "\r\n"):
        raise ValueError("newline in trunk value")
    return value.replace(";", r"\;")


def trunk_name(line_id: str) -> str:
    return f"hermes_{line_id}"


def render_trunks(lines: Optional[List[Dict[str, Any]]] = None) -> str:
    lines = [line for line in (lines if lines is not None else list_lines())
             if line["provider"] == "zadarma" and line["enabled"]]
    servers: Dict[str, int] = {}
    for line in lines:
        server = line["config"].get("sip_server") or "sip.zadarma.com"
        servers[server] = servers.get(server, 0) + 1
    blocks = ["; Generated by backend/phone_lines.py — do not edit, changes are overwritten."]
    for line in lines:
        cfg = line["config"]
        login = cfg.get("sip_login") or ""
        password = line_secrets(line["id"]).get("sip_password") or ""
        if not login or not password:
            continue
        server = cfg.get("sip_server") or "sip.zadarma.com"
        name = trunk_name(line["id"])
        callerid = line.get("number") or login
        blocks.append(f"""
[{name}]
type=registration
transport=transport-udp
outbound_auth={name}_auth
server_uri=sip:{_conf_value(server)}
client_uri=sip:{_conf_value(login)}@{_conf_value(server)}
contact_user={_conf_value(login)}
retry_interval=60
forbidden_retry_interval=600
expiration=120
line=yes
endpoint={name}

[{name}_auth]
type=auth
auth_type=userpass
username={_conf_value(login)}
password={_conf_value(password)}

[{name}]
type=aor
contact=sip:{_conf_value(server)}
qualify_frequency=60

[{name}]
type=endpoint
transport=transport-udp
context=hermes-inbound
disallow=all
allow=alaw,ulaw
outbound_auth={name}_auth
aors={name}
from_user={_conf_value(login)}
from_domain={_conf_value(server.split(':')[0])}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
callerid={_conf_value(callerid)}
""")
        if servers.get(server) == 1:
            # Matching by source address is only unambiguous with one line per server;
            # otherwise the registration's ;line= parameter identifies the line.
            blocks.append(f"""[{name}]
type=identify
endpoint={name}
match={_conf_value(server.split(':')[0])}
""")
    return "\n".join(blocks) + "\n"


def write_trunks() -> Path:
    path = asterisk_state_dir() / "pjsip_trunks.conf"
    content = render_trunks()
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    os.replace(temp, path)
    return path


def line_id_from_channel(channel_name: str) -> str:
    match = re.match(r"^PJSIP/hermes_([A-Za-z0-9]+)-", channel_name or "")
    return match.group(1) if match else ""


# ── public URLs & signatures ────────────────────────────────────────────────
def public_base_url() -> str:
    base = str(get_setting("public_base_url", "") or "").rstrip("/")
    if not base:
        raise PhoneProviderError("Не задан публичный HTTPS-адрес (Телефония → Настройки) — Twilio/Telnyx не смогут достучаться.")
    return base


def provider_urls(line: Dict[str, Any], call_id: str = "") -> Dict[str, str]:
    base = public_base_url()
    ws_base = "wss://" + base.split("://", 1)[1]
    urls = {
        "twilio_voice": f"{base}/api/phone/twilio/voice/{line['id']}",
        "twilio_status": f"{base}/api/phone/twilio/status/{line['id']}",
        "telnyx_events": f"{base}/api/phone/telnyx/events/{line['id']}",
    }
    if call_id:
        token = media_token(call_id)
        urls["twilio_media"] = f"{ws_base}/api/phone/twilio/media/{line['id']}/{call_id}/{token}"
        urls["telnyx_media"] = f"{ws_base}/api/phone/telnyx/media/{line['id']}/{call_id}/{token}"
    return urls


def validate_twilio_signature(auth_token: str, url: str, params: Dict[str, str], signature: str) -> bool:
    """X-Twilio-Signature: base64 HMAC-SHA1 over the full URL followed by every POST
    parameter name+value, sorted by name."""
    if not auth_token or not signature:
        return False
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature)


def verify_telnyx_signature(public_key_b64: str, timestamp: str, raw_body: bytes, signature_b64: str,
                            tolerance_s: int = 300, now: Optional[float] = None) -> bool:
    if not (public_key_b64 and timestamp and signature_b64):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > tolerance_s:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), f"{timestamp}|".encode() + raw_body)
        return True
    except Exception:
        return False


# ── provider REST clients ───────────────────────────────────────────────────
def _twiml_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def twiml_stream(media_url: str, call_id: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f'<Stream url="{_twiml_escape(media_url)}"><Parameter name="call_id" value="{_twiml_escape(call_id)}"/></Stream>'
            '</Connect></Response>')


def twiml_dial(number: str, caller_id: str = "") -> str:
    attr = f' callerId="{_twiml_escape(caller_id)}"' if caller_id else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial{attr}>{_twiml_escape(number)}</Dial></Response>'


def twiml_reject() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="rejected"/></Response>'


class TwilioClient:
    BASE = "https://api.twilio.com/2010-04-01"

    def __init__(self, account_sid: str, auth_token: str):
        if not account_sid or not auth_token:
            raise PhoneProviderError("Для Twilio нужны Account SID и Auth Token")
        self.account_sid = account_sid
        self.auth_token = auth_token

    async def _request(self, method: str, path: str, *, data: Optional[Dict[str, Any]] = None,
                       params: Optional[Dict[str, Any]] = None, absolute: bool = False) -> Dict[str, Any]:
        import httpx

        url = path if absolute else f"{self.BASE}/Accounts/{self.account_sid}{path}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
                response = await client.request(method, url, data=data, params=params,
                                                 auth=(self.account_sid, self.auth_token))
        except httpx.HTTPError as exc:
            raise PhoneProviderError(f"Twilio недоступен ({type(exc).__name__})") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else ""
            raise PhoneProviderError(f"Twilio: {message or response.status_code}")
        return body if isinstance(body, dict) else {}

    async def validate(self) -> Dict[str, Any]:
        body = await self._request("GET", ".json")
        return {"name": body.get("friendly_name"), "status": body.get("status")}

    async def search_numbers(self, country: str, kind: str = "Local", contains: str = "") -> List[Dict[str, Any]]:
        if kind not in ("Local", "Mobile", "TollFree"):
            raise ValueError("kind must be Local, Mobile or TollFree")
        params: Dict[str, Any] = {"VoiceEnabled": "true", "PageSize": 20}
        if contains:
            params["Contains"] = contains
        body = await self._request("GET", f"/AvailablePhoneNumbers/{country.upper()}/{kind}.json", params=params)
        return [{
            "phone_number": item.get("phone_number"), "friendly_name": item.get("friendly_name"),
            "locality": item.get("locality") or item.get("region") or "", "country": country.upper(), "kind": kind,
            "capabilities": item.get("capabilities") or {},
            "address_requirements": item.get("address_requirements") or "none",
        } for item in body.get("available_phone_numbers") or []]

    async def monthly_price(self, country: str, kind: str = "Local") -> Optional[Dict[str, Any]]:
        try:
            body = await self._request("GET", f"https://pricing.twilio.com/v1/PhoneNumbers/Countries/{country.upper()}", absolute=True)
        except PhoneProviderError:
            return None
        wanted = {"Local": "local", "Mobile": "mobile", "TollFree": "toll free"}[kind]
        for price in body.get("phone_number_prices") or []:
            if str(price.get("number_type") or "").lower() == wanted:
                return {"amount": price.get("current_price"), "currency": body.get("price_unit")}
        return None

    async def owned_numbers(self) -> List[Dict[str, Any]]:
        body = await self._request("GET", "/IncomingPhoneNumbers.json", params={"PageSize": 100})
        return [{"sid": item.get("sid"), "phone_number": item.get("phone_number"), "voice_url": item.get("voice_url")}
                for item in body.get("incoming_phone_numbers") or []]

    async def buy_number(self, phone_number: str, voice_url: str, status_url: str) -> Dict[str, Any]:
        body = await self._request("POST", "/IncomingPhoneNumbers.json", data={
            "PhoneNumber": phone_number, "VoiceUrl": voice_url, "VoiceMethod": "POST",
            "StatusCallback": status_url, "StatusCallbackMethod": "POST",
        })
        return {"sid": body.get("sid"), "phone_number": body.get("phone_number")}

    async def configure_number(self, sid: str, voice_url: str, status_url: str) -> None:
        await self._request("POST", f"/IncomingPhoneNumbers/{sid}.json", data={
            "VoiceUrl": voice_url, "VoiceMethod": "POST", "StatusCallback": status_url, "StatusCallbackMethod": "POST",
        })

    async def create_call(self, to: str, from_: str, twiml: str, status_url: str) -> str:
        body = await self._request("POST", "/Calls.json", data={
            "To": to, "From": from_, "Twiml": twiml, "StatusCallback": status_url, "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "answered", "completed"],
        })
        return str(body.get("sid") or "")

    async def update_call(self, sid: str, *, twiml: Optional[str] = None, status: Optional[str] = None) -> None:
        data: Dict[str, Any] = {}
        if twiml:
            data["Twiml"] = twiml
        if status:
            data["Status"] = status
        await self._request("POST", f"/Calls/{sid}.json", data=data)


class TelnyxClient:
    BASE = "https://api.telnyx.com/v2"

    def __init__(self, api_key: str):
        if not api_key:
            raise PhoneProviderError("Для Telnyx нужен API Key")
        self.api_key = api_key

    async def _request(self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None,
                       params: Any = None) -> Dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
                response = await client.request(method, f"{self.BASE}{path}", json=json_body, params=params,
                                                headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise PhoneProviderError(f"Telnyx недоступен ({type(exc).__name__})") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            errors = body.get("errors") if isinstance(body, dict) else None
            detail = (errors[0].get("detail") or errors[0].get("title")) if errors else ""
            raise PhoneProviderError(f"Telnyx: {detail or response.status_code}")
        return body if isinstance(body, dict) else {}

    async def validate(self) -> Dict[str, Any]:
        body = await self._request("GET", "/balance")
        data = body.get("data") or {}
        return {"balance": data.get("balance"), "currency": data.get("currency")}

    async def search_numbers(self, country: str, contains: str = "") -> List[Dict[str, Any]]:
        params: List[Any] = [("filter[country_code]", country.upper()), ("filter[features][]", "voice"),
                             ("filter[limit]", "20")]
        if contains:
            params.append(("filter[phone_number][contains]", contains))
        body = await self._request("GET", "/available_phone_numbers", params=params)
        out = []
        for item in body.get("data") or []:
            cost = item.get("cost_information") or {}
            region = next((r.get("region_name") for r in item.get("region_information") or [] if r.get("region_name")), "")
            out.append({
                "phone_number": item.get("phone_number"), "friendly_name": item.get("phone_number"),
                "locality": region or "", "country": country.upper(), "kind": item.get("phone_number_type") or "",
                "monthly_cost": cost.get("monthly_cost"), "upfront_cost": cost.get("upfront_cost"),
                "currency": cost.get("currency"),
            })
        return out

    async def owned_numbers(self) -> List[Dict[str, Any]]:
        body = await self._request("GET", "/phone_numbers", params=[("page[size]", "100")])
        return [{"sid": item.get("id"), "phone_number": item.get("phone_number"), "connection_id": item.get("connection_id")}
                for item in body.get("data") or []]

    async def buy_number(self, phone_number: str, connection_id: str) -> Dict[str, Any]:
        body = await self._request("POST", "/number_orders", json_body={
            "phone_numbers": [{"phone_number": phone_number}], "connection_id": connection_id,
        })
        data = body.get("data") or {}
        return {"sid": data.get("id"), "phone_number": phone_number, "status": data.get("status")}

    @staticmethod
    def stream_params(media_url: str) -> Dict[str, Any]:
        return {"stream_url": media_url, "stream_track": "inbound_track",
                "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU"}

    async def create_call(self, *, connection_id: str, to: str, from_: str, webhook_url: str, media_url: str,
                          client_state: str) -> str:
        body = await self._request("POST", "/calls", json_body={
            "connection_id": connection_id, "to": to, "from": from_, "webhook_url": webhook_url,
            "client_state": base64.b64encode(client_state.encode()).decode(), **self.stream_params(media_url),
        })
        return str((body.get("data") or {}).get("call_control_id") or "")

    async def answer(self, call_control_id: str, media_url: str, client_state: str) -> None:
        await self._request("POST", f"/calls/{call_control_id}/actions/answer", json_body={
            "client_state": base64.b64encode(client_state.encode()).decode(), **self.stream_params(media_url),
        })

    async def transfer(self, call_control_id: str, to: str) -> None:
        await self._request("POST", f"/calls/{call_control_id}/actions/transfer", json_body={"to": to})

    async def hangup(self, call_control_id: str) -> None:
        await self._request("POST", f"/calls/{call_control_id}/actions/hangup", json_body={})

    async def reject(self, call_control_id: str) -> None:
        await self._request("POST", f"/calls/{call_control_id}/actions/reject", json_body={"cause": "CALL_REJECTED"})


def twilio_client(line: Dict[str, Any]) -> TwilioClient:
    return TwilioClient(line["config"].get("account_sid") or "", line_secrets(line["id"]).get("auth_token") or "")


def telnyx_client(line: Dict[str, Any]) -> TelnyxClient:
    return TelnyxClient(line_secrets(line["id"]).get("api_key") or "")


async def validate_line_account(line: Dict[str, Any]) -> Dict[str, Any]:
    """Checks the provider credentials and stores the outcome on the line."""
    try:
        if line["provider"] == "twilio":
            info = await twilio_client(line).validate()
        elif line["provider"] == "telnyx":
            info = await telnyx_client(line).validate()
        else:
            from backend import phone_asterisk

            info = await phone_asterisk.controller.trunk_state(line["id"])
    except Exception as exc:
        set_line_status(line["id"], "error", str(exc))
        return {"ok": False, "error": str(exc)[:300]}
    set_line_status(line["id"], "ok")
    return {"ok": True, **(info or {})}


async def search_numbers(line: Dict[str, Any], country: str, kind: str = "Local", contains: str = "") -> List[Dict[str, Any]]:
    if not re.match(r"^[A-Za-z]{2}$", country or ""):
        raise ValueError("Код страны из двух букв, например IL, US, PL")
    contains = re.sub(r"[^0-9*]", "", contains or "")[:10]
    if line["provider"] == "twilio":
        client = twilio_client(line)
        numbers = await client.search_numbers(country, kind, contains)
        price = await client.monthly_price(country, kind)
        for item in numbers:
            item["monthly_cost"] = (price or {}).get("amount")
            item["currency"] = (price or {}).get("currency")
        return numbers
    if line["provider"] == "telnyx":
        return await telnyx_client(line).search_numbers(country, contains)
    raise ValueError("У этого провайдера номера покупаются в его личном кабинете")


async def buy_number(line: Dict[str, Any], phone_number: str) -> Dict[str, Any]:
    from backend.phone_audio import normalize_e164

    number = normalize_e164(phone_number)
    if not _E164.match(number):
        raise ValueError("Некорректный номер")
    urls = provider_urls(line)
    if line["provider"] == "twilio":
        result = await twilio_client(line).buy_number(number, urls["twilio_voice"], urls["twilio_status"])
    elif line["provider"] == "telnyx":
        connection_id = line["config"].get("connection_id") or ""
        if not connection_id:
            raise ValueError("Сначала укажите Call Control App ID")
        result = await telnyx_client(line).buy_number(number, connection_id)
    else:
        raise ValueError("У этого провайдера номера покупаются в его личном кабинете")
    config = dict(line["config"])
    config["number_sid"] = result.get("sid") or ""
    update_line(line["id"], {"number": number, "config": config})
    return result


async def attach_owned_number(line: Dict[str, Any], phone_number: str) -> Dict[str, Any]:
    """Points an already-owned number at this line (Twilio: sets its Voice URL)."""
    from backend.phone_audio import normalize_e164

    number = normalize_e164(phone_number)
    if line["provider"] == "twilio":
        client = twilio_client(line)
        owned = {item["phone_number"]: item for item in await client.owned_numbers()}
        if number not in owned:
            raise ValueError("Этого номера нет в аккаунте Twilio")
        urls = provider_urls(line)
        await client.configure_number(owned[number]["sid"], urls["twilio_voice"], urls["twilio_status"])
        config = dict(line["config"])
        config["number_sid"] = owned[number]["sid"]
        return update_line(line["id"], {"number": number, "config": config})
    return update_line(line["id"], {"number": number})
