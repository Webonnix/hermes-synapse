"""Routing rules for arriving contacts: pass to a person, block, or let the AI answer.

The question answered here is asked once per incoming message, before the access gate
spends anything: **who should handle this sender right now?**

* A rule is written against a *person*, not against a channel-specific id. The gate
  hands in every identity the sender arrived with (chat id, user id, @handle, address)
  and a rule about any of them is a rule about this contact — so "+7 900 123-45-67" in
  a rule matches the same person on WhatsApp and in a CRM contact card.
* Matching order is exact before prefix, a longer prefix before a shorter one, and a
  binding's own rule before a global one. The most specific statement the owner made
  about somebody wins.
* Business hours bend ``pass`` and nothing else. Outside them there is nobody at the
  desk to pass to, so the AI answers. ``block`` is block at any hour: the clock decides
  who answers, never whether somebody unwanted gets in.

Only the *rules* live here. What a decision does to a conversation (silence, archive,
owner ping) is bot_access_gate's job; what the archive shows is backend/inbox.py's.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("hermes.inbox_rules")

ACTIONS = ("pass", "block", "ai")
MATCHES = ("exact", "prefix")
DEFAULT_TIMEZONE = "Asia/Jerusalem"
MAX_PATTERN_CHARS = 320

_DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_WINDOW = re.compile(
    r"^(?:(?P<first>mo|tu|we|th|fr|sa|su)(?:-(?P<last>mo|tu|we|th|fr|sa|su))?\s+)?"
    r"(?P<start>[0-2]\d:[0-5]\d)-(?P<end>[0-2]\d:[0-5]\d)$"
)

_schema_ready_for: Optional[str] = None
_schema_lock = threading.Lock()


def _db_path() -> str:
    from backend import database

    return database.DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def reset_schema_cache() -> None:
    global _schema_ready_for
    _schema_ready_for = None


def _init_schema() -> None:
    global _schema_ready_for
    path = _db_path()
    if _schema_ready_for == path:
        return
    with _schema_lock:
        if _schema_ready_for == path:
            return
        with _connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox_rules (
                    id TEXT PRIMARY KEY,
                    binding_id TEXT,
                    pattern TEXT NOT NULL,
                    match TEXT NOT NULL DEFAULT 'exact',
                    action TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_rules_binding ON inbox_rules (binding_id);

                -- Per-binding settings that belong to routing rather than to the
                -- channel credential, so they live with the rules instead of growing
                -- agent_messenger_bindings, which another module owns.
                CREATE TABLE IF NOT EXISTS inbox_binding_settings (
                    binding_id TEXT PRIMARY KEY,
                    business_hours TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
        _schema_ready_for = path


# ── business hours ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Window:
    days: frozenset
    start: dtime
    end: dtime


def parse_hours(raw: str) -> List[Window]:
    """``"mo-fr 09:00-18:00; sa 10:00-14:00"`` → windows. Raises ValueError on
    anything it does not understand: a mistyped schedule saved silently would make
    ``pass`` apply at the wrong hours with no visible sign of why."""
    windows: List[Window] = []
    for part in (raw or "").split(";"):
        chunk = part.strip().lower()
        if not chunk:
            continue
        match = _WINDOW.match(chunk)
        if not match:
            raise ValueError(f"Не понимаю окно «{part.strip()}» — формат «mo-fr 09:00-18:00»")
        first, last = match.group("first"), match.group("last")
        if first is None:
            days = frozenset(range(7))
        else:
            a = _DAYS.index(first)
            b = _DAYS.index(last) if last else a
            days = frozenset(range(a, b + 1)) if a <= b else frozenset(list(range(a, 7)) + list(range(0, b + 1)))
        try:
            start = dtime.fromisoformat(match.group("start"))
            end = dtime.fromisoformat(match.group("end"))
        except ValueError as exc:
            raise ValueError(f"Неверное время в окне «{part.strip()}»") from exc
        if start == end:
            raise ValueError(f"Окно «{part.strip()}» имеет нулевую длину")
        windows.append(Window(days=days, start=start, end=end))
    return windows


def validate_timezone(name: str) -> str:
    value = (name or "").strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Неизвестный часовой пояс: {value}") from exc
    return value


def within_hours(windows: List[Window], moment: datetime) -> bool:
    """Empty schedule = hours not configured = always within them. That is the honest
    reading of an owner who never filled the field in: ``pass`` then means pass."""
    if not windows:
        return True
    weekday, now = moment.weekday(), moment.time()
    for window in windows:
        if window.start < window.end:
            if weekday in window.days and window.start <= now < window.end:
                return True
        else:
            # Overnight window ("fr 22:00-02:00"): the evening half belongs to the
            # named day, the early-morning half to the day after it.
            if weekday in window.days and now >= window.start:
                return True
            if ((weekday - 1) % 7) in window.days and now < window.end:
                return True
    return False


def get_binding_settings(binding_id: str) -> Dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM inbox_binding_settings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
    if row is None:
        return {"binding_id": binding_id, "business_hours": "", "timezone": DEFAULT_TIMEZONE, "updated_at": None}
    item = dict(row)
    item["timezone"] = item.get("timezone") or DEFAULT_TIMEZONE
    return item


def set_binding_settings(binding_id: str, business_hours: str, timezone_name: str) -> Dict[str, Any]:
    if not (binding_id or "").strip():
        raise ValueError("binding_id is required")
    hours = (business_hours or "").strip()
    parse_hours(hours)
    tz = validate_timezone(timezone_name)
    _init_schema()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO inbox_binding_settings (binding_id, business_hours, timezone, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (binding_id) DO UPDATE SET business_hours = excluded.business_hours,
                timezone = excluded.timezone, updated_at = excluded.updated_at
            """,
            (binding_id, hours, tz, _now()),
        )
    return get_binding_settings(binding_id)


