"""Tables, settings and the event journal of the Autonomous Earner.

Lives in hermes.db next to everything else (not Qdrant, not Valkey): approvals
and budgets need transactions, and they must survive a restart. Writes that
decide something — consuming an approval, reserving budget, claiming a stage —
run inside ``tx()`` (BEGIN IMMEDIATE), so two requests or two processes cannot
both win the same compare-and-set.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

from backend import database

# ── opportunity statuses ─────────────────────────────────────────────────────
NEW = "new"                                # found by Scout, not evaluated yet
EVALUATING = "evaluating"
PROPOSED = "proposed"                      # waiting for the generate approval
NOT_FIT = "rejected_by_evaluator"
QUEUED_BUILD = "queued_build"              # generate approval consumed
BUILDING = "building"
REVIEWING = "reviewing"
REVIEW_REJECTED = "rejected_by_reviewer"
AWAITING_EXECUTION = "awaiting_execution"  # waiting for the execute approval
QUEUED_EXECUTION = "queued_execution"      # execute approval consumed
EXECUTING = "executing"
CHECKS_PASSED = "checks_passed"
CHECKS_FAILED = "checks_failed"
FAILED = "failed"
INTERRUPTED = "interrupted"
DISMISSED = "dismissed"

# A stage that was running when the process died. They are never resumed on
# their own: half of a build may already be paid for in tokens, and repeating
# it silently is exactly the uncontrolled retry the budget exists to prevent.
IN_PROGRESS = (EVALUATING, BUILDING, REVIEWING, EXECUTING)

RUN_QUEUED, RUN_RUNNING, RUN_DONE, RUN_FAILED, RUN_INTERRUPTED = (
    "queued", "running", "done", "failed", "interrupted")

DEFAULT_LIMITS: Dict[str, int] = {
    "tasks_per_day": 5,
    "llm_calls_per_day": 20,
    "tokens_per_day": 100_000,
    "fetches_per_day": 60,
    "sandbox_runs_per_day": 10,
    "max_candidates_per_run": 10,
    "max_evaluations_per_run": 3,
    "max_fetches_per_run": 6,
    "submissions_per_day": 3,
}
# The operator may move a limit up to its cap, never past it; sandbox
# concurrency is not a setting at all — it is one.
LIMIT_CAPS: Dict[str, int] = {
    "tasks_per_day": 20,
    "llm_calls_per_day": 100,
    "tokens_per_day": 500_000,
    "fetches_per_day": 300,
    "sandbox_runs_per_day": 30,
    "max_candidates_per_run": 30,
    "max_evaluations_per_run": 10,
    "max_fetches_per_run": 20,
    "submissions_per_day": 10,
}
SANDBOX_CONCURRENCY = 1

SOURCE_KINDS = ("github_issues", "rss", "demo", "freelancer_com", "freelancehunt", "opire", "superteam")
# Host each API-backed source kind needs on the operator's allowlist.
SOURCE_HOSTS = {"github_issues": "api.github.com", "freelancer_com": "www.freelancer.com",
                "freelancehunt": "api.freelancehunt.com", "opire": "api.opire.dev",
                "superteam": "superteam.fun"}
SUPERTEAM_HOST = SOURCE_HOSTS["superteam"]
SUPERTEAM_TYPES = ("bounty", "project")

DEFAULT_EXTERNAL_MODEL = "dva/deepseek-v4-pro-high"
EXTERNAL_MONTH_USD_CAP = 200.0
# Networks the wallet watcher reads; RPC hosts and token contracts are code,
# not settings (wallet.py), so a setting can never point the watcher elsewhere.
WALLET_NETWORKS = ("base", "polygon", "ethereum")
MAX_SOURCES = 6


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime] = None) -> str:
    return (value or now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def canonical_hash(value: Any) -> str:
    """sha256 of a canonical JSON rendering — the identity an approval binds to."""
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def artifact_hash(artifact: Optional[Dict[str, Any]]) -> str:
    """The identity execute and submit approvals bind to. A standalone module
    is its files (as before); a repository patch is the pinned snapshot, the
    exact diff, the resulting files and the test plan."""
    artifact = artifact or {}
    if artifact.get("mode") == "repo":
        base = artifact.get("repo") or {}
        return canonical_hash({
            "mode": "repo",
            "repo": {key: base.get(key) for key in ("owner", "repo", "default_branch", "commit", "tarball_sha256")},
            "diff": artifact.get("diff") or "",
            "files": artifact.get("files") or [],
            "test_plan": artifact.get("test_plan") or {},
        })
    return canonical_hash(artifact.get("files") or [])


def url_hash(url: str) -> str:
    parsed = urlparse((url or "").strip())
    normalized = f"{parsed.scheme.lower()}://{(parsed.netloc or '').lower()}{parsed.path.rstrip('/')}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def connect() -> sqlite3.Connection:
    # The path is read on every connect so a test that points database.DB_PATH
    # at a temporary file never writes into the live data directory.
    connection = sqlite3.connect(database.DB_PATH, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front: the read that decides and
    the write that acts happen with nobody else in between."""
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS earner_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    directions TEXT NOT NULL DEFAULT '[]',
    constraints TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',
    allowed_hosts TEXT NOT NULL DEFAULT '[]',
    limits TEXT NOT NULL DEFAULT '{}',
    paid_search_enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS earner_runs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    sources_report TEXT NOT NULL DEFAULT '[]',
    candidates_new INTEGER NOT NULL DEFAULT 0,
    candidates_seen INTEGER NOT NULL DEFAULT 0,
    evaluated INTEGER NOT NULL DEFAULT 0,
    proposed INTEGER NOT NULL DEFAULT 0,
    fetches INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_earner_runs_status ON earner_runs (status, created_at);
