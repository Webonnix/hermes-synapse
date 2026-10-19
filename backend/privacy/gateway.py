"""Protect a message list before it goes to an external provider, and
restore the provider's answer afterwards.

Every string the external model would see is covered: plain `content`,
text parts of multimodal `content` lists, and the JSON `arguments` of
assistant `tool_calls` in the history. JSON arguments are redacted and
restored value by value on the parsed tree, never on the raw string: a
`password: x` regex match on raw JSON swallows the closing quote and comma,
and a restored value containing `"` or `\\` would break the JSON.

All strings of one message go through the local-model pass together, so a
message costs one local check (chunked if long) however many parts it has.
"""

from __future__ import annotations

import copy
import json
import logging
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Tuple

from backend.privacy import config, keys
from backend.privacy.detectors import local_llm
from backend.privacy.detectors.regex import regex_redact
from backend.privacy.vault import PLACEHOLDER_RE, PlaceholderVault

logger = logging.getLogger("hermes.privacy")

_LEAF_SEPARATOR = "\n\n"


class PrivacyCheckFailed(RuntimeError):
    """Fail-closed mode: a message could not be fully checked, so nothing was sent."""


# ── Redaction ────────────────────────────────────────────────────────────────

def _replace_outside_placeholders(text: str, span: str, placeholder: str) -> str:
    parts = PLACEHOLDER_RE.split(text)
    tokens = PLACEHOLDER_RE.findall(text)
    out = [parts[0].replace(span, placeholder)]
    for token, part in zip(tokens, parts[1:]):
        out.append(token)
        out.append(part.replace(span, placeholder))
    return "".join(out)


def _occurs_outside_placeholders(text: str, span: str) -> bool:
    return any(span in part for part in PLACEHOLDER_RE.split(text))


class _Pass:
    """Whether every part of a message was actually checked."""

    def __init__(self) -> None:
        self.complete = True


async def _local_spans(text: str, full_scan: bool, status: _Pass) -> List[str]:
    if not text.strip():
        return []
    try:
        return await local_llm.find_spans(text, full_scan=full_scan)
    except local_llm.LocalPassError as exc:
        if config.fail_mode() == config.FAIL_CLOSED:
            raise PrivacyCheckFailed(
                f"Local privacy check unavailable ({exc}); request not sent to the external provider."
            ) from exc
        status.complete = False
        logger.warning("privacy: local pass failed, regex-only for the unchecked part: %s", exc)
        return exc.spans


async def _redact_leaves(leaves: List[str], vault: PlaceholderVault, *,
                         full_scan: bool = True, status: _Pass | None = None) -> List[str]:
    status = status or _Pass()
    redacted = [regex_redact(leaf, vault) for leaf in leaves]
    spans = await _local_spans(_LEAF_SEPARATOR.join(leaf for leaf in redacted if leaf), full_scan, status)
    # Longest first, so "pass word123" is not pre-empted by its own "word123".
    for span in sorted(spans, key=len, reverse=True):
        if PLACEHOLDER_RE.search(span) or "[SECRET_" in span:
            continue
        if not any(_occurs_outside_placeholders(leaf, span) for leaf in redacted):
            continue
        placeholder = vault.placeholder_for(span)
        redacted = [_replace_outside_placeholders(leaf, span, placeholder) for leaf in redacted]
    return redacted


async def redact_text(text: str, vault: PlaceholderVault) -> str:
    return (await _redact_leaves([text], vault))[0]


def _map_strings(node: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, list):
        return [_map_strings(item, fn) for item in node]
    if isinstance(node, dict):
        return {key: _map_strings(value, fn) for key, value in node.items()}
    return node


def _collect_strings(node: Any, out: List[str]) -> None:
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for item in node:
            _collect_strings(item, out)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, out)


class _Slot:
    """One redactable string inside a message, plus how to write it back."""

    def __init__(self, leaves: List[str], write: Callable[[List[str]], None]) -> None:
        self.leaves = leaves
        self.write = write


def _json_slot(raw: str, write: Callable[[str], None]) -> _Slot:
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError):
        return _Slot([raw], lambda new: write(new[0]))
    leaves: List[str] = []
    _collect_strings(tree, leaves)

    def _write(new: List[str]) -> None:
        it = iter(new)
        write(json.dumps(_map_strings(tree, lambda _old: next(it)), ensure_ascii=False))

    return _Slot(leaves, _write)


def _message_slots(msg: Dict[str, Any]) -> List[_Slot]:
    slots: List[_Slot] = []
    content = msg.get("content")
    if isinstance(content, str) and content:
        slots.append(_Slot([content], lambda new: msg.__setitem__("content", new[0])))
    elif isinstance(content, list):
        parts = [copy.copy(part) if isinstance(part, dict) else part for part in content]
        text_parts = [p for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
        if text_parts:
            def _write_parts(new: List[str]) -> None:
                for part, text in zip(text_parts, new):
                    part["text"] = text
                msg["content"] = parts
            slots.append(_Slot([p["text"] for p in text_parts], _write_parts))

    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        calls = copy.deepcopy(tool_calls)
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str) and args:
                def _write_args(new_args: str, fn=fn) -> None:
                    fn["arguments"] = new_args
                    msg["tool_calls"] = calls
                slots.append(_json_slot(args, _write_args))
    return slots


