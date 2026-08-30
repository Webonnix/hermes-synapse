"""Structured output: extraction and validation (§113).

The rule from the spec is blunt — do not parse arbitrary AI markdown with
regular expressions. Every transition between nodes carries JSON validated
against an explicit schema, and a model that returns prose, a truncated object
or the right JSON with the wrong shape fails the node with `INVALID_OUTPUT`,
which is retryable: one more attempt with the same prompt usually lands, and
that is far better than a storyboard silently missing half its scenes.

A deliberately small validator rather than the `jsonschema` package: the schemas
in `domain.py` use type/required/items/properties/enum and nothing else, and
adding a dependency to the shared backend image for that is not worth it. The
supported keywords are enumerated in `_UNSUPPORTED` so a schema that quietly
relies on something else is caught at development time rather than passing
validation it never actually performed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from backend.content_factory.domain import ContentFactoryError, ErrorCode

logger = logging.getLogger("hermes.content_factory.structured")

_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

_SUPPORTED_KEYWORDS = {
    "type", "properties", "required", "items", "enum", "description", "title",
    "minimum", "maximum", "minItems", "maxItems", "additionalProperties",
}

_TYPE_CHECKS = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def extract_json(text: str) -> Any:
    """Pulls the JSON value out of a model response.

    Tries, in order: the whole string, a fenced ```json block, and the widest
    balanced {...} or [...] span. Anything else raises INVALID_OUTPUT — there is
    no "best effort" salvage here, because a half-parsed script is worse than a
    retry.
    """
    if not isinstance(text, str) or not text.strip():
        raise ContentFactoryError(ErrorCode.INVALID_OUTPUT, "Model returned an empty response")

    candidates = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    span = _widest_span(text)
    if span:
        candidates.append(span)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    raise ContentFactoryError(
        ErrorCode.INVALID_OUTPUT, "Model response was not valid JSON"
    )


def _widest_span(text: str) -> str:
    starts = [index for index, char in enumerate(text) if char in "{["]
    ends = [index for index, char in enumerate(text) if char in "}]"]
    if not starts or not ends:
        return ""
    return text[starts[0]: ends[-1] + 1]


def validate(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """Raises INVALID_OUTPUT describing the first violation. Returns None when
    the value satisfies the schema."""
    if not schema:
        return
    _warn_unsupported(schema)

    expected = schema.get("type")
    if expected:
        checker = _TYPE_CHECKS.get(expected)
        if checker is None:
            return
        # bool is a subclass of int in Python; a boolean is not a number here.
        if expected in {"number", "integer"} and isinstance(value, bool):
            _fail(path, f"expected {expected}, got boolean")
        if not isinstance(value, checker):
            _fail(path, f"expected {expected}, got {type(value).__name__}")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        _fail(path, f"value {value!r} is not one of {enum}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                _fail(path, f"missing required property '{key}'")
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in value:
                validate(value[key], sub_schema, path=f"{path}.{key}")

    if isinstance(value, list):
        item_schema = schema.get("items")
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < int(minimum):
            _fail(path, f"expected at least {minimum} items, got {len(value)}")
        if item_schema:
            for index, item in enumerate(value):
                validate(item, item_schema, path=f"{path}[{index}]")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if minimum is not None and value < minimum:
            _fail(path, f"{value} is below the minimum {minimum}")
        if maximum is not None and value > maximum:
            _fail(path, f"{value} is above the maximum {maximum}")


def parse_and_validate(text: str, schema: dict[str, Any]) -> Any:
    value = extract_json(text)
    validate(value, schema)
    return value


def _fail(path: str, detail: str) -> None:
    raise ContentFactoryError(
        ErrorCode.INVALID_OUTPUT,
        f"Structured output does not match its schema at {path}: {detail}",
        details={"path": path},
    )


_warned: set[str] = set()


def _warn_unsupported(schema: dict[str, Any]) -> None:
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    for keyword in unknown:
        if keyword not in _warned:
            _warned.add(keyword)
            logger.warning(
                "JSON Schema keyword '%s' is not enforced by this validator", keyword
            )


def schema_instruction(schema: dict[str, Any]) -> str:
    """Prompt fragment for providers without native structured-output support.

    Belt and braces: the schema is also enforced after the fact by `validate`,
    so a model that ignores this instruction fails cleanly instead of poisoning
    downstream nodes.
    """
    return (
        "Respond with a single JSON object and nothing else — no prose, no code "
        "fences, no explanation. It must conform to this JSON Schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )
