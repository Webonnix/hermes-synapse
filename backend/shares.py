"""Public read-only conversation shares.

A share is an **immutable, sanitized snapshot** of a conversation, not a live
view of it. That distinction is the whole design:

* A live view would keep leaking whatever the conversation later accumulates,
  and would have to re-run authorization on every request against a URL that by
  definition has no session behind it.
* A snapshot is decided once, at creation, by code whose only job is to decide
  what may leave the system. Revisiting a share can never reveal more than the
  moment it was created.

What a snapshot deliberately drops
──────────────────────────────────
* ``system`` and ``tool`` messages entirely — tool payloads carry arguments,
  file paths and raw provider responses that were never meant to be published.
* Run metadata, cost, model, provider and message ids: operational detail about
  the installation, of no use to a reader.
* Absolute local paths, which name the host's filesystem layout.
* Anything the credential redactor recognises.

Revocation deletes the snapshot, so a revoked link stops working immediately
rather than merely becoming unlisted.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.shares")

TOKEN_BYTES = 24
# Roles that may appear in a published transcript. Everything else is dropped —
# an allowlist, so a role added later is excluded until someone decides it is
# safe to publish.
PUBLISHABLE_ROLES = ("user", "assistant")
MAX_MESSAGES = 500
MAX_MESSAGE_CHARS = 20000

# Local filesystem paths. Published transcripts must not describe the host.
_PATH_PATTERNS = (
    re.compile(r"/(?:home|root|Users)/[^\s\"'`)\]]+"),
    re.compile(r"/app/[^\s\"'`)\]]+"),
    re.compile(r"[A-Za-z]:\\\\[^\s\"'`)\]]+"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_schema(cursor, dialect: str) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_shares (
            token TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            title TEXT,
            snapshot TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_shares_chat ON conversation_shares (chat_id)"
    )


def _backend():
    from backend.database import _get_backend

    return _get_backend()


def _execute(sql: str, params: tuple = ()) -> None:
    backend = _backend()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(backend.translate_placeholder(sql), params)
        conn.commit()


def _query(sql: str, params: tuple = ()) -> List[tuple]:
    backend = _backend()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(backend.translate_placeholder(sql), params)
        return cursor.fetchall()


# ── sanitization ────────────────────────────────────────────────────────
def redact_paths(text: str) -> str:
    result = text
    for pattern in _PATH_PATTERNS:
        result = pattern.sub("[path]", result)
    return result


def sanitize_text(text: Any) -> str:
    """Strip credentials and local paths from one message body."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    from backend.redaction import _regex_redact

    redacted, _mapping = _regex_redact(text)
    redacted = redact_paths(redacted)
    if len(redacted) > MAX_MESSAGE_CHARS:
        redacted = redacted[:MAX_MESSAGE_CHARS] + "\n…[truncated]"
    return redacted


def sanitize_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reduce a stored message to what may be published, or None to drop it."""
    role = str(message.get("role") or "")
    if role not in PUBLISHABLE_ROLES:
        return None
    content = sanitize_text(message.get("content"))
    if not content.strip():
        return None
    # Only these three fields cross the boundary. Building the published shape by
    # allowlist means a new column on `messages` is never published by accident.
    return {"role": role, "content": content, "ts": message.get("timestamp")}


def build_snapshot(chat_id: str, messages: List[Dict[str, Any]], title: str = "") -> Dict[str, Any]:
    published = []
    for message in messages[-MAX_MESSAGES:]:
        item = sanitize_message(message)
        if item is not None:
            published.append(item)
    return {
        "version": 1,
        "title": sanitize_text(title or "Conversation"),
        "created_at": _now(),
        "messages": published,
    }


# ── lifecycle ───────────────────────────────────────────────────────────
def create_share(chat_id: str, messages: List[Dict[str, Any]], title: str = "") -> Dict[str, Any]:
    """Create (or replace) the share for a chat and return its token.

    One live share per chat: re-sharing supersedes the previous snapshot rather
    than leaving an older, still-valid link the owner has forgotten about.
    """
    snapshot = build_snapshot(chat_id, messages, title)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    revoke_share(chat_id)
    _execute(
        """
        INSERT INTO conversation_shares (token, chat_id, title, snapshot, message_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (token, str(chat_id), snapshot["title"], json.dumps(snapshot, ensure_ascii=False),
         len(snapshot["messages"]), snapshot["created_at"]),
    )
    logger.info("Created share for chat %s (%d messages)", chat_id, len(snapshot["messages"]))
    return {"token": token, "url": f"/share/{token}", "message_count": len(snapshot["messages"]),
            "created_at": snapshot["created_at"]}


