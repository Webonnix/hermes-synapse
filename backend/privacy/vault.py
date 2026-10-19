"""Placeholder table for one external call.

One vault spans every message of a call (and, through the per-message cache
in llm_client, every call of a tool loop), so the same secret always maps to
the same placeholder: two messages mentioning one key read as one key to the
external model, not two.
"""

from __future__ import annotations

import re
import secrets as _secrets
from typing import Dict, Iterable, Optional, Set

PLACEHOLDER_RE = re.compile(r"\[SECRET_[0-9a-f]{6}\]")


class PlaceholderVault:
    def __init__(self) -> None:
        self._by_placeholder: Dict[str, str] = {}
        self._by_value: Dict[str, str] = {}
        self._touched: Set[str] = set()

    def __bool__(self) -> bool:
        return bool(self._by_placeholder)

    def __len__(self) -> int:
        return len(self._by_placeholder)

    def placeholder_for(self, value: str) -> str:
        """The placeholder for `value`, minting one on first sight."""
        placeholder = self._by_value.get(value)
        if placeholder is None:
            placeholder = self._new_placeholder()
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

    def start_message(self) -> None:
        self._touched = set()

    def touched_mapping(self) -> Dict[str, str]:
        """placeholder -> value for everything used since start_message()."""
        return {p: self._by_placeholder[p] for p in self._touched}

    def as_dict(self) -> Dict[str, str]:
        return dict(self._by_placeholder)

    def values(self) -> Iterable[str]:
        return self._by_value.keys()

    def _new_placeholder(self) -> str:
        while True:
            candidate = f"[SECRET_{_secrets.token_hex(3)}]"
            if candidate not in self._by_placeholder:
                return candidate
