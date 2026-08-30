"""Per-agent WhatsApp Business (Cloud API) runtime.

Same webhook-driven shape as the Meta messaging channels — WhatsApp Cloud is
part of the same Graph API — with two differences that matter:

* The conversation id is the sender's phone number in international format,
  not an opaque scoped id, so it is also what the owner sees in the dialogs
  panel and the draft queue.
* Outside the 24-hour customer service window, free-form text is rejected and
  only pre-approved templates may be sent. This module does not send templates:
  a reply that falls outside the window fails loudly instead of being quietly
  converted into some template the owner never wrote.
"""

from __future__ import annotations

import logging

from backend.social_turn import split_text
from backend.social_webhook_channel import WebhookChannelManager, graph_post

logger = logging.getLogger("hermes.agent_whatsapp_channel")

WHATSAPP_TEXT_LIMIT = 3800


class AgentWhatsAppChannelManager(WebhookChannelManager):
    platforms = ("whatsapp",)

    def _platform_for_credentials(self, credentials: dict) -> str:
        return "whatsapp"

    async def _deliver(self, binding: dict, chat_id: str, text: str) -> None:
        credentials = binding["credentials"]
        for chunk in split_text(text, WHATSAPP_TEXT_LIMIT):
            await graph_post(
                f"{credentials['phone_number_id']}/messages",
                credentials["access_token"],
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": str(chat_id),
                    "type": "text",
                    "text": {"preview_url": False, "body": chunk},
                },
            )


manager = AgentWhatsAppChannelManager()
