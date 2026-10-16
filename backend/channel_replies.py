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
import json
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
        try:
            # image_urls: JSON list of /api/plots or /api/generated-images
            # URLs captured straight from a tool call during the turn that
            # drafted this reply (agent.py's last_generated_images) -- kept
            # as its own column rather than only being re-parsed out of
            # drafted_reply's markdown later, so an approved draft still
            # delivers the actual photo even if the model's own text
            # mistyped the filename it echoed back.
            connection.execute(
                "ALTER TABLE pending_channel_replies ADD COLUMN image_urls TEXT NOT NULL DEFAULT '[]'"
            )
        except sqlite3.OperationalError:
            pass  # already migrated


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
    image_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    _init_schema()
    reply_id = f"draft-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO pending_channel_replies
                (id, binding_id, platform, subagent_id, chat_id, incoming_from,
                 incoming_text, drafted_reply, status, created_at, updated_at, image_urls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (reply_id, binding_id, platform, subagent_id, chat_id, incoming_from,
             incoming_text, drafted_reply, now, now, json.dumps(image_urls or [])),
        )
        row = connection.execute("SELECT * FROM pending_channel_replies WHERE id = ?", (reply_id,)).fetchone()
    result = _row_to_dict(row)
    try:
        from backend import inbox

        inbox.record_draft(binding_id, platform, chat_id, drafted_reply, reply_id)
    except Exception:
        logger.exception("Could not archive draft %s in the inbox", reply_id)
    _notify_owner_new_draft(result)
    return result


def _archive_delivery(reply_id: str, delivery: str, final_text: Optional[str] = None) -> None:
    try:
        from backend import inbox

        inbox.mark_draft(reply_id, delivery, final_text)
    except Exception:
        logger.exception("Could not update the archived draft %s", reply_id)


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

    try:
        known_image_urls = json.loads(reply.get("image_urls") or "[]")
    except (TypeError, ValueError):
        known_image_urls = []
    try:
        await send_to_channel(
            reply["binding_id"], reply["platform"], reply["chat_id"], final_text,
            known_image_urls=known_image_urls,
        )
    except Exception:
        # The draft itself stays pending so the owner can retry; the archive
        # records that this attempt did not reach the person.
        _archive_delivery(reply_id, "failed", final_text)
        raise
    _set_status(reply_id, "sent", sent_at=_now())
    _archive_delivery(reply_id, "sent", final_text)
    return get_pending_reply(reply_id)


async def send_to_channel(
    binding_id: str, platform: str, chat_id: str, final_text: str,
    known_image_urls: Optional[list[str]] = None,
) -> None:
    """Delivers one message on a bound channel, using that channel's own stored
    credential. Split out of send_pending_reply() so the billing webhook can
    tell a buyer their access is open on the same line they bought it
    (backend/payments.py), without going through the draft queue."""
    reply = {"binding_id": binding_id, "platform": platform, "chat_id": chat_id}

    if reply["platform"] == "telegram":
        from backend.agent_messenger_governance import resolve_telegram_binding_token
        from backend import agent_bot

        token = resolve_telegram_binding_token(reply["binding_id"])
        if not token:
            raise RuntimeError("This channel's bot token is no longer available — was it revoked?")
        import httpx

        # A drafted reply may embed one of our own /api/plots or
        # /api/generated-images markdown links (e.g. from the generate_image
        # tool) — Telegram can't fetch those from localhost, so resolve them
        # to the actual file on disk and send it as a real photo attachment.
        # known_image_urls (captured straight from the tool call that drafted
        # this reply) still gets the photo out even if the model's own text
        # mistyped the filename it echoed back.
        clean_text, image_paths = agent_bot.extract_local_images(final_text, known_urls=known_image_urls)
        async with httpx.AsyncClient(timeout=30) as client:
            for path in image_paths:
                try:
                    with open(path, "rb") as photo:
                        response = await client.post(
                            f"https://api.telegram.org/bot{token}/sendPhoto",
                            data={"chat_id": reply["chat_id"]},
                            files={"photo": (os.path.basename(path), photo.read())},
                        )
                        response.raise_for_status()
                except Exception:
                    logger.exception("Failed to send attached image %s for reply", path)
            text_to_send = clean_text if image_paths else final_text
            if text_to_send.strip():
                for chunk in agent_bot._split_text(text_to_send):
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": reply["chat_id"], "text": chunk},
                    )
                    response.raise_for_status()
    elif reply["platform"] == "matrix":
        from backend import agent_matrix_bot

        # Sent through the running bot rather than a throwaway client: that one
        # holds the olm store, so a reply into an encrypted room is encrypted.
        # A fresh client would happily post it as plaintext into an E2EE room.
        await agent_matrix_bot.manager.send_text(reply["binding_id"], reply["chat_id"], final_text)
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
    elif reply["platform"] == "vk":
        from backend import agent_vk_bot

        await agent_vk_bot.manager.send_text(reply["binding_id"], reply["chat_id"], final_text)
    elif reply["platform"] in ("instagram", "facebook"):
        from backend import agent_meta_channel

        # Meta only accepts a free-form reply inside the 24-hour window that the
        # person's own last message opened. A draft reviewed after that fails
        # here with Meta's own wording rather than silently vanishing.
        await agent_meta_channel.manager.send_text(reply["binding_id"], reply["chat_id"], final_text)
    elif reply["platform"] == "whatsapp":
        from backend import agent_whatsapp_channel

        await agent_whatsapp_channel.manager.send_text(reply["binding_id"], reply["chat_id"], final_text)
    else:
        raise ValueError(f"Unknown platform: {reply['platform']}")


def discard_pending_reply(reply_id: str) -> None:
    reply = get_pending_reply(reply_id)
    if not reply:
        raise KeyError(reply_id)
    if reply["status"] != "pending":
        raise ValueError(f"Reply {reply_id} is already {reply['status']}")
    _set_status(reply_id, "discarded")
    _archive_delivery(reply_id, "discarded")


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
                f"Проверьте и отправьте в разделе «Входящие» (или «SMM Factory») дашборда."
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
