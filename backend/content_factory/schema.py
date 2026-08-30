"""Content Factory schema — declared once, rendered per dialect.

Every table lives in this module as data. `ensure_schema()` creates what is
missing and adds columns that appeared since the last deployment, on SQLite and
PostgreSQL alike, from the same declaration. Adding a column here is the whole
migration: there is no second list to keep in sync (contrast `database.py`,
whose parallel SQLite/Postgres migration lists drift — see
`tests/test_postgres_schema.py`).

Conventions inherited from the rest of the backend:

* ids are TEXT (uuid-ish, prefixed per entity), never autoincrement integers;
* booleans are INTEGER 0/1;
* structured values are JSON in TEXT columns;
* timestamps are ISO-8601 UTC strings in TEXT columns;
* no foreign-key constraints — references are soft and checked in code, exactly
  as `subagents.tier_id` / `dev_runs.parent_run_id` already are;
* `workspace_id` on every table (§80). VEXA is single-tenant today and every row
  gets 'default', but retrofitting a tenant key onto live tables is far more
  expensive than carrying one from the start.

Binary media never lands here (§44): assets carry a storage key, the bytes live
in object storage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("hermes.content_factory.schema")

WORKSPACE_DEFAULT = "default"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # TEXT | INT | REAL | JSON | BOOL
    null: bool = True
    default: Optional[str] = None
    primary_key: bool = False


@dataclass(frozen=True)
class Index:
    columns: tuple[str, ...]
    unique: bool = False

    def name(self, table: str) -> str:
        suffix = "_".join(self.columns)
        return f"idx_{table}_{suffix}"[:63]  # Postgres identifier limit


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    indexes: tuple[Index, ...] = field(default_factory=tuple)


_SQLITE_TYPES = {"TEXT": "TEXT", "INT": "INTEGER", "REAL": "REAL", "JSON": "TEXT", "BOOL": "INTEGER"}
_POSTGRES_TYPES = {"TEXT": "TEXT", "INT": "BIGINT", "REAL": "DOUBLE PRECISION", "JSON": "TEXT", "BOOL": "SMALLINT"}


def _column_sql(column: Column, postgres: bool) -> str:
    sql_type = (_POSTGRES_TYPES if postgres else _SQLITE_TYPES)[column.type]
    parts = [f"{column.name} {sql_type}"]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if not column.null:
        parts.append("NOT NULL")
    if column.default is not None:
        parts.append(f"DEFAULT {column.default}")
    return " ".join(parts)


def _id() -> Column:
    return Column("id", "TEXT", null=False, primary_key=True)


def _workspace() -> Column:
    return Column("workspace_id", "TEXT", null=False, default=f"'{WORKSPACE_DEFAULT}'")


def _created() -> tuple[Column, Column]:
    return (
        Column("created_at", "TEXT", null=False),
        Column("updated_at", "TEXT", null=False),
    )


# ── Tables (§77) ──────────────────────────────────────────────────────────────

# §9 — Content-specific settings for an existing `projects` row. The Project
# entity itself is NOT re-implemented here: `projects.py` already owns naming,
# nesting and lifecycle, and duplicating it would give the owner two places to
# rename the same thing.
CF_PROJECT_CONFIGS = Table(
    "cf_project_configs",
    (
        Column("project_id", "TEXT", null=False, primary_key=True),
        _workspace(),
        Column("status", "TEXT", null=False, default="'ACTIVE'"),  # ACTIVE|PAUSED|ARCHIVED
        Column("default_language", "TEXT", null=False, default="'ru'"),
        Column("timezone", "TEXT", null=False, default="'UTC'"),
        Column("content_niche", "TEXT", null=False, default="''"),
        Column("target_audience", "TEXT", null=False, default="''"),
        Column("brand_config", "JSON", null=False, default="'{}'"),
        Column("content_config", "JSON", null=False, default="'{}'"),
        Column("publishing_defaults", "JSON", null=False, default="'{}'"),
        Column("ai_defaults", "JSON", null=False, default="'{}'"),
        Column("default_pipeline_id", "TEXT"),
        *_created(),
    ),
    (Index(("workspace_id", "status")),),
)

# §10 — the mutable header. Everything that actually executes lives in an
# immutable version row (§11), so editing a pipeline can never rewrite the
# history of runs that already used it.
CF_PIPELINES = Table(
    "cf_pipelines",
    (
        _id(),
        _workspace(),
        Column("project_id", "TEXT"),
        Column("name", "TEXT", null=False),
        Column("description", "TEXT", null=False, default="''"),
        Column("status", "TEXT", null=False, default="'DRAFT'"),  # DRAFT|ACTIVE|PAUSED|ARCHIVED
        Column("active_version_id", "TEXT"),
        Column("draft_version_id", "TEXT"),
        Column("publishing_mode", "TEXT", null=False, default="'REVIEW_REQUIRED'"),
        Column("error_strategy", "TEXT", null=False, default="'FAIL_FAST'"),
        Column("max_cost_per_run", "REAL"),
        Column("max_concurrent_runs", "INT", null=False, default="1"),
        Column("created_by", "TEXT", null=False, default="'owner'"),
        *_created(),
    ),
    (Index(("workspace_id", "status")), Index(("project_id", "status"))),
)

# §11 — immutable snapshot. Never UPDATEd once `published_at` is set; a change
# to an active pipeline produces a new draft row instead.
CF_PIPELINE_VERSIONS = Table(
    "cf_pipeline_versions",
    (
        _id(),
        _workspace(),
        Column("pipeline_id", "TEXT", null=False),
        Column("version", "INT", null=False),
        Column("schema_version", "INT", null=False, default=str(SCHEMA_VERSION)),
        Column("nodes", "JSON", null=False, default="'[]'"),
        Column("edges", "JSON", null=False, default="'[]'"),
        Column("variables", "JSON", null=False, default="'{}'"),
        Column("config", "JSON", null=False, default="'{}'"),
        Column("notes", "TEXT", null=False, default="''"),
        Column("published_at", "TEXT"),
        Column("created_by", "TEXT", null=False, default="'owner'"),
        *_created(),
    ),
    (Index(("pipeline_id", "version"), unique=True),),
)

# §16
CF_PIPELINE_RUNS = Table(
    "cf_pipeline_runs",
    (
        _id(),
        _workspace(),
        Column("pipeline_id", "TEXT", null=False),
        Column("pipeline_version_id", "TEXT", null=False),
        Column("project_id", "TEXT"),
        Column("trigger_type", "TEXT", null=False, default="'MANUAL'"),
        Column("trigger_data", "JSON", null=False, default="'{}'"),
        Column("status", "TEXT", null=False, default="'QUEUED'"),
        Column("progress", "INT", null=False, default="0"),
        # Test runs (§107) force every publish node into dry-run (§108) at the
        # engine level. Stored on the run, not the node config, so it cannot be
        # switched off by editing a node.
        Column("dry_run", "BOOL", null=False, default="0"),
        Column("publishing_mode", "TEXT", null=False, default="'REVIEW_REQUIRED'"),
        Column("started_at", "TEXT"),
        Column("completed_at", "TEXT"),
        Column("estimated_cost", "REAL", null=False, default="0"),
        Column("actual_cost", "REAL", null=False, default="0"),
        Column("cost_budget", "REAL"),
        Column("error_code", "TEXT", null=False, default="''"),
        Column("error_message", "TEXT", null=False, default="''"),
        Column("trace_id", "TEXT", null=False, default="''"),
        # Heartbeat of the worker that owns this run. A run whose lease has
        # expired is up for recovery after a restart (§17 durability without
        # Temporal) — same idea as dev_runs.recover_crashed_runs.
        Column("worker_id", "TEXT", null=False, default="''"),
        Column("lease_expires_at", "TEXT"),
        Column("created_by", "TEXT", null=False, default="'owner'"),
        *_created(),
    ),
    (
        Index(("status", "created_at")),  # §79
        Index(("pipeline_id", "created_at")),
        Index(("trace_id",)),
    ),
)

# §15
CF_NODE_RUNS = Table(
    "cf_node_runs",
    (
        _id(),
        _workspace(),
        Column("pipeline_run_id", "TEXT", null=False),
        Column("node_id", "TEXT", null=False),
        Column("node_type", "TEXT", null=False, default="''"),
        Column("name", "TEXT", null=False, default="''"),
        Column("status", "TEXT", null=False, default="'PENDING'"),
        Column("attempt", "INT", null=False, default="0"),
        Column("progress", "INT", null=False, default="0"),
        Column("started_at", "TEXT"),
        Column("finished_at", "TEXT"),
        Column("duration_ms", "INT", null=False, default="0"),
        Column("input", "JSON", null=False, default="'{}'"),
        Column("output", "JSON", null=False, default="'{}'"),
        Column("error_code", "TEXT", null=False, default="''"),
        Column("error_message", "TEXT", null=False, default="''"),
        Column("retryable", "BOOL", null=False, default="0"),
        Column("agent_id", "TEXT", null=False, default="''"),
        Column("provider", "TEXT", null=False, default="''"),
        Column("model", "TEXT", null=False, default="''"),
        # Which fallback rung actually ran (§21): 0 = primary provider.
        Column("fallback_index", "INT", null=False, default="0"),
        Column("cost", "REAL", null=False, default="0"),
        # Long-running external jobs (video generation, renders) park here and
        # are polled; the id survives a backend restart so a paid job is never
        # abandoned or paid for twice.
        Column("external_job_id", "TEXT", null=False, default="''"),
        Column("next_poll_at", "TEXT"),
        Column("logs", "JSON", null=False, default="'[]'"),
        Column("metrics", "JSON", null=False, default="'{}'"),
        *_created(),
    ),
    (
        Index(("pipeline_run_id", "status")),  # §79
        Index(("pipeline_run_id", "node_id"), unique=True),
        Index(("status", "next_poll_at")),
    ),
)

# §78
CF_CONTENT_ITEMS = Table(
    "cf_content_items",
    (
        _id(),
        _workspace(),
        Column("project_id", "TEXT"),
        Column("pipeline_run_id", "TEXT"),
        Column("type", "TEXT", null=False, default="'VIDEO'"),
        Column("status", "TEXT", null=False, default="'DRAFT'"),
        Column("title", "TEXT", null=False, default="''"),
        Column("description", "TEXT", null=False, default="''"),
        Column("language", "TEXT", null=False, default="'ru'"),
        Column("hashtags", "JSON", null=False, default="'[]'"),
        Column("storyboard_id", "TEXT"),
        Column("primary_asset_id", "TEXT"),
        Column("thumbnail_asset_id", "TEXT"),
        Column("quality_score", "REAL"),
        Column("qa_report", "JSON", null=False, default="'{}'"),
        Column("cost", "REAL", null=False, default="0"),
        Column("review_note", "TEXT", null=False, default="''"),
        Column("reviewed_by", "TEXT", null=False, default="''"),
        Column("reviewed_at", "TEXT"),
        *_created(),
    ),
    (Index(("project_id", "status")), Index(("status", "created_at"))),  # §79
)

# §28 — structured, versioned; never a markdown blob.
CF_STORYBOARDS = Table(
    "cf_storyboards",
    (
        _id(),
        _workspace(),
        Column("content_id", "TEXT"),
        Column("pipeline_run_id", "TEXT"),
        Column("version", "INT", null=False, default="1"),
        Column("title", "TEXT", null=False, default="''"),
        Column("duration", "REAL", null=False, default="0"),
        Column("scenes", "JSON", null=False, default="'[]'"),
        Column("metadata", "JSON", null=False, default="'{}'"),
        *_created(),
    ),
    (Index(("content_id", "version")),),
)

# §45 — assets are immutable: a regenerated visual is a new row, so an old run's
# trace keeps pointing at what it actually produced.
CF_ASSETS = Table(
    "cf_assets",
    (
        _id(),
        _workspace(),
        Column("project_id", "TEXT"),
        Column("pipeline_run_id", "TEXT"),
        Column("node_run_id", "TEXT"),
        Column("content_id", "TEXT"),
        Column("type", "TEXT", null=False),  # IMAGE|VIDEO|AUDIO|VOICE|MUSIC|SUBTITLE|THUMBNAIL|DOCUMENT|RENDER
        Column("storage_key", "TEXT", null=False),
        Column("mime_type", "TEXT", null=False, default="''"),
        Column("size_bytes", "INT", null=False, default="0"),
        Column("duration", "REAL"),
        Column("width", "INT"),
        Column("height", "INT"),
        Column("checksum", "TEXT", null=False, default="''"),
        Column("provider", "TEXT", null=False, default="''"),
        Column("model", "TEXT", null=False, default="''"),
        Column("cost", "REAL", null=False, default="0"),
        Column("metadata", "JSON", null=False, default="'{}'"),
        Column("created_at", "TEXT", null=False),
    ),
    (
        Index(("pipeline_run_id",)),  # §79
        Index(("project_id", "type", "created_at")),
        Index(("storage_key",), unique=True),
    ),
)

# §49 — OAuth material is NOT stored here (§50, §63): `credentials_ref` names a
# secret in the same Valkey-backed store provider_governance.py already uses.
CF_CHANNELS = Table(
    "cf_channels",
    (
        _id(),
        _workspace(),
        Column("project_id", "TEXT"),
        Column("platform", "TEXT", null=False),  # YOUTUBE|TIKTOK|...
        Column("account_name", "TEXT", null=False, default="''"),
        Column("account_external_id", "TEXT", null=False, default="''"),
        Column("status", "TEXT", null=False, default="'DISCONNECTED'"),
        Column("credentials_ref", "TEXT", null=False, default="''"),
        # Proxy credentials live beside OAuth material in Valkey. SQL only
        # carries a boolean and a display-safe host so API responses can never
        # reveal proxy usernames/passwords.
        Column("proxy_enabled", "BOOL", null=False, default="0"),
        Column("proxy_host", "TEXT", null=False, default="''"),
        Column("scopes", "JSON", null=False, default="'[]'"),
        Column("metadata", "JSON", null=False, default="'{}'"),
        Column("last_error", "TEXT", null=False, default="''"),
        Column("analytics_updated_at", "TEXT"),
        Column("connected_at", "TEXT"),
        *_created(),
    ),
    (Index(("workspace_id", "platform", "status")),),
)

# §54 — the unique index is the entire duplicate-publication defence (§55, §117).
CF_PUBLICATIONS = Table(
    "cf_publications",
    (
        _id(),
        _workspace(),
        Column("content_id", "TEXT", null=False),
        Column("channel_id", "TEXT", null=False),
        Column("publication_version", "INT", null=False, default="1"),
        Column("platform", "TEXT", null=False),
        Column("status", "TEXT", null=False, default="'DRAFT'"),
        Column("dry_run", "BOOL", null=False, default="0"),
        Column("scheduled_at", "TEXT"),
        Column("started_at", "TEXT"),
        Column("published_at", "TEXT"),
        Column("external_id", "TEXT", null=False, default="''"),
        Column("external_url", "TEXT", null=False, default="''"),
        Column("title", "TEXT", null=False, default="''"),
        Column("description", "TEXT", null=False, default="''"),
        Column("metadata", "JSON", null=False, default="'{}'"),
        Column("error_code", "TEXT", null=False, default="''"),
        Column("error_message", "TEXT", null=False, default="''"),
        *_created(),
    ),
    (
        Index(("content_id", "channel_id", "publication_version"), unique=True),  # §117
        Index(("status", "scheduled_at")),  # §79
    ),
)

# §48 — timezone is stored explicitly and never inferred from the host clock.
CF_SCHEDULES = Table(
    "cf_schedules",
    (
        _id(),
        _workspace(),
        Column("pipeline_id", "TEXT", null=False),
        Column("kind", "TEXT", null=False, default="'CRON'"),  # CRON|INTERVAL|ONCE
        Column("expression", "TEXT", null=False, default="''"),
        Column("timezone", "TEXT", null=False, default="'UTC'"),
        Column("enabled", "BOOL", null=False, default="1"),
        Column("next_run_at", "TEXT"),
        Column("last_run_at", "TEXT"),
        Column("last_run_id", "TEXT"),
        *_created(),
    ),
    (Index(("enabled", "next_run_at")),),
)

# §59 — one row per external AI call, whatever the modality.
CF_USAGE_RECORDS = Table(
    "cf_usage_records",
    (
        _id(),
        _workspace(),
        Column("pipeline_run_id", "TEXT"),
        Column("node_run_id", "TEXT"),
        Column("project_id", "TEXT"),
        Column("provider", "TEXT", null=False, default="''"),
        Column("model", "TEXT", null=False, default="''"),
        Column("operation", "TEXT", null=False, default="''"),
        Column("input_tokens", "INT", null=False, default="0"),
        Column("output_tokens", "INT", null=False, default="0"),
        Column("seconds_generated", "REAL", null=False, default="0"),
        Column("images_generated", "INT", null=False, default="0"),
        Column("video_seconds", "REAL", null=False, default="0"),
        Column("currency", "TEXT", null=False, default="'USD'"),
        Column("cost", "REAL", null=False, default="0"),
        Column("created_at", "TEXT", null=False),
    ),
    (Index(("created_at",)), Index(("pipeline_run_id",)), Index(("provider", "created_at"))),
)

# §57 — snapshots, not last-known values, so growth can be plotted (§56).
CF_ANALYTICS_SNAPSHOTS = Table(
    "cf_analytics_snapshots",
    (
        _id(),
        _workspace(),
        Column("publication_id", "TEXT", null=False),
        Column("timestamp", "TEXT", null=False),
        Column("views", "INT", null=False, default="0"),
        Column("likes", "INT", null=False, default="0"),
        Column("comments", "INT", null=False, default="0"),
        Column("shares", "INT", null=False, default="0"),
        Column("watch_time", "REAL", null=False, default="0"),
        Column("avg_view_duration", "REAL", null=False, default="0"),
        Column("followers_delta", "INT", null=False, default="0"),
        Column("engagement_rate", "REAL", null=False, default="0"),
        Column("metadata", "JSON", null=False, default="'{}'"),
        Column("created_at", "TEXT", null=False),
    ),
    (Index(("publication_id", "timestamp")),),  # §79
)

# §98 — prompts are data, not source code, so a wording change does not need a
# redeploy and an old run can still say which text it actually sent.
CF_PROMPT_TEMPLATES = Table(
    "cf_prompt_templates",
    (
        _id(),
        _workspace(),
        Column("name", "TEXT", null=False),
        Column("description", "TEXT", null=False, default="''"),
        Column("active_version_id", "TEXT"),
        *_created(),
    ),
    (Index(("workspace_id", "name"), unique=True),),
)

CF_PROMPT_TEMPLATE_VERSIONS = Table(
    "cf_prompt_template_versions",
    (
        _id(),
        _workspace(),
        Column("template_id", "TEXT", null=False),
        Column("version", "INT", null=False),
        Column("template", "TEXT", null=False),
        Column("variables", "JSON", null=False, default="'[]'"),
        Column("output_schema", "JSON", null=False, default="'{}'"),
        Column("created_at", "TEXT", null=False),
    ),
    (Index(("template_id", "version"), unique=True),),
)

TABLES: tuple[Table, ...] = (
    CF_PROJECT_CONFIGS,
    CF_PIPELINES,
    CF_PIPELINE_VERSIONS,
    CF_PIPELINE_RUNS,
    CF_NODE_RUNS,
    CF_CONTENT_ITEMS,
    CF_STORYBOARDS,
    CF_ASSETS,
    CF_CHANNELS,
    CF_PUBLICATIONS,
    CF_SCHEDULES,
    CF_USAGE_RECORDS,
    CF_ANALYTICS_SNAPSHOTS,
    CF_PROMPT_TEMPLATES,
    CF_PROMPT_TEMPLATE_VERSIONS,
)


# ── DDL rendering & migration ────────────────────────────────────────────────

def create_table_sql(table: Table, postgres: bool) -> str:
    columns = ",\n    ".join(_column_sql(column, postgres) for column in table.columns)
    return f"CREATE TABLE IF NOT EXISTS {table.name} (\n    {columns}\n)"


def create_index_sql(table: Table, index: Index) -> str:
    unique = "UNIQUE " if index.unique else ""
    columns = ", ".join(index.columns)
    return f"CREATE {unique}INDEX IF NOT EXISTS {index.name(table.name)} ON {table.name} ({columns})"


def _existing_columns(cursor: Any, table: str, postgres: bool) -> set[str]:
    if postgres:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        )
        return {row[0] for row in cursor.fetchall()}
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


_ensured = False


def ensure_schema(force: bool = False) -> None:
    """Creates missing tables/indexes and adds columns declared since the last
    run. Idempotent and safe to call from module import; never drops or
    rewrites anything."""
    global _ensured
    if _ensured and not force:
        return

    from backend.content_factory import db

    postgres = db.is_postgres()
    with db.connect() as conn:
        cursor = conn.cursor()
        for table in TABLES:
            cursor.execute(create_table_sql(table, postgres))
            present = _existing_columns(cursor, table.name, postgres)
            for column in table.columns:
                if column.name in present:
                    continue
                # A column added to a table that already has rows can be neither
                # PRIMARY KEY nor NOT NULL on either backend, so the addition
                # keeps only the type and the literal default.
                addition = Column(column.name, column.type, default=column.default)
                cursor.execute(
                    f"ALTER TABLE {table.name} ADD COLUMN {_column_sql(addition, postgres)}"
                )
                logger.info("content_factory: added column %s.%s", table.name, column.name)
            for index in table.indexes:
                cursor.execute(create_index_sql(table, index))
        conn.commit()
    _ensured = True
