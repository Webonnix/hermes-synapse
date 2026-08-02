"""Fake Telegram Bot API for exercising the real approve -> live-bot -> send
code paths without a real bot token or network access.

Mocks at the httpx transport boundary (via respx) rather than monkeypatching
_verify_bot_token or send_pending_reply directly, so the regex/token-shape
check, the getMe response parsing, and the sendMessage payload building all
run for real -- only the actual network call is faked.

Dispatches on the Bot API method name (the last URL path segment) instead of
registering one route per method: python-telegram-bot calls several
lifecycle methods on startup (getMe, deleteWebhook, ...) beyond the ones this
test cares about, and python-telegram-bot always POSTs while
_verify_bot_token uses a plain GET -- a catch-all avoids hand-tracking every
combination.
"""

from __future__ import annotations

import json
from typing import Any

import respx
from httpx import Response


class FakeTelegramAPI:
    def __init__(self, bot_username: str = "test_bot") -> None:
        self.bot_username = bot_username
        self.sent_messages: list[dict[str, Any]] = []
        self.get_me_calls = 0

    def install(self, router: respx.MockRouter, token: str) -> None:
        base_path = f"/bot{token}"
        router.route(url__regex=rf"https://api\.telegram\.org{base_path}/\w+$").mock(
            side_effect=self._dispatch
        )

    def _dispatch(self, request) -> Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return self._handle_get_me()
        if method == "sendMessage":
            return self._handle_send_message(request)
        # getUpdates (long-poll), deleteWebhook, and anything else PTB's
        # startup/shutdown lifecycle calls: a generic "ok" response is
        # enough since none of it is asserted on in tests.
        return Response(200, json={"ok": True, "result": []})

    def _handle_get_me(self) -> Response:
        self.get_me_calls += 1
        return Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": 1,
                    "is_bot": True,
                    "username": self.bot_username,
                    "first_name": self.bot_username,
                    "can_join_groups": True,
                    "can_read_all_group_messages": False,
                    "supports_inline_queries": False,
                },
            },
        )

    def _handle_send_message(self, request) -> Response:
        payload = json.loads(request.content)
        self.sent_messages.append(payload)
        return Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": len(self.sent_messages),
                    "date": 0,
                    "chat": {"id": payload.get("chat_id")},
                    "text": payload.get("text", ""),
                },
            },
        )
