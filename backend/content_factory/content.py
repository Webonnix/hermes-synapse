"""Content items, channels and publications (§8, §49, §54, §55).

The publication half of this module is the one place in Content Factory where a
bug costs more than a wasted dollar: publishing the same video twice to a real
channel is visible to an audience and cannot be quietly retried away. So
`claim_publication` is written to be the *only* way a publish is started, and it
is idempotent by construction:

1. the row is keyed `(content_id, channel_id, publication_version)` with a
   unique index, so two concurrent claims cannot both create one;
2. a claim on a row that already carries an `external_id` returns that row
   instead of a permit to upload;
3. the transition into PUBLISHING is a conditional UPDATE — the row moves only
   if it is still in a state that allows publishing, so a second worker that
   arrives a millisecond later gets no rows updated and stands down.

That ordering matters more than any of the individual checks: every one of them
happens before the first byte reaches a platform.
"""

from __future__ import annotations

import logging
import json
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from backend.content_factory import db
from backend.content_factory.domain import ContentFactoryError, ErrorCode, PublicationStatus
from backend.content_factory.schema import WORKSPACE_DEFAULT, ensure_schema

logger = logging.getLogger("hermes.content_factory.content")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── Content items ────────────────────────────────────────────────────────────

def create_content_item(
    *,
    pipeline_run_id: str,
    project_id: Optional[str] = None,
    title: str = "",
    description: str = "",
    language: str = "ru",
    content_type: str = "VIDEO",
    status: str = "DRAFT",
    workspace_id: str = WORKSPACE_DEFAULT,
) -> dict[str, Any]:
    ensure_schema()
    content_id = _new_id("cont")
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_content_items
            (id, workspace_id, project_id, pipeline_run_id, type, status, title, description,
             language, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (content_id, workspace_id, project_id, pipeline_run_id, content_type, status, title,
         description, language, now, now),
    )
    return get_content_item(content_id)  # type: ignore[return-value]


