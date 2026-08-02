"""Bounded conversation-history condenser.

agent.py's get_history() fetches only the last `max_history_len` messages
(database.py::get_chat_history uses a SQL LIMIT) -- everything older simply
never reaches the model again, even though it is still sitting in the
`messages` table for the dashboard to display. When the condenser is
enabled, messages about to fall out of that window are folded into a
running per-session summary instead of silently disappearing from the
model's context, and that summary is prepended to the verbatim window as a
synthetic system message (see agent.py::get_history).

Off by default (LLM_CONDENSER_ENABLED=false) -- it costs one extra LLM call
per condense cycle, and the hard-truncate behavior it replaces is what every
existing deployment already runs on.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.condenser")

CONDENSE_SYSTEM_PROMPT = (
    "You maintain a running summary of an ongoing conversation, for an AI "
    "assistant's own later reference -- not for the user to read. Given the "
    "previous summary (if any) and a batch of older messages that are about "
    "to be dropped from the assistant's visible context window, produce one "
    "updated summary that preserves durable facts, decisions, names, and "
    "open threads the assistant may still need. Be concise: a few short "
    "paragraphs at most. Write it as notes for the assistant itself, never "
    "addressed to the user."
)


async def _summarize_chunk(agent: Any, previous_summary: str, new_messages: List[Dict[str, Any]]) -> str:
    """Isolated so tests can monkeypatch it instead of needing a real LLM."""
    from backend.llm_client import call_llm_normalized

    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in new_messages)
    user_content = (
        (f"Previous summary:\n{previous_summary}\n\n" if previous_summary else "")
        + f"New messages to fold in:\n{transcript}"
    )
    result = await call_llm_normalized(
        api_base=agent.api_base,
        api_key=agent.api_key,
        model=agent.model,
        messages=[
            {"role": "system", "content": CONDENSE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=500,
        provider_options={
            "provider": agent.provider,
            "num_ctx": agent.ollama_num_ctx,
            "keep_alive": agent.ollama_keep_alive,
        },
    )
    return (result.content or "").strip()


async def maybe_condense(agent: Any, session_id: str) -> None:
    """Best-effort: never raises. A failed or skipped condense just means the
    next call retries with a slightly larger backlog -- it must never block
    or break a live reply."""
    if not getattr(agent, "condenser_enabled", False):
        return
    try:
        from backend import database as db

        keep_n = max(int(getattr(agent, "max_history_len", 20)), 1)
        trigger_extra = max(int(getattr(agent, "condense_trigger_extra", 10)), 1)

        keep_from_id = db.get_keep_from_message_id(session_id, keep_n)
        if keep_from_id is None:
            return  # fewer than keep_n messages total -- nothing falls out of the window yet

        existing = db.get_condensed_summary(session_id)
        covered_through_id = existing["covered_through_id"] if existing else 0
        if keep_from_id - 1 <= covered_through_id:
            return  # nothing new has fallen out of the window since the last condense

        new_chunk = db.get_messages_in_range(session_id, after_id=covered_through_id, before_id=keep_from_id)
        if len(new_chunk) < trigger_extra:
            return  # batch too small yet -- avoid summarizing one message at a time

        previous_summary = existing["summary"] if existing else ""
        summary = await _summarize_chunk(agent, previous_summary, new_chunk)
        if not summary:
            return
        db.save_condensed_summary(session_id, summary, covered_through_id=keep_from_id - 1)
        logger.info(
            "Condensed session %s: folded %d messages, now covering up to id=%d",
            session_id, len(new_chunk), keep_from_id - 1,
        )
    except Exception:
        logger.exception("Condense attempt failed for session %s (non-fatal)", session_id)


def summary_message(session_id: str) -> Optional[Dict[str, str]]:
    """Synchronous lookup used by get_history() to prepend the current
    summary (if any) to the verbatim window -- does not trigger condensing."""
    from backend import database as db

    existing = db.get_condensed_summary(session_id)
    if not existing or not existing.get("summary"):
        return None
    return {
        "role": "system",
        "content": f"[Сводка более ранней части этого диалога]\n{existing['summary']}",
    }
