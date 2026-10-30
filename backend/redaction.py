"""Compatibility layer over backend.privacy, the sensitive-data gateway for
messages routed to an external LLM provider.

The implementation moved to backend/privacy/ (detectors, vault, gateway);
llm_client.call_llm_normalized calls backend.privacy.gateway directly. The
names below stay for existing callers: safe_log_preview (agent, orchestrator),
_regex_redact (shares, tests) and the single-text detect/restore pair.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Tuple

from backend.privacy.detectors.regex import (  # noqa: F401 — re-exported
    _REGEX_PATTERNS,
    _looks_like_hash_or_uuid,
    _passes_luhn,
    regex_redact_standalone as _regex_redact,
)
from backend.privacy.gateway import redact_text, restore_text
from backend.privacy.vault import PlaceholderVault


async def detect_and_redact(text: str) -> Tuple[str, Dict[str, str]]:
    """Returns (redacted_text, mapping). mapping is placeholder -> real value."""
    vault = PlaceholderVault()
    redacted = await redact_text(text, vault)
    return redacted, vault.as_dict()


def safe_log_preview(text: Any, limit: int = 80) -> str:
    """Safe representation of user text / prompts / tool args for operational logs:
    Length and a short fingerprint; no part of the payload is retained."""
    raw = text if isinstance(text, str) else str(text)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"len={len(raw)} sha={digest}"


def restore_secrets_with_mapping(text: str, mapping: Dict[str, str]) -> str:
    """Substitutes any [SECRET_xxxxxx] placeholders in `text` back to their
    real values using the given placeholder->value mapping."""
    if not mapping:
        return text
    vault = PlaceholderVault()
    vault.merge(mapping)
    return restore_text(text, vault)


def diagnostic_metadata(log: Dict[str, Any]) -> Dict[str, Any]:
    """Decision telemetry stores sizes/fingerprints, not a second transcript."""
    result = dict(log)
    for field in ("user_message", "assistant_response", "error"):
        value = result.get(field)
        if value:
            # Idempotent when a record passes both memory and persistence gates.
            result[field] = value if isinstance(value, str) and re.fullmatch(r"len=\d+ sha=[0-9a-f]{10}", value) else safe_log_preview(value)
    safe_traces = []
    for trace in result.get("traces") or []:
        if isinstance(trace, dict):
            safe_traces.append({key: value for key, value in trace.items()
                                if key in ("duration_ms", "latency_ms", "input_tokens", "output_tokens")
                                and isinstance(value, (int, float))})
    result["traces"] = safe_traces
    return result
