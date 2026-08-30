"""Public webhook receiver for the Meta social channels.

Mounted at `/api/social/webhook/{platform}` and listed in main.py's auth
middleware as public, for the same reason `/api/payments/webhook` is: Meta
cannot hold a dashboard session. It authenticates every delivery itself
instead, and grants nothing without that proof:

* **GET** is Meta's registration handshake. It carries no account id, only the
  verify token the owner pasted into the app console, so the challenge is
  echoed back only if that token matches an active binding of this platform.
* **POST** is a real delivery. The body is HMAC-signed with the app secret
  (`X-Hub-Signature-256`); the account id inside the payload picks the binding,
  and that binding's own app secret has to reproduce the signature over the
  raw bytes before a single word reaches an agent. Anyone can POST here — only
  Meta can sign, so an unsigned or mis-signed body is dropped with a 403 and
  recorded in the activity log, where an admin can actually see it.

The handler answers 200 as soon as the delivery is authenticated and runs the
agent turn in the background: Meta retries anything it does not get a prompt
200 for, and an agent turn is far slower than its patience. Retries are deduped
by message id in backend/social_webhook_channel.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger("hermes.social_webhooks")

router = APIRouter(prefix="/api/social/webhook", tags=["smm-factory"])

# asyncio only holds a weak reference to a running task, so a turn dispatched
# and forgotten here could be collected mid-flight. Held until it finishes.
_in_flight: set[asyncio.Task] = set()

WEBHOOK_PLATFORMS = ("instagram", "facebook", "whatsapp")


def _runtime_manager(platform: str):
    if platform == "whatsapp":
        from backend import agent_whatsapp_channel

        return agent_whatsapp_channel.manager
    from backend import agent_meta_channel

    return agent_meta_channel.manager


def _signature_ok(app_secret: str, raw_body: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


@router.get("/{platform}")
async def verify_webhook(platform: str, request: Request):
    """Meta's subscription handshake — see the module docstring."""
    if platform not in WEBHOOK_PLATFORMS:
        return JSONResponse(status_code=404, content={"detail": "Unknown platform"})
    params = request.query_params
    if params.get("hub.mode") != "subscribe":
        return JSONResponse(status_code=400, content={"detail": "Unsupported hub.mode"})

    from backend.agent_messenger_governance import social_webhook_verify_token

    binding = await asyncio.to_thread(social_webhook_verify_token, platform, params.get("hub.verify_token", ""))
    if not binding:
        logger.warning("Rejected %s webhook verification: no active binding with that verify token", platform)
        return JSONResponse(status_code=403, content={"detail": "Verify token does not match any active channel"})

    from backend import channel_activity

    channel_activity.record(binding["id"], platform, "invite", "Webhook подтверждён в Meta")
    return PlainTextResponse(params.get("hub.challenge", ""))


@router.post("/{platform}")
async def receive_webhook(platform: str, request: Request):
    if platform not in WEBHOOK_PLATFORMS:
        return JSONResponse(status_code=404, content={"detail": "Unknown platform"})

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body or b"{}")
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Malformed body"})

    messages = _extract_messages(platform, payload)
    if not messages:
        # Read receipts, delivery notices, echoes of our own replies, status
        # updates: all normal traffic Meta sends and nothing to answer.
        return {"status": "ignored"}

    from backend.agent_messenger_governance import find_social_binding_by_account, resolve_social_binding_credentials
    from backend import channel_activity

    signature = request.headers.get("X-Hub-Signature-256", "")
    handled = 0
    for message in messages:
        binding = await asyncio.to_thread(find_social_binding_by_account, platform, message["account_id"])
        if not binding:
            logger.warning("%s webhook for unknown/inactive account %s", platform, message["account_id"])
            continue
        credentials = await asyncio.to_thread(resolve_social_binding_credentials, binding["id"]) or {}
        app_secret = credentials.get("app_secret") or ""
        if not app_secret or not _signature_ok(app_secret, raw_body, signature):
            channel_activity.record(
                binding["id"], platform, "error",
                "Отклонена доставка с неверной подписью X-Hub-Signature-256",
                message.get("chat_id", ""),
            )
            return JSONResponse(status_code=403, content={"detail": "Bad signature"})

        # Authenticated: answer Meta now, run the turn after.
        task = asyncio.create_task(
            _runtime_manager(platform).handle_message(
                binding["id"], message["chat_id"], message["text"],
                message_id=message.get("message_id", ""),
                display_name=message.get("display_name", ""),
                external_user_id=message["chat_id"],
            )
        )
        _in_flight.add(task)
        task.add_done_callback(_in_flight.discard)
        handled += 1

    return {"status": "accepted", "handled": handled}


def _extract_messages(platform: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flattens Meta's nested delivery into the handful of fields a turn needs.

    Facebook and Instagram share the Messenger `entry[].messaging[]` shape and
    identify the account by `entry[].id`. WhatsApp nests under
    `entry[].changes[].value` and identifies it by `metadata.phone_number_id` —
    the entry id there is the business account, which is not what a binding is
    keyed on.
    """
    out: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        if platform == "whatsapp":
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                account_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
                names = {
                    str(contact.get("wa_id")): ((contact.get("profile") or {}).get("name") or "")
                    for contact in value.get("contacts", []) or []
                }
                for message in value.get("messages", []) or []:
                    if message.get("type") != "text":
                        continue
                    sender = str(message.get("from") or "")
                    text = ((message.get("text") or {}).get("body") or "").strip()
                    if not sender or not text or not account_id:
                        continue
                    out.append({
                        "account_id": account_id,
                        "chat_id": sender,
                        "text": text,
                        "message_id": str(message.get("id") or ""),
                        "display_name": names.get(sender, ""),
                    })
            continue

        account_id = str(entry.get("id") or "")
        for event in entry.get("messaging", []) or []:
            message = event.get("message") or {}
            # `is_echo` is our own reply coming back; answering it would be a
            # loop, and on Instagram a fast one.
            if not message or message.get("is_echo"):
                continue
            text = (message.get("text") or "").strip()
            sender = str((event.get("sender") or {}).get("id") or "")
            if not text or not sender or not account_id:
                continue
            out.append({
                "account_id": account_id,
                "chat_id": sender,
                "text": text,
                "message_id": str(message.get("mid") or ""),
                "display_name": "",
            })
    return out
