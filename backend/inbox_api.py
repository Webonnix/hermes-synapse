"""HTTP surface of the inbox, conversation routing and external voice providers.

A router of its own rather than more ``@app`` routes in main.py: every endpoint here
checks a permission from backend/permissions.py, so the whole surface can be read and
reviewed in one place. The auth middleware in main.py already requires a session for
every /api/ path; the one public route (the voice provider webhook) authenticates itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend import permissions

logger = logging.getLogger("hermes.inbox_api")

router = APIRouter()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Не найдено")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Inbox API error")
    return HTTPException(status_code=500, detail="Внутренняя ошибка")


def _strip_money(item: Dict[str, Any], role: str) -> Dict[str, Any]:
    if not permissions.can_view_financials(role):
        item = dict(item)
        item.pop("cost_usd", None)
    return item


async def _run(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        raise _http_error(exc)


# ── meta ────────────────────────────────────────────────────────────────────
@router.get("/api/inbox/meta")
async def inbox_meta_api(request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_VIEW)
    granted = permissions.permissions_for(role)
    return {
        "fts": await asyncio.to_thread(inbox.fts_enabled),
        "unread_conversations": await asyncio.to_thread(inbox.count_unread),
        "can_reply": permissions.INBOX_REPLY in granted,
        "can_edit_rules": permissions.INBOX_RULES_EDIT in granted,
        "can_manage_voice": permissions.VOICE_PROVIDERS_MANAGE in granted,
        "can_create_clients": permissions.CLIENTS_CREATE in granted,
        "can_view_financials": permissions.can_view_financials(role),
    }


@router.get("/api/inbox/bindings")
async def inbox_bindings_api(request: Request):
    from backend import inbox

    permissions.require(request, permissions.INBOX_VIEW)
    return await _run(inbox.list_bindings_brief)


# ── conversations ───────────────────────────────────────────────────────────
@router.get("/api/inbox/conversations")
async def list_inbox_conversations_api(
    request: Request,
    search: str = "",
    platform: Optional[str] = None,
    status: Optional[str] = None,
    handling: Optional[str] = None,
    client_id: Optional[str] = None,
    project_id: Optional[str] = None,
    binding_id: Optional[str] = None,
    unread: bool = False,
    cursor: Optional[str] = None,
    limit: int = 50,
):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_VIEW)
    result = await _run(
        inbox.list_conversations, search=search[:200], platform=platform, status=status, handling=handling,
        client_id=client_id, project_id=project_id, binding_id=binding_id, unread=unread,
        cursor=cursor, limit=limit,
    )
    result["items"] = [_strip_money(item, role) for item in result["items"]]
    return result


@router.get("/api/inbox/conversations/{conversation_id}")
async def get_inbox_conversation_api(conversation_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_VIEW)
    conversation = await _run(inbox.get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Разговор не найден")
    return _strip_money(conversation, role)


@router.get("/api/inbox/conversations/{conversation_id}/messages")
async def list_inbox_messages_api(conversation_id: str, request: Request, cursor: Optional[str] = None, limit: int = 100):
    from backend import inbox

    permissions.require(request, permissions.INBOX_VIEW)
    if await _run(inbox.get_conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Разговор не найден")
    before_id = None
    if cursor:
        try:
            before_id = int(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="cursor is malformed")
    return await _run(inbox.list_messages, conversation_id, before_id=before_id, limit=limit)


@router.get("/api/clients/{client_id}/conversations")
async def client_conversations_api(client_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.CLIENTS_VIEW, permissions.INBOX_VIEW)
    items = await _run(inbox.conversations_for_client, client_id)
    return [_strip_money(item, role) for item in items]


@router.post("/api/inbox/conversations/{conversation_id}/read")
async def read_inbox_conversation_api(conversation_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_VIEW)
    return _strip_money(await _run(inbox.mark_read, conversation_id), role)


@router.post("/api/inbox/conversations/{conversation_id}/close")
async def close_inbox_conversation_api(conversation_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_REPLY)
    return _strip_money(await _run(inbox.set_status, conversation_id, "closed"), role)


@router.post("/api/inbox/conversations/{conversation_id}/reopen")
async def reopen_inbox_conversation_api(conversation_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_REPLY)
    return _strip_money(await _run(inbox.set_status, conversation_id, "open"), role)


class LinkClientRequest(BaseModel):
    client_id: Optional[str] = None
    create: Optional[Dict[str, Any]] = None


@router.post("/api/inbox/conversations/{conversation_id}/link-client")
async def link_inbox_client_api(conversation_id: str, payload: LinkClientRequest, request: Request):
    from backend import inbox

    required = [permissions.INBOX_REPLY, permissions.CLIENTS_VIEW]
    if payload.create is not None:
        required.append(permissions.CLIENTS_CREATE)
    role = permissions.require(request, *required)
    return _strip_money(
        await _run(inbox.link_client, conversation_id, client_id=payload.client_id, create=payload.create, actor_id=role),
        role,
    )


@router.post("/api/inbox/conversations/{conversation_id}/takeover")
async def takeover_inbox_conversation_api(conversation_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_REPLY)
    return _strip_money(await _run(inbox.takeover, conversation_id), role)


@router.post("/api/inbox/conversations/{conversation_id}/resume")
async def resume_inbox_conversation_api(conversation_id: str, request: Request):
    from backend import inbox

    role = permissions.require(request, permissions.INBOX_REPLY)
    return _strip_money(await _run(inbox.resume, conversation_id), role)


class TextRequest(BaseModel):
    text: str


@router.post("/api/inbox/conversations/{conversation_id}/reply")
async def reply_inbox_conversation_api(conversation_id: str, payload: TextRequest, request: Request):
    from backend import inbox

    permissions.require(request, permissions.INBOX_REPLY)
    try:
        result = await inbox.send_human_reply(conversation_id, payload.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Канал не принял сообщение: {exc}")
    except Exception as exc:
        # A provider error carries its own wording (e.g. Meta's 24-hour window);
        # everything else is mapped to the usual statuses.
        if exc.__class__.__module__.startswith(("httpx", "slack_sdk")):
            raise HTTPException(status_code=502, detail="Канал не принял сообщение")
        raise _http_error(exc)
    return result


@router.post("/api/inbox/conversations/{conversation_id}/whisper")
async def whisper_inbox_conversation_api(conversation_id: str, payload: TextRequest, request: Request):
    from backend import inbox

    permissions.require(request, permissions.INBOX_REPLY)
    return await _run(inbox.add_whisper, conversation_id, payload.text)


class BlockRequest(BaseModel):
    note: str = ""


@router.post("/api/inbox/conversations/{conversation_id}/block")
async def block_inbox_sender_api(conversation_id: str, payload: BlockRequest, request: Request):
    """Blocks this sender by writing an ordinary exact rule on this binding — so the
    block is visible, editable and removable in the rules list like any other."""
    from backend import inbox, inbox_rules

    permissions.require(request, permissions.INBOX_RULES_EDIT)
    conversation = await _run(inbox.get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Разговор не найден")
    return await _run(
        inbox_rules.create_rule, conversation["chat_id"], "block", "exact", conversation["binding_id"],
        payload.note or f"Заблокирован из «Входящих»: {conversation.get('display_name') or conversation['chat_id']}",
    )


# ── attachments ─────────────────────────────────────────────────────────────
@router.get("/api/inbox/attachments/{attachment_id}")
async def inbox_attachment_api(attachment_id: str, request: Request):
    from backend import inbox

    permissions.require(request, permissions.INBOX_VIEW)
    attachment = await _run(inbox.get_attachment, attachment_id)
    if attachment is None or not attachment["path"].is_file():
        raise HTTPException(status_code=404, detail="Вложение не найдено")
    return FileResponse(
        attachment["path"], media_type=attachment["mime"] or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


# ── routing ─────────────────────────────────────────────────────────────────
@router.get("/api/inbox/route-targets")
async def inbox_route_targets_api(request: Request):
    from backend import inbox_routing

    permissions.require(request, permissions.INBOX_VIEW)
    return await _run(inbox_routing.list_targets)


@router.get("/api/inbox/conversations/{conversation_id}/route")
async def get_inbox_route_api(conversation_id: str, request: Request):
    from backend import inbox, inbox_routing

    permissions.require(request, permissions.INBOX_VIEW)
    if await _run(inbox.get_conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Разговор не найден")
    return await _run(inbox_routing.get_route, conversation_id)


class RouteRequest(BaseModel):
    target_type: str
    target_id: str


@router.post("/api/inbox/conversations/{conversation_id}/route")
async def pin_inbox_route_api(conversation_id: str, payload: RouteRequest, request: Request):
    from backend import inbox, inbox_routing

    role = permissions.require(request, permissions.INBOX_REPLY)
    if await _run(inbox.get_conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Разговор не найден")
    return await _run(inbox_routing.pin_route, conversation_id, payload.target_type, payload.target_id, role)


@router.delete("/api/inbox/conversations/{conversation_id}/route")
async def unpin_inbox_route_api(conversation_id: str, request: Request):
    from backend import inbox, inbox_routing

    permissions.require(request, permissions.INBOX_REPLY)
    if await _run(inbox.get_conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Разговор не найден")
    return await _run(inbox_routing.unpin_route, conversation_id)


# ── rules & business hours ──────────────────────────────────────────────────
class RuleRequest(BaseModel):
    pattern: Optional[str] = None
    action: Optional[str] = None
    match: Optional[str] = None
    binding_id: Optional[str] = None
    note: Optional[str] = None


@router.get("/api/inbox/rules")
async def list_inbox_rules_api(request: Request, binding_id: Optional[str] = None):
    from backend import inbox_rules

    permissions.require(request, permissions.INBOX_VIEW)
    return await _run(inbox_rules.list_rules, binding_id)


@router.post("/api/inbox/rules")
async def create_inbox_rule_api(payload: RuleRequest, request: Request):
    from backend import inbox_rules

    permissions.require(request, permissions.INBOX_RULES_EDIT)
    return await _run(
        inbox_rules.create_rule, payload.pattern or "", payload.action or "", payload.match or "exact",
        payload.binding_id, payload.note or "",
    )


@router.patch("/api/inbox/rules/{rule_id}")
async def update_inbox_rule_api(rule_id: str, payload: RuleRequest, request: Request):
    from backend import inbox_rules

    permissions.require(request, permissions.INBOX_RULES_EDIT)
    changes = payload.model_dump(exclude_unset=True)
    return await _run(inbox_rules.update_rule, rule_id, **changes)


@router.delete("/api/inbox/rules/{rule_id}")
async def delete_inbox_rule_api(rule_id: str, request: Request):
    from backend import inbox_rules

    permissions.require(request, permissions.INBOX_RULES_EDIT)
    if not await _run(inbox_rules.delete_rule, rule_id):
        raise HTTPException(status_code=404, detail="Правило не найдено")
    return {"status": "success", "id": rule_id}


class BindingSettingsRequest(BaseModel):
    business_hours: str = ""
    timezone: str = ""


@router.get("/api/inbox/bindings/{binding_id}/settings")
async def get_inbox_binding_settings_api(binding_id: str, request: Request):
    from backend import inbox_rules

    permissions.require(request, permissions.INBOX_VIEW)
    settings = await _run(inbox_rules.get_binding_settings, binding_id)
    settings["within_hours_now"] = await _run(inbox_rules.is_within_business_hours, binding_id)
    return settings


@router.put("/api/inbox/bindings/{binding_id}/settings")
async def set_inbox_binding_settings_api(binding_id: str, payload: BindingSettingsRequest, request: Request):
    from backend import inbox_rules

    permissions.require(request, permissions.INBOX_RULES_EDIT)
    return await _run(inbox_rules.set_binding_settings, binding_id, payload.business_hours, payload.timezone)


# ── external voice providers ────────────────────────────────────────────────
class VoiceProviderRequest(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None
    capabilities: Optional[list] = None
    config: Optional[Dict[str, Any]] = None
    timeout_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    api_key: Optional[str] = None
    clear_api_key: Optional[bool] = None
    webhook_secret: Optional[str] = None


@router.get("/api/voice/providers")
async def list_voice_providers_api(request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.INBOX_VIEW)
    providers = await _run(voice_providers.list_providers)
    return {"kinds": voice_providers.KINDS, "failure_policies": voice_providers.FAILURE_POLICIES, "items": providers}


@router.post("/api/voice/providers")
async def create_voice_provider_api(payload: VoiceProviderRequest, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    return await _run(voice_providers.create_provider, payload.model_dump(exclude_none=True))


@router.patch("/api/voice/providers/{provider_id}")
async def update_voice_provider_api(provider_id: str, payload: VoiceProviderRequest, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    return await _run(voice_providers.update_provider, provider_id, payload.model_dump(exclude_none=True))


@router.delete("/api/voice/providers/{provider_id}")
async def delete_voice_provider_api(provider_id: str, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    if not await _run(voice_providers.delete_provider, provider_id):
        raise HTTPException(status_code=404, detail="Провайдер не найден")
    return {"status": "success", "id": provider_id}


@router.post("/api/voice/providers/{provider_id}/test")
async def test_voice_provider_api(provider_id: str, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    try:
        return await voice_providers.test_provider(provider_id)
    except Exception as exc:
        raise _http_error(exc)


@router.get("/api/voice/providers/{provider_id}/voices")
async def list_voice_provider_voices_api(provider_id: str, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.INBOX_VIEW)
    try:
        return await voice_providers.list_provider_voices(provider_id)
    except Exception as exc:
        raise _http_error(exc)


@router.post("/api/voice/providers/{provider_id}/webhook-secret")
async def rotate_voice_webhook_secret_api(provider_id: str, request: Request):
    """Returns the new secret exactly once — it is never readable again."""
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    secret = await _run(voice_providers.generate_webhook_secret, provider_id)
    return {"webhook_secret": secret, "webhook_path": f"/api/voice/webhook/{provider_id}"}


class VoiceTargetRequest(BaseModel):
    provider_id: Optional[str] = None
    name: Optional[str] = None
    capability_mode: Optional[str] = None
    voice_id: Optional[str] = None
    agent_id: Optional[str] = None
    failure_policy: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/api/voice/targets")
async def list_voice_targets_api(request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.INBOX_VIEW)
    return await _run(voice_providers.list_targets)


@router.post("/api/voice/targets")
async def create_voice_target_api(payload: VoiceTargetRequest, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    return await _run(voice_providers.create_target, payload.model_dump(exclude_none=True))


@router.patch("/api/voice/targets/{target_id}")
async def update_voice_target_api(target_id: str, payload: VoiceTargetRequest, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    return await _run(voice_providers.update_target, target_id, payload.model_dump(exclude_none=True))


@router.delete("/api/voice/targets/{target_id}")
async def delete_voice_target_api(target_id: str, request: Request):
    from backend import voice_providers

    permissions.require(request, permissions.VOICE_PROVIDERS_MANAGE)
    if not await _run(voice_providers.delete_target, target_id):
        raise HTTPException(status_code=404, detail="Цель не найдена")
    return {"status": "success", "id": target_id}


@router.post("/api/voice/webhook/{provider_id}")
async def voice_provider_webhook_api(provider_id: str, request: Request):
    from backend import voice_providers

    raw = await request.body()
    status, body = await asyncio.to_thread(
        voice_providers.handle_webhook, provider_id[:64], raw, dict(request.headers)
    )
    return JSONResponse(status_code=status, content=body)
