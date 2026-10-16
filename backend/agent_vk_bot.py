"""Per-agent VK community runtime (Bots Long Poll).

One polling task per active row in agent_messenger_bindings with
platform='vk'. VK offers two ways to receive community messages — a Callback
API that pushes to a public URL, and Bots Long Poll that the backend pulls.
This uses Long Poll for the same reason every other channel here polls or holds
an outbound socket: it works from a machine that is not reachable from the
internet, which is where this backend actually runs.

The message-handling itself lives in backend/social_turn.py, shared with the
Meta channels — this module is the VK transport and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

import httpx

from backend.social_turn import handle_incoming, split_text

logger = logging.getLogger("hermes.agent_vk_bot")

VK_API_BASE = "https://api.vk.com/method"
VK_API_VERSION = "5.199"
VK_TEXT_LIMIT = 4000
# VK holds the request open for this long when nothing is happening; a shorter
# wait would just mean more empty round-trips.
LONG_POLL_WAIT_SECONDS = 25
RECONNECT_DELAY_SECONDS = 5


class VKApiError(RuntimeError):
    pass


async def _vk_call(client: httpx.AsyncClient, method: str, access_token: str, **params: Any) -> Any:
    response = await client.post(
        f"{VK_API_BASE}/{method}",
        data={**params, "access_token": access_token, "v": VK_API_VERSION},
        timeout=30,
    )
    data = response.json()
    if data.get("error"):
        raise VKApiError((data["error"] or {}).get("error_msg") or "unknown VK error")
    return data.get("response")


class AgentVKBotManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._credentials: Dict[str, dict] = {}
        self._allowed_peer_ids: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}
        self._names: Dict[str, str] = {}  # from_id -> display name, per process

    async def start(
        self, binding_id: str, subagent_id: str, credentials: dict,
        allowed_chat_ids: Optional[List[str]] = None, overrides: Optional[dict] = None,
        response_mode: str = "draft",
    ) -> None:
        if binding_id in self._tasks:
            return
        self._credentials[binding_id] = dict(credentials or {})
        self._allowed_peer_ids[binding_id] = list(allowed_chat_ids or [])
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode
        self._tasks[binding_id] = asyncio.create_task(self._poll_loop(binding_id, subagent_id))
        logger.info("Agent VK bot started: binding=%s subagent=%s mode=%s", binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        if binding_id not in self._tasks:
            return
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode

    async def stop(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        self._credentials.pop(binding_id, None)
        self._allowed_peer_ids.pop(binding_id, None)
        self._overrides.pop(binding_id, None)
        self._response_modes.pop(binding_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown must not raise
                pass
        logger.info("Agent VK bot stopped: binding=%s", binding_id)

    async def send_text(self, binding_id: str, peer_id: str, text: str) -> None:
        """Used by the draft queue (backend/channel_replies.py) and by the turn
        handler. Resolves the credential fresh each time so a reconnected
        channel is not sending with a token this manager cached at startup."""
        credentials = self._credentials.get(binding_id)
        if not credentials:
            from backend.agent_messenger_governance import resolve_social_binding_credentials

            credentials = await asyncio.to_thread(resolve_social_binding_credentials, binding_id)
        if not credentials:
            raise RuntimeError("VK-канал больше не активен — ключ доступа недоступен")
        async with httpx.AsyncClient() as client:
            for chunk in split_text(text, VK_TEXT_LIMIT):
                await _vk_call(
                    client, "messages.send", credentials["access_token"],
                    peer_id=str(peer_id), message=chunk, random_id=random.getrandbits(31),
                )

    async def _display_name(self, client: httpx.AsyncClient, access_token: str, from_id: str) -> str:
        """Best-effort human name for the activity log and draft queue. A community
        or a privacy-restricted profile simply stays a bare id."""
        if from_id in self._names:
            return self._names[from_id]
        name = ""
        try:
            if not str(from_id).startswith("-"):
                users = await _vk_call(client, "users.get", access_token, user_ids=str(from_id))
                if users:
                    name = " ".join(filter(None, [users[0].get("first_name"), users[0].get("last_name")]))
        except Exception:
            name = ""
        self._names[from_id] = name
        return name

    async def _poll_loop(self, binding_id: str, subagent_id: str) -> None:
        credentials = self._credentials[binding_id]
        access_token = credentials["access_token"]
        group_id = str(credentials["group_id"]).lstrip("-")
        async with httpx.AsyncClient() as client:
            server: dict[str, Any] = {}
            while True:
                try:
                    if not server:
                        server = await _vk_call(
                            client, "groups.getLongPollServer", access_token, group_id=group_id
                        )
                    response = await client.get(
                        server["server"],
                        params={
                            "act": "a_check", "key": server["key"], "ts": server["ts"],
                            "wait": LONG_POLL_WAIT_SECONDS,
                        },
                        timeout=LONG_POLL_WAIT_SECONDS + 15,
                    )
                    payload = response.json()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Agent VK bot %s: long poll failed (%s), reconnecting", binding_id, exc)
                    server = {}
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                    continue

                failed = payload.get("failed")
                if failed == 1:
                    # History expired — VK hands back the ts to resume from.
                    server["ts"] = payload.get("ts", server.get("ts"))
                    continue
                if failed in (2, 3):
                    # Key expired / session lost: ask for a fresh server.
                    server = {}
                    continue

                server["ts"] = payload.get("ts", server.get("ts"))
                for update in payload.get("updates", []):
                    if update.get("type") != "message_new":
                        continue
                    try:
                        await self._handle_update(client, binding_id, subagent_id, access_token, update)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Agent VK bot %s: error handling update", binding_id)

    async def _handle_update(
        self, client: httpx.AsyncClient, binding_id: str, subagent_id: str, access_token: str, update: dict,
    ) -> None:
        message = (update.get("object") or {}).get("message") or {}
        text = (message.get("text") or "").strip()
        peer_id = str(message.get("peer_id") or "")
        from_id = str(message.get("from_id") or "")
        if not text or not peer_id:
            return
        # A community's own outgoing messages come back on the same long poll.
        if from_id.startswith("-"):
            return

        async def send(reply: str) -> None:
            await self.send_text(binding_id, peer_id, reply)

        await handle_incoming(
            binding_id=binding_id,
            platform="vk",
            subagent_id=subagent_id,
            chat_id=peer_id,
            text=text,
            send=send,
            external_user_id=from_id,
            display_name=await self._display_name(client, access_token, from_id),
            overrides=self._overrides.get(binding_id),
            response_mode=self._response_modes.get(binding_id, "draft"),
            event_id=str(message.get("conversation_message_id") or message.get("id") or ""),
        )

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_social_bindings, resolve_social_binding_credentials

        for binding in list_social_bindings("vk"):
            if binding.get("status") != "active":
                continue
            credentials = resolve_social_binding_credentials(binding["id"])
            if not credentials:
                logger.warning("Agent VK bot %s: active binding has no resolvable key, skipping", binding["id"])
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
                logger.exception("Failed to start agent VK bot %s at startup", binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._tasks.keys()):
            await self.stop(binding_id)


manager = AgentVKBotManager()
