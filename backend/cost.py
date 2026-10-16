"""Centralized cost calculation for LLM usage.

Single source of truth for token pricing so estimates and real usage agree.
Rates are USD per 1,000,000 tokens (prompt_rate, completion_rate).

The previous logic lived inline in ``agent.py`` (``calculate_cost``); it is kept
here and re-exported for backward compatibility.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional, Tuple

logger = logging.getLogger("hermes.cost")

# model-substring -> (prompt_rate, completion_rate) per 1M tokens.
# Ordered by specificity is not required; longest match wins.
_PRICING: Dict[str, Tuple[float, float]] = {
    "gemini-2.5-pro": (0.075, 0.30),
    "gemini-2.5-flash": (0.0375, 0.15),
    "gemini-3-pro": (1.25, 10.00),
    "gemini-3-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-4": (3.00, 15.00),
    "deepseek-r2": (0.55, 2.19),
    "deepseek-r1": (0.55, 2.19),
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-v4-flash": (0.07, 0.14),
    "deepseek-v4-pro": (0.28, 1.10),
    "deepseek-v3": (0.14, 0.28),
    "deepseek-chat": (0.14, 0.28),
}

# Default when the model is unknown (Gemini 2.5 Pro pricing, as before).
_DEFAULT_RATES: Tuple[float, float] = (0.075, 0.30)


def _match_key(model: str) -> str:
    """The part of a model id the table above is written against.

    A router namespaces every model as `<provider>/<model>` — `ds/deepseek-chat`,
    `ag/gemini-3.1-pro-low` — and the substring match below never saw the model
    half as its own token, so every routed call fell through to `_DEFAULT_RATES`
    and was billed at Gemini 2.5 Pro's rate whatever it actually was. Dropping
    the provider half makes a namespaced id price the same as the bare one.

    The version separator is normalised too: vendors write `gemini-3.1-pro` and
    `gemini-3-1-pro` for the same model, and a price table cannot carry both
    spellings of every entry.
    """
    lowered = (model or "").lower().strip()
    if "/" in lowered:
        lowered = lowered.rsplit("/", 1)[1]
    return lowered.replace(".", "-")


# The table is written the way vendors print their model names; matching happens
# on the normalised form of both sides, so an entry never has to be spelled twice.
_PRICING_NORMALISED: Dict[str, Tuple[float, float]] = {
    _match_key(key): rates for key, rates in _PRICING.items()
}

# Families, tried when no literal entry matched. A vendor ships a model under a
# new minor version every few weeks (`gemini-3.7-flash`, `gemini-3.6-flash`) and
# a router adds an effort suffix on top (`-high`, `-low`, `-agent`); listing each
# one literally means the table is stale by the time it is deployed, and a stale
# table silently bills at `_DEFAULT_RATES`. Ordered — first match wins, so the
# cheaper sub-family goes above the family it belongs to.
_PRICING_PATTERNS: Tuple[Tuple["re.Pattern[str]", Tuple[float, float]], ...] = (
    (re.compile(r"gemini-3(-\d+)?-flash-lite"), (0.10, 0.40)),
    (re.compile(r"gemini-3(-\d+)?-flash"), (0.30, 2.50)),
    (re.compile(r"gemini-3(-\d+)?-pro"), (1.25, 10.00)),
    # 9Router aliases the top Gemini tier without a version number.
    (re.compile(r"gemini-pro-agent"), (1.25, 10.00)),
    (re.compile(r"deepseek-v4-pro"), (0.28, 1.10)),
)


def _provider_override_rates(provider_id: Optional[str]) -> Optional[Tuple[float, float]]:
    """A provider_bindings row may carry its own contracted cost_per_1m_input/
    output, set by the owner because it's known to differ from this module's
    model-name guess (e.g. a discounted/enterprise rate, or a model this table
    has no entry for at all). Lazy-imported to avoid a cost.py <-> provider_
    governance.py import cycle; the local model is never overridden — it's
    always free."""
    from backend.llamacpp_client import is_local_provider_id

    if is_local_provider_id(provider_id):
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
    model_lower = _match_key(model)
    best: Optional[Tuple[str, Tuple[float, float]]] = None
    for key, rates in _PRICING_NORMALISED.items():
        if key in model_lower:
            if best is None or len(key) > len(best[0]):
                best = (key, rates)
    if best:
        return best[1]
    for pattern, rates in _PRICING_PATTERNS:
        if pattern.search(model_lower):
            return rates
    return _DEFAULT_RATES


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
    model_lower = _match_key(model)
    if any(key in model_lower for key in _PRICING_NORMALISED):
        return True
    return any(pattern.search(model_lower) for pattern, _ in _PRICING_PATTERNS)
