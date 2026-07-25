"""Sensitive-data redaction gateway for messages routed to an external LLM provider.

Two passes, in order:
1. Regex — deterministic, catches obviously key/token/password-shaped substrings.
2. Local-model confirmation — the already-regex-redacted text is shown to the LOCAL
   Ollama model (never the external one) with a tight, focused prompt asking it to
   flag any remaining contextual secrets the regex missed. Only runs on text that's
   about to leave to an external provider, so the extra local call is scoped and cheap.

Placeholders are mapped back to their real values (restore_secrets) once the external
provider's response comes back, using a short-TTL mapping in Valkey (see
valkey_client.py) keyed by a per-turn request_id — one-time read, then deleted.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets_module
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("hermes.redaction")

_MAPPING_TTL_SECONDS = 15 * 60

# Deterministic, shape-based patterns. Order matters: longer/more-specific patterns
# first so a private-key block isn't partially eaten by a shorter generic pattern.
_REGEX_PATTERNS: List[re.Pattern] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                      # OpenAI/Anthropic/DeepSeek-style
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                     # GitHub personal access token
    re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"),                     # GitHub OAuth token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                         # AWS access key id
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"),                  # Google API key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),             # Slack token
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE),
    re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/]{40,}={0,2}"),
    # Password labeled explicitly (ru/en), value = rest of the "word" after the label.
    re.compile(r"(?:пароль|password|passwd|pwd)\s*[:=\-—]\s*\S+", re.IGNORECASE),
    re.compile(r"(?:api[ _-]?key|токен|token|secret)\s*[:=\-—]\s*\S+", re.IGNORECASE),
    # Credit-card-shaped digit sequences (13-19 digits, optionally grouped).
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    # Generic long random-looking token (last resort, catches unlabeled opaque secrets).
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
]

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
    for pattern in _REGEX_PATTERNS:
        def _replace(match: re.Match) -> str:
            value = match.group(0)
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


def store_mapping(request_id: str, mapping: Dict[str, str]) -> None:
    if not mapping:
        return
    from backend.valkey_client import set_value

    set_value(f"redaction:{request_id}", json.dumps(mapping, ensure_ascii=False), ttl_seconds=_MAPPING_TTL_SECONDS)


def restore_secrets(text: str, request_id: str) -> str:
    """Substitutes any placeholders in `text` back to their real values, then
    deletes the mapping (one-time use — a response should only ever reference a
    given turn's secrets once)."""
    from backend.valkey_client import get_value, delete_value

    raw = get_value(f"redaction:{request_id}")
    if not raw:
        return text
    try:
        mapping: Dict[str, str] = json.loads(raw)
    except (TypeError, ValueError):
        return text
    restored = text
    for placeholder, value in mapping.items():
        restored = restored.replace(placeholder, value)
    delete_value(f"redaction:{request_id}")
    return restored
