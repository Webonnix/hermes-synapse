"""One catalogue of every model a chat turn may be sent to, and the resolution
of a picked entry back into a concrete (api_base, api_key, model, provider).

Two sources, deliberately kept apart in the payload so the picker can label
them the way Open WebUI's model selector does (Local / External):

* ``local``  — what this box's llama.cpp server is serving (GET /v1/models).
  llama-server holds exactly one model for the life of the process, so this is
  normally a single entry, always resident.
* ``router`` — the external catalogue reachable through the OmniRoute sidecar
  (GET /v1/models with ROUTER_API_KEY). Entry ids are namespaced by provider
  (``kimi/kimi-k3``), and a chat request must use that id verbatim.

Why resolution lives here and not in the WebSocket handler: a frame arriving
from the browser must never be able to name its own endpoint. The handler
passes the *id the user picked*, this module looks it up in the live catalogue
and only then hands back an endpoint — an id that is not in the catalogue
resolves to nothing and the turn falls back to the configured default model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.llamacpp_client import LOCAL_PROVIDER, normalize_provider

logger = logging.getLogger(__name__)

# The catalogue is two network calls (llama.cpp + OmniRoute) and changes only when
# the server is restarted or someone connects a model, so every chat turn
# re-fetching it would be pure latency.
_CACHE_TTL_SECONDS = 60.0
_cache: Dict[str, Any] = {"data": None, "timestamp": 0.0}
_cache_lock = asyncio.Lock()

SOURCE_LOCAL = "local"
SOURCE_ROUTER = "router"


def router_credentials() -> Tuple[str, str]:
    """(base_url, api_key) for the OmniRoute sidecar. The key lives in the DB
    (Settings → API Keys) with an env var as the fallback — same lookup order
    the AI Router tab's endpoints use."""
    from backend import database as db

    base = os.getenv("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/")
    key = db.get_api_key("ROUTER_API_KEY") or os.getenv("ROUTER_API_KEY", "")
    return base, key


def _human_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return ""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _parameter_label(n_params: Any) -> str:
    """27_000_000_000 -> "27.0B", the way model cards quote it."""
    try:
        value = float(n_params)
    except (TypeError, ValueError):
        return ""
    if value >= 1e9:
        return f"{value / 1e9:.1f}B"
    if value >= 1e6:
        return f"{value / 1e6:.0f}M"
    return ""


async def _list_local_models(local_base_url: str) -> Tuple[List[Dict[str, Any]], Optional[str], List[Dict[str, str]]]:
    from backend.llamacpp_client import LlamaCppClient

    client = LlamaCppClient(local_base_url, timeout=8)
    try:
        raw = await client.list_models()
    except Exception as exc:  # noqa: BLE001 - any transport failure is "no local model"
        logger.warning("Chat model catalogue: llama.cpp server unreachable (%s)", exc)
        return [], "Локальный сервер llama.cpp недоступен — локальная модель не получена.", []

    context_length: Optional[int] = None
    try:
        props = await client.props()
        context_length = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
    except Exception:  # noqa: BLE001 - the context badge is best-effort
        pass

    models: List[Dict[str, Any]] = []
    for item in raw:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        models.append({
            "id": item["id"],
            "name": item["id"],
            "source": SOURCE_LOCAL,
            "owned_by": "llama.cpp",
            "size": meta.get("size"),
            "size_label": _human_size(meta.get("size")),
            "parameter_size": _parameter_label(meta.get("n_params")),
            "quantization": "",
            "family": "",
            "modified_at": "",
            "context_length": context_length,
            # llama-server keeps its model resident for the life of the process.
            "loaded": True,
        })
    models.sort(key=lambda m: m["id"].lower())
    # "local_hidden" stays in the payload for the picker's contract; a single
    # --alias has nothing to hide.
    return models, None, []


