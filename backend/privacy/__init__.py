"""Privacy gateway: everything that keeps sensitive data from leaving the box
in a call to an external LLM provider.

Layout:
- detectors/regex.py     — deterministic, shape-based secret patterns
- detectors/local_llm.py — contextual pass on the LOCAL llama.cpp model only
- vault.py               — placeholder <-> real value table for one call
- gateway.py             — protect a message list, restore a response

backend/redaction.py re-exports the old names for existing callers.
"""

from backend.privacy.gateway import (
    PrivacyCheckFailed,
    protect_messages,
    redact_text,
    restore_response,
    restore_text,
)
from backend.privacy.vault import PlaceholderVault

__all__ = [
    "PlaceholderVault",
    "PrivacyCheckFailed",
    "protect_messages",
    "redact_text",
    "restore_response",
    "restore_text",
]
