"""Database access for Content Factory.

Two things this module exists to avoid.

**Dialect drift.** `database.py` maintains two hand-written schemas — one for
SQLite, one for PostgreSQL — and they have silently diverged before (see
`tests/test_postgres_schema.py`, which exists solely to catch that). Content
Factory declares its tables *once* as data (`schema.py`) and renders the DDL
per dialect from that single declaration, so a forgotten column is not
expressible.

**Row shape.** `database._get_backend()` hands back a raw DBAPI connection
whose rows are tuples on both backends. Everything here returns plain dicts,
built from `cursor.description`, so callers never index by position and never
care which backend they are on.

Placeholders are always written as `?` and translated by the backend, matching
the convention in `database.py`.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

logger = logging.getLogger("hermes.content_factory.db")


def now_iso() -> str:
    """UTC timestamp in the same format the rest of the backend stores."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_postgres() -> bool:
    from backend.database import PostgresBackend, _get_backend

    return isinstance(_get_backend(), PostgresBackend)


@contextmanager
def connect() -> Iterator[Any]:
    """Yields a raw connection from whichever backend `database.py` selected."""
    from backend.database import _get_backend

    backend = _get_backend()
    with backend.connect() as conn:
        yield conn


def _translate(sql: str) -> str:
    from backend.database import _get_backend

    return _get_backend().translate_placeholder(sql)


def _rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    if cursor.description is None:
        return []
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(_translate(sql), params)
        return _rows_to_dicts(cursor)


def query_one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    """Runs a write and returns the affected row count."""
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(_translate(sql), params)
        conn.commit()
        return cursor.rowcount


def execute_many(sql: str, params_list: list[tuple]) -> None:
    if not params_list:
        return
    with connect() as conn:
        cursor = conn.cursor()
        cursor.executemany(_translate(sql), params_list)
        conn.commit()


@contextmanager
def transaction() -> Iterator[Any]:
    """One connection, one commit — for the multi-statement writes §116 requires
    to be atomic (creating a pipeline version, approving content, scheduling a
    publication, claiming a node run).

    Yields a cursor-like helper rather than the raw connection so callers keep
    writing `?` placeholders and dict rows.
    """
    with connect() as conn:
        cursor = conn.cursor()

        class _Tx:
            @staticmethod
            def execute(sql: str, params: tuple = ()) -> int:
                cursor.execute(_translate(sql), params)
                return cursor.rowcount

            @staticmethod
            def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
                cursor.execute(_translate(sql), params)
                return _rows_to_dicts(cursor)

            @staticmethod
            def query_one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
                rows = _Tx.query(sql, params)
                return rows[0] if rows else None

        try:
            yield _Tx()
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def dumps(value: Any) -> str:
    """JSON for a TEXT column. Never returns NULL — callers can always parse."""
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(raw: Any, default: Any = None) -> Any:
    """Parses a JSON TEXT column, tolerating already-parsed values (Postgres
    JSON columns) and corrupt rows."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Discarding unparseable JSON column value")
        return default
    return default if parsed is None else parsed
