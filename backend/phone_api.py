"""HTTP and WebSocket surface of telephony.

Dashboard routes check a permission on every call. The provider routes under
``/api/phone/twilio/`` and ``/api/phone/telnyx/`` are public (they are listed in the auth
middleware's allow-list): each authenticates itself — X-Twilio-Signature, Telnyx Ed25519,
or the per-call media token for WebSockets — and answers nothing useful without it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

from backend import permissions

logger = logging.getLogger("hermes.phone_api")

router = APIRouter()


def _error(exc: Exception) -> HTTPException:
    from backend import phone_lines, phone_speech

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Не найдено")
    if isinstance(exc, (phone_lines.PhoneProviderError, phone_speech.SpeechError)):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Phone API error")
    return HTTPException(status_code=500, detail="Внутренняя ошибка")


async def _run(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        raise _error(exc)


async def _await(coro):
    try:
        return await coro
    except Exception as exc:
        raise _error(exc)


async def _line(line_id: str) -> Dict[str, Any]:
    from backend import phone_lines

    line = await asyncio.to_thread(phone_lines.get_line, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Линия не найдена")
    return line


async def _after_line_change(line: Optional[Dict[str, Any]] = None) -> None:
    if line is None or line.get("provider") == "zadarma":
        from backend.phone_asterisk import controller

        try:
            await controller.reload_trunks()
        except Exception:
            logger.exception("Could not reload Asterisk trunks")


# ── overview & settings ─────────────────────────────────────────────────────
@router.get("/api/phone/overview")
async def phone_overview_api(request: Request):
    from backend import phone_lines, phone_service, phone_speech

    role = permissions.require(request, permissions.INBOX_VIEW)
    granted = permissions.permissions_for(role)
    lines = await _run(phone_lines.list_lines)
    return {
        "providers": phone_lines.PROVIDERS,
        "lines": lines,
        "status": await _run(phone_service.status),
        "speech": await _run(phone_speech.catalog_status),
        "can_manage": permissions.PHONE_MANAGE in granted,
        "can_call": permissions.PHONE_CALL in granted,
        "can_control_calls": permissions.INBOX_REPLY in granted,
    }


class SettingsRequest(BaseModel):
    public_base_url: Optional[str] = None
    max_concurrent_calls: Optional[int] = None


@router.put("/api/phone/settings")
async def phone_settings_api(payload: SettingsRequest, request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    return await _run(phone_lines.update_settings, payload.model_dump(exclude_none=True))


# ── lines ───────────────────────────────────────────────────────────────────
@router.post("/api/phone/lines")
async def create_phone_line_api(payload: Dict[str, Any], request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    line = await _run(phone_lines.create_line, payload)
    await _after_line_change(line)
    return line


@router.patch("/api/phone/lines/{line_id}")
async def update_phone_line_api(line_id: str, payload: Dict[str, Any], request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    await _line(line_id)
    line = await _run(phone_lines.update_line, line_id, payload)
    await _after_line_change(line)
    return line


@router.delete("/api/phone/lines/{line_id}")
async def delete_phone_line_api(line_id: str, request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    line = await _line(line_id)
    await _run(phone_lines.delete_line, line_id)
    await _after_line_change(line)
    return {"status": "success", "id": line_id}


@router.post("/api/phone/lines/{line_id}/check")
async def check_phone_line_api(line_id: str, request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    line = await _line(line_id)
    return await _await(phone_lines.validate_line_account(line))


@router.get("/api/phone/lines/{line_id}/numbers/search")
async def search_phone_numbers_api(line_id: str, request: Request, country: str = "", kind: str = "Local", contains: str = ""):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    line = await _line(line_id)
    return await _await(phone_lines.search_numbers(line, country, kind, contains))


@router.get("/api/phone/lines/{line_id}/numbers/owned")
async def owned_phone_numbers_api(line_id: str, request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    line = await _line(line_id)
    if line["provider"] == "twilio":
        return await _await(phone_lines.twilio_client(line).owned_numbers())
    if line["provider"] == "telnyx":
        return await _await(phone_lines.telnyx_client(line).owned_numbers())
    return []


class NumberRequest(BaseModel):
    phone_number: str
    confirm: bool = False


@router.post("/api/phone/lines/{line_id}/numbers/buy")
async def buy_phone_number_api(line_id: str, payload: NumberRequest, request: Request):
    """Spends money on the owner's provider account — refused without an explicit
    confirmation from the person clicking the button."""
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Покупку номера нужно подтвердить")
    line = await _line(line_id)
    return await _await(phone_lines.buy_number(line, payload.phone_number))


@router.post("/api/phone/lines/{line_id}/numbers/attach")
async def attach_phone_number_api(line_id: str, payload: NumberRequest, request: Request):
    from backend import phone_lines

    permissions.require(request, permissions.PHONE_MANAGE)
    line = await _line(line_id)
    return await _await(phone_lines.attach_owned_number(line, payload.phone_number))


# ── speech ──────────────────────────────────────────────────────────────────
@router.put("/api/phone/speech")
async def update_phone_speech_api(payload: Dict[str, Any], request: Request):
    from backend import phone_speech

    permissions.require(request, permissions.PHONE_MANAGE)
    return await _run(phone_speech.update_config, payload)


class KeyRequest(BaseModel):
    api_key: str


@router.put("/api/phone/speech/keys/{provider_id}")
async def set_phone_speech_key_api(provider_id: str, payload: KeyRequest, request: Request):
    from backend import phone_speech

    permissions.require(request, permissions.PHONE_MANAGE)
    await _run(phone_speech.set_secret, provider_id, payload.api_key)
    return {"status": "success", "provider": provider_id, "has_secret": True}


@router.delete("/api/phone/speech/keys/{provider_id}")
async def clear_phone_speech_key_api(provider_id: str, request: Request):
    from backend import phone_speech

    permissions.require(request, permissions.PHONE_MANAGE)
    await _run(phone_speech.clear_secret, provider_id)
    return {"status": "success", "provider": provider_id, "has_secret": False}


@router.post("/api/phone/speech/test")
async def test_phone_speech_api(request: Request):
    from backend import phone_speech

    permissions.require(request, permissions.PHONE_MANAGE)
    return await _await(phone_speech.test_tts())


@router.post("/api/phone/selftest")
async def phone_selftest_api(request: Request):
    from backend.phone_asterisk import controller

    permissions.require(request, permissions.PHONE_MANAGE)
    return await _await(controller.selftest())


# ── calls ───────────────────────────────────────────────────────────────────
@router.get("/api/phone/calls")
async def list_phone_calls_api(request: Request, line_id: Optional[str] = None, conversation_id: Optional[str] = None,
                               limit: int = 50):
    from backend import phone_lines

    permissions.require(request, permissions.INBOX_VIEW)
    return await _run(phone_lines.list_calls, line_id=line_id, conversation_id=conversation_id, limit=limit)


class CallRequest(BaseModel):
    line_id: str
    to: str
    purpose: str = ""
    client_id: Optional[str] = None


@router.post("/api/phone/calls")
async def start_phone_call_api(payload: CallRequest, request: Request):
    from backend import phone_service

    role = permissions.require(request, permissions.PHONE_CALL)
    return await _await(phone_service.start_outbound(
        payload.line_id, payload.to, purpose=payload.purpose, client_id=payload.client_id, initiated_by=role,
    ))


class TransferRequest(BaseModel):
    number: str = ""


@router.post("/api/phone/calls/{call_id}/hangup")
async def hangup_phone_call_api(call_id: str, request: Request):
    from backend import phone_service

    permissions.require(request, permissions.INBOX_REPLY)
    if not await _await(phone_service.hangup_call(call_id)):
        raise HTTPException(status_code=409, detail="Звонок уже не активен")
    return {"status": "success"}


@router.post("/api/phone/calls/{call_id}/transfer")
async def transfer_phone_call_api(call_id: str, payload: TransferRequest, request: Request):
    from backend import phone_service

    permissions.require(request, permissions.INBOX_REPLY)
    if call_id not in phone_service.ACTIVE:
        raise HTTPException(status_code=409, detail="Звонок уже не активен")
    ok = await _await(phone_service.transfer_call(call_id, payload.number))
    return {"status": "success" if ok else "failed"}


# ── provider webhooks (public, self-authenticating) ─────────────────────────
def _twiml(status: int, body: str) -> Response:
    return Response(content=body, status_code=status, media_type="application/xml")


async def _twilio_params(request: Request) -> Dict[str, str]:
    form = await request.form()
    return {key: str(value) for key, value in form.items()}


@router.post("/api/phone/twilio/voice/{line_id}")
async def twilio_voice_api(line_id: str, request: Request):
    from backend import phone_lines, phone_service

    line = await asyncio.to_thread(phone_lines.get_line, line_id)
    if not line:
        return Response(status_code=404)
    try:
        url = phone_lines.provider_urls(line)["twilio_voice"]
    except phone_lines.PhoneProviderError:
        return Response(status_code=503)
    params = await _twilio_params(request)
    status, body = await asyncio.to_thread(
        phone_service.twilio_voice_webhook, line_id, url, params, request.headers.get("x-twilio-signature", ""),
    )
    return _twiml(status, body) if body else Response(status_code=status)


@router.post("/api/phone/twilio/status/{line_id}")
async def twilio_status_api(line_id: str, request: Request):
    from backend import phone_lines, phone_service

    line = await asyncio.to_thread(phone_lines.get_line, line_id)
    if not line:
        return Response(status_code=404)
    try:
        url = phone_lines.provider_urls(line)["twilio_status"]
    except phone_lines.PhoneProviderError:
        return Response(status_code=503)
    params = await _twilio_params(request)
    status = await asyncio.to_thread(
        phone_service.twilio_status_webhook, line_id, url, params, request.headers.get("x-twilio-signature", ""),
    )
    return Response(status_code=status)


@router.post("/api/phone/telnyx/events/{line_id}")
async def telnyx_events_api(line_id: str, request: Request):
    from backend import phone_service

    raw = await request.body()
    if len(raw) > 256 * 1024:
        return Response(status_code=413)
    status = await phone_service.telnyx_event_webhook(line_id, raw, dict(request.headers))
    return Response(status_code=status)


@router.websocket("/api/phone/twilio/media/{line_id}/{call_id}/{token}")
async def twilio_media_ws(websocket: WebSocket, line_id: str, call_id: str, token: str):
    from backend import phone_service

    await phone_service.handle_twilio_media(websocket, line_id, call_id, token)


@router.websocket("/api/phone/telnyx/media/{line_id}/{call_id}/{token}")
async def telnyx_media_ws(websocket: WebSocket, line_id: str, call_id: str, token: str):
    from backend import phone_service

    await phone_service.handle_telnyx_media(websocket, line_id, call_id, token)
