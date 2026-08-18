"""
_init_postgres_schema() only ever runs against a real PostgreSQL instance in
production, and no such instance is available in this test environment. These
tests exercise the function directly against a fake backend/connection that
mimics psycopg2's cursor semantics (%s placeholders, information_schema.columns
lookups) closely enough to run the function's control flow end-to-end and
capture every ALTER TABLE it issues.

The main thing being guarded against is column-list drift between
_init_sqlite_schema() and _init_postgres_schema(): both functions maintain
independent, hand-written ALTER TABLE migration lists for the same tables,
and it's easy to add a column to one and forget the other (see subagents'
model_provider/model_type/... columns and decision_logs.provider_id, which
drifted out of the Postgres path for months before being caught).
"""

import re
from contextlib import contextmanager

import pytest

from backend import database


class _FakeCursor:
    def __init__(self, log):
        self._log = log
        self._last_sql = ""

    def execute(self, sql, params=None):
        self._last_sql = sql
        self._log.append((sql.strip(), params))

    def executemany(self, sql, params_list):
        self._log.append((sql.strip(), list(params_list)))

    def fetchone(self):
        sql = self._last_sql
        if "information_schema.columns" in sql:
            # Simulate a brand-new table missing every migrated column, so
            # every ALTER TABLE in the migration loops actually fires and
            # gets captured in the log.
            return None
        if "SELECT COUNT(*)" in sql:
            # Non-empty table: skip the bulk-seed branch and exercise the
            # ON CONFLICT upsert (_migrate_existing_subagents_postgres) path
            # instead, matching what actually happens on every real restart.
            return (1,)
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return _FakeCursor(self._log)

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakePostgresBackend(database.DatabaseBackend):
    """Records every statement _init_postgres_schema() executes."""

    def __init__(self):
        self.log = []

    @contextmanager
    def connect(self):
        yield _FakeConn(self.log)

    def translate_placeholder(self, sql: str) -> str:
        return sql

    def init_schema(self) -> None:
        database._init_postgres_schema()


def _added_columns(log, table: str) -> set:
    pattern = re.compile(rf"ALTER TABLE {table} ADD COLUMN (\w+)")
    cols = set()
    for sql, _params in log:
        m = pattern.search(sql)
        if m:
            cols.add(m.group(1))
    return cols


@pytest.fixture
def fake_backend(monkeypatch):
    backend = _FakePostgresBackend()
    monkeypatch.setattr(database, "_get_backend", lambda: backend)
    return backend


def test_init_postgres_schema_runs_without_error(fake_backend):
    """The function must execute end-to-end against a fresh table (all
    information_schema lookups reporting the column missing) without
    raising — this alone would catch a typo'd column type or SQL syntax
    error in the migration lists."""
    database._init_postgres_schema()


def test_postgres_subagents_migration_matches_sqlite(fake_backend):
    """The Postgres subagents ALTER TABLE list must add every column that
    the SQLite path adds via ALTER TABLE (excluding the handful baked
    directly into Postgres's CREATE TABLE, which are equivalent, just
    declared up front instead of migrated in)."""
    database._init_postgres_schema()

    # Columns _init_sqlite_schema() adds to `subagents` via its ALTER TABLE
    # migration loop (see database.py, _init_sqlite_schema).
    sqlite_migrated_columns = {
        "agent_type", "parent_id", "skills", "x", "y", "temperature",
        "role", "status", "is_enabled", "model_provider", "model_type",
        "model_params", "current_task", "last_action", "last_error",
        "progress", "updated_at", "budget_usd_limit", "budget_period",
        "tier_id", "allowed_provider_ids", "budget_fallback_to_local",
        "project_id",
    }

    postgres_added = _added_columns(fake_backend.log, "subagents")

    missing = sqlite_migrated_columns - postgres_added
    assert not missing, (
        f"Postgres subagents migration is missing columns present in SQLite: {missing}. "
        "Add them to _init_postgres_schema()'s subagents ALTER TABLE list."
    )


def test_postgres_decision_logs_migration_matches_sqlite(fake_backend):
    """Same parity check as above, for decision_logs."""
    database._init_postgres_schema()

    sqlite_migrated_columns = {
        "agent_id", "completion_tokens_estimate", "cost_usd", "provider_id",
    }

    postgres_added = _added_columns(fake_backend.log, "decision_logs")

    missing = sqlite_migrated_columns - postgres_added
    assert not missing, (
        f"Postgres decision_logs migration is missing columns present in SQLite: {missing}. "
        "Add them to _init_postgres_schema()'s decision_logs ALTER TABLE list."
    )
