"""Per-agent Instagram and Facebook Page messaging runtime.

Both platforms are the same Meta Graph endpoint with a different account id, so
one manager serves both: an Instagram binding sends through the IG business
account id, a Facebook one through the page id, and each carries its own page
access token. Incoming messages arrive at backend/social_webhooks.py and are
handed to `manager.handle_message`.

Meta's 24-hour standard messaging window applies to both: a reply outside it is
rejected by the API. That surfaces as a delivery error on the draft rather than
being hidden — a draft the owner wrote and could not send is something they need
to see, not something to swallow.
"""

from __future__ import annotations

import logging

from backend.social_turn import split_text
from backend.social_webhook_channel import WebhookChannelManager, graph_post

logger = logging.getLogger("hermes.agent_meta_channel")

# Meta accepts 2000 characters per message on both Instagram and Messenger.
META_TEXT_LIMIT = 1900


class AgentMetaChannelManager(WebhookChannelManager):
    platforms = ("instagram", "facebook")

    def _platform_for_credentials(self, credentials: dict) -> str:
        return "instagram" if credentials.get("ig_user_id") else "facebook"

    @staticmethod
    def _account_id(state: dict) -> str:
        credentials = state["credentials"]
        return str(credentials.get("ig_user_id") or credentials.get("page_id") or "")

    async def _deliver(self, binding: dict, chat_id: str, text: str) -> None:
        access_token = binding["credentials"]["access_token"]
        account_id = self._account_id(binding)
        for chunk in split_text(text, META_TEXT_LIMIT):
            await graph_post(
                f"{account_id}/messages",
                access_token,
                {
                    "recipient": {"id": str(chat_id)},
                    "messaging_type": "RESPONSE",
                    "message": {"text": chunk},
                },
            )


manager = AgentMetaChannelManager()
