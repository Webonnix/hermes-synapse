"""One incoming message, from the gate to the reply, shared by the social channels.

The five messenger runtimes (agent_bot.py, agent_matrix_bot.py,
agent_discord_bot.py, agent_slack_bot.py, agent_email_channel.py) each carry
their own inline copy of this sequence. It is the same sequence every time:

    authorize → agent turn → record → send (auto-labeled) or queue (draft)

Copying it a sixth, seventh and eighth time for VK, Meta and WhatsApp would
mean three more places where the rule that matters most here — *nothing goes
out under the owner's name without either a human click or an explicit
assistant disclosure* — could drift. So the social channels share this one
implementation and only supply "how do I put text on the wire".

The transport callback receives the whole reply; splitting is left to it
because every provider has a different limit and a different idea of what a
message is.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("hermes.social_turn")

# Every provider here accepts at least 4096 characters in one text message;
# the transports narrow it where their own limit is lower.
DEFAULT_TEXT_LIMIT = 4000

ERROR_REPLY = "Произошла ошибка при обработке запроса."


def split_text(text: str, limit: int = DEFAULT_TEXT_LIMIT) -> list[str]:
    """Same paragraph/line-boundary splitting as backend/agent_bot.py's _split_text."""
    remaining = (text or "").strip()
    if not remaining:
        return ["Ответ модели пуст."]
    chunks = []
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split_at < limit // 3:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


async def handle_incoming(
    *,
    binding_id: str,
    platform: str,
    subagent_id: str,
    chat_id: str,
    text: str,
    send: Callable[[str], Awaitable[None]],
    external_user_id: str = "",
    display_name: str = "",
    overrides: Optional[dict[str, Any]] = None,
    response_mode: str = "draft",
) -> None:
    """Runs one turn for a social channel. Never raises to its caller: a webhook
    handler that raises would make the provider retry the same message, and a
    long-poll loop that raises would drop the connection."""
    from backend import bot_access_gate

    try:
        decision = await asyncio.to_thread(
            bot_access_gate.authorize,
            binding_id, platform, chat_id, text, external_user_id, display_name,
        )
    except Exception:
        logger.exception("%s %s: access gate failed", platform, binding_id)
        return

    if decision.action == bot_access_gate.IGNORE:
        return
    if decision.action == bot_access_gate.REPLY:
        # A gate reply is plumbing (a token prompt, a menu, a paywall notice),
        # not the agent speaking, so it goes out as-is with no disclosure.
        await _safe_send(send, decision.reply_text, platform, binding_id)
        return

    from backend.database import get_subagent

    subagent = await asyncio.to_thread(get_subagent, subagent_id)
    if not subagent or not subagent.get("is_enabled"):
        await _safe_send(
            send,
            bot_access_gate.AGENT_OFFLINE_MESSAGE if decision.scope == "public" else "Этот агент сейчас отключён, Альберт.",
            platform, binding_id,
        )
        return

    from backend.agent_messenger_governance import apply_binding_overrides

    subagent = apply_binding_overrides(subagent, overrides)
    subagent = bot_access_gate.apply_subscriber_context(subagent, decision)

    from backend.agent import agent_instance

    session_id = decision.session_id or f"{platform}:{binding_id}:{chat_id}"
    try:
        response_text = await agent_instance._respond_as_subagent(text, subagent, chat_id=session_id)
    except Exception:
        logger.exception("%s %s: error handling message", platform, binding_id)
        bot_access_gate.record_turn(decision, agent_instance.last_run_metadata.get(session_id), "")
        await _safe_send(send, ERROR_REPLY, platform, binding_id)
        return
    bot_access_gate.record_turn(decision, agent_instance.last_run_metadata.get(session_id), response_text)

    mode = bot_access_gate.effective_response_mode(decision, response_mode)
    if mode == "auto_labeled":
        from backend.agent_messenger_governance import auto_reply_disclosure

        await _safe_send(send, response_text + auto_reply_disclosure(binding_id), platform, binding_id)
        return

    # draft mode: the person on the other end gets nothing until the owner
    # reviews and sends it themselves from the SMM Factory dashboard.
    from backend.channel_replies import create_pending_reply

    # Called on the event loop, not in a worker thread: create_pending_reply
    # notifies the owner by scheduling a task on the *running* loop, and off the
    # loop there is none — the draft would be queued with the ping silently
    # skipped, which is the one part of draft mode the owner actually sees.
    create_pending_reply(
        binding_id=binding_id,
        platform=platform,
        subagent_id=subagent_id,
        chat_id=chat_id,
        incoming_text=text,
        drafted_reply=response_text,
        incoming_from=display_name or external_user_id or chat_id,
    )


async def _safe_send(send: Callable[[str], Awaitable[None]], text: str, platform: str, binding_id: str) -> None:
    """A failed delivery is logged and recorded, never raised: the turn itself
    already happened (and was already billed), so tearing down the caller on a
    transport hiccup would only cost a duplicate run on the provider's retry."""
    try:
        await send(text)
    except Exception as exc:
        logger.exception("%s %s: could not deliver reply", platform, binding_id)
        try:
            from backend import channel_activity

            channel_activity.record(binding_id, platform, "error", f"Не удалось отправить ответ: {exc}")
        except Exception:
            pass
