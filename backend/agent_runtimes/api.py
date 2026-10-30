"""Dashboard API for agent desktops: start/stop, agent turns, events, live view.

HTTP routes sit under /api/, so main.py's auth middleware has validated the
session before they run; each also requires agents.desktops.manage. The VNC
WebSocket is not covered by that middleware (it is not HTTP), so it checks the
session token itself, from the ``hermes-auth.<token>`` subprotocol like
/api/ws — a browser cannot put an Authorization header on a WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field

from backend import permissions
from backend.agent_runtimes import desktop, llm_proxy
from backend.agent_runtimes.sessions import RUNTIMES, SessionBusy, manager

logger = logging.getLogger("hermes.agent_runtimes.api")

router = APIRouter()
# The desktops' door to the local model rides on this router, so main.py
# mounts both with one include (it authenticates itself, see llm_proxy.py).
router.include_router(llm_proxy.router)

VNC_SUBPROTOCOL = "hermes-vnc"


class TurnPayload(BaseModel):
    runtime: str
    text: str = Field(min_length=1, max_length=20000)


class CancelPayload(BaseModel):
    runtime: str


class StartPayload(BaseModel):
    kind: Optional[str] = None


def _key(key: str) -> str:
    try:
        return desktop._safe_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _require_enabled() -> None:
    if not desktop.enabled():
        raise HTTPException(status_code=503, detail="Agent desktops are disabled (AGENT_DESKTOPS_ENABLED).")


@router.get("/api/agent-desktops")
async def list_agent_desktops(request: Request):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    runtimes = [{"id": rid, **meta} for rid, meta in RUNTIMES.items()]
    kinds = [{"id": kind.name, "enabled": kind.enabled(), "screens": kind.screens} for kind in desktop.KINDS.values()]
    if not desktop.enabled():
        return {"enabled": False, "runtimes": runtimes, "kinds": kinds, "desktops": []}
    try:
        desktops = await desktop.list_desktops()
    except Exception as exc:
        logger.warning("Listing agent desktops failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Docker is unreachable ({type(exc).__name__}).")
    for item in desktops:
        item["busy"] = manager.busy(item["key"])
        item["llm_usage"] = llm_proxy.usage_by_desktop.get(item["key"])
    return {"enabled": True, "runtimes": runtimes, "kinds": kinds, "desktops": desktops,
            "external_models": llm_proxy.external_enabled()}


@router.post("/api/agent-desktops/{key}/start")
async def start_agent_desktop(key: str, request: Request, payload: Optional[StartPayload] = None):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    _require_enabled()
    kind = payload.kind if payload else None
    if kind is not None and kind not in desktop.KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown desktop kind: {kind}")
    try:
        health = await desktop.ensure_desktop(key, kind)
    except desktop.DesktopUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"key": key, "kind": desktop.recorded_kind(key), "health": health}


@router.get("/api/agent-desktops/{key}/model-access")
async def agent_desktop_model_access(key: str, request: Request):
    """What to type into an agent's own settings (OpenBot's custom provider)
    to reach the models through llm_proxy — the desktop's own credential."""
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    _require_enabled()
    try:
        access = desktop.model_access(key)
    except desktop.DesktopUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    external = await llm_proxy.external_models() or []
    access["models"] = [access["model"]] + [f"{llm_proxy.EXTERNAL_PREFIX}{model}" for model in external]
    return {"key": key, **access}


@router.post("/api/agent-desktops/{key}/stop")
async def stop_agent_desktop(key: str, request: Request):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    _require_enabled()
    return {"key": key, "stopped": await manager.stop_desktop(key)}


@router.get("/api/agent-desktops/{key}/health")
async def agent_desktop_health(key: str, request: Request):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    _require_enabled()
    return {"key": key, "health": await desktop.health(key)}


@router.post("/api/agent-desktops/{key}/turns")
async def start_agent_turn(key: str, payload: TurnPayload, request: Request):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    _require_enabled()
    if desktop.recorded_kind(key) == desktop.OPENBOT:
        raise HTTPException(status_code=422, detail="An OpenBot desktop is driven from OpenBot itself, not from here.")
    try:
        run_id = manager.start_turn(key, payload.runtime, payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except SessionBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"key": key, "run_id": run_id}


@router.post("/api/agent-desktops/{key}/turns/cancel")
async def cancel_agent_turn(key: str, payload: CancelPayload, request: Request):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    return {"key": key, "cancelled": await manager.cancel(key, payload.runtime)}


@router.get("/api/agent-desktops/{key}/events")
async def agent_desktop_events(key: str, request: Request, after: int = 0):
    permissions.require(request, permissions.AGENT_DESKTOPS_MANAGE)
    key = _key(key)
    return {"key": key, "events": manager.events_since(key, after)}


def _ws_allowed(websocket: WebSocket) -> bool:
    from backend.auth import role_for_session, validate_session

    offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    from backend.auth_http import connection_token, origin_allowed
    if not origin_allowed(websocket):
        return False
    token = connection_token(websocket) or next((p for p in offered if p.startswith("hermes-auth.")), "").removeprefix("hermes-auth.")
    if not token or not validate_session(token):
        return False
    return permissions.has_permission(role_for_session(token), permissions.AGENT_DESKTOPS_MANAGE)


@router.websocket("/api/agent-desktops/{key}/vnc")
async def agent_desktop_vnc(websocket: WebSocket, key: str):
    """Relays the desktop's VNC stream between the dashboard's noVNC viewer and
    the desktop bridge. Bytes only — RFB is not parsed here."""
    from backend.auth_http import websocket_session, watch_websocket_session
    token, session = await websocket_session(websocket)
    if not session or not permissions.has_permission(session["role"], permissions.AGENT_DESKTOPS_MANAGE) or not desktop.enabled():
        await websocket.close(code=1008)
        return
    screen = websocket.query_params.get("screen", "0")
    try:
        key = desktop._safe_key(key)
        kind = desktop.KINDS[desktop.recorded_kind(key) or desktop.AGENT]
        if not screen.isdigit() or int(screen) >= kind.screens:
            raise ValueError("unknown screen")
        upstream = await desktop._ws_connect(desktop.ws_url(key, f"/vnc?screen={int(screen)}"),
                                             {"Authorization": f"Bearer {desktop.desktop_token(key)}"})
    except Exception as exc:
        logger.info("Agent desktop VNC for %s unavailable: %s", key, type(exc).__name__)
        await websocket.close(code=1011)
        return
    # The browser offered hermes-vnc next to the auth protocol; answering with a
    # protocol it did not offer (or none) would make it drop the connection.
    await websocket.accept(subprotocol=VNC_SUBPROTOCOL)

    async def to_viewer() -> None:
        async for message in upstream:
            if isinstance(message, str):
                message = message.encode("latin-1", errors="ignore")
            await websocket.send_bytes(message)

    async def to_desktop() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None and message.get("text") is not None:
                data = message["text"].encode("latin-1", errors="ignore")
            if data:
                await upstream.send(data)

    tasks = [asyncio.create_task(to_viewer()), asyncio.create_task(to_desktop()),
             asyncio.create_task(watch_websocket_session(websocket, token))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
