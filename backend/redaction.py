"""Sensitive-data redaction gateway for messages routed to an external LLM provider.

Two passes, in order:
1. Regex — deterministic, catches obviously key/token/password-shaped substrings.
2. Local-model confirmation — the already-regex-redacted text is shown to the LOCAL
   Ollama model (never the external one) with a tight, focused prompt asking it to
   flag any remaining contextual secrets the regex missed. Only runs on text that's
   about to leave to an external provider, so the extra local call is scoped and cheap.

Placeholders are mapped back to their real values via restore_secrets_with_mapping,
called by llm_client.call_llm_normalized right after the provider responds — the
whole redact/send/restore cycle happens in-process within that one call, so there's
no need to persist the mapping anywhere in between.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets_module
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("hermes.redaction")

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


_LOCAL_CONFIRM_SYSTEM_PROMPT = (
    "You are a security filter. You will be shown a message that already had obvious "
    "secrets replaced with [SECRET_xxxxxx] placeholders. Find any REMAINING sensitive "
    "information: passwords, API keys, tokens, private keys, or other credentials "
    "written in a way a regex would miss (e.g. spelled out, described casually, split "
    "across words). Reply with ONLY a JSON array of the exact substrings to redact, "
    "copied verbatim from the message. If nothing remains, reply with []. No other text."
)


def _new_placeholder(existing: Dict[str, str]) -> str:
    while True:
        candidate = f"[SECRET_{_secrets_module.token_hex(3)}]"
        if candidate not in existing:
            return candidate


def _regex_redact(text: str) -> Tuple[str, Dict[str, str]]:
    mapping: Dict[str, str] = {}
    value_to_placeholder: Dict[str, str] = {}
    result = text
    for pattern, guard in _REGEX_PATTERNS:
        def _replace(match: re.Match, guard=guard) -> str:
            value = match.group(0)
            if guard is not None and guard(value):
                return value
            if value in value_to_placeholder:
                return value_to_placeholder[value]
            placeholder = _new_placeholder(mapping)
            mapping[placeholder] = value
            value_to_placeholder[value] = placeholder
            return placeholder

        result = pattern.sub(_replace, result)
    return result, mapping


async def _local_model_confirm(redacted_text: str) -> List[str]:
    """Ask the local model to flag any remaining contextual secrets. Best-effort —
    any failure (timeout, bad JSON, model unavailable) just means the regex pass is
    the only line of defense for this message, not a hard failure of the send."""
    try:
        from backend.agent import agent_instance
        from backend.ollama_client import OllamaClient

        client = OllamaClient(agent_instance.ollama_base_url, timeout=15)
        result = await client.chat(
            model=agent_instance.model,
            messages=[
                {"role": "system", "content": _LOCAL_CONFIRM_SYSTEM_PROMPT},
                {"role": "user", "content": redacted_text[:4000]},
            ],
            temperature=0.0,
            max_tokens=512,
            think=False,
        )
        raw = (result.content or "").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return []
        found = json.loads(raw[start:end + 1])
        return [str(item) for item in found if isinstance(item, str) and item.strip()]
    except Exception as exc:
        logger.warning("Local-model redaction confirmation pass failed (regex-only fallback): %s", exc)
        return []


async def detect_and_redact(text: str) -> Tuple[str, Dict[str, str]]:
    """Returns (redacted_text, mapping). mapping is placeholder -> real value."""
    redacted, mapping = _regex_redact(text)
    extra_spans = await _local_model_confirm(redacted)
    for span in extra_spans:
        if not span or span not in redacted:
            continue
        placeholder = _new_placeholder(mapping)
        mapping[placeholder] = span
        redacted = redacted.replace(span, placeholder)
    return redacted, mapping


def safe_log_preview(text: Any, limit: int = 80) -> str:
    """Safe representation of user text / prompts / tool args for operational logs:
    regex-redacted first `limit` chars plus length and a short hash — never the
    full payload."""
    import hashlib

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
    restored = text
    for placeholder, value in mapping.items():
        restored = restored.replace(placeholder, value)
    return restored
