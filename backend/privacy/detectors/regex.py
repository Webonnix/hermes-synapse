"""Deterministic, shape-based secret detection (first pass of the gateway).

Catches obviously key/token/password-shaped substrings. Cheap and always on;
the local-model pass in local_llm.py only adds to what this finds.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from backend.privacy.vault import PlaceholderVault

# Deterministic, shape-based patterns. Order matters: longer/more-specific patterns
# first so a private-key block isn't partially eaten by a shorter generic pattern.
# Each entry is (pattern, guard). guard, if set, is called with the matched text
# and may veto the match (return True to SKIP redacting it) — used only by the
# generic catch-all below, which is broad enough to need one.
_REGEX_PATTERNS: List[Tuple[re.Pattern, Any]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"), None),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), None),                      # OpenAI/Anthropic/DeepSeek-style
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), None),                     # GitHub personal access token
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"), None),                     # GitHub OAuth token
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),                         # AWS access key id
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"), None),                  # Google API key
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), None),             # Slack token
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE), None),
    (re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/]{40,}={0,2}"), None),
    # Password labeled explicitly (ru/en), value = rest of the "word" after the label.
    (re.compile(r"(?:пароль|password|passwd|pwd)\s*[:=\-—]\s*\S+", re.IGNORECASE), None),
    (re.compile(r"(?:api[ _-]?key|токен|token|secret)\s*[:=\-—]\s*\S+", re.IGNORECASE), None),
    # Credit-card-shaped digit sequences (13-19 digits, optionally grouped).
    # Guarded by a Luhn checksum: without it, any 13-19-digit run (order IDs,
    # timestamps, tracking numbers) gets redacted, and those are common
    # enough in normal conversation that it's real context lost for no
    # security benefit — real card numbers are Luhn-valid by construction,
    # arbitrary digit strings essentially never are (1-in-10 chance per digit).
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), lambda v: not _passes_luhn(v)),
    # Generic long random-looking token (last resort, catches unlabeled opaque
    # secrets). Guarded: git SHAs and UUIDs are exactly this shape (32+ hex
    # chars) and show up constantly in normal dev conversation — without the
    # guard every commit hash or UUID a user pastes gets replaced with a
    # placeholder the external model never sees, which is real context lost
    # for something that was never a secret. Real opaque tokens (base64
    # session tokens, vendor keys without a recognized prefix) are virtually
    # always mixed-case/mixed-alphabet, so this guard doesn't weaken coverage
    # of what it's actually there to catch.
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), lambda v: _looks_like_hash_or_uuid(v)),
]


def _passes_luhn(value: str) -> bool:
    """Standard Luhn checksum, used to tell an actual card number apart from
    an arbitrary 13-19 digit run. Ignores grouping spaces/dashes."""
    digits = [int(c) for c in value if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _looks_like_hash_or_uuid(value: str) -> bool:
    """True for common non-secret 32+ char tokens: hex hashes (git SHAs,
    checksums) and canonical UUIDs. Real API keys/tokens are essentially
    never pure hex or UUID-shaped — they mix case, digits and often a
    vendor-specific prefix already caught by a more specific pattern above."""
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value,
    ))


def regex_redact(text: str, vault: PlaceholderVault) -> str:
    """Replace every pattern match in `text` with its vault placeholder."""
    result = text
    for pattern, guard in _REGEX_PATTERNS:
        def _replace(match: re.Match, guard=guard) -> str:
            value = match.group(0)
            if guard is not None and guard(value):
                return value
            return vault.placeholder_for(value)

        result = pattern.sub(_replace, result)
    return result


def regex_redact_standalone(text: str) -> Tuple[str, Dict[str, str]]:
    """regex_redact with a throwaway vault: (redacted, placeholder -> value)."""
    vault = PlaceholderVault()
    redacted = regex_redact(text, vault)
    return redacted, vault.as_dict()
