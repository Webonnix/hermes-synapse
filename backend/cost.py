"""Centralized cost calculation for LLM usage.

Single source of truth for token pricing so estimates and real usage agree.
Rates are USD per 1,000,000 tokens (prompt_rate, completion_rate).

The previous logic lived inline in ``agent.py`` (``calculate_cost``); it is kept
here and re-exported for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger("hermes.cost")

# model-substring -> (prompt_rate, completion_rate) per 1M tokens.
# Ordered by specificity is not required; longest match wins.
_PRICING: Dict[str, Tuple[float, float]] = {
    "gemini-2.5-pro": (0.075, 0.30),
    "gemini-2.5-flash": (0.0375, 0.15),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-4": (3.00, 15.00),
    "deepseek-r2": (0.55, 2.19),
    "deepseek-r1": (0.55, 2.19),
    "deepseek-v4-flash": (0.07, 0.14),
    "deepseek-v3": (0.14, 0.28),
}

# Default when the model is unknown (Gemini 2.5 Pro pricing, as before).
_DEFAULT_RATES: Tuple[float, float] = (0.075, 0.30)


def _provider_override_rates(provider_id: Optional[str]) -> Optional[Tuple[float, float]]:
    """A provider_bindings row may carry its own contracted cost_per_1m_input/
    output, set by the owner because it's known to differ from this module's
    model-name guess (e.g. a discounted/enterprise rate, or a model this table
    has no entry for at all). Lazy-imported to avoid a cost.py <-> provider_
    governance.py import cycle; 'ollama' (local) is never overridden — it's
    always free."""
    if not provider_id or provider_id == "ollama":
        return None
    try:
        from backend.provider_governance import get_binding
        binding = get_binding(provider_id)
    except Exception:
        logger.debug("Could not look up provider %s for a pricing override", provider_id, exc_info=True)
        return None
    if not binding:
        return None
    rate_in, rate_out = binding.get("cost_per_1m_input"), binding.get("cost_per_1m_output")
    if rate_in is None or rate_out is None:
        return None
    return float(rate_in), float(rate_out)


def get_rates(model: str, provider_id: Optional[str] = None) -> Tuple[float, float]:
    """Return (prompt_rate, completion_rate) per 1M tokens for a model. A
    provider-level pricing override (see _provider_override_rates) always wins
    over the model-name guess below when both rates are set on that binding."""
    override = _provider_override_rates(provider_id)
    if override:
        return override
    model_lower = (model or "").lower()
    best: Optional[Tuple[str, Tuple[float, float]]] = None
    for key, rates in _PRICING.items():
        if key in model_lower:
            if best is None or len(key) > len(best[0]):
                best = (key, rates)
    return best[1] if best else _DEFAULT_RATES


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int, provider_id: Optional[str] = None) -> float:
    """Cost in USD for a single completion. `provider_id` (a provider_bindings
    id) is optional — pass it whenever known so a per-provider pricing
    override can apply; omitting it keeps the old model-name-only behavior."""
    prompt_rate, completion_rate = get_rates(model, provider_id)
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    return (prompt_tokens * prompt_rate + completion_tokens * completion_rate) / 1_000_000.0


def is_model_priced(model: str) -> bool:
    """True if we have explicit (non-default) pricing for this model."""
    model_lower = (model or "").lower()
    return any(key in model_lower for key in _PRICING)
