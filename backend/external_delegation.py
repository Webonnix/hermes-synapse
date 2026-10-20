"""Explicit delegation from the local model to the external AI Router tiers.

backend/local_orchestrator.py already escalates a *whole turn* when its
yes/no gate decides the request is too hard for the local model. That gate
runs before the model has thought about the task, so the model itself never
learns that stronger models exist and cannot hand them just one piece of the
work (draft this module, check this argument) while keeping the rest of the
turn — tools, memory, the conversation — local.

This module gives it that choice:

* ``delegate_to_external_model`` (backend/tools.py) sends one self-contained
  subtask to a configured binding tier (backend/router_tiers.py; in this
  deployment OmniRoute → DeepSeek) and returns the answer as a tool result.
  "fast" tries tiers from the lowest rank up, "strong" from the highest down,
  and each failure falls through to the next tier.
* ``delegation_hint()`` is the system-prompt paragraph that tells the main
  agent which external models it has and when to use them.

The call goes through ``call_llm_normalized`` like every other external call,
so the privacy gateway (backend/privacy) pseudonymizes personal data before
it leaves the box and restores it in the answer. Spend is logged to
router_usage and returned as ``cost_usd`` so agent.py's paid-tool budget
accounting records it against the calling agent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("hermes.external_delegation")

TOOL_NAME = "delegate_to_external_model"

_SYSTEM = (
    "You are a strong specialist model helping a smaller local assistant. "
    "Solve the task completely and return only the result, without preamble. "
    "Answer in the language of the task. Placeholders like [PERSON_1] are "
    "pseudonymized data: keep them exactly as they are."
)

_MAX_TASK_CHARS = 60_000
_MIN_TOKENS = 4096


def external_tiers() -> List[Dict[str, Any]]:
    """Active binding tiers ordered by rank; local tiers are not external."""
    from backend import router_tiers

    try:
        tiers = router_tiers.list_active_tiers_resolved()
    except Exception as exc:
        logger.warning("Could not read router tiers: %s", exc)
        return []
    return sorted((t for t in tiers if t.get("kind") == "binding"), key=lambda t: t.get("tier_rank", 0))


def _model(tier: Dict[str, Any]) -> str:
    return tier.get("model_override") or tier.get("default_model") or ""


def delegation_hint() -> str:
    """System-prompt paragraph for the main agent; empty when no external tier exists."""
    tiers = external_tiers()
    if not tiers:
        return ""
    lines = "\n".join(f"- {t['label']} ({_model(t) or 'модель по умолчанию'})" for t in tiers)
    return (
        "\n\n[Внешние модели через AI Router (OmniRoute)]:\n"
        f"{lines}\n"
        f"Ты можешь поручить им часть работы инструментом {TOOL_NAME}: level=fast — первая модель "
        "списка, level=strong — последняя (самая сильная). Делегируй, когда задача тебе не по силам "
        "или требует лучшего качества: большой или сложный код, глубокий анализ, длинный "
        "качественный текст, перевод, проверка рассуждений, а также когда пользователь прямо просит "
        "DeepSeek, OmniRoute или «внешнюю модель». Короткие ответы, разговор и вызовы обычных "
        "инструментов делай сам. Передавай задачу целиком: внешняя модель не видит переписку и "
        "инструменты, поэтому положи в context всё нужное. Личные данные псевдонимизируются "
        "автоматически. Результат проверь и подай пользователю своими словами, указав, что он от "
        "внешней модели."
    )


def _ordered(tiers: List[Dict[str, Any]], level: str) -> List[Dict[str, Any]]:
    return list(reversed(tiers)) if level == "strong" else list(tiers)


async def delegate(task: str, context: str = "", level: str = "fast", max_tokens: int = 4096) -> Dict[str, Any]:
    from backend.llm_client import STATUS_SUCCESS, call_llm_normalized
    from backend.local_orchestrator import _log

    task = (task or "").strip()
    if not task:
        return {"status": "error", "error": "task is required"}
    level = level if level in {"fast", "strong"} else "fast"
    content = task if not context.strip() else f"{task}\n\nКонтекст:\n{context.strip()}"
    content = content[:_MAX_TASK_CHARS]
    # DeepSeek-style tiers reason before they answer and the reasoning counts
    # against max_tokens: the local model asked for 1024 on a live run and got
    # "reasoning only, no visible answer" from both tiers. The model's number is
    # a floor-clamped hint, not a hard cap below what a reasoning tier needs.
    max_tokens = max(_MIN_TOKENS, min(int(max_tokens or _MIN_TOKENS), 16384))

    tiers = external_tiers()
    if not tiers:
        return {"status": "unavailable", "error": "No external AI Router tier is configured (Admin → AI Router)."}

    errors = []
    for tier in _ordered(tiers, level):
        api_base, api_key = tier["api_base"], tier["api_key"]
        model = _model(tier)
        result = await call_llm_normalized(
            api_base=api_base, api_key=api_key, model=model,
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            temperature=0.2, max_tokens=max_tokens, timeout=240, max_retries=1,
            provider_options={"provider": "openai_compatible"},
        )
        _log(tier.get("id"), tier["label"], tier.get("tier_rank", 0), "binding", result, tier["provider_binding_id"])
        if result.status == STATUS_SUCCESS and (result.content or "").strip():
            cost = 0.0
            try:
                from backend.cost import calculate_cost

                usage = result.usage
                cost = calculate_cost(
                    result.model or model,
                    (usage.input_tokens or 0) if usage else 0,
                    (usage.output_tokens or 0) if usage else 0,
                    provider_id=tier["provider_binding_id"],
                )
            except Exception:
                logger.exception("Delegation cost calculation failed (non-fatal)")
            return {
                "status": "success",
                "tier": tier["label"],
                "model": result.model or model,
                "answer": result.content,
                "cost_usd": round(cost, 6),
            }
        errors.append(f"{tier['label']}: {result.status} {result.error_message or ''}".strip())
    return {"status": "failed", "error": "Every external tier failed", "attempts": errors}
