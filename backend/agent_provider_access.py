"""Per-agent provider access control.

Two ceilings, both optional and both defaulting to today's unrestricted
behavior so existing agents are unaffected:

1. ``agent_tiers.allow_external_provider`` (backend/agent_tiers.py) — a
   preset an agent can be assigned to. False forbids that agent from using
   *any* external provider at all, full stop.
2. ``subagents.allowed_provider_ids`` (backend/database.py) — an ordered
   whitelist of additional provider_bindings ids a specific agent may fall
   back to besides its own ``model_provider``. Empty means "no extra
   restriction": the agent behaves exactly as before this module existed
   (its single ``model_provider``, or the full global router_tiers chain
   when that's 'ollama').

Mirrors the "ceiling narrows, never grants" pattern tool_permissions.py
already uses for skills/tools: a tier can only take capability away, an
agent's own allowlist can only be a subset of what the tier still permits.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple


def tier_permits_external(agent_row: Dict[str, Any]) -> bool:
    """False only when the agent is assigned to a tier that explicitly
    forbids external providers. No tier, or a tier with the flag on
    (the default), permits external providers."""
    tier_id = agent_row.get("tier_id")
    if not tier_id:
        return True
    from backend.agent_tiers import get_tier
    tier = get_tier(tier_id)
    return bool(tier["allow_external_provider"]) if tier else True


def resolve_provider_candidates(agent_row: Dict[str, Any]) -> List[str]:
    """Ordered list of provider_bindings ids (never 'ollama') this agent may
    try, primary (model_provider) first, then allowed_provider_ids, deduped.
    Empty if the agent's tier forbids external providers outright, or if
    model_provider is 'ollama' and no allowed_provider_ids are set (nothing
    to try beyond the free local model / the global router chain)."""
    if not tier_permits_external(agent_row):
        return []
    primary = agent_row.get("model_provider") or "ollama"
    allowed_raw = agent_row.get("allowed_provider_ids") or []
    ordered: List[str] = []
    for provider_id in [primary, *allowed_raw]:
        if provider_id and provider_id != "ollama" and provider_id not in ordered:
            ordered.append(provider_id)
    return ordered


def resolve_best_provider(agent_row: Dict[str, Any]) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
    """Tries each candidate provider in priority order and returns
    ``(provider_id, (api_base, api_key))`` for the first active/resolvable
    one. ``(None, None)`` means: use the local model — either every
    candidate is currently unresolvable (revoked/missing), or the agent has
    none configured at all."""
    from backend.provider_governance import resolve_binding_credentials

    for provider_id in resolve_provider_candidates(agent_row):
        creds = resolve_binding_credentials(provider_id)
        if creds:
            return provider_id, creds
    return None, None


def allowed_binding_ids_for_chain(agent_row: Dict[str, Any]) -> Optional[Set[str]]:
    """For an agent still on the local default (model_provider == 'ollama'):
    which provider_bindings ids the global router_tiers escalation chain
    (backend/local_orchestrator.py) may use for this specific agent.

    - ``None`` = unrestricted — use the full chain, unchanged legacy behavior.
    - ``set()`` = the tier forbids external providers — never escalate.
    - a non-empty set = only escalate to these bindings (local/free tiers in
      the chain are always allowed regardless, they cost nothing).
    """
    if not tier_permits_external(agent_row):
        return set()
    allowed_raw = agent_row.get("allowed_provider_ids") or []
    if not allowed_raw:
        return None
    return {pid for pid in allowed_raw if pid and pid != "ollama"}