def load_share(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) > 128:
        return None
    rows = _query("SELECT snapshot FROM conversation_shares WHERE token = ?", (token,))
    if not rows:
        return None
    try:
        return json.loads(rows[0][0])
    except (TypeError, ValueError):
        logger.warning("Share %s holds an unreadable snapshot", token[:8])
        return None


def get_share_for_chat(chat_id: str) -> Optional[Dict[str, Any]]:
    rows = _query(
        "SELECT token, message_count, created_at FROM conversation_shares WHERE chat_id = ?",
        (str(chat_id),),
    )
    if not rows:
        return None
    token, count, created_at = rows[0]
    return {"token": token, "url": f"/share/{token}", "message_count": count, "created_at": created_at}


def revoke_share(chat_id: str) -> bool:
    """Delete the snapshot. A revoked link stops working, it is not just unlisted."""
    existing = get_share_for_chat(chat_id)
    if existing is None:
        return False
    _execute("DELETE FROM conversation_shares WHERE chat_id = ?", (str(chat_id),))
    return True


# ── rendering ───────────────────────────────────────────────────────────
def _escape(text: str) -> str:
    import html

    return html.escape(text, quote=True)


def render_html(snapshot: Dict[str, Any]) -> str:
    """One self-contained page: no scripts, no CDN, no app assets.

    Rendering server-side without JavaScript means the published page cannot
    call the API, cannot read a session cookie, and has no path back into the
    app — a shared transcript is a document, not a view of the product.
    """
    rows = []
    for message in snapshot.get("messages", []):
        role = _escape(str(message.get("role", "")))
        body = _escape(str(message.get("content", "")))
        rows.append(
            f'<article class="msg {role}"><header>{role}</header><pre>{body}</pre></article>'
        )
    title = _escape(str(snapshot.get("title") or "Conversation"))
    created = _escape(str(snapshot.get("created_at") or ""))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; padding:2rem 1rem; background:#0b1020; color:#e6ecff;
  font:14px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
main {{ max-width:52rem; margin:0 auto; }}
h1 {{ font-size:1.25rem; margin:0 0 .25rem; }}
.meta {{ color:#7e8bb5; font-size:.8rem; margin-bottom:2rem; }}
.msg {{ border:1px solid #1e2a4d; border-radius:10px; margin-bottom:1rem; overflow:hidden; }}
.msg > header {{ padding:.4rem .75rem; font-size:.72rem; letter-spacing:.08em;
  text-transform:uppercase; background:#131c36; color:#8ea2d8; }}
.msg.assistant > header {{ color:#6ee7d5; }}
.msg pre {{ margin:0; padding:.85rem .75rem; white-space:pre-wrap; word-wrap:break-word;
  font:inherit; }}
.foot {{ margin-top:2.5rem; color:#5c6a94; font-size:.75rem; }}
</style></head>
<body><main>
<h1>{title}</h1>
<p class="meta">Read-only snapshot · {created}</p>
{''.join(rows) or '<p class="meta">This conversation has no publishable messages.</p>'}
<p class="foot">Published from Hermes. This page is a fixed snapshot — it does not update.</p>
</main></body></html>"""
