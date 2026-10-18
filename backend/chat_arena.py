"""Model arena: one prompt, two models, answers side by side.

The chat UI's arena sends the same question to two catalogue models and shows
both answers next to each other with their timings, so the owner can pick a
model for a task by looking rather than guessing.

Deliberately a plain completion, not an agent turn: no tools, no history, no
memory. An agent turn may act — send a message, write a file — and running it
twice to compare wording would perform the action twice. Nothing is persisted;
the answers live only in the response.

Contenders are resolved through ``chat_models.resolve_chat_model`` like any
chat turn, so the browser names catalogue ids, never endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.chat_arena")

MAX_PROMPT_CHARS = 20000
MAX_CONTENDERS = 2
ARENA_SYSTEM_PROMPT = "Ты полезный ассистент. Отвечай по существу, на языке вопроса."


async def run_arena(
    prompt: str,
    contenders: List[Dict[str, str]],
    *,
    local_base_url: str,
    api_key: str,
    default_llm: Dict[str, str],
    system_prompt: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: float = 300.0,
) -> List[Dict[str, Any]]:
    """Run ``prompt`` against each contender in turn; one result per contender.

    Sequential on purpose: the local model serves one request at a time, and
    two at once would just make both slower and their timings meaningless.
    A contender that fails returns its error in place, the other still runs.
    """
    from backend import chat_models
    from backend.llm_client import STATUS_SUCCESS, call_llm_normalized

    messages = [
        {"role": "system", "content": (system_prompt or "").strip() or ARENA_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    results: List[Dict[str, Any]] = []
    for contender in contenders[:MAX_CONTENDERS]:
        model_id = str(contender.get("model") or "")[:256]
        source = str(contender.get("model_source") or "")
        result: Dict[str, Any] = {"model": model_id, "model_source": source}
        try:
            llm = await chat_models.resolve_chat_model(
                source, model_id, local_base_url=local_base_url, api_key=api_key,
            )
            llm = llm or default_llm
            started = time.perf_counter()
            normalized = await call_llm_normalized(
                api_base=llm.get("api_base", ""),
                api_key=llm.get("api_key", ""),
                model=llm.get("model", ""),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                provider_options={"provider": llm.get("provider", ""), "think": False},
            )
            result.update({
                "model": llm.get("model") or model_id,
                "status": normalized.status,
                "content": normalized.content or "",
                "error": None if normalized.status == STATUS_SUCCESS else (normalized.error_message or normalized.status),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "decode_ms": normalized.eval_ms,
                "input_tokens": normalized.usage.input_tokens,
                "output_tokens": normalized.usage.output_tokens,
            })
        except Exception as exc:
            logger.exception("Arena contender %s failed", model_id or "<default>")
            result.update({"status": "error", "content": "", "error": type(exc).__name__})
        results.append(result)
    return results


def validate_request(prompt: Any, contenders: Any) -> Optional[str]:
    """An error message for a malformed arena request, or None."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt is required"
    if len(prompt) > MAX_PROMPT_CHARS:
        return f"prompt is longer than {MAX_PROMPT_CHARS} characters"
    if not isinstance(contenders, list) or len(contenders) != MAX_CONTENDERS:
        return f"exactly {MAX_CONTENDERS} contenders are required"
    return None