def is_within_business_hours(binding_id: str, moment: Optional[datetime] = None) -> bool:
    settings = get_binding_settings(binding_id)
    try:
        windows = parse_hours(settings.get("business_hours") or "")
        zone = ZoneInfo(settings.get("timezone") or DEFAULT_TIMEZONE)
    except Exception:
        # A schedule that stopped parsing (hand-edited row, removed tz) must not
        # silence customers: treat it as "no hours configured" and say so.
        logger.warning("Business hours for binding %s are unreadable — treating as always open", binding_id)
        return True
    current = (moment or datetime.now(timezone.utc)).astimezone(zone)
    return within_hours(windows, current)


# ── rules ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuleDecision:
    """``action`` is the *effective* outcome: ``pass`` outside business hours has
    already become ``ai`` here, with ``configured_action`` keeping what the rule said."""

    action: str
    configured_action: str
    pattern: Optional[str]
    rule_id: Optional[str]
    within_hours: bool


def _normalize(value: str) -> str:
    from backend.inbox import normalize_identity

    return normalize_identity(value)


def _validate(pattern: str, match: str, action: str, note: str) -> Dict[str, str]:
    clean_pattern = _normalize(pattern)
    if not clean_pattern:
        raise ValueError("Шаблон правила пустой")
    if len(clean_pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"Шаблон длиннее {MAX_PATTERN_CHARS} символов")
    if match not in MATCHES:
        raise ValueError(f"match must be one of {MATCHES}")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    return {"pattern": clean_pattern, "match": match, "action": action, "note": (note or "").strip()[:200]}


def list_rules(binding_id: Optional[str] = None) -> List[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        if binding_id:
            rows = connection.execute(
                "SELECT * FROM inbox_rules WHERE binding_id = ? OR binding_id IS NULL ORDER BY created_at DESC",
                (binding_id,),
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM inbox_rules ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM inbox_rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def create_rule(pattern: str, action: str, match: str = "exact", binding_id: Optional[str] = None, note: str = "") -> Dict[str, Any]:
    fields = _validate(pattern, match, action, note)
    _init_schema()
    rule_id = f"rule-{uuid.uuid4().hex[:12]}"
    with _connect() as connection:
        connection.execute(
            "INSERT INTO inbox_rules (id, binding_id, pattern, match, action, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rule_id, (binding_id or None), fields["pattern"], fields["match"], fields["action"], fields["note"], _now()),
        )
    return get_rule(rule_id)  # type: ignore[return-value]


def update_rule(rule_id: str, **changes: Any) -> Dict[str, Any]:
    current = get_rule(rule_id)
    if current is None:
        raise KeyError(rule_id)
    merged = {
        "pattern": changes.get("pattern") if changes.get("pattern") is not None else current["pattern"],
        "match": changes.get("match") or current["match"],
        "action": changes.get("action") or current["action"],
        "note": changes.get("note") if changes.get("note") is not None else current["note"],
    }
    fields = _validate(merged["pattern"], merged["match"], merged["action"], merged["note"])
    binding_id = current["binding_id"]
    if "binding_id" in changes:
        binding_id = changes["binding_id"] or None
    with _connect() as connection:
        connection.execute(
            "UPDATE inbox_rules SET pattern = ?, match = ?, action = ?, note = ?, binding_id = ? WHERE id = ?",
            (fields["pattern"], fields["match"], fields["action"], fields["note"], binding_id, rule_id),
        )
    return get_rule(rule_id)  # type: ignore[return-value]


def delete_rule(rule_id: str) -> bool:
    _init_schema()
    with _connect() as connection:
        return connection.execute("DELETE FROM inbox_rules WHERE id = ?", (rule_id,)).rowcount > 0


def _specificity(rule: Dict[str, Any]) -> tuple:
    # Higher sorts first: exact over prefix, a longer prefix over a shorter one,
    # a binding's own rule over a global one.
    return (
        1 if rule["match"] == "exact" else 0,
        len(rule["pattern"]),
        1 if rule.get("binding_id") else 0,
    )


def match_rule(rules: Iterable[Dict[str, Any]], identities: Iterable[str]) -> Optional[Dict[str, Any]]:
    ids = [i for i in identities if i]
    candidates = []
    for rule in rules:
        pattern = rule["pattern"]
        for identity in ids:
            if (rule["match"] == "exact" and identity == pattern) or (
                rule["match"] == "prefix" and identity.startswith(pattern)
            ):
                candidates.append(rule)
                break
    if not candidates:
        return None
    return max(candidates, key=_specificity)


def evaluate(binding_id: str, identities: Iterable[str], moment: Optional[datetime] = None) -> Optional[RuleDecision]:
    """The decision for one arriving sender, or None when no rule mentions them."""
    rule = match_rule(list_rules(binding_id), identities)
    if rule is None:
        return None
    configured = rule["action"]
    in_hours = True
    effective = configured
    if configured == "pass":
        in_hours = is_within_business_hours(binding_id, moment)
        if not in_hours:
            effective = "ai"
    return RuleDecision(
        action=effective, configured_action=configured, pattern=rule["pattern"],
        rule_id=rule["id"], within_hours=in_hours,
    )
