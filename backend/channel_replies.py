"""Draft-approval queue for 'draft' mode messenger bindings.

When a binding's response_mode is 'draft' (the default — see
agent_messenger_governance.py), the per-agent channel runtimes (agent_bot.py,
agent_matrix_bot.py, agent_discord_bot.py, agent_slack_bot.py,
agent_email_channel.py) never send a reply on their own. Instead they call
create_pending_reply() here, which stores the drafted text and pings the
owner on the main Vexa Telegram bot (same owner-notification pattern as
dev_runs.py). The owner reviews, optionally edits, and explicitly sends
from the dashboard — that's the human click that keeps a 'draft' channel
from ever putting words in the owner's mouth without them seeing it first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database import DB_PATH

logger = logging.getLogger("hermes.channel_replies")

STATUSES = ("pending", "sent", "discarded")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_channel_replies (
                id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                subagent_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                incoming_from TEXT NOT NULL DEFAULT '',
                incoming_text TEXT NOT NULL,
                drafted_reply TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def create_pending_reply(
    binding_id: str,
    platform: str,
    subagent_id: str,
    chat_id: str,
    incoming_text: str,
    drafted_reply: str,
    incoming_from: str = "",
) -> dict[str, Any]:
    _init_schema()
    reply_id = f"draft-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO pending_channel_replies
                (id, binding_id, platform, subagent_id, chat_id, incoming_from,
                 incoming_text, drafted_reply, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (reply_id, binding_id, platform, subagent_id, chat_id, incoming_from,
             incoming_text, drafted_reply, now, now),
        )
        row = connection.execute("SELECT * FROM pending_channel_replies WHERE id = ?", (reply_id,)).fetchone()
    result = _row_to_dict(row)
    _notify_owner_new_draft(result)
    return result


def list_pending_replies(status: str = "pending") -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM pending_channel_replies WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_pending_reply(reply_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM pending_channel_replies WHERE id = ?", (reply_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _set_status(reply_id: str, status: str, sent_at: Optional[str] = None) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE pending_channel_replies SET status = ?, updated_at = ?, sent_at = ? WHERE id = ?",
            (status, _now(), sent_at, reply_id),
        )


async def send_pending_reply(reply_id: str, edited_text: Optional[str] = None) -> dict[str, Any]:
    """Delivers the (optionally edited) draft through the same platform it
    came in on, then marks it sent. Raises KeyError/ValueError on bad state."""
    reply = get_pending_reply(reply_id)
    if not reply:
        raise KeyError(reply_id)
    if reply["status"] != "pending":
        raise ValueError(f"Reply {reply_id} is already {reply['status']}")

    final_text = (edited_text if edited_text is not None else reply["drafted_reply"]).strip()
    if not final_text:
        raise ValueError("Cannot send an empty reply")

    if reply["platform"] == "telegram":
        from backend.agent_messenger_governance import resolve_telegram_binding_token
        from backend import agent_bot

        token = resolve_telegram_binding_token(reply["binding_id"])
        if not token:
            raise RuntimeError("This channel's bot token is no longer available — was it revoked?")
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            for chunk in agent_bot._split_text(final_text):
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": reply["chat_id"], "text": chunk},
                )
                response.raise_for_status()
    elif reply["platform"] == "matrix":
        from backend.agent_messenger_governance import resolve_matrix_binding_credentials
        from backend import agent_matrix_bot
        from nio import AsyncClient

        credentials = resolve_matrix_binding_credentials(reply["binding_id"])
        if not credentials:
            raise RuntimeError("This channel's credentials are no longer available — was it revoked?")
        client = AsyncClient(credentials["homeserver_url"], credentials["user_id"])
        client.access_token = credentials["access_token"]
        client.user_id = credentials["user_id"]
        try:
            for chunk in agent_matrix_bot._split_text(final_text):
                await client.room_send(
                    reply["chat_id"], message_type="m.room.message", content={"msgtype": "m.text", "body": chunk}
                )
        finally:
            await client.close()
    elif reply["platform"] == "discord":
        from backend.agent_messenger_governance import resolve_discord_binding_token
        from backend import agent_discord_bot

        token = resolve_discord_binding_token(reply["binding_id"])
        if not token:
            raise RuntimeError("This channel's bot token is no longer available — was it revoked?")
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            for chunk in agent_discord_bot._split_text(final_text):
                response = await client.post(
                    f"https://discord.com/api/v10/channels/{reply['chat_id']}/messages",
                    headers={"Authorization": f"Bot {token}"},
                    json={"content": chunk},
                )
                response.raise_for_status()
    elif reply["platform"] == "slack":
        from backend.agent_messenger_governance import resolve_slack_binding_credentials
        from backend import agent_slack_bot
        from slack_sdk.web.async_client import AsyncWebClient

        credentials = resolve_slack_binding_credentials(reply["binding_id"])
        if not credentials:
            raise RuntimeError("This channel's credentials are no longer available — was it revoked?")
        client = AsyncWebClient(token=credentials["bot_token"])
        for chunk in agent_slack_bot._split_text(final_text):
            await client.chat_postMessage(channel=reply["chat_id"], text=chunk)
    elif reply["platform"] == "email":
        import json as _json
        from backend.agent_messenger_governance import resolve_email_binding_credentials
        from backend import agent_email_channel

        credentials = resolve_email_binding_credentials(reply["binding_id"])
        if not credentials:
            raise RuntimeError("This channel's credentials are no longer available — was it revoked?")
        thread = _json.loads(reply["chat_id"])
        await asyncio.to_thread(
            agent_email_channel.manager._send_smtp,
            credentials, thread["to"], thread.get("subject", ""), final_text,
            thread.get("message_id", ""), thread.get("references", ""),
        )
    else:
        raise ValueError(f"Unknown platform: {reply['platform']}")

    _set_status(reply_id, "sent", sent_at=_now())
    return get_pending_reply(reply_id)


def discard_pending_reply(reply_id: str) -> None:
    reply = get_pending_reply(reply_id)
    if not reply:
        raise KeyError(reply_id)
    if reply["status"] != "pending":
        raise ValueError(f"Reply {reply_id} is already {reply['status']}")
    _set_status(reply_id, "discarded")


def _notify_owner_new_draft(reply: dict[str, Any]) -> None:
    """Pings the owner on the main Vexa bot that a draft is waiting — same
    owner-chat pattern dev_runs.py uses for run events. Best-effort: a failed
    notification never blocks the draft from being queued."""
    import asyncio

    async def _send() -> None:
        try:
            import backend.bot as bot
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
            if not chat_id or not getattr(bot, "telegram_app", None) or not bot.telegram_app.bot:
                return
            preview = (reply["drafted_reply"] or "")[:300]
            text = (
                f"✉️ Новый черновик ответа ждёт подтверждения ({reply['platform']}, от {reply.get('incoming_from') or 'неизвестно'}):\n\n"
                f"{preview}\n\n"
                f"Проверьте и отправьте в разделе «Каналы связи» дашборда."
            )
            await bot.telegram_app.bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:
            logger.warning("Draft-reply owner notification failed: %s", exc)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send())
    except RuntimeError:
        # No running loop (e.g. called from sync test code) — skip the notification.
        pass


_init_schema()
