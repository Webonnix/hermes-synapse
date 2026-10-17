"""HTTP surface of agent self-development (backend/agent_learning.py).

Every route checks a permission from backend/permissions.py; the auth middleware
in main.py already requires a session for every /api/ path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend import agent_learning, permissions

logger = logging.getLogger("hermes.agent_learning_api")

router = APIRouter(prefix="/api/agent-learning")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Не найдено")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Agent learning API error")
    return HTTPException(status_code=500, detail="Внутренняя ошибка")


class TrackRequest(BaseModel):
    title: Optional[str] = None
    enabled: Optional[bool] = None
    topics: Optional[List[str]] = None
    feeds: Optional[List[Dict[str, Any]]] = None
    cadence_hours: Optional[int] = None
    max_sources: Optional[int] = None
    max_items: Optional[int] = None


class PinRequest(BaseModel):
    pinned: bool = True


@router.get("/tracks")
async def list_tracks(request: Request, agent_id: Optional[str] = None):
    permissions.require(request, permissions.AGENTS_LEARNING_VIEW)
    return await asyncio.to_thread(agent_learning.list_tracks, agent_id)


@router.put("/tracks/{track_id}")
async def put_track(track_id: str, payload: TrackRequest, request: Request):
    permissions.require(request, permissions.AGENTS_LEARNING_MANAGE)
    from backend import database as db

    agent_id = track_id.split(":", 1)[0]
    if not await asyncio.to_thread(db.get_subagent, agent_id):
        raise HTTPException(status_code=404, detail="Агент не найден")
    fields = {key: value for key, value in payload.model_dump().items() if value is not None}
    try:
        return await asyncio.to_thread(agent_learning.upsert_track, track_id, agent_id, **fields)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/tracks/{track_id}")
async def delete_track(track_id: str, request: Request):
    permissions.require(request, permissions.AGENTS_LEARNING_MANAGE)
    if not await asyncio.to_thread(agent_learning.delete_track, track_id):
        raise HTTPException(status_code=404, detail="Трек не найден")
    return {"status": "deleted", "id": track_id}


@router.post("/tracks/{track_id}/run", status_code=202)
async def run_track(track_id: str, request: Request):
    """Starts a run in the background — a run takes minutes. One run at a time
    process-wide; a second request while one is going is refused, not queued,
    so a double click cannot stack GPU work behind live chat."""
    permissions.require(request, permissions.AGENTS_LEARNING_MANAGE)
    if not await asyncio.to_thread(agent_learning.get_track, track_id):
        raise HTTPException(status_code=404, detail="Трек не найден")
    if agent_learning.is_running():
        raise HTTPException(status_code=409, detail="Уже идёт прогон обучения, дождитесь его окончания")
    from backend import crash_visibility

    crash_visibility.spawn(agent_learning.run_track(track_id, trigger="manual"),
                           f"agent_learning.run:{track_id}", oneshot=True)
    return {"status": "started", "track_id": track_id}


@router.get("/runs")
async def list_runs(request: Request, track_id: Optional[str] = None, limit: int = 50):
    permissions.require(request, permissions.AGENTS_LEARNING_VIEW)
    return await asyncio.to_thread(agent_learning.list_runs, track_id, limit)


@router.get("/items")
async def list_items(request: Request, agent_id: Optional[str] = None,
                     status: Optional[str] = None, limit: int = 100):
    permissions.require(request, permissions.AGENTS_LEARNING_VIEW)
    return await asyncio.to_thread(agent_learning.list_items, agent_id, status, limit)


@router.post("/items/{item_id}/approve")
async def approve_item(item_id: str, request: Request):
    permissions.require(request, permissions.AGENTS_LEARNING_MANAGE)
    try:
        return await asyncio.to_thread(agent_learning.approve_item, item_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/items/{item_id}/reject")
async def reject_item(item_id: str, request: Request):
    permissions.require(request, permissions.AGENTS_LEARNING_MANAGE)
    try:
        return await asyncio.to_thread(agent_learning.reject_item, item_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/items/{item_id}/pin")
async def pin_item(item_id: str, payload: PinRequest, request: Request):
    permissions.require(request, permissions.AGENTS_LEARNING_MANAGE)
    try:
        return await asyncio.to_thread(agent_learning.pin_item, item_id, payload.pinned)
    except Exception as exc:
        raise _http_error(exc) from exc
