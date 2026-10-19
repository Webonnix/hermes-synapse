"""Compatibility layer over backend.privacy, the sensitive-data gateway for
messages routed to an external LLM provider.

The implementation moved to backend/privacy/ (detectors, vault, gateway);
llm_client.call_llm_normalized calls backend.privacy.gateway directly. The
names below stay for existing callers: safe_log_preview (agent, orchestrator),
_regex_redact (shares, tests) and the single-text detect/restore pair.
"""

from __future__ import annotations

import hashlib
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
    regex-redacted first `limit` chars plus length and a short hash — never the
    full payload."""
    raw = text if isinstance(text, str) else str(text)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    redacted, _ = _regex_redact(raw[: limit * 2])
    preview = redacted[:limit].replace("\n", " ")
    suffix = "…" if len(raw) > limit else ""
    return f"len={len(raw)} sha={digest} preview='{preview}{suffix}'"


def restore_secrets_with_mapping(text: str, mapping: Dict[str, str]) -> str:
    """Substitutes any [SECRET_xxxxxx] placeholders in `text` back to their
    real values using the given placeholder->value mapping."""
    if not mapping:
        return text
    vault = PlaceholderVault()
    vault.merge(mapping)
    return restore_text(text, vault)