CREATE TABLE IF NOT EXISTS earner_opportunities (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]',
    claimed_reward TEXT NOT NULL DEFAULT '',
    source_state TEXT NOT NULL DEFAULT '',
    source_created_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    stage_started_at TEXT,
    grant_id TEXT NOT NULL DEFAULT '',
    evaluation TEXT NOT NULL DEFAULT '{}',
    proposal TEXT NOT NULL DEFAULT '{}',
    proposal_hash TEXT NOT NULL DEFAULT '',
    artifact TEXT NOT NULL DEFAULT '{}',
    artifact_hash TEXT NOT NULL DEFAULT '',
    review TEXT NOT NULL DEFAULT '{}',
    execution TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    delivered_at TEXT,
    delivered_note TEXT NOT NULL DEFAULT '',
    paid_at TEXT,
    paid_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earner_opps_status ON earner_opportunities (status, updated_at DESC);
CREATE TABLE IF NOT EXISTS earner_approvals (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_fp TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_earner_approvals_status ON earner_approvals (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_earner_approvals_target ON earner_approvals (target_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_earner_approvals_pending
    ON earner_approvals (action, target_id) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS earner_budget_days (
    day TEXT PRIMARY KEY,
    tasks INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    tokens_reserved INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    fetches INTEGER NOT NULL DEFAULT 0,
    sandbox_runs INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS earner_llm_calls (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earner_llm_calls_day ON earner_llm_calls (day, created_at DESC);
CREATE TABLE IF NOT EXISTS earner_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earner_events_entity ON earner_events (entity_id, id DESC);
CREATE TABLE IF NOT EXISTS earner_schedule (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    approval_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    interval_hours INTEGER NOT NULL,
    valid_until TEXT NOT NULL,
    max_runs INTEGER NOT NULL,
    runs_done INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS earner_budget_months (
    month TEXT PRIMARY KEY,
    usd_reserved REAL NOT NULL DEFAULT 0,
    usd_used REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS earner_income (
    id TEXT PRIMARY KEY,
    network TEXT NOT NULL,
    token TEXT NOT NULL,
    amount TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    from_address TEXT NOT NULL,
    to_address TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    opportunity_id TEXT NOT NULL DEFAULT '',
    link_kind TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (network, tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS earner_wallet_cursor (
    network TEXT NOT NULL,
    address TEXT NOT NULL,
    last_block INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (network, address)
);
CREATE TABLE IF NOT EXISTS earner_submissions (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    response_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_earner_submissions_opp ON earner_submissions (opportunity_id, created_at DESC);
CREATE TABLE IF NOT EXISTS earner_sandbox_slot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder TEXT,
    acquired_at TEXT
);
"""


# Additive columns for databases created by an earlier version of this module.
_COLUMNS = (
    ("earner_settings", "external_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("earner_settings", "external_model", f"TEXT NOT NULL DEFAULT '{DEFAULT_EXTERNAL_MODEL}'"),
    ("earner_settings", "external_month_usd", "REAL NOT NULL DEFAULT 30"),
    ("earner_settings", "wallet", "TEXT NOT NULL DEFAULT '{}'"),
    ("earner_llm_calls", "provider", "TEXT NOT NULL DEFAULT 'local'"),
    ("earner_llm_calls", "cost_usd", "REAL NOT NULL DEFAULT 0"),
    ("earner_budget_days", "submissions", "INTEGER NOT NULL DEFAULT 0"),
    ("earner_schedule", "auto_generate", "INTEGER NOT NULL DEFAULT 0"),
    ("earner_schedule", "auto_execute", "INTEGER NOT NULL DEFAULT 0"),
    ("earner_submissions", "title", "TEXT NOT NULL DEFAULT ''"),
    ("earner_submissions", "payload", "TEXT NOT NULL DEFAULT '{}'"),
)


def init_schema() -> None:
    with read() as connection:
        connection.executescript(SCHEMA)
        for table, column, ddl in _COLUMNS:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass  # already there
        connection.execute(
            "INSERT OR IGNORE INTO earner_settings (id, limits, updated_at) VALUES (1, ?, ?)",
            (json.dumps(DEFAULT_LIMITS), iso()),
        )
        connection.execute("INSERT OR IGNORE INTO earner_sandbox_slot (id, holder) VALUES (1, NULL)")


# ── rows ─────────────────────────────────────────────────────────────────────

_JSON_FIELDS = {
    "directions", "sources", "allowed_hosts", "limits", "plan", "sources_report", "labels",
    "evaluation", "proposal", "artifact", "review", "execution", "content", "summary", "data", "wallet",
    "payload",
}


def row_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result: Dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in _JSON_FIELDS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        result[key] = value
    return result


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ── settings ─────────────────────────────────────────────────────────────────

def get_settings(connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    if connection is None:
        with read() as own:
            return get_settings(own)
    row = row_dict(connection.execute("SELECT * FROM earner_settings WHERE id = 1").fetchone())
    if row is None:
        raise RuntimeError("earner schema is not initialised")
    limits = dict(DEFAULT_LIMITS)
    limits.update({k: int(v) for k, v in (row.get("limits") or {}).items() if k in DEFAULT_LIMITS})
    row["limits"] = limits
    row["enabled"] = bool(row["enabled"])
    row["external_enabled"] = bool(row.get("external_enabled"))
    row["external_month_usd"] = float(row.get("external_month_usd") or 0)
    wallet = row.get("wallet") or {}
    row["wallet"] = {"address": wallet.get("address", ""), "networks": wallet.get("networks") or []}
    row["paid_search_enabled"] = bool(row["paid_search_enabled"])
    return row


_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,120}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_host(raw: str) -> str:
    host = str(raw or "").strip().lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"недопустимое имя хоста: {raw!r}") from exc
    if not _HOST.match(host):
        # Rejects IP literals, ports, wildcards and paths: the allowlist is a
        # list of exact public DNS names, nothing that could widen it.
        raise ValueError(f"нужно точное DNS-имя без порта и масок: {raw!r}")
    return host


def validate_sources(sources: Any, allowed_hosts: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(sources, list):
        raise ValueError("sources должен быть списком")
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"не больше {MAX_SOURCES} источников")
    result: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in sources:
        if not isinstance(raw, dict):
            raise ValueError("источник должен быть объектом")
        source_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        if not _SOURCE_ID.match(source_id) or source_id in seen:
            raise ValueError(f"недопустимый или повторный id источника: {source_id!r}")
        if kind not in SOURCE_KINDS:
            raise ValueError(f"неизвестный тип источника: {kind!r}")
        seen.add(source_id)
        source: Dict[str, Any] = {"id": source_id, "kind": kind, "enabled": bool(raw.get("enabled", True))}
        needed_host = SOURCE_HOSTS.get(kind)
        if needed_host and needed_host not in allowed_hosts:
            raise ValueError(f"для источника {source_id} добавьте {needed_host} в разрешённые хосты")
        if kind in ("github_issues", "freelancer_com"):
            query = " ".join(str(raw.get("query") or "").split())
            if not query or len(query) > 200:
                raise ValueError(f"запрос для {source_id}: от 1 до 200 символов")
            source["query"] = query
        elif kind == "freelancehunt":
            try:
                skills = sorted({int(item) for item in (raw.get("skill_ids") or [22])})
            except (TypeError, ValueError) as exc:
                raise ValueError("skill_ids Freelancehunt — числа") from exc
            if not skills or len(skills) > 5 or any(item <= 0 or item > 100000 for item in skills):
                raise ValueError("skill_ids Freelancehunt: от 1 до 5 положительных id")
            source["skill_ids"] = skills
        elif kind == "rss":
            url = str(raw.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
                raise ValueError("лента: только https:// без порта и учётных данных")
            host = normalize_host(parsed.hostname or "")
            if host not in allowed_hosts:
                raise ValueError(f"хост ленты {host} не в списке разрешённых")
            source["url"] = url[:500]
        elif kind == "superteam":
            types = [t for t in (raw.get("types") or ["bounty"]) if t in SUPERTEAM_TYPES]
            source["types"] = sorted(set(types)) or ["bounty"]
        result.append(source)
    return result


def update_settings(fields: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Every change bumps ``revision``. Approvals and schedules that were bound
    to the previous sources or limits stop matching their content hash and are
    invalidated here, in the same transaction."""
    from backend.earner import approvals

    with tx() as connection:
        current = get_settings(connection)
        allowed = current["allowed_hosts"]
        if "allowed_hosts" in fields:
            hosts = fields["allowed_hosts"]
            if not isinstance(hosts, list) or len(hosts) > 10:
                raise ValueError("allowed_hosts: список не длиннее 10")
            allowed = sorted({normalize_host(host) for host in hosts})
        sources = validate_sources(fields["sources"], allowed) if "sources" in fields else current["sources"]
        if "sources" not in fields:
            validate_sources(sources, allowed)  # a removed host must not leave a source behind
        directions = current["directions"]
        if "directions" in fields:
            raw = fields["directions"]
            if not isinstance(raw, list) or len(raw) > 10:
                raise ValueError("directions: список не длиннее 10")
            directions = [" ".join(str(item).split())[:120] for item in raw if str(item).strip()]
        constraints = str(fields.get("constraints", current["constraints"]) or "")[:2000]
        limits = dict(current["limits"])
        for key, value in (fields.get("limits") or {}).items():
            if key not in DEFAULT_LIMITS:
                raise ValueError(f"неизвестный лимит: {key}")
            number = int(value)
            if number < 0 or number > LIMIT_CAPS[key]:
                raise ValueError(f"{key}: от 0 до {LIMIT_CAPS[key]}")
            limits[key] = number
        if fields.get("paid_search_enabled"):
            # No paid search integration exists in this version, so there is
            # nothing to budget for; refusing beats a switch that does nothing.
            raise ValueError("платные поисковые сервисы в этой версии не подключены")
        enabled = bool(fields["enabled"]) if "enabled" in fields else current["enabled"]
        external_enabled = bool(fields.get("external_enabled", current["external_enabled"]))
        external_model = str(fields.get("external_model", current["external_model"]) or "").strip()
        if external_model and not _MODEL_ID.match(external_model):
            raise ValueError("недопустимый id внешней модели")
        if external_enabled and not external_model:
            raise ValueError("выберите внешнюю модель")
        month_usd = float(fields.get("external_month_usd", current["external_month_usd"]) or 0)
        if not 0 <= month_usd <= EXTERNAL_MONTH_USD_CAP:
            raise ValueError(f"лимит внешних моделей: от 0 до {EXTERNAL_MONTH_USD_CAP:.0f} $ в месяц")
        wallet = current["wallet"]
        if "wallet" in fields:
            raw_wallet = fields["wallet"] or {}
            address = str(raw_wallet.get("address") or "").strip()
            if address and not _EVM_ADDRESS.match(address):
                raise ValueError("адрес кошелька: 0x и 40 шестнадцатеричных символов (только публичный адрес)")
            networks = [n for n in (raw_wallet.get("networks") or []) if n in WALLET_NETWORKS]
            wallet = {"address": address.lower(), "networks": sorted(set(networks))}
        connection.execute(
            "UPDATE earner_settings SET enabled = ?, directions = ?, constraints = ?, sources = ?, "
            "allowed_hosts = ?, limits = ?, external_enabled = ?, external_model = ?, external_month_usd = ?, "
            "wallet = ?, revision = revision + 1, updated_at = ?, updated_by = ? WHERE id = 1",
            (int(enabled), dumps(directions), constraints, dumps(sources), dumps(allowed), dumps(limits),
             int(external_enabled), external_model, month_usd, dumps(wallet), iso(), actor),
        )
        changed_scope = any(key in fields for key in ("allowed_hosts", "sources", "directions", "constraints", "limits",
                                                      "external_enabled", "external_model", "external_month_usd"))
        if changed_scope:
            approvals.invalidate_settings_bound(connection, "настройки Earner изменены")
            if any(key in fields for key in ("external_enabled", "external_model", "external_month_usd")):
                # Pending generate approvals named the old model/budget on their screen.
                approvals.invalidate_action(connection, "generate", "настройки внешней модели изменены")
        log_event(connection, "settings", "settings", "settings_updated",
                  "Настройки изменены" if enabled else "Earner выключен оператором",
                  {"fields": sorted(fields.keys())}, actor)
        return get_settings(connection)


# ── events ───────────────────────────────────────────────────────────────────

def log_event(connection: Optional[sqlite3.Connection], entity_type: str, entity_id: str, kind: str,
              message: str, data: Optional[Dict[str, Any]] = None, actor: str = "") -> None:
    params = (entity_type, entity_id, kind, message[:1000], dumps(data or {}), actor, iso())
    sql = ("INSERT INTO earner_events (entity_type, entity_id, kind, message, data, actor, created_at) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")
    if connection is not None:
        connection.execute(sql, params)
        return
    with read() as own:
        own.execute(sql, params)


def list_events(entity_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    with read() as connection:
        if entity_id:
            rows = connection.execute(
                "SELECT * FROM earner_events WHERE entity_id = ? ORDER BY id DESC LIMIT ?", (entity_id, limit))
        else:
            rows = connection.execute("SELECT * FROM earner_events ORDER BY id DESC LIMIT ?", (limit,))
        return [row_dict(row) for row in rows]


# ── runs and opportunities ───────────────────────────────────────────────────

def get_run(run_id: str, connection: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    if connection is None:
        with read() as own:
            return get_run(run_id, own)
    return row_dict(connection.execute("SELECT * FROM earner_runs WHERE id = ?", (run_id,)).fetchone())


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    with read() as connection:
        rows = connection.execute("SELECT * FROM earner_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = row_dict(row)
            item.pop("plan", None)
            result.append(item)
        return result


def get_opportunity(opp_id: str, connection: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    if connection is None:
        with read() as own:
            return get_opportunity(opp_id, own)
    return row_dict(connection.execute("SELECT * FROM earner_opportunities WHERE id = ?", (opp_id,)).fetchone())


_LIST_COLUMNS = (
    "id, run_id, source_id, source_kind, url, title, claimed_reward, source_state, fetched_at, last_seen_at, "
    "is_demo, stale, status, error, reject_reason, delivered_at, paid_at, created_at, updated_at, proposal"
)


def list_opportunities(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    with read() as connection:
        if status:
            rows = connection.execute(
                f"SELECT {_LIST_COLUMNS} FROM earner_opportunities WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit))
        else:
            rows = connection.execute(
                f"SELECT {_LIST_COLUMNS} FROM earner_opportunities ORDER BY updated_at DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = row_dict(row)
            proposal = item.pop("proposal", None) or {}
            item["effort_hours"] = proposal.get("effort_hours")
            item["uncertainty"] = proposal.get("uncertainty")
            result.append(item)
        return result


def set_opportunity(connection: sqlite3.Connection, opp_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = []
    values: List[Any] = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(dumps(value) if key in _JSON_FIELDS else value)
    assignments.append("updated_at = ?")
    values.append(iso())
    values.append(opp_id)
    connection.execute(f"UPDATE earner_opportunities SET {', '.join(assignments)} WHERE id = ?", values)


def transition(connection: sqlite3.Connection, opp_id: str, expected: str, new_status: str, **fields: Any) -> bool:
    """Compare-and-set on the status: False means somebody else moved it first."""
    assignments = ["status = ?", "updated_at = ?"]
    values: List[Any] = [new_status, iso()]
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(dumps(value) if key in _JSON_FIELDS else value)
    values.extend([opp_id, expected])
    cursor = connection.execute(
        f"UPDATE earner_opportunities SET {', '.join(assignments)} WHERE id = ? AND status = ?", values)
    return cursor.rowcount == 1


def reconcile_interrupted() -> Dict[str, int]:
    """Startup: whatever was mid-stage belonged to a dead process. Mark it,
    record why, and leave the next step to the operator."""
    counts = {"opportunities": 0, "runs": 0}
    with tx() as connection:
        rows = connection.execute(
            f"SELECT id, status FROM earner_opportunities WHERE status IN ({','.join('?' * len(IN_PROGRESS))})",
            IN_PROGRESS).fetchall()
        for row in rows:
            set_opportunity(connection, row["id"], status=INTERRUPTED,
                            error=f"Процесс перезапущен во время стадии «{row['status']}». "
                                  "Автоматический повтор отключён: нужно новое подтверждение.")
            log_event(connection, "opportunity", row["id"], "interrupted",
                      f"Стадия {row['status']} прервана перезапуском", {"stage": row["status"]})
            counts["opportunities"] += 1
        runs = connection.execute("SELECT id FROM earner_runs WHERE status = ?", (RUN_RUNNING,)).fetchall()
        for row in runs:
            connection.execute(
                "UPDATE earner_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (RUN_INTERRUPTED, "Процесс перезапущен во время исследования; повтор не выполняется.", iso(), row["id"]))
            log_event(connection, "run", row["id"], "interrupted", "Исследование прервано перезапуском")
            counts["runs"] += 1
        connection.execute("UPDATE earner_sandbox_slot SET holder = NULL, acquired_at = NULL WHERE id = 1")
    return counts