async def protect_messages(messages: List[Any], *, mark_key: str) -> PlaceholderVault:
    """Redact `messages` in place for an external provider.

    Each message dict is checked at most once: its own placeholder mapping
    (possibly {}) is cached on it under `mark_key`, so a tool loop that
    re-sends a growing list never re-runs the local pass on old messages,
    and a placeholder minted three calls ago still restores. Cached mappings
    are merged first, so a secret repeated in a new message reuses its old
    placeholder.
    """
    vault = PlaceholderVault()
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get(mark_key), dict):
            vault.merge(msg[mark_key])

    full_scan_roles = config.full_scan_roles()
    version = local_llm._version()
    for msg in messages:
        if not isinstance(msg, dict) or isinstance(msg.get(mark_key), dict):
            continue
        vault.start_message()
        slots = _message_slots(msg)
        flat = [leaf for slot in slots for leaf in slot.leaves]
        if flat:
            full_scan = msg.get("role") in full_scan_roles
            memo_key = keys.cache_key(json.dumps([version, full_scan, flat], ensure_ascii=False))
            hit = _memo_get(memo_key)
            if hit is not None:
                redacted, mapping = hit
                vault.adopt(mapping)
            else:
                status = _Pass()
                redacted = await _redact_leaves(flat, vault, full_scan=full_scan, status=status)
                if status.complete:
                    _memo_put(memo_key, redacted, vault.touched_mapping())
            offset = 0
            for slot in slots:
                new = redacted[offset:offset + len(slot.leaves)]
                offset += len(slot.leaves)
                if new != slot.leaves:
                    slot.write(new)
        msg[mark_key] = vault.touched_mapping()
    return vault


# ── Whole-message memo ───────────────────────────────────────────────────────
# A 1M-token history is rebuilt and re-sent every turn. The L2 cache already
# spares the GPU; this spares the CPU too (regexes + trigger scan run ~1 s per
# million chars): a message seen before, fully checked, is a dict lookup.
# In-process only — the mapping it holds is real secrets — and bounded by
# PRIVACY_MEMO_MB. Only complete passes are kept, so a fail-open partial
# result is retried next time instead of being frozen in.

_memo: "OrderedDict[str, Tuple[List[str], Dict[str, str]]]" = OrderedDict()
_memo_sizes: Dict[str, int] = {}
_memo_total = 0


def _memo_get(key: str):
    item = _memo.get(key)
    if item is not None:
        _memo.move_to_end(key)
        return list(item[0]), dict(item[1])
    return None


def _memo_put(key: str, redacted: List[str], mapping: Dict[str, str]) -> None:
    global _memo_total
    limit = config.memo_bytes()
    size = sum(len(s) for s in redacted) + sum(len(k) + len(v) for k, v in mapping.items()) + 64
    if not limit or size > limit:
        return
    if key in _memo:
        _memo_total -= _memo_sizes.pop(key, 0)
        del _memo[key]
    _memo[key] = (list(redacted), dict(mapping))
    _memo_sizes[key] = size
    _memo_total += size
    while _memo_total > limit and _memo:
        old, _ = _memo.popitem(last=False)
        _memo_total -= _memo_sizes.pop(old, 0)


def clear_memo() -> None:
    global _memo_total
    _memo.clear()
    _memo_sizes.clear()
    _memo_total = 0


# ── Restore ──────────────────────────────────────────────────────────────────

def restore_text(text: str, vault: PlaceholderVault) -> str:
    if not text or not vault:
        return text
    return PLACEHOLDER_RE.sub(lambda m: vault.value_for(m.group(0)) or m.group(0), text)


def restore_arguments(raw: str, vault: PlaceholderVault) -> str:
    if not raw or not vault or not PLACEHOLDER_RE.search(raw):
        return raw
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError):
        return restore_text(raw, vault)
    return json.dumps(_map_strings(tree, lambda s: restore_text(s, vault)), ensure_ascii=False)


def restore_response(resp: Any, vault: PlaceholderVault) -> Any:
    """Put real values back into a NormalizedLLMResponse-shaped object."""
    if not vault:
        return resp
    if resp.content:
        resp.content = restore_text(resp.content, vault)
    if resp.reasoning:
        resp.reasoning = restore_text(resp.reasoning, vault)
    for call in resp.tool_calls or []:
        fn = call.get("function") if isinstance(call, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args, str) and args:
            fn["arguments"] = restore_arguments(args, vault)
    return resp
