"""Schema rendering and migration for Content Factory.

The point of the declarative schema is that SQLite and PostgreSQL can never
drift apart the way `database.py`'s two hand-written migration lists did. These
tests pin that: the same declaration renders for both dialects, and adding a
column to the declaration is enough to migrate a table that already exists.
"""

import pytest

from backend import database
from backend.content_factory import db, schema


@pytest.fixture()
def cf_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cf.db"))
    database._set_backend_for_tests(database.SQLiteBackend())
    schema.ensure_schema(force=True)
    yield
    database._set_backend_for_tests(None)


def test_every_declared_table_is_created(cf_db):
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    created = {row["name"] for row in rows}
    for table in schema.TABLES:
        assert table.name in created


def test_declared_indexes_are_created(cf_db):
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'index'")
    created = {row["name"] for row in rows}
    for table in schema.TABLES:
        for index in table.indexes:
            assert index.name(table.name) in created


def test_publication_uniqueness_is_enforced(cf_db):
    """§55/§117: the same content may not be published twice to one channel at
    the same publication_version, whatever the caller does."""
    now = db.now_iso()
    insert = """
        INSERT INTO cf_publications
            (id, workspace_id, content_id, channel_id, publication_version, platform,
             status, created_at, updated_at)
        VALUES (?, 'default', 'content-1', 'channel-1', 1, 'YOUTUBE', 'DRAFT', ?, ?)
    """
    db.execute(insert, ("pub-1", now, now))
    with pytest.raises(Exception):
        db.execute(insert.replace("'pub-1'", "'pub-2'"), ("pub-2", now, now))


def test_asset_storage_key_is_unique(cf_db):
    now = db.now_iso()
    insert = """
        INSERT INTO cf_assets (id, workspace_id, type, storage_key, created_at)
        VALUES (?, 'default', 'RENDER', 'runs/r1/final.mp4', ?)
    """
    db.execute(insert, ("asset-1", now))
    with pytest.raises(Exception):
        db.execute(insert, ("asset-2", now))


def test_ensure_schema_adds_a_column_declared_later(cf_db, monkeypatch):
    """A column added to the declaration migrates an existing table — the whole
    migration story for this module."""
    original = schema.CF_PIPELINES
    extended = schema.Table(
        original.name,
        original.columns + (schema.Column("thumbnail_url", "TEXT", default="''"),),
        original.indexes,
    )
    monkeypatch.setattr(
        schema, "TABLES", tuple(extended if t is original else t for t in schema.TABLES)
    )

    schema.ensure_schema(force=True)

    columns = {row["name"] for row in db.query("PRAGMA table_info(cf_pipelines)")}
    assert "thumbnail_url" in columns


def test_postgres_rendering_uses_portable_types():
    """No SQLite-only spelling leaks into the Postgres DDL (§65 keeps the door
    open for a backend switch by environment variable)."""
    for table in schema.TABLES:
        sql = schema.create_table_sql(table, postgres=True)
        assert "AUTOINCREMENT" not in sql
        assert "INTEGER PRIMARY KEY" not in sql
        for column in table.columns:
            if column.type == "REAL":
                assert "DOUBLE PRECISION" in sql
            if column.type == "INT":
                assert "BIGINT" in sql


def test_every_table_carries_workspace_id():
    """§80: retrofitting a tenant key later is far more expensive than carrying
    one now, so nothing is allowed to skip it."""
    for table in schema.TABLES:
        assert any(column.name == "workspace_id" for column in table.columns), table.name
