"""In-memory activity feed for messenger channels.

Every incoming message across all five channel managers passes through
``bot_access_gate.authorize()`` — that single chokepoint is where this module
gets called, so one integration point covers Telegram/Matrix/Discord/Slack/
Email uniformly. Lets the admin see whether messages are actually reaching a
bound bot (and what the gate decided to do with them) without SSHing into the
server to grep logs.

Deliberately not persisted to SQL: this is a live debugging aid, not an audit
trail, and resets on backend restart along with the channel connections it
describes.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional

_MAX_EVENTS = 500
_events: "deque[dict[str, Any]]" = deque(maxlen=_MAX_EVENTS)


def record(
    binding_id: str,
    platform: str,
    kind: str,
    detail: str = "",
    chat_id: str = "",
    sender: str = "",
) -> None:
    _events.append(
        {
            "ts": time.time(),
            "binding_id": binding_id,
            "platform": platform,
            "kind": kind,
            "detail": (detail or "")[:300],
            "chat_id": str(chat_id or "")[:120],
            "sender": str(sender or "")[:120],
        }
    )


def recent(binding_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    items = [e for e in _events if binding_id is None or e["binding_id"] == binding_id]
    return list(reversed(items))[:limit]
