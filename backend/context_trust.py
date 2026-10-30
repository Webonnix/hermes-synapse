"""Conservative provenance: unknown context never grants write authority.

History, memory, delegated turns and CLI contexts cannot yet prove a clean
origin, so the default remains untrusted. A model cannot clear this state.
"""
from contextvars import ContextVar

SOURCES: ContextVar[set | None] = ContextVar("context_sources", default=None)
LOCAL_ACTIONS = frozenset({"set_timer", "set_alarm", "cancel_timer_or_alarm",
                           "set_recurring_reminder", "add_price_alert", "call_subagent"})


def mark_untrusted(source: str) -> None:
    sources = SOURCES.get()
    if sources is None:
        sources = {"unknown"}
        SOURCES.set(sources)
    sources.add(source[:80])


def provenance() -> list[str]:
    return sorted(SOURCES.get() or {"unknown"})


def approval_floor(tool_name: str, risk: str) -> int:
    if risk in ("R0", "R1") or tool_name in LOCAL_ACTIONS:
        return 0
    # R2 can still publish, execute networked code or act in a logged-in browser.
    return 2 if risk == "R4" else 1
