"""Shared runtime for the social channels that are pushed to, not polled.

Telegram, Discord, Slack, Matrix, Email and VK all *pull*: the backend holds a
connection open and asks for new messages, which is why they work from a
machine with no inbound route from the internet. The three Meta platforms
(Instagram, Facebook Pages, WhatsApp Cloud) have no equivalent — Meta only
delivers by HTTP POST to a URL you register. So instead of a polling task per
binding, these managers keep a registry of what is active and wait to be called
by backend/social_webhooks.py.

Two consequences worth stating plainly, because they surface in the dashboard:

* A binding here can be perfectly healthy — credential valid, agent enabled —
  and still receive nothing, because the webhook was never registered or the
  backend is not publicly reachable. That is a deployment fact, so it is shown
  as such in the SMM Factory connection card rather than faked as an error.
* Meta retries a delivery it did not get a 200 for, and retries carry the same
  message id. Answering twice would cost two agent runs and read as the bot
  repeating itself, so message ids are remembered here and replays dropped.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.social_webhook_channel")

# Enough to cover a burst of retries without pretending to be a durable store —
# a replay arriving after a backend restart is answered again, and that is an
# acceptable trade for not adding a table for it.
_SEEN_MESSAGE_LIMIT = 2000


class WebhookChannelManager:
    """Same manager surface every channel runtime exposes (start / stop /
    update_live_settings / start_all_active / stop_all), minus the polling task.

    Subclasses declare which platforms they serve and how to put text on the
    wire; everything else — registry, dedup, the turn itself — is here.
    """

    platforms: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._bindings: Dict[str, dict] = {}
        self._seen_message_ids: "OrderedDict[str, None]" = OrderedDict()

    # ── subclass contract ───────────────────────────────────────────────────

    async def _deliver(self, binding: dict, chat_id: str, text: str) -> None:
        raise NotImplementedError

    def _platform_for_credentials(self, credentials: dict) -> str:
        raise NotImplementedError

    # ── manager surface ─────────────────────────────────────────────────────

    async def start(
        self, binding_id: str, subagent_id: str, credentials: dict,
        allowed_chat_ids: Optional[List[str]] = None, overrides: Optional[dict] = None,
        response_mode: str = "draft",
    ) -> None:
        platform = self._platform_for_credentials(credentials or {})
        self._bindings[binding_id] = {
            "id": binding_id,
            "platform": platform,
            "subagent_id": subagent_id,
            "credentials": dict(credentials or {}),
            "allowed_chat_ids": list(allowed_chat_ids or []),
            "overrides": overrides or {},
            "response_mode": response_mode,
        }
        logger.info("%s channel active: binding=%s subagent=%s mode=%s", platform, binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        state = self._bindings.get(binding_id)
        if not state:
            return
        state["overrides"] = overrides or {}
        state["response_mode"] = response_mode

    async def stop(self, binding_id: str) -> None:
        state = self._bindings.pop(binding_id, None)
        if state:
            logger.info("%s channel stopped: binding=%s", state["platform"], binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_social_bindings, resolve_social_binding_credentials

        for platform in self.platforms:
            for binding in list_social_bindings(platform):
                if binding.get("status") != "active":
                    continue
                credentials = resolve_social_binding_credentials(binding["id"])
                if not credentials:
                    logger.warning(
                        "%s channel %s: active binding has no resolvable credentials, skipping",
                        platform, binding["id"],
                    )
                    continue
                overrides = {
                    "system_prompt": binding.get("system_prompt_override"),
                    "model": binding.get("model_override"),
                    "model_provider": binding.get("model_provider_override"),
                }
                try:
                    await self.start(
                        binding["id"], binding["subagent_id"], credentials, binding.get("allowed_chat_ids"),
                        overrides, binding.get("response_mode") or "draft",
                    )
                except Exception:
                    logger.exception("Failed to register %s channel %s at startup", platform, binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._bindings):
            await self.stop(binding_id)

    # ── used by the webhook router and the draft queue ──────────────────────

    def is_active(self, binding_id: str) -> bool:
        return binding_id in self._bindings

    async def _state_for(self, binding_id: str) -> Optional[dict]:
        """The live registry entry, rebuilt from the DB if this process has not
        seen the binding yet (a webhook can arrive before start_all_active has
        run, and a binding activated in another worker is not in this one)."""
        state = self._bindings.get(binding_id)
        if state:
            return state
        from backend.agent_messenger_governance import get_binding, resolve_social_binding_credentials

        binding = await asyncio.to_thread(get_binding, binding_id)
        if not binding or binding.get("status") != "active":
            return None
        credentials = await asyncio.to_thread(resolve_social_binding_credentials, binding_id)
        if not credentials:
            return None
        await self.start(
            binding_id, binding["subagent_id"], credentials, binding.get("allowed_chat_ids"),
            {
                "system_prompt": binding.get("system_prompt_override"),
                "model": binding.get("model_override"),
                "model_provider": binding.get("model_provider_override"),
            },
            binding.get("response_mode") or "draft",
        )
        return self._bindings.get(binding_id)

    async def send_text(self, binding_id: str, chat_id: str, text: str) -> None:
        state = await self._state_for(binding_id)
        if not state:
            raise RuntimeError("Канал больше не активен — учётные данные недоступны")
        await self._deliver(state, chat_id, text)

    def _already_handled(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = None
        while len(self._seen_message_ids) > _SEEN_MESSAGE_LIMIT:
            self._seen_message_ids.popitem(last=False)
        return False

    async def handle_message(
        self, binding_id: str, chat_id: str, text: str, *,
        message_id: str = "", display_name: str = "", external_user_id: str = "",
    ) -> None:
        """One inbound message from the webhook router."""
        if self._already_handled(message_id):
            logger.debug("Duplicate %s delivery ignored: %s", binding_id, message_id)
            return
        state = await self._state_for(binding_id)
        if not state:
            logger.warning("Webhook for %s arrived but the binding is not active", binding_id)
            return

        from backend.social_turn import handle_incoming

        async def send(reply: str) -> None:
            await self._deliver(state, chat_id, reply)

        await handle_incoming(
            binding_id=binding_id,
            platform=state["platform"],
            subagent_id=state["subagent_id"],
            chat_id=str(chat_id),
            text=text,
            send=send,
            external_user_id=external_user_id or str(chat_id),
            display_name=display_name,
            overrides=state.get("overrides"),
            response_mode=state.get("response_mode", "draft"),
        )


async def graph_post(path: str, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One Graph API write, with Meta's error envelope surfaced as a RuntimeError
    carrying its own message — those are specific ("outside the 24 hour window",
    "message failed to send because more than 24 hours…") and worth showing."""
    import httpx

    from backend.agent_messenger_governance import GRAPH_API_BASE

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{GRAPH_API_BASE}/{path.lstrip('/')}",
            params={"access_token": access_token},
            json=payload,
        )
    try:
        data = response.json()
    except Exception:
        response.raise_for_status()
        return {}
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError((data["error"] or {}).get("message") or "Meta Graph API error")
    return data if isinstance(data, dict) else {}
