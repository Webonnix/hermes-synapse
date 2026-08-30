"""Publication register for SMM Factory — what was posted, what is queued, what
is still a draft.

Everything else in this section is *inbound*: a person writes to a bound
account and an agent answers (agent_bot.py and friends, queued through
channel_replies.py when the binding is in draft mode). Outbound posting had no
home at all — the owner wrote posts somewhere else and the dashboard could not
say what had gone out, what was scheduled for tonight, or how far any of it
reached. This module is that missing half.

Two rules it holds to, both learned from content_factory:

* **Never claim more than actually happened.** A row is `published` only after
  either the backend delivered it (`delivery_mode='auto'`, through the same
  per-platform sender the draft queue uses) or the owner explicitly said they
  posted it by hand (`delivery_mode='manual'`). A failed auto delivery lands in
  `failed` carrying the provider's own wording in `error`, never a silent retry
  that leaves the row looking sent.
* **Reach is a measurement, not a guess.** `reach` stays NULL until something
  records a real number, and the UI shows "—" rather than a zero that reads as
  "nobody saw it".

The platform/account pair is denormalised on purpose: a publication is history,
and it has to keep saying which account it went out on long after that binding
was revoked and deleted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from backend.database import DB_PATH

logger = logging.getLogger("hermes.smm_posts")

STATUSES = ("draft", "scheduled", "published", "failed")
DELIVERY_MODES = ("manual", "auto")

#: Column each sort key maps to. A whitelist rather than string interpolation of
#: whatever the query string carried — the sort key reaches SQL directly.
SORT_COLUMNS = {
    "date": "effective_at",
    "reach": "reach",
    "title": "title",
    "status": "status",
    "platform": "platform",
}

#: The one expression that decides where a publication sits in time, and it is
#: status-aware because the "Дата" column is: a published post shows when it
#: went out, a scheduled one when it is due, a draft when it was last saved.
#: Sorting, the period filter and the column all read this, so a row cannot
#: show one date and sort by another. A draft keeps its old `scheduled_at`
#: after being unscheduled, which is exactly why COALESCE alone was wrong.
EFFECTIVE_AT = """CASE status
        WHEN 'published' THEN COALESCE(published_at, updated_at)
        WHEN 'scheduled' THEN COALESCE(scheduled_at, created_at)
        ELSE updated_at
    END"""

MAX_PER_PAGE = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    # SQLite's own lower() only folds ASCII, so "ПОСТ" would not find "пост" —
    # in a Russian-language product that is the search box not working. Python's
    # str.lower() knows Unicode; registering it costs one call per connection.
    connection.create_function("u_lower", 1, _unicode_lower, deterministic=True)
    return connection


def _unicode_lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS smm_publications (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT '',
                binding_id TEXT NOT NULL DEFAULT '',
                target_chat_id TEXT NOT NULL DEFAULT '',
                delivery_mode TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT 'draft',
                tags TEXT NOT NULL DEFAULT '[]',
                media_url TEXT NOT NULL DEFAULT '',
                reach INTEGER,
                reach_delta REAL,
                scheduled_at TEXT,
                published_at TEXT,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_smm_pub_status ON smm_publications(status, scheduled_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_smm_pub_platform ON smm_publications(platform)"
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("effective_at", None)
    try:
        tags = json.loads(item.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    item["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []
    return item


def _clean_tags(tags: Optional[Iterable[Any]]) -> str:
    if not tags:
        return "[]"
    cleaned = []
    for tag in tags:
        text = str(tag).strip().lstrip("#").strip()
        if text and text not in cleaned:
            cleaned.append(text[:40])
    return json.dumps(cleaned[:12], ensure_ascii=False)


def _validate(
    status: str,
    delivery_mode: str,
    scheduled_at: Optional[str],
    already_published: bool = False,
) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    if delivery_mode not in DELIVERY_MODES:
        raise ValueError(f"Unknown delivery mode: {delivery_mode}")
    if status == "scheduled" and not scheduled_at:
        raise ValueError("A scheduled publication needs a date")
    if status == "published" and delivery_mode == "auto" and not already_published:
        # The whole point of auto delivery is that the row says "published"
        # because the backend saw the provider accept it. Letting an edit set
        # that flag would turn the register into a wish list. Editing a post
        # that is *already* published is not that: the send happened, and its
        # text or measured reach still has to be correctable.
        raise ValueError("Автопубликацию нельзя пометить опубликованной вручную — отправьте её кнопкой «Опубликовать»")


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD
# ═══════════════════════════════════════════════════════════════════════════════

def create_publication(
    title: str,
    platform: str,
    body: str = "",
    account: str = "",
    binding_id: str = "",
    target_chat_id: str = "",
    delivery_mode: str = "manual",
    status: str = "draft",
    tags: Optional[Iterable[Any]] = None,
    media_url: str = "",
    scheduled_at: Optional[str] = None,
    published_at: Optional[str] = None,
    reach: Optional[int] = None,
    reach_delta: Optional[float] = None,
) -> dict[str, Any]:
    _init_schema()
    title = (title or "").strip()
    platform = (platform or "").strip()
    if not title:
        raise ValueError("A publication needs a title")
    if not platform:
        raise ValueError("A publication needs a platform")
    # The form sends "" for "no date"; the column has to hold NULL, or an
    # empty string would sort ahead of every real timestamp.
    scheduled_at = scheduled_at or None
    published_at = published_at or None
    _validate(status, delivery_mode, scheduled_at)
    if status == "published" and not published_at:
        published_at = _now()

    post_id = f"post-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO smm_publications
                (id, title, body, platform, account, binding_id, target_chat_id, delivery_mode,
                 status, tags, media_url, reach, reach_delta, scheduled_at, published_at,
                 error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (post_id, title, body or "", platform, account or "", binding_id or "",
             target_chat_id or "", delivery_mode, status, _clean_tags(tags), media_url or "",
             reach, reach_delta, scheduled_at, published_at, now, now),
        )
    return get_publication(post_id)  # type: ignore[return-value]


#: Fields update_publication() will write. `status`/`published_at` are in here
#: because moving a post back to draft is a legitimate edit; actually *sending*
#: one goes through publish_publication() instead, which is the only path that
#: may set 'published' on an auto-delivery post.
UPDATABLE = (
    "title", "body", "platform", "account", "binding_id", "target_chat_id",
    "delivery_mode", "status", "media_url", "reach", "reach_delta",
    "scheduled_at", "published_at", "error",
)


def update_publication(post_id: str, **fields: Any) -> dict[str, Any]:
    existing = get_publication(post_id)
    if not existing:
        raise KeyError(post_id)

    updates: dict[str, Any] = {}
    if "tags" in fields and fields["tags"] is not None:
        updates["tags"] = _clean_tags(fields["tags"])
    for key in UPDATABLE:
        if key not in fields or fields[key] is None:
            continue
        # An empty string is how the form says "no date" — SQL needs a NULL, or
        # '' would sort ahead of every real timestamp.
        if key in ("scheduled_at", "published_at") and fields[key] == "":
            updates[key] = None
        else:
            updates[key] = fields[key]

    merged_status = updates.get("status", existing["status"])
    merged_mode = updates.get("delivery_mode", existing["delivery_mode"])
    merged_schedule = updates.get("scheduled_at", existing["scheduled_at"])
    _validate(merged_status, merged_mode, merged_schedule, already_published=existing["status"] == "published")
    if merged_status == "published" and not updates.get("published_at") and not existing["published_at"]:
        updates["published_at"] = _now()
    # Leaving 'failed' clears the stale provider error so the card stops
    # explaining a problem that no longer applies.
    if merged_status != "failed" and existing["status"] == "failed" and "error" not in updates:
        updates["error"] = ""

    if not updates:
        return existing
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with _connect() as connection:
        connection.execute(
            f"UPDATE smm_publications SET {assignments} WHERE id = ?",
            (*updates.values(), post_id),
        )
    return get_publication(post_id)  # type: ignore[return-value]


def _mark_published(post_id: str) -> dict[str, Any]:
    """The one writer allowed to set 'published'. update_publication() refuses
    that transition for auto posts on purpose (see _validate), so the flag can
    only be set here — after a real delivery — or by an owner explicitly
    recording a manual post."""
    now = _now()
    with _connect() as connection:
        connection.execute(
            "UPDATE smm_publications SET status = 'published', published_at = ?, error = '', updated_at = ? WHERE id = ?",
            (now, now, post_id),
        )
    return get_publication(post_id)  # type: ignore[return-value]


def _mark_failed(post_id: str, error: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE smm_publications SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:500], _now(), post_id),
        )


def get_publication(post_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM smm_publications WHERE id = ?", (post_id,)).fetchone()
    return _row_to_dict(row) if row else None


def delete_publication(post_id: str) -> None:
    _init_schema()
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM smm_publications WHERE id = ?", (post_id,))
        if cursor.rowcount == 0:
            raise KeyError(post_id)


def delete_publications(post_ids: Iterable[str]) -> int:
    """Bulk delete behind the table's selection checkboxes. Returns how many
    rows actually went — the caller reports that number rather than the number
    asked for, since a stale selection can name rows someone else removed."""
    ids = [pid for pid in post_ids if pid]
    if not ids:
        return 0
    _init_schema()
    placeholders = ", ".join("?" for _ in ids)
    with _connect() as connection:
        cursor = connection.execute(
            f"DELETE FROM smm_publications WHERE id IN ({placeholders})", tuple(ids)
        )
        return cursor.rowcount


# ═══════════════════════════════════════════════════════════════════════════════
# QUERY
# ═══════════════════════════════════════════════════════════════════════════════

def _filters(
    status: Optional[str],
    platform: Optional[str],
    query: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        if status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        clauses.append("status = ?")
        params.append(status)
    if platform and platform != "all":
        clauses.append("platform = ?")
        params.append(platform)
    if query:
        needle = f"%{query.strip().lower()}%"
        clauses.append(
            "(u_lower(title) LIKE ? OR u_lower(body) LIKE ? OR u_lower(tags) LIKE ? OR u_lower(account) LIKE ?)"
        )
        params.extend([needle, needle, needle, needle])
    # Period compares dates, not timestamps: the picker offers days, so an
    # end-of-period post at 23:30 must fall inside its own last day.
    if date_from:
        clauses.append(f"substr({EFFECTIVE_AT}, 1, 10) >= ?")
        params.append(date_from[:10])
    if date_to:
        clauses.append(f"substr({EFFECTIVE_AT}, 1, 10) <= ?")
        params.append(date_to[:10])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_publications(
    status: Optional[str] = None,
    platform: Optional[str] = None,
    query: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,
    sort: str = "date",
    direction: str = "desc",
) -> dict[str, Any]:
    _init_schema()
    column = SORT_COLUMNS.get(sort, SORT_COLUMNS["date"])
    order = "ASC" if str(direction).lower() == "asc" else "DESC"
    per_page = max(1, min(int(per_page or 10), MAX_PER_PAGE))
    page = max(1, int(page or 1))

    where, params = _filters(status, platform, query, date_from, date_to)
    with _connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) FROM smm_publications{where}", tuple(params)
        ).fetchone()[0]
        # Clamp the page rather than returning an empty list: deleting the last
        # row of page 4 should show page 3, not a blank table.
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        rows = connection.execute(
            f"""
            SELECT *, {EFFECTIVE_AT} AS effective_at FROM smm_publications{where}
            ORDER BY ({column} IS NULL), {column} {order}, created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, (page - 1) * per_page),
        ).fetchall()

    return {
        "items": [_row_to_dict(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


def _share(part: int, whole: int) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def publication_stats(
    platform: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """The KPI row above the table. Deliberately computed over the *same*
    filter the table uses (minus status, which the cards break down), so the
    numbers and the rows below them can never disagree."""
    _init_schema()
    where, params = _filters(None, platform, None, date_from, date_to)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")

    with _connect() as connection:
        counts = {row["status"]: row["n"] for row in connection.execute(
            f"SELECT status, COUNT(*) AS n FROM smm_publications{where} GROUP BY status", tuple(params)
        ).fetchall()}
        added_week = connection.execute(
            f"SELECT COUNT(*) FROM smm_publications{where}{' AND' if where else ' WHERE'} created_at >= ?",
            (*params, week_ago),
        ).fetchone()[0]
        best = connection.execute(
            f"""
            SELECT reach, reach_delta FROM smm_publications{where}
            {'AND' if where else 'WHERE'} reach IS NOT NULL
            ORDER BY reach DESC LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        # Oldest-first so the sparkline reads left to right like the dates do.
        series = connection.execute(
            f"""
            SELECT reach FROM smm_publications{where}
            {'AND' if where else 'WHERE'} reach IS NOT NULL AND status = 'published'
            ORDER BY {EFFECTIVE_AT} DESC LIMIT 12
            """,
            tuple(params),
        ).fetchall()

    total = sum(counts.values())
    published = counts.get("published", 0)
    scheduled = counts.get("scheduled", 0)
    drafts = counts.get("draft", 0)
    failed = counts.get("failed", 0)
    return {
        "total": total,
        "published": published,
        "scheduled": scheduled,
        "drafts": drafts,
        "failed": failed,
        "published_share": _share(published, total),
        "scheduled_share": _share(scheduled, total),
        "drafts_share": _share(drafts, total),
        "failed_share": _share(failed, total),
        "added_last_week": added_week,
        "best_reach": best["reach"] if best else None,
        "best_reach_delta": best["reach_delta"] if best else None,
        "reach_series": [row["reach"] for row in reversed(series)],
    }


def list_platforms_in_use() -> list[str]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT DISTINCT platform FROM smm_publications ORDER BY platform"
        ).fetchall()
    return [row["platform"] for row in rows]


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLISHING
# ═══════════════════════════════════════════════════════════════════════════════

def _composed_text(post: dict[str, Any]) -> str:
    parts = [post["title"].strip(), (post["body"] or "").strip()]
    tags = " ".join(f"#{tag}" for tag in post.get("tags") or [])
    if tags:
        parts.append(tags)
    return "\n\n".join(part for part in parts if part)


async def publish_publication(post_id: str) -> dict[str, Any]:
    """Puts one publication out now.

    `auto` posts go through channel_replies.send_to_channel — the same
    per-platform sender the draft queue uses, so there is one implementation of
    "talk to Telegram/VK/Slack" and a revoked credential fails the same way in
    both places. `manual` posts are the owner stating they posted it elsewhere;
    the backend records that claim and attributes nothing to itself.
    """
    post = get_publication(post_id)
    if not post:
        raise KeyError(post_id)
    if post["status"] == "published":
        raise ValueError("Публикация уже опубликована")

    if post["delivery_mode"] == "auto":
        if not post["binding_id"] or not post["target_chat_id"]:
            raise ValueError("Для автопубликации нужен подключённый канал и адрес назначения")
        from backend.channel_replies import send_to_channel

        try:
            await send_to_channel(post["binding_id"], post["platform"], post["target_chat_id"], _composed_text(post))
        except Exception as exc:
            logger.warning("SMM publication %s failed on %s: %s", post_id, post["platform"], exc)
            _mark_failed(post_id, f"{type(exc).__name__}: {exc}")
            raise

    return _mark_published(post_id)


async def publish_due(now: Optional[str] = None) -> list[dict[str, Any]]:
    """Sends every `auto` post whose slot has passed.

    Manual scheduled posts are left alone on purpose — the backend has no way
    to post them, so flipping them to 'published' on a timer would be inventing
    an event. They stay 'Запланировано' until the owner confirms.
    """
    _init_schema()
    cutoff = now or _now()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id FROM smm_publications
            WHERE status = 'scheduled' AND delivery_mode = 'auto'
              AND scheduled_at IS NOT NULL AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            """,
            (cutoff,),
        ).fetchall()

    sent: list[dict[str, Any]] = []
    for row in rows:
        try:
            sent.append(await publish_publication(row["id"]))
        except Exception:
            # publish_publication already recorded the failure on the row; the
            # sweep must carry on to the other posts due this minute.
            continue
    return sent


async def scheduler_loop(interval_seconds: int = 60) -> None:
    while True:
        try:
            published = await publish_due()
            if published:
                logger.info("SMM Factory: published %d scheduled post(s)", len(published))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SMM Factory scheduled-publication sweep failed")
        await asyncio.sleep(interval_seconds)


_init_schema()
