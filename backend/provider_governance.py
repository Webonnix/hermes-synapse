"""Validation, approval and activation for external LLM provider bindings.

Mirrors mcp_governance.py's proposal/approve/execute shape exactly: a binding is
proposed (validated, R3 review task opened), sits `awaiting_approval` until an
owner approves it (Telegram `/approve` or the dashboard), then execute_approved_provider
activates it. The one real difference from MCP: the caller supplies a literal API
key up front (not an `${ENV_VAR}` reference), so it's written to Valkey immediately
under a short TTL that's cleared (made permanent) only once the binding is approved
and activated — never written to the SQL DB.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.database import DB_PATH
from backend.mcp_governance import _validate_url  # reuse the same SSRF/HTTPS guard

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ALLOWED_PROVIDER_TYPES = {"openai_compatible"}
_PENDING_SECRET_TTL_SECONDS = 24 * 60 * 60  # abandoned proposals self-clean after a day


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_bindings (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                api_base TEXT NOT NULL,
                valkey_secret_key TEXT NOT NULL,
                status TEXT NOT NULL,
                config_digest TEXT NOT NULL,
                control_task_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def validate_binding_config(name: str, provider_type: str, api_base: str) -> dict[str, Any]:
    clean_name = str(name).strip().lower()
    if not _NAME.fullmatch(clean_name):
        raise ValueError("Provider name must contain only lowercase letters, digits, underscores or hyphens")
    clean_type = str(provider_type).strip().lower()
    if clean_type not in _ALLOWED_PROVIDER_TYPES:
        raise ValueError(f"Unsupported provider_type: {clean_type}")
    clean_base = _validate_url(str(api_base).strip().rstrip("/"), "api_base")
    return {"name": clean_name, "provider_type": clean_type, "api_base": clean_base}


def _digest(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _binding_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("valkey_secret_key", None)  # never surfaced outside this module
    return item


def create_binding_proposal(name: str, provider_type: str, api_base: str, api_key: str) -> dict[str, Any]:
    _init_schema()
    validated = validate_binding_config(name, provider_type, api_base)
    if not str(api_key).strip():
        raise ValueError("api_key is required")
    digest = _digest(validated)

    with _connect() as connection:
        existing = connection.execute(
            """
            SELECT * FROM provider_bindings
            WHERE config_digest = ? AND status IN ('awaiting_approval', 'approved', 'active')
            ORDER BY created_at DESC LIMIT 1
            """,
            (digest,),
        ).fetchone()
    if existing:
        return _binding_from_row(existing)

    from backend.control_plane import create_review_task

    binding_id = f"prov-{uuid.uuid4().hex[:12]}"
    secret_key = f"provider_secret:{binding_id}"

    task = create_review_task(
        goal=f"Bind agent provider: {validated['name']} ({validated['provider_type']})",
        arguments={
            "name": validated["name"],
            "provider_type": validated["provider_type"],
            "api_base": validated["api_base"],
            "config_digest": digest,
            "secret_policy": "API key stored only in Valkey (internal-only, password-protected), never in the SQL DB",
        },
        risk_class="R3",
        acceptance=[
            "api_base passed the same SSRF/HTTPS validation as MCP server connections",
            "API key never appears in the SQL database or application logs",
            "Sensitive-data redaction applies automatically to every call routed through this binding",
        ],
        rollback=f"Revoke binding '{validated['name']}' and delete its Valkey secret.",
        requester="autonomy:provider",
    )

    from backend.valkey_client import set_value

    now = _now()
    set_value(secret_key, str(api_key), ttl_seconds=_PENDING_SECRET_TTL_SECONDS)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO provider_bindings
                (id, name, provider_type, api_base, valkey_secret_key, status, config_digest, control_task_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'awaiting_approval', ?, ?, ?, ?)
            """,
            (
                binding_id,
                validated["name"],
                validated["provider_type"],
                validated["api_base"],
                secret_key,
                digest,
                task["id"],
                now,
                now,
            ),
        )
        row = connection.execute("SELECT * FROM provider_bindings WHERE id = ?", (binding_id,)).fetchone()
    result = _binding_from_row(row)
    result["control_task"] = task
    return result


def get_provider_proposal(control_task_id: str) -> dict[str, Any] | None:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM provider_bindings WHERE control_task_id = ? ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    return _binding_from_row(row) if row else None


def _set_status(binding_id: str, status: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE provider_bindings SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), binding_id),
        )


async def execute_approved_provider(control_task_id: str) -> dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM provider_bindings WHERE control_task_id = ? ORDER BY created_at DESC LIMIT 1",
            (control_task_id,),
        ).fetchone()
    if not row:
        raise KeyError(control_task_id)
    binding = dict(row)

    from backend.control_plane import finish_task, get_task, start_task

    task = get_task(control_task_id)
    if not task or task["status"] != "approved":
        raise PermissionError("Provider binding is not approved")
    if binding["status"] not in {"awaiting_approval", "approved", "failed"}:
        raise ValueError(f"Provider binding cannot activate from status {binding['status']}")

    start_task(control_task_id)
    try:
        from backend.valkey_client import get_value, set_value

        secret = get_value(binding["valkey_secret_key"])
        if not secret:
            raise RuntimeError("Provider API key expired before approval — re-create the binding")
        # Make the secret permanent now that the binding is active (was TTL'd while pending).
        set_value(binding["valkey_secret_key"], secret, ttl_seconds=None)
        _set_status(binding["id"], "active")
        result = {"status": "active", "id": binding["id"], "name": binding["name"]}
    except Exception as exc:
        _set_status(binding["id"], "failed")
        finish_task(control_task_id, "", error=str(exc))
        raise
    finish_task(control_task_id, json.dumps(result, ensure_ascii=False))
    return result


def list_bindings() -> list[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM provider_bindings ORDER BY created_at DESC").fetchall()
    return [_binding_from_row(row) for row in rows]


def get_binding(binding_id: str) -> dict[str, Any] | None:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM provider_bindings WHERE id = ?", (binding_id,)).fetchone()
    return _binding_from_row(row) if row else None


def resolve_binding_credentials(binding_id: str) -> tuple[str, str] | None:
    """Return (api_base, api_key) for an active binding, or None if inactive/missing."""
    binding = get_binding(binding_id)
    if not binding or binding["status"] != "active":
        return None
    from backend.valkey_client import get_value

    secret = get_value(f"provider_secret:{binding_id}")
    if not secret:
        return None
    return binding["api_base"], secret


def revoke_binding(binding_id: str) -> None:
    binding = get_binding(binding_id)
    if not binding:
        raise KeyError(binding_id)
    from backend.valkey_client import delete_value

    delete_value(f"provider_secret:{binding_id}")
    _set_status(binding_id, "revoked")


def delete_binding(binding_id: str) -> None:
    """Permanently removes a provider binding — unlike revoke_binding (which only
    flips status to 'revoked' and leaves the row so it stays listed), this drops the
    row entirely. Used by the dashboard's trash-icon action, which the user expects
    to make the entry disappear regardless of its current status."""
    binding = get_binding(binding_id)
    if not binding:
        raise KeyError(binding_id)
    from backend.valkey_client import delete_value

    delete_value(f"provider_secret:{binding_id}")
    with _connect() as connection:
        connection.execute("DELETE FROM provider_bindings WHERE id = ?", (binding_id,))


_init_schema()