async def _list_router_models() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    base, key = router_credentials()
    if not key:
        return [], "ROUTER_API_KEY не задан (Настройки → API-ключи → AI Router)."

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        logger.warning("Chat model catalogue: OmniRoute unreachable (%s)", exc)
        return [], "OmniRoute недоступен — запущен ли контейнер 'omniroute'?"

    if resp.status_code != 200:
        detail = "неверный ROUTER_API_KEY" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}"
        return [], f"GET /v1/models: {detail}"

    try:
        raw = resp.json().get("data") or []
    except ValueError:
        raw = []

    seen: set[str] = set()
    models: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
        # The provider prefix is the part a human actually reads in the picker;
        # OmniRoute namespaces every catalogue id as "<provider>/<model>".
        owner = entry.get("owned_by") or (model_id.split("/", 1)[0] if "/" in model_id else "")
        models.append({
            "id": model_id,
            "name": model_id.split("/", 1)[1] if "/" in model_id else model_id,
            "source": SOURCE_ROUTER,
            "owned_by": owner,
            "capabilities": capabilities,
            "context_length": entry.get("context_length") or capabilities.get("context_length") or None,
        })
    models.sort(key=lambda m: m["id"].lower())
    return models, None


async def list_chat_models(*, local_base_url: str, default_model: str, default_provider: str,
                           force_refresh: bool = False) -> Dict[str, Any]:
    """The full picker payload. Never raises: a source that cannot be reached
    comes back as an empty list plus a human-readable error, because one dead
    provider must not take the model picker down with it."""
    now = time.monotonic()
    async with _cache_lock:
        cached = _cache.get("data")
        if cached and not force_refresh and (now - _cache["timestamp"] < _CACHE_TTL_SECONDS):
            return {**cached, "default": _default_entry(cached, default_model, default_provider)}

        local, local_error, local_hidden = await _list_local_models(local_base_url)
        router, router_error = await _list_router_models()
        payload = {
            "local": local,
            "router": router,
            "local_error": local_error,
            "router_error": router_error,
            "local_hidden": local_hidden,
        }
        _cache["data"] = payload
        _cache["timestamp"] = now

    return {**payload, "default": _default_entry(payload, default_model, default_provider)}


def _default_entry(payload: Dict[str, Any], default_model: str, default_provider: str) -> Dict[str, Any]:
    """What "по умолчанию" resolves to right now — the model the agent is
    configured with, annotated with the source it was found in so the picker
    can show it selected without a second lookup."""
    source = SOURCE_LOCAL if normalize_provider(default_provider) == LOCAL_PROVIDER else SOURCE_ROUTER
    for entry in payload.get(source) or []:
        if entry["id"] == default_model:
            return {"id": default_model, "source": source, "name": entry.get("name") or default_model}
    return {"id": default_model, "source": source, "name": default_model}


def invalidate_cache() -> None:
    _cache["data"] = None
    _cache["timestamp"] = 0.0


async def resolve_chat_model(source: str, model_id: str, *, local_base_url: str,
                             api_key: str) -> Optional[Dict[str, str]]:
    """Turn a picked catalogue entry into the LLM endpoint for one turn.

    Returns None — meaning "use the configured default" — when the id is empty
    or is not in the live catalogue. That check is the security boundary: the
    browser sends an id, never an endpoint, so a crafted frame cannot point a
    turn (and the API key that goes with it) at an attacker's host.
    """
    model_id = (model_id or "").strip()
    if not model_id:
        return None
    source = (source or "").strip().lower() or SOURCE_LOCAL

    catalogue = await list_chat_models(
        local_base_url=local_base_url, default_model="", default_provider="",
    )
    entries = catalogue.get(source) or []
    if not any(entry["id"] == model_id for entry in entries):
        logger.warning("Chat model '%s' (%s) is not in the live catalogue — using the default.", model_id, source)
        return None

    if source == SOURCE_LOCAL:
        return {
            "source": SOURCE_LOCAL,
            "model": model_id,
            "api_base": local_base_url,
            "api_key": api_key,
            "provider": LOCAL_PROVIDER,
        }

    base, key = router_credentials()
    if not key:
        return None
    return {
        "source": SOURCE_ROUTER,
        "model": model_id,
        "api_base": f"{base}/v1",
        "api_key": key,
        "provider": "openai_compatible",
    }
