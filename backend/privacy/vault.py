"""Placeholder table for one external call.

Placeholders are deterministic: [<KIND>_<first 8 hex of HMAC(value)>], keyed
by PRIVACY_KEY (see keys.py). The same secret gets the same placeholder in
every message, every call and every turn, so a re-sent history is
byte-identical and the provider's prompt cache stays warm. On the rare
digest-prefix collision between two different values the later one gets a
longer prefix.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Set

from backend.privacy import keys

# The kind tells the external model what stood there (a person, an e-mail…)
# without the value. SECRET is the stage-0 kind; 6 hex: placeholders minted
# before stage 1a, still found in cached mappings.
KINDS = ("SECRET", "PERSON", "EMAIL", "PHONE", "ADDRESS", "ID")
PLACEHOLDER_RE = re.compile(r"\[(?:" + "|".join(KINDS) + r")_[0-9a-f]{6,16}\]")
_DIGEST_LENGTHS = (8, 12, 16)


class PlaceholderVault:
    def __init__(self) -> None:
        self._by_placeholder: Dict[str, str] = {}
        self._by_value: Dict[str, str] = {}
        self._touched: Set[str] = set()

    def __bool__(self) -> bool:
        return bool(self._by_placeholder)

    def __len__(self) -> int:
        return len(self._by_placeholder)

    def placeholder_for(self, value: str, kind: str = "SECRET") -> str:
        """The placeholder for `value`, minting one of `kind` on first sight."""
        placeholder = self._by_value.get(value)
        if placeholder is None:
            placeholder = self._new_placeholder(value, kind if kind in KINDS else "SECRET")
            self._by_placeholder[placeholder] = value
            self._by_value[value] = placeholder
        self._touched.add(placeholder)
        return placeholder

    def value_for(self, placeholder: str) -> Optional[str]:
        return self._by_placeholder.get(placeholder)

    def merge(self, mapping: Dict[str, str]) -> None:
        """Adopt a mapping minted by an earlier call (placeholder -> value)."""
        for placeholder, value in mapping.items():
            if placeholder in self._by_placeholder:
                continue
            self._by_placeholder[placeholder] = value
            self._by_value.setdefault(value, placeholder)

    def adopt(self, mapping: Dict[str, str]) -> None:
        """merge() plus count those placeholders as used by this message."""
        self.merge(mapping)
        self._touched.update(p for p in mapping if p in self._by_placeholder)

    def start_message(self) -> None:
        self._touched = set()

    def touched_mapping(self) -> Dict[str, str]:
        """placeholder -> value for everything used since start_message()."""
        return {p: self._by_placeholder[p] for p in self._touched}

    def as_dict(self) -> Dict[str, str]:
        return dict(self._by_placeholder)

    def values(self) -> Iterable[str]:
        return self._by_value.keys()

    def _new_placeholder(self, value: str, kind: str) -> str:
        digest = keys.placeholder_digest(value)
        for length in _DIGEST_LENGTHS:
            candidate = f"[{kind}_{digest[:length]}]"
            if self._by_placeholder.get(candidate, value) == value:
                return candidate
        raise RuntimeError("privacy: 64-bit placeholder collision")  # practically unreachable