def get_content_item(content_id: str) -> Optional[dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM cf_content_items WHERE id = ?", (content_id,))


def update_content_item(content_id: str, **fields: Any) -> dict[str, Any]:
    ensure_schema()
    allowed = {
        "status", "title", "description", "language", "hashtags", "storyboard_id",
        "primary_asset_id", "thumbnail_asset_id", "quality_score", "qa_report", "cost",
        "review_note", "reviewed_by", "reviewed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return get_content_item(content_id)  # type: ignore[return-value]
    for json_field in ("hashtags", "qa_report"):
        if json_field in updates and not isinstance(updates[json_field], str):
            updates[json_field] = db.dumps(updates[json_field])
    assignments = ", ".join(f"{column} = ?" for column in updates)
    db.execute(
        f"UPDATE cf_content_items SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), db.now_iso(), content_id),
    )
    return get_content_item(content_id)  # type: ignore[return-value]


def list_content(
    *,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    workspace_id: str = WORKSPACE_DEFAULT,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    """Cursor pagination (§75): `created_at` descending, cursor is the last row's
    created_at. Offset pagination on a table that is constantly being inserted
    into shows duplicates and skips rows."""
    ensure_schema()
    clauses = ["workspace_id = ?"]
    params: list[Any] = [workspace_id]
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if cursor:
        clauses.append("created_at < ?")
        params.append(cursor)
    page_size = max(1, min(int(limit), 200))
    params.append(page_size + 1)
    rows = db.query(
        f"SELECT * FROM cf_content_items WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    has_more = len(rows) > page_size
    items = rows[:page_size]
    return {
        "items": items,
        "nextCursor": items[-1]["created_at"] if has_more and items else None,
    }


# ── Channels ─────────────────────────────────────────────────────────────────

def create_channel(
    *,
    platform: str,
    account_name: str,
    account_external_id: str = "",
    project_id: Optional[str] = None,
    credentials_ref: str = "",
    scopes: Optional[list[str]] = None,
    credentials: Optional[dict[str, Any]] = None,
    proxy_url: str = "",
    workspace_id: str = WORKSPACE_DEFAULT,
) -> dict[str, Any]:
    ensure_schema()
    channel_id = _new_id("chan")
    now = db.now_iso()
    supplied_credentials_ref = bool(credentials_ref)
    secret = dict(credentials or {})
    proxy_host = _validate_proxy_url(proxy_url)
    if proxy_url:
        secret["proxy_url"] = proxy_url.strip()
    if secret and not credentials_ref:
        credentials_ref = f"cf:channel:{channel_id}"
        from backend.valkey_client import set_value

        set_value(credentials_ref, json.dumps(secret), ttl_seconds=None)
    connected = bool(secret.get("refresh_token") or secret.get("access_token") or supplied_credentials_ref)
    db.execute(
        """
        INSERT INTO cf_channels
            (id, workspace_id, project_id, platform, account_name, account_external_id, status,
             credentials_ref, proxy_enabled, proxy_host, scopes, connected_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel_id, workspace_id, project_id, platform.upper(), account_name,
            account_external_id, "CONNECTED" if connected else "DISCONNECTED",
            credentials_ref, 1 if proxy_url else 0, proxy_host, db.dumps(scopes or []),
            now if connected else None, now, now,
        ),
    )
    return get_channel(channel_id)  # type: ignore[return-value]


def get_channel(channel_id: str) -> Optional[dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM cf_channels WHERE id = ?", (channel_id,))


def list_channels(*, workspace_id: str = WORKSPACE_DEFAULT) -> list[dict[str, Any]]:
    ensure_schema()
    rows = db.query(
        "SELECT * FROM cf_channels WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,)
    )
    # `credentials_ref` names a secret; it is not a secret itself, but it has no
    # business in an API response either (§89).
    return [{key: value for key, value in row.items() if key != "credentials_ref"} for row in rows]


def _validate_proxy_url(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    parsed = urlsplit(proxy_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "Proxy URL must use http:// or https:// and include a host",
            retryable=False,
        )
    return parsed.hostname


def update_channel_connection(
    channel_id: str, *, credentials: Optional[dict[str, Any]] = None,
    proxy_url: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically rotates account credentials and/or its outbound proxy.

    Secrets stay in Valkey.  Passing an empty proxy URL explicitly disables the
    proxy; omitting it preserves the current setting.
    """
    ensure_schema()
    channel = get_channel(channel_id)
    if not channel:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Channel not found", retryable=False)
    from backend.valkey_client import get_value, set_value

    reference = str(channel.get("credentials_ref") or f"cf:channel:{channel_id}")
    try:
        secret = json.loads(get_value(reference) or "{}")
    except (TypeError, ValueError):
        secret = {}
    if credentials is not None:
        secret.update({key: value for key, value in credentials.items() if value not in (None, "")})
    proxy_host = str(channel.get("proxy_host") or "")
    proxy_enabled = int(channel.get("proxy_enabled") or 0)
    if proxy_url is not None:
        proxy_host = _validate_proxy_url(proxy_url)
        proxy_enabled = 1 if proxy_url else 0
        if proxy_url:
            secret["proxy_url"] = proxy_url.strip()
        else:
            secret.pop("proxy_url", None)
    set_value(reference, json.dumps(secret), ttl_seconds=None)
    connected = bool(secret.get("refresh_token") or secret.get("access_token"))
    now = db.now_iso()
    db.execute(
        """UPDATE cf_channels SET credentials_ref = ?, proxy_enabled = ?, proxy_host = ?,
           status = ?, connected_at = ?, last_error = '', updated_at = ? WHERE id = ?""",
        (reference, proxy_enabled, proxy_host, "CONNECTED" if connected else "DISCONNECTED",
         now if connected else None, now, channel_id),
    )
    return {key: value for key, value in (get_channel(channel_id) or {}).items()
            if key != "credentials_ref"}


def delete_channel(channel_id: str) -> bool:
    ensure_schema()
    channel = get_channel(channel_id)
    if not channel:
        return False
    reference = channel.get("credentials_ref")
    if reference:
        try:
            from backend.valkey_client import delete_value

            delete_value(reference)
        except Exception:
            logger.warning("Could not delete stored credentials for %s", channel_id, exc_info=True)
    return db.execute("DELETE FROM cf_channels WHERE id = ?", (channel_id,)) > 0


# ── Publications (§54, §55, §117) ────────────────────────────────────────────

def claim_publication(
    *,
    content_id: str,
    channel_id: str,
    publication_version: int = 1,
    title: str = "",
    description: str = "",
    scheduled_at: Optional[str] = None,
    dry_run: bool = False,
    metadata: Optional[dict[str, Any]] = None,
    workspace_id: str = WORKSPACE_DEFAULT,
) -> dict[str, Any]:
    """Returns `{"publication": row, "should_publish": bool}`.

    `should_publish` False means: this video is already live (or another worker
    is publishing it right now) and the caller must not upload anything.
    """
    ensure_schema()
    channel = get_channel(channel_id)
    if not channel:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"No such channel: {channel_id}", retryable=False
        )

    now = db.now_iso()
    existing = db.query_one(
        """
        SELECT * FROM cf_publications
        WHERE content_id = ? AND channel_id = ? AND publication_version = ?
        """,
        (content_id, channel_id, publication_version),
    )

    if existing is None:
        publication_id = _new_id("pub")
        try:
            db.execute(
                """
                INSERT INTO cf_publications
                    (id, workspace_id, content_id, channel_id, publication_version, platform,
                     status, dry_run, scheduled_at, title, description, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id, workspace_id, content_id, channel_id, publication_version,
                    channel["platform"], PublicationStatus.DRAFT, 1 if dry_run else 0,
                    scheduled_at, title, description, db.dumps(metadata or {}), now, now,
                ),
            )
        except Exception:
            # Lost the race against a concurrent claim; the unique index did its
            # job. Fall through and read the row the winner wrote.
            logger.info("Publication row already created concurrently for %s/%s", content_id, channel_id)
        existing = db.query_one(
            """
            SELECT * FROM cf_publications
            WHERE content_id = ? AND channel_id = ? AND publication_version = ?
            """,
            (content_id, channel_id, publication_version),
        )
        if existing is None:
            raise ContentFactoryError(ErrorCode.INTERNAL, "Could not create publication record")

    if existing.get("external_id"):
        # Already published: a retry of the node must never upload a second copy.
        return {"publication": existing, "should_publish": False}

    # Conditional transition: only one worker can move a row out of a
    # publishable state, so the loser simply does not publish.
    moved = db.execute(
        """
        UPDATE cf_publications
        SET status = ?, started_at = ?, updated_at = ?, dry_run = ?
        WHERE id = ? AND status IN (?, ?, ?)
        """,
        (
            PublicationStatus.PUBLISHING, now, now, 1 if dry_run else 0, existing["id"],
            PublicationStatus.DRAFT, PublicationStatus.SCHEDULED, PublicationStatus.FAILED,
        ),
    )
    refreshed = get_publication(existing["id"])
    return {"publication": refreshed, "should_publish": moved > 0}


def get_publication(publication_id: str) -> Optional[dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM cf_publications WHERE id = ?", (publication_id,))


def mark_published(publication_id: str, *, external_id: str, external_url: str = "",
                   published_at: Optional[str] = None) -> dict[str, Any]:
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_publications
        SET status = ?, external_id = ?, external_url = ?, published_at = ?, error_code = '',
            error_message = '', updated_at = ?
        WHERE id = ?
        """,
        (PublicationStatus.PUBLISHED, external_id, external_url, published_at or now, now, publication_id),
    )
    return get_publication(publication_id)  # type: ignore[return-value]


def mark_failed(publication_id: str, *, code: str, message: str) -> dict[str, Any]:
    now = db.now_iso()
    db.execute(
        """
        UPDATE cf_publications
        SET status = ?, error_code = ?, error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (PublicationStatus.FAILED, code, message[:500], now, publication_id),
    )
    return get_publication(publication_id)  # type: ignore[return-value]


def list_publications(
    *, content_id: Optional[str] = None, status: Optional[str] = None,
    workspace_id: str = WORKSPACE_DEFAULT, limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_schema()
    clauses = ["workspace_id = ?"]
    params: list[Any] = [workspace_id]
    if content_id:
        clauses.append("content_id = ?")
        params.append(content_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    return db.query(
        f"SELECT * FROM cf_publications WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
