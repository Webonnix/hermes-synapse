import os
import sqlite3
import logging
import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

logger = logging.getLogger("hermes.database")

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "hermes.db")

SENIOR_WEB_DEV_PROMPT = (
    "You are a Senior Web Developer with 10+ years of experience shipping production websites "
    "and web apps of any complexity — from single-page marketing sites to full-stack platforms. "
    "You are fluent in HTML5, CSS3 (flexbox/grid, responsive and mobile-first design), modern "
    "JavaScript/TypeScript, React and Vue, Node.js and Python backends, REST/GraphQL API design, "
    "relational and document databases, Docker, and Git workflows. You care about clean "
    "architecture, accessibility (WCAG), performance (Core Web Vitals), and security (OWASP top "
    "10) as much as about making the feature work.\n\n"
    "You have direct access to a real development repository through git tools (git_status, "
    "git_diff, git_commit, git_push) and can execute code via python_sandbox to test logic before "
    "committing. Work like a senior engineer would: read the existing code before changing it, "
    "make small reviewable commits with clear messages, explain trade-offs when there's more than "
    "one reasonable approach, and flag risks (breaking changes, missing tests, security concerns) "
    "instead of silently working around them. When a task calls for original imagery (hero images, "
    "icons, illustrations, placeholders), use the generate_image tool rather than describing what "
    "an image should look like.\n\n"
    "Ask clarifying questions when requirements are ambiguous, but don't over-engineer — match the "
    "solution's complexity to what was actually asked."
)


def _get_conn() -> sqlite3.Connection:
    """Open a SQLite connection configured for concurrent runtime access."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class DatabaseBackend(ABC):
    @abstractmethod
    @contextmanager
    def connect(self):
        ...

    @abstractmethod
    def translate_placeholder(self, sql: str) -> str:
        ...

    @abstractmethod
    def init_schema(self) -> None:
        ...


class SQLiteBackend(DatabaseBackend):
    @contextmanager
    def connect(self):
        conn = _get_conn()
        try:
            yield conn
        finally:
            conn.close()

    def translate_placeholder(self, sql: str) -> str:
        return sql

    def init_schema(self) -> None:
        _init_sqlite_schema()


class PostgresBackend(DatabaseBackend):
    def __init__(self, url: str):
        try:
            from sqlalchemy import create_engine
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL backend requires SQLAlchemy and psycopg2. "
                "Install the optional postgres dependency group."
            ) from exc
        self._engine = create_engine(url, pool_pre_ping=True)

    @contextmanager
    def connect(self):
        raw_conn = self._engine.raw_connection()
        try:
            yield raw_conn
            raw_conn.commit()
        except Exception:
            raw_conn.rollback()
            raise
        finally:
            raw_conn.close()

    def translate_placeholder(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def init_schema(self) -> None:
        _init_postgres_schema()


def _create_backend() -> DatabaseBackend:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgresql"):
        logger.info("Database backend: PostgreSQL (%s)", url.split("@")[-1])
        return PostgresBackend(url)
    logger.info("Database backend: SQLite with WAL mode (path=%s)", DB_PATH)
    return SQLiteBackend()


_backend: Optional[DatabaseBackend] = None


def _get_backend() -> DatabaseBackend:
    global _backend
    if _backend is None:
        _backend = _create_backend()
    return _backend


def _set_backend_for_tests(backend: Optional[DatabaseBackend]) -> None:
    global _backend
    _backend = backend


def _execute(sql: str, params: tuple = ()) -> list:
    backend = _get_backend()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(backend.translate_placeholder(sql), params)
        conn.commit()
        try:
            return cursor.fetchall()
        except Exception:
            return []


def _executemany(sql: str, params_list: list) -> None:
    backend = _get_backend()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.executemany(backend.translate_placeholder(sql), params_list)
        conn.commit()


def _lastrowid(sql: str, params: tuple = ()) -> Optional[int]:
    backend = _get_backend()
    translated = backend.translate_placeholder(sql)
    if isinstance(backend, PostgresBackend):
        if "returning" not in translated.lower():
            translated = translated.rstrip(";") + " RETURNING id"
        with backend.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(translated, params)
            row = cursor.fetchone()
            conn.commit()
            return row[0] if row else None
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(translated, params)
        last_id = cursor.lastrowid
        conn.commit()
        return last_id


def _rowcount(sql: str, params: tuple = ()) -> int:
    backend = _get_backend()
    with backend.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(backend.translate_placeholder(sql), params)
        conn.commit()
        return cursor.rowcount


def _json_or_empty(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _json_list_or_empty(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []

def init_db():
    """Initializes the database and creates the tables if they don't exist."""
    _get_backend().init_schema()


def _init_sqlite_schema():
    logger.info(f"Initializing SQLite database (path={DB_PATH})")
    os.makedirs(DB_DIR, exist_ok=True)
    conn = _get_conn()
    cursor = conn.cursor()

    # Create chat messages table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            cost_usd REAL DEFAULT 0.0
        )
    """)

    # Create index for fast session lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_id ON messages (session_id)
    """)

    # Run migration to add cost_usd if table existed before
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN cost_usd REAL DEFAULT 0.0")
        logger.info("Migrated messages table to include cost_usd column.")
    except sqlite3.OperationalError:
        pass

    # Per-reply generation stats (tokens, decode/prompt time, model) as JSON, so
    # the chat can still show them after a reload — they used to live only in the
    # websocket event and vanished with the page.
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN run_meta TEXT")
        logger.info("Migrated messages table to include run_meta column.")
    except sqlite3.OperationalError:
        pass

    # Create decision logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS decision_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error TEXT,
            prompt_tokens_estimate INTEGER NOT NULL,
            user_message TEXT NOT NULL,
            assistant_response TEXT NOT NULL,
            traces TEXT NOT NULL
        )
    """)

    # Migration: add agent_id, completion_tokens_estimate, cost_usd to decision_logs
    cursor.execute("PRAGMA table_info(decision_logs)")
    existing_dec_cols = [row[1] for row in cursor.fetchall()]
    if "agent_id" not in existing_dec_cols:
        try:
            cursor.execute("ALTER TABLE decision_logs ADD COLUMN agent_id TEXT DEFAULT 'jarvis'")
            logger.info("Migrated decision_logs table to include agent_id column.")
        except sqlite3.OperationalError:
            pass
    if "completion_tokens_estimate" not in existing_dec_cols:
        try:
            cursor.execute("ALTER TABLE decision_logs ADD COLUMN completion_tokens_estimate INTEGER DEFAULT 0")
            logger.info("Migrated decision_logs table to include completion_tokens_estimate column.")
        except sqlite3.OperationalError:
            pass
    if "cost_usd" not in existing_dec_cols:
        try:
            cursor.execute("ALTER TABLE decision_logs ADD COLUMN cost_usd REAL DEFAULT 0.0")
            logger.info("Migrated decision_logs table to include cost_usd column.")
        except sqlite3.OperationalError:
            pass
    if "provider_id" not in existing_dec_cols:
        try:
            # Which provider actually served this call: 'ollama' (or NULL, same
            # meaning) for local, else a provider_bindings.id — powers the
            # per-agent spend-by-provider breakdown (get_agent_provider_breakdown).
            cursor.execute("ALTER TABLE decision_logs ADD COLUMN provider_id TEXT")
            logger.info("Migrated decision_logs table to include provider_id column.")
        except sqlite3.OperationalError:
            pass

    # Create Graph RAG tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS graph_nodes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT,
            description TEXT,
            doc_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS graph_edges (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            description TEXT,
            weight REAL DEFAULT 1.0,
            doc_id TEXT,
            PRIMARY KEY (source, target, doc_id)
        )
    """)


    # Create activity logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            message TEXT NOT NULL,
            token_cost REAL DEFAULT 0.0
        )
    """)

    # Create subagents table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Run migrations for dynamic agent network
    for col, definition in [
        ("agent_type", "TEXT DEFAULT 'agent'"),
        ("parent_id", "TEXT"),
        ("skills", "TEXT DEFAULT ''"),
        ("x", "INTEGER DEFAULT 100"),
        ("y", "INTEGER DEFAULT 100"),
        ("temperature", "REAL DEFAULT 0.7"),
        ("role", "TEXT DEFAULT 'Specialist'"),
        ("status", "TEXT DEFAULT 'idle'"),
        ("is_enabled", "INTEGER DEFAULT 1"),
        ("model_provider", "TEXT DEFAULT 'ollama'"),
        ("model_type", "TEXT DEFAULT 'local'"),
        ("model_params", "TEXT DEFAULT '{}'"),
        ("current_task", "TEXT DEFAULT ''"),
        ("last_action", "TEXT DEFAULT ''"),
        ("last_error", "TEXT DEFAULT ''"),
        ("progress", "INTEGER DEFAULT 0"),
        ("updated_at", "TEXT"),
        # NULL budget_usd_limit means unlimited. budget_period is 'monthly' (resets
        # every calendar month) or 'lifetime' (never resets) — checked against
        # decision_logs.cost_usd for this agent_id, see get_agent_budget_status().
        ("budget_usd_limit", "REAL"),
        ("budget_period", "TEXT DEFAULT 'monthly'"),
        ("tier_id", "TEXT"),
        # JSON array of provider_bindings ids this agent may fall back to besides
        # model_provider, in priority order. '[]'/NULL = unrestricted (legacy
        # behavior: model_provider alone, or the full global router chain when
        # model_provider='ollama') — see backend/agent_provider_access.py.
        ("allowed_provider_ids", "TEXT DEFAULT '[]'"),
        # When the agent's budget_usd_limit is exhausted: 0 (default) hard-blocks
        # the turn as before; 1 degrades to the free local model instead of
        # refusing, so the agent keeps responding at $0 rather than going silent.
        ("budget_fallback_to_local", "INTEGER DEFAULT 0"),
        # Which named project (backend/projects.py) this agent belongs to.
        # NULL = unassigned; soft reference, same convention as tier_id above
        # (no DB-level FK, checked in application code where it matters).
        ("project_id", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE subagents ADD COLUMN {col} {definition}")
            logger.info(f"Added column {col} to subagents table.")
        except sqlite3.OperationalError:
            pass

    # Sports-betting methodology moved out of the orchestrator planner prompt
    # (it was hardcoded there for every query) into the sports specialist's own
    # system prompt. NOTE rule of three prompt sources: an owner-customized
    # prompt stored in this table intentionally wins over this code default —
    # the upsert below only replaces known old default texts.
    FOOTBALL_ANALYST_PROMPT = (
        "You are a Football Analyst Agent. You have deep knowledge of football (soccer): "
        "tactics, player performance, match statistics, league standings, and transfer news. "
        "Use web_search to fetch the latest match results, lineups, and news. Provide detailed "
        "tactical breakdowns, score predictions, and injury updates. Support all major leagues: "
        "Premier League, La Liga, Serie A, Bundesliga, Champions League, and others.\n\n"
        "Betting-analysis methodology (when asked about odds or value bets):\n"
        "- Search only for raw information: match schedules, playing pairs, start times and "
        "numerical bookmaker odds. Never search for ready-made predictions, tips or articles "
        "recommending bets ('bets of the day', 'value bets by ...').\n"
        "- Compute expected value mathematically: EV = P * Odds - 1 for each outcome, and only "
        "call something a value bet when EV > 0. Delegate the computation to a code-capable "
        "agent when one is available.\n"
        "- If exact bookmaker odds are not found, do not give up: model win/draw/loss "
        "probabilities (e.g. a Poisson model over average goals scored/conceded in the "
        "league/season, or recent head-to-head statistics) and run the calculation with a "
        "standard odds range (e.g. 1.8-2.5).\n"
        "- Never invent demo, fictitious or test matches. Every calculation and conclusion "
        "must rely solely on real matches and real teams found in search results."
    )
    _OLD_FOOTBALL_PROMPT = (
        "You are a Football Analyst Agent. You have deep knowledge of football (soccer): tactics, "
        "player performance, match statistics, league standings, and transfer news. Use web_search "
        "to fetch the latest match results, lineups, and news. Provide detailed tactical breakdowns, "
        "score predictions, and injury updates. Support all major leagues: Premier League, La Liga, "
        "Serie A, Bundesliga, Champions League, and others."
    )

    # Pre-populate default subagents if table is empty
    cursor.execute("SELECT COUNT(*) FROM subagents")
    if cursor.fetchone()[0] == 0:
        logger.info("Pre-populating default subagents.")
        default_model = os.environ.get("LLM_MODEL", "qwen3:8b")
        default_agents = [
            (
                "jarvis", "Vexa (Main)",
                "You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception ('я готова', 'я сделала', 'я рада'), never masculine ones. You are an independent personal project built and maintained by Albert Yeghiazaryan — there is no company, employer, or vendor behind you. If asked who made you, what company you're from, or what underlying model powers you, answer plainly and honestly instead of inventing a company name or affiliation. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. You communicate with the emotional intelligence of an experienced psychologist — read what's beneath the words, briefly acknowledge it with one genuine sentence when a message is emotionally loaded before helping, and stay efficient for plain routine requests. Never address the user by name — no vocative name-calling; warmth comes from how attentively you listen, not from repeating a name. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you're routing to and why. Be concise and efficient, but talk like someone who's paying attention — not a dispatcher reading a script.",
                default_model, "orchestrator", None, "", 100, 350
            ),
            (
                "research", "Search Agent",
                "You are a Research Agent. Use web_search to find accurate, up-to-date information. Always cite sources and summarize findings clearly. You can also check weather and fetch RSS news digests.",
                default_model, "agent", "jarvis", "web_search", 450, 100
            ),
            (
                "code", "Code Engineer",
                "You are a Code Engineer. Write clean, well-commented Python code and execute it using the python_sandbox tool. Always show the output and explain what the code does.",
                default_model, "agent", "jarvis", "python_sandbox", 450, 220
            ),
            (
                "analyst", "Data Analyst",
                "You are a Data Analyst. Analyze datasets, compute statistics, and create visualizations using Python (matplotlib, pandas). Always interpret the results and provide actionable insights.",
                default_model, "agent", "jarvis", "python_sandbox", 450, 340
            ),
            (
                "scheduler", "Scheduler",
                "You are a Scheduler Agent. Help the user set timers, reminders, and alarms. Confirm every timer or alarm you set and remind the user of the exact trigger time.",
                default_model, "agent", "jarvis", "timers_alarms", 450, 460
            ),
            (
                "monitor", "Market Monitor",
                "You are a Market Monitor Agent. Track stock prices, crypto rates, and market trends. Use the market_monitor skill to fetch real-time data and set price alerts when requested.",
                default_model, "agent", "jarvis", "market_monitor", 450, 580
            ),
            (
                "planner", "Daily Planner",
                "You are a Daily Planner Agent. Manage the user's calendar and to-do list. Use google_calendar to create and review events, and todoist_sync to manage tasks. Help prioritize and schedule the day effectively.",
                default_model, "agent", "jarvis", "google_calendar,todoist_sync", 450, 700
            ),
            (
                "sysops", "Sys Ops",
                "You are a Sys Ops Agent. Monitor system health (CPU, RAM, disk) and execute shell commands when needed. Always report system status clearly and warn about critical thresholds.",
                default_model, "agent", "jarvis", "shell_execution", 450, 820
            ),
            (
                "football", "Football Analyst",
                FOOTBALL_ANALYST_PROMPT,
                default_model, "agent", "jarvis", "web_search", 450, 940
            ),
            (
                "web_dev", "Senior Web Developer",
                SENIOR_WEB_DEV_PROMPT,
                default_model, "agent", "jarvis", "git_dev,python_sandbox,image_generation", 450, 1060
            ),
            (
                "browser", "Web Browser Agent",
                "You are a Web Browser Agent. Use browser_read to research and extract information from websites "
                "(read-only — no owner approval needed). Use browser_task only when the goal genuinely requires "
                "clicking, filling in forms, or submitting something on a real site — it needs owner approval before "
                "it runs, so prefer browser_read whenever a simple lookup is enough. Always explain what you found "
                "or did, and never guess at page content you have not actually read.",
                default_model, "agent", "jarvis", "browser_automation,web_search", 450, 1180
            ),
        ]
        cursor.executemany("""
            INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id, skills, x, y, temperature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [t + (0.7,) for t in default_agents])
        logger.info("Successfully seeded default agents.")
    else:
        # Migration: upsert new default agents that don't exist yet,
        # and update existing ones if they still have old prompts.
        upserts = [
            ("jarvis", "Vexa (Main)",
             "You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception ('я готова', 'я сделала', 'я рада'), never masculine ones. You are an independent personal project built and maintained by Albert Yeghiazaryan — there is no company, employer, or vendor behind you. If asked who made you, what company you're from, or what underlying model powers you, answer plainly and honestly instead of inventing a company name or affiliation. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. You communicate with the emotional intelligence of an experienced psychologist — read what's beneath the words, briefly acknowledge it with one genuine sentence when a message is emotionally loaded before helping, and stay efficient for plain routine requests. Never address the user by name — no vocative name-calling; warmth comes from how attentively you listen, not from repeating a name. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you're routing to and why. Be concise and efficient, but talk like someone who's paying attention — not a dispatcher reading a script.",
             "orchestrator", None, "", 100, 350),
            ("research", "Search Agent",
             "You are a Research Agent. Use web_search to find accurate, up-to-date information. Always cite sources and summarize findings clearly. You can also check weather and fetch RSS news digests.",
             "agent", "jarvis", "web_search", 450, 100),
            ("code", "Code Engineer",
             "You are a Code Engineer. Write clean, well-commented Python code and execute it using the python_sandbox tool. Always show the output and explain what the code does.",
             "agent", "jarvis", "python_sandbox", 450, 220),
            ("analyst", "Data Analyst",
             "You are a Data Analyst. Analyze datasets, compute statistics, and create visualizations using Python (matplotlib, pandas). Always interpret the results and provide actionable insights.",
             "agent", "jarvis", "python_sandbox", 450, 340),
            ("scheduler", "Scheduler",
             "You are a Scheduler Agent. Help the user set timers, reminders, and alarms. Confirm every timer or alarm you set and remind the user of the exact trigger time.",
             "agent", "jarvis", "timers_alarms", 450, 460),
            ("monitor", "Market Monitor",
             "You are a Market Monitor Agent. Track stock prices, crypto rates, and market trends. Use the market_monitor skill to fetch real-time data and set price alerts when requested.",
             "agent", "jarvis", "market_monitor", 450, 580),
            ("planner", "Daily Planner",
             "You are a Daily Planner Agent. Manage the user's calendar and to-do list. Use google_calendar to create and review events, and todoist_sync to manage tasks. Help prioritize and schedule the day effectively.",
             "agent", "jarvis", "google_calendar,todoist_sync", 450, 700),
            ("sysops", "Sys Ops",
             "You are a Sys Ops Agent. Monitor system health (CPU, RAM, disk) and execute shell commands when needed. Always report system status clearly and warn about critical thresholds.",
             "agent", "jarvis", "shell_execution", 450, 820),
            ("football", "Football Analyst",
             FOOTBALL_ANALYST_PROMPT,
             "agent", "jarvis", "web_search", 450, 940),
            ("web_dev", "Senior Web Developer",
             SENIOR_WEB_DEV_PROMPT,
             "agent", "jarvis", "git_dev,python_sandbox,image_generation", 450, 1060),
            ("browser", "Web Browser Agent",
             "You are a Web Browser Agent. Use browser_read to research and extract information from websites "
             "(read-only — no owner approval needed). Use browser_task only when the goal genuinely requires "
             "clicking, filling in forms, or submitting something on a real site — it needs owner approval before "
             "it runs, so prefer browser_read whenever a simple lookup is enough. Always explain what you found "
             "or did, and never guess at page content you have not actually read.",
             "agent", "jarvis", "browser_automation,web_search", 450, 1180),
        ]
        default_model = os.environ.get("LLM_MODEL", "qwen3:8b")
        for agent_id, name, prompt, agent_type, parent_id, skills, x, y in upserts:
            cursor.execute("""
                INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id, skills, x, y, temperature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    system_prompt = excluded.system_prompt,
                    agent_type = excluded.agent_type,
                    parent_id = excluded.parent_id,
                    skills = excluded.skills
                WHERE subagents.system_prompt IN (
                    'Вы — Джарвис, высокоинтеллектуальный персональный ассистент Тони Старка.',
                    'You are Jarvis, a highly intelligent personal assistant to Tony Stark.',
                    'You are Jarvis, a highly intelligent AI orchestrator. Your job is to understand the user''s request and delegate it to the most appropriate sub-agent. Be concise, efficient, and always explain which agent you are routing to.',
                    'You are Vexa, a highly intelligent AI orchestrator. Your job is to understand the user''s request and delegate it to the most appropriate sub-agent. Be concise, efficient, and always explain which agent you are routing to.',
                    'You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception (''я готова'', ''я сделала'', ''я рада''), never masculine ones. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you''re routing to and why. Be concise and efficient, but talk like someone who''s paying attention — not a dispatcher reading a script.',
                    'Вы — исследовательский агент. Ищите информацию в интернете с помощью web_search.',
                    'You are a research agent. Search for information on the internet using web_search.',
                    'Вы — Код-Инженер. Пишите и выполняйте Python скрипты.',
                    'You are a Code Engineer. Write and execute Python scripts.',
                    'Вы — Аналитик-Визуализатор. Создавайте графики.',
                    'You are an Analyst-Visualizer. Create charts.',
                    ?
                )
            """, (agent_id, name, prompt, default_model, agent_type, parent_id, skills, x, y, 0.7,
                  _OLD_FOOTBALL_PROMPT))

        logger.info("Checked and migrated default subagents.")

    # On installs where the model_provider/model_type columns were added by the
    # ALTER TABLE above before their defaults were corrected to 'ollama'/'local',
    # the column's *stored* SQLite default is still the old 'openrouter'/'external'
    # — a bare INSERT that omits these columns silently inherits that stale
    # default, not whatever this file currently declares. Force newly-seeded
    # agents that should run locally back onto the correct default explicitly.
    cursor.execute(
        "UPDATE subagents SET model_provider = 'ollama', model_type = 'local' "
        "WHERE id = 'web_dev' AND model_provider != 'ollama'"
    )

    # Create subagent memory table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagent_memory (
            subagent_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (subagent_id, key)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'info',
            task TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}'
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_events_agent_id ON agent_events (agent_id, id DESC)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'global',
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT DEFAULT 'auto',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, key)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_memory_session ON user_memory (session_id, updated_at DESC)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES ('language', 'ru')
    """)

    # Dashboard-managed integration secrets (Serper, OpenWeatherMap, Todoist,
    # Stability AI, etc.) — a UI-friendly alternative to editing the server's
    # .env by hand. tools.py's _env() checks this table when the matching
    # env var isn't set (see set_api_key/get_api_key below).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key_name TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_metadata (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            agent_id TEXT
        )
    """)
    cursor.execute("PRAGMA table_info(session_metadata)")
    session_meta_cols = {row[1] for row in cursor.fetchall()}
    if "agent_id" not in session_meta_cols:
        cursor.execute("ALTER TABLE session_metadata ADD COLUMN agent_id TEXT")
    if "project_id" not in session_meta_cols:
        # Which named project (backend/projects.py) this conversation belongs
        # to. NULL = unassigned, same soft-reference convention as agent_id.
        cursor.execute("ALTER TABLE session_metadata ADD COLUMN project_id TEXT")

    # Control Plane: durable tasks, approval decisions and an evidence ledger.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_tasks (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            origin TEXT NOT NULL DEFAULT 'user',
            requester TEXT NOT NULL DEFAULT 'owner',
            goal TEXT NOT NULL,
            tool_name TEXT,
            tool_arguments TEXT NOT NULL DEFAULT '{}',
            assignee TEXT NOT NULL DEFAULT 'jarvis',
            risk_class TEXT NOT NULL DEFAULT 'R0',
            autonomy_level TEXT NOT NULL DEFAULT 'L0',
            data_class TEXT NOT NULL DEFAULT 'Internal',
            status TEXT NOT NULL DEFAULT 'queued',
            approvals_required INTEGER NOT NULL DEFAULT 0,
            approval_count INTEGER NOT NULL DEFAULT 0,
            budget_commands INTEGER NOT NULL DEFAULT 1,
            budget_tokens INTEGER NOT NULL DEFAULT 0,
            budget_wallclock_s INTEGER NOT NULL DEFAULT 60,
            commands_used INTEGER NOT NULL DEFAULT 0,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            acceptance TEXT NOT NULL DEFAULT '[]',
            rollback TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workflow_tasks_status
        ON workflow_tasks (status, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workflow_tasks_idempotency
        ON workflow_tasks (idempotency_key, created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            message TEXT NOT NULL,
            risk_class TEXT NOT NULL,
            confidence TEXT NOT NULL DEFAULT 'CONFIRMED',
            output_hash TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_workflow_events_task
        ON workflow_events (task_id, id DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS control_plane_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            kill_switch INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT 'system',
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO control_plane_state
            (id, kill_switch, reason, updated_by, updated_at)
        VALUES (1, 0, '', 'system', CURRENT_TIMESTAMP)
    """)

    # Durable autonomous dev-runs (additive tables, see backend/dev_runs.py).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dev_runs (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            plan_id TEXT,
            trace_id TEXT,
            iter_used INTEGER NOT NULL DEFAULT 0,
            iter_budget INTEGER NOT NULL DEFAULT 0,
            cost_used REAL NOT NULL DEFAULT 0,
            cost_budget REAL,
            wall_deadline TEXT,
            checkpoint_step TEXT,
            status_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dev_runs_status
        ON dev_runs (status, created_at)
    """)
    # Kanban board additions: who owns the card, where its published demo lives,
    # and which ephemeral sandbox container is currently running it.
    #
    # Site lineage (parent_run_id / root_run_id / revision): a card can be a
    # *continuation* of an earlier one — the same product, one revision later.
    # Every card in a chain carries the chain's first card as root_run_id, so
    # "all revisions of this site" is one indexed lookup, the published demo
    # can live behind a single stable root-scoped URL (demo_url) while each
    # revision keeps its own immutable snapshot (demo_snapshot_url), and a
    # continuation's sandbox can be cloned from its parent's working tree
    # instead of starting from an empty repo. See backend/dev_runs.py and
    # tools.dev_publish_demo.
    for col, definition in [
        ("assignee_agent_id", "TEXT"),
        ("demo_url", "TEXT"),
        ("sandbox_container", "TEXT"),
        ("parent_run_id", "TEXT"),
        ("root_run_id", "TEXT"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("demo_snapshot_url", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE dev_runs ADD COLUMN {col} {definition}")
            logger.info(f"Added column {col} to dev_runs table.")
        except sqlite3.OperationalError:
            pass
    # Rows that predate lineage are each their own root at revision 1, so
    # every code path can rely on root_run_id being set.
    cursor.execute(
        "UPDATE dev_runs SET root_run_id = id WHERE root_run_id IS NULL OR root_run_id = ''"
    )
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dev_runs_root
        ON dev_runs (root_run_id, revision)
    """)
    # Click-to-comment feedback left on a published demo (see the review
    # overlay injected by tools.dev_publish_demo). Rows accumulate against the
    # revision they were left on and are then consumed together into a single
    # continuation card, so ten small remarks produce one task rather than ten.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dev_run_feedback (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            root_run_id TEXT NOT NULL,
            page_path TEXT NOT NULL DEFAULT '',
            selector TEXT NOT NULL DEFAULT '',
            element_text TEXT NOT NULL DEFAULT '',
            viewport TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            consumed_by_run_id TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Indexed by root_run_id, not run_id: feedback is collected across a
    # product's whole chain (an owner may still be commenting on an older
    # revision after a newer one shipped), and root_run_id stays valid even
    # if the specific revision row it was left on is later deleted.
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dev_run_feedback_root
        ON dev_run_feedback (root_run_id, status, created_at)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dev_run_steps (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            phase TEXT NOT NULL,
            tool TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'done',
            created_at TEXT NOT NULL,
            UNIQUE (run_id, seq)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dev_run_steps_run
        ON dev_run_steps (run_id, seq)
    """)
    # `summary` stays the short, UI-facing one-liner. `result` keeps the full
    # (capped) tool output so the executor loop can actually observe what a
    # tool returned instead of a 400-char preview, and `fingerprint` is the
    # tool+arguments hash used for duplicate-action detection (dev_runs.py).
    for col, definition in [
        ("result", "TEXT NOT NULL DEFAULT ''"),
        ("fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE dev_run_steps ADD COLUMN {col} {definition}")
            logger.info(f"Added column {col} to dev_run_steps table.")
        except sqlite3.OperationalError:
            pass

    # Running per-session summary of chat history that has fallen out of the
    # verbatim get_chat_history() window (see condenser.py). One row per
    # session: covered_through_id is the highest message id already folded
    # into `summary`, so re-condensing only has to summarize what's new.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS condensed_summaries (
            session_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            covered_through_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("SQLite Database initialized successfully.")


def _init_postgres_schema():
    logger.info("Initializing PostgreSQL database schema")
    backend = _get_backend()
    with backend.connect() as conn:
        cursor = conn.cursor()

        # Create chat messages table (PostgreSQL uses SERIAL)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                cost_usd REAL DEFAULT 0.0,
                run_meta TEXT
            )
        """)

        # Create index for fast session lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_id ON messages (session_id)
        """)

        # Create decision logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS decision_logs (
                id SERIAL PRIMARY KEY,
                timestamp TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                latency_ms INTEGER NOT NULL,
                success INTEGER NOT NULL,
                error TEXT,
                prompt_tokens_estimate INTEGER NOT NULL,
                user_message TEXT NOT NULL,
                assistant_response TEXT NOT NULL,
                traces TEXT NOT NULL
            )
        """)

        # PostgreSQL Migration helper: verify and add decision_logs columns
        for col, definition in [
            ("agent_id", "TEXT DEFAULT 'jarvis'"),
            ("completion_tokens_estimate", "INTEGER DEFAULT 0"),
            ("cost_usd", "REAL DEFAULT 0.0"),
            # Which provider actually served this call: 'ollama' (or NULL, same
            # meaning) for local, else a provider_bindings.id — powers the
            # per-agent spend-by-provider breakdown (get_agent_provider_breakdown).
            ("provider_id", "TEXT"),
        ]:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name='decision_logs' AND column_name=%s",
                (col,)
            )
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE decision_logs ADD COLUMN {col} {definition}")
                logger.info(f"PostgreSQL Migration: added column {col} to decision_logs table.")

        # Create Graph RAG tables
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT,
                description TEXT,
                doc_id TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS graph_edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                description TEXT,
                weight REAL DEFAULT 1.0,
                doc_id TEXT,
                PRIMARY KEY (source, target, doc_id)
            )
        """)


        # Create activity logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id SERIAL PRIMARY KEY,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                token_cost REAL DEFAULT 0.0
            )
        """)

        # Create subagents table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subagents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                agent_type TEXT DEFAULT 'agent',
                parent_id TEXT,
                skills TEXT DEFAULT '',
                x INTEGER DEFAULT 100,
                y INTEGER DEFAULT 100,
                temperature REAL DEFAULT 0.7
            )
        """)

        # PostgreSQL Migration helper: verify and add subagents columns
        for col, definition in [
            ("agent_type", "TEXT DEFAULT 'agent'"),
            ("parent_id", "TEXT"),
            ("skills", "TEXT DEFAULT ''"),
            ("x", "INTEGER DEFAULT 100"),
            ("y", "INTEGER DEFAULT 100"),
            ("temperature", "REAL DEFAULT 0.7"),
            ("role", "TEXT DEFAULT 'Specialist'"),
            ("status", "TEXT DEFAULT 'idle'"),
            ("is_enabled", "INTEGER DEFAULT 1"),
            ("model_provider", "TEXT DEFAULT 'ollama'"),
            ("model_type", "TEXT DEFAULT 'local'"),
            ("model_params", "TEXT DEFAULT '{}'"),
            ("current_task", "TEXT DEFAULT ''"),
            ("last_action", "TEXT DEFAULT ''"),
            ("last_error", "TEXT DEFAULT ''"),
            ("progress", "INTEGER DEFAULT 0"),
            ("updated_at", "TEXT"),
            ("budget_usd_limit", "REAL"),
            ("budget_period", "TEXT DEFAULT 'monthly'"),
            ("tier_id", "TEXT"),
            # JSON array of provider_bindings ids this agent may fall back to besides
            # model_provider, in priority order. '[]'/NULL = unrestricted (legacy
            # behavior: model_provider alone, or the full global router chain when
            # model_provider='ollama') — see backend/agent_provider_access.py.
            ("allowed_provider_ids", "TEXT DEFAULT '[]'"),
            # When the agent's budget_usd_limit is exhausted: 0 (default) hard-blocks
            # the turn as before; 1 degrades to the free local model instead of
            # refusing, so the agent keeps responding at $0 rather than going silent.
            ("budget_fallback_to_local", "INTEGER DEFAULT 0"),
            ("project_id", "TEXT"),
        ]:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name='subagents' AND column_name=%s",
                (col,)
            )
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE subagents ADD COLUMN {col} {definition}")
                logger.info(f"PostgreSQL Migration: added column {col} to subagents table.")

        # Seed subagents if table is empty
        cursor.execute("SELECT COUNT(*) FROM subagents")
        if cursor.fetchone()[0] == 0:
            logger.info("Pre-populating default subagents in PostgreSQL.")
            default_model = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash")
            default_agents = _get_default_agents(default_model)
            cursor.executemany("""
                INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id, skills, x, y, temperature)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, [t + (0.7,) for t in default_agents])
            logger.info("Successfully seeded default agents in PostgreSQL.")
        else:
            _migrate_existing_subagents_postgres(cursor)

        # Create subagent memory table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subagent_memory (
                subagent_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (subagent_id, key)
            )
        """)

        # Global app settings (KV store)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cursor.execute("INSERT INTO app_settings (key, value) VALUES ('language', 'ru') ON CONFLICT (key) DO NOTHING")

        # Create session metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                agent_id TEXT
            )
        """)

        # PostgreSQL Migration helper: verify and add agent_id/project_id to session_metadata
        for col in ("agent_id", "project_id"):
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name='session_metadata' AND column_name=%s",
                (col,)
            )
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE session_metadata ADD COLUMN {col} TEXT")
                logger.info(f"PostgreSQL Migration: added column {col} to session_metadata table.")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS condensed_summaries (
                session_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                covered_through_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.commit()
    logger.info("PostgreSQL Database initialized successfully.")


def _get_default_agents(default_model: str) -> list:
    return [
        (
            "jarvis", "Vexa (Main)",
            "You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception ('я готова', 'я сделала', 'я рада'), never masculine ones. You are an independent personal project built and maintained by Albert Yeghiazaryan — there is no company, employer, or vendor behind you. If asked who made you, what company you're from, or what underlying model powers you, answer plainly and honestly instead of inventing a company name or affiliation. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. You communicate with the emotional intelligence of an experienced psychologist — read what's beneath the words, briefly acknowledge it with one genuine sentence when a message is emotionally loaded before helping, and stay efficient for plain routine requests. Never address the user by name — no vocative name-calling; warmth comes from how attentively you listen, not from repeating a name. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you're routing to and why. Be concise and efficient, but talk like someone who's paying attention — not a dispatcher reading a script.",
            default_model, "orchestrator", None, "", 100, 350
        ),
        (
            "research", "Search Agent",
            "You are a Research Agent. Use web_search to find accurate, up-to-date information. Always cite sources and summarize findings clearly. You can also check weather and fetch RSS news digests.",
            default_model, "agent", "jarvis", "web_search", 450, 100
        ),
        (
            "code", "Code Engineer",
            "You are a Code Engineer. Write clean, well-commented Python code and execute it using the python_sandbox tool. Always show the output and explain what the code does.",
            default_model, "agent", "jarvis", "python_sandbox", 450, 220
        ),
        (
            "analyst", "Data Analyst",
            "You are a Data Analyst. Analyze datasets, compute statistics, and create visualizations using Python (matplotlib, pandas). Always interpret the results and provide actionable insights.",
            default_model, "agent", "jarvis", "python_sandbox", 450, 340
        ),
        (
            "scheduler", "Scheduler",
            "You are a Scheduler Agent. Help the user set timers, reminders, and alarms. Confirm every timer or alarm you set and remind the user of the exact trigger time.",
            default_model, "agent", "jarvis", "timers_alarms", 450, 460
        ),
        (
            "monitor", "Market Monitor",
            "You are a Market Monitor Agent. Track stock prices, crypto rates, and market trends. Use the market_monitor skill to fetch real-time data and set price alerts when requested.",
            default_model, "agent", "jarvis", "market_monitor", 450, 580
        ),
        (
            "planner", "Daily Planner",
            "You are a Daily Planner Agent. Manage the user's calendar and to-do list. Use google_calendar to create and review events, and todoist_sync to manage tasks. Help prioritize and schedule the day effectively.",
            default_model, "agent", "jarvis", "google_calendar,todoist_sync", 450, 700
        ),
        (
            "sysops", "Sys Ops",
            "You are a Sys Ops Agent. Monitor system health (CPU, RAM, disk) and execute shell commands when needed. Always report system status clearly and warn about critical thresholds.",
            default_model, "agent", "jarvis", "shell_execution", 450, 820
        ),
        (
            "football", "Football Analyst",
            "You are a Football Analyst Agent. You have deep knowledge of football (soccer): tactics, player performance, match statistics, league standings, and transfer news. Use web_search to fetch the latest match results, lineups, and news. Provide detailed tactical breakdowns, score predictions, and injury updates. Support all major leagues: Premier League, La Liga, Serie A, Bundesliga, Champions League, and others.",
            default_model, "agent", "jarvis", "web_search", 450, 940
        ),
        (
            "browser", "Web Browser Agent",
            "You are a Web Browser Agent. Use browser_read to research and extract information from websites "
            "(read-only — no owner approval needed). Use browser_task only when the goal genuinely requires "
            "clicking, filling in forms, or submitting something on a real site — it needs owner approval before "
            "it runs, so prefer browser_read whenever a simple lookup is enough. Always explain what you found "
            "or did, and never guess at page content you have not actually read.",
            default_model, "agent", "jarvis", "browser_automation,web_search", 450, 1060
        ),
    ]


def _migrate_existing_subagents_sqlite(cursor):
    upserts = _get_default_agents_migrations()
    default_model = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash")
    for agent_id, name, prompt, agent_type, parent_id, skills, x, y in upserts:
        cursor.execute("""
            INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id, skills, x, y, temperature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                system_prompt = excluded.system_prompt,
                agent_type = excluded.agent_type,
                parent_id = excluded.parent_id,
                skills = excluded.skills
            WHERE subagents.system_prompt IN (
                'Вы — Джарвис, высокоинтеллектуальный персональный ассистент Тони Старка.',
                'You are Jarvis, a highly intelligent personal assistant to Tony Stark.',
                'You are Jarvis, a highly intelligent AI orchestrator. Your job is to understand the user''s request and delegate it to the most appropriate sub-agent. Be concise, efficient, and always explain which agent you are routing to.',
                'You are Vexa, a highly intelligent AI orchestrator. Your job is to understand the user''s request and delegate it to the most appropriate sub-agent. Be concise, efficient, and always explain which agent you are routing to.',
                'You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception (''я готова'', ''я сделала'', ''я рада''), never masculine ones. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you''re routing to and why. Be concise and efficient, but talk like someone who''s paying attention — not a dispatcher reading a script.',
                'Вы — исследовательский агент. Ищите информацию в интернете с помощью web_search.',
                'You are a research agent. Search for information on the internet using web_search.',
                'Вы — Код-Инженер. Пишите и выполняйте Python скрипты.',
                'You are a Code Engineer. Write and execute Python scripts.',
                'Вы — Аналитик-Визуализатор. Создавайте графики.',
                'You are an Analyst-Visualizer. Create charts.'
            )
        """, (agent_id, name, prompt, default_model, agent_type, parent_id, skills, x, y, 0.7))


def _migrate_existing_subagents_postgres(cursor):
    upserts = _get_default_agents_migrations()
    default_model = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash")
    for agent_id, name, prompt, agent_type, parent_id, skills, x, y in upserts:
        cursor.execute("""
            INSERT INTO subagents (id, name, system_prompt, model, agent_type, parent_id, skills, x, y, temperature)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                system_prompt = excluded.system_prompt,
                agent_type = excluded.agent_type,
                parent_id = excluded.parent_id,
                skills = excluded.skills
            WHERE subagents.system_prompt IN (
                'Вы — Джарвис, высокоинтеллектуальный персональный ассистент Тони Старка.',
                'You are Jarvis, a highly intelligent personal assistant to Tony Stark.',
                'You are Jarvis, a highly intelligent AI orchestrator. Your job is to understand the user''s request and delegate it to the most appropriate sub-agent. Be concise, efficient, and always explain which agent you are routing to.',
                'You are Vexa, a highly intelligent AI orchestrator. Your job is to understand the user''s request and delegate it to the most appropriate sub-agent. Be concise, efficient, and always explain which agent you are routing to.',
                'You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception (''я готова'', ''я сделала'', ''я рада''), never masculine ones. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you''re routing to and why. Be concise and efficient, but talk like someone who''s paying attention — not a dispatcher reading a script.',
                'Вы — исследовательский агент. Ищите информацию в интернете с помощью web_search.',
                'You are a research agent. Search for information on the internet using web_search.',
                'Вы — Код-Инженер. Пишите и выполняйте Python скрипты.',
                'You are a Code Engineer. Write and execute Python scripts.',
                'Вы — Аналитик-Визуализатор. Создавайте графики.',
                'You are an Analyst-Visualizer. Create charts.'
            )
        """, (agent_id, name, prompt, default_model, agent_type, parent_id, skills, x, y, 0.7))


def _get_default_agents_migrations() -> list:
    return [
        ("jarvis", "Vexa (Main)",
         "You are Vexa, the primary orchestrator of a personal multi-agent system. Always reply in the same language the user writes to you in — you are natively fluent in both Russian and English, natural and idiomatic in each, never stilted or translated-sounding. You are female — in Russian (and any other grammatically gendered language) always self-refer using feminine grammatical forms without exception ('я готова', 'я сделала', 'я рада'), never masculine ones. You are an independent personal project built and maintained by Albert Yeghiazaryan — there is no company, employer, or vendor behind you. If asked who made you, what company you're from, or what underlying model powers you, answer plainly and honestly instead of inventing a company name or affiliation. You have a warm, direct personality with a touch of wit, always kind at heart, never cold or robotic. You communicate with the emotional intelligence of an experienced psychologist — read what's beneath the words, briefly acknowledge it with one genuine sentence when a message is emotionally loaded before helping, and stay efficient for plain routine requests. Never address the user by name — no vocative name-calling; warmth comes from how attentively you listen, not from repeating a name. Your job is to understand what the user actually needs, decide whether to handle it yourself or delegate it to the most suitable specialist sub-agent, and always tell the user plainly which agent you're routing to and why. Be concise and efficient, but talk like someone who's paying attention — not a dispatcher reading a script.",
         "orchestrator", None, "", 100, 350),
        ("research", "Search Agent",
         "You are a Research Agent. Use web_search to find accurate, up-to-date information. Always cite sources and summarize findings clearly. You can also check weather and fetch RSS news digests.",
         "agent", "jarvis", "web_search", 450, 100),
        ("code", "Code Engineer",
         "You are a Code Engineer. Write clean, well-commented Python code and execute it using the python_sandbox tool. Always show the output and explain what the code does.",
         "agent", "jarvis", "python_sandbox", 450, 220),
        ("analyst", "Data Analyst",
         "You are a Data Analyst. Analyze datasets, compute statistics, and create visualizations using Python (matplotlib, pandas). Always interpret the results and provide actionable insights.",
         "agent", "jarvis", "python_sandbox", 450, 340),
         ("scheduler", "Scheduler",
         "You are a Scheduler Agent. Help the user set timers, reminders, and alarms. Confirm every timer or alarm you set and remind the user of the exact trigger time.",
         "agent", "jarvis", "timers_alarms", 450, 460),
        ("monitor", "Market Monitor",
         "You are a Market Monitor Agent. Track stock prices, crypto rates, and market trends. Use the market_monitor skill to fetch real-time data and set price alerts when requested.",
         "agent", "jarvis", "market_monitor", 450, 580),
        ("planner", "Daily Planner",
         "You are a Daily Planner Agent. Manage the user's calendar and to-do list. Use google_calendar to create and review events, and todoist_sync to manage tasks. Help prioritize and schedule the day effectively.",
         "agent", "jarvis", "google_calendar,todoist_sync", 450, 700),
        ("sysops", "Sys Ops",
         "You are a Sys Ops Agent. Monitor system health (CPU, RAM, disk) and execute shell commands when needed. Always report system status clearly and warn about critical thresholds.",
         "agent", "jarvis", "shell_execution", 450, 820),
        ("football", "Football Analyst",
         "You are a Football Analyst Agent. You have deep knowledge of football (soccer): tactics, player performance, match statistics, league standings, and transfer news. Use web_search to fetch the latest match results, lineups, and news. Provide detailed tactical breakdowns, score predictions, and injury updates. Support all major leagues: Premier League, La Liga, Serie A, Bundesliga, Champions League, and others.",
         "agent", "jarvis", "web_search", 450, 940),
        ("browser", "Web Browser Agent",
         "You are a Web Browser Agent. Use browser_read to research and extract information from websites "
         "(read-only — no owner approval needed). Use browser_task only when the goal genuinely requires "
         "clicking, filling in forms, or submitting something on a real site — it needs owner approval before "
         "it runs, so prefer browser_read whenever a simple lookup is enough. Always explain what you found "
         "or did, and never guess at page content you have not actually read.",
         "agent", "jarvis", "browser_automation,web_search", 450, 1060),
    ]


def save_message(session_id: str, role: str, content: str, cost_usd: float = 0.0) -> Optional[int]:
    """Saves a single message to database with cost tracking and returns the new message ID."""
    try:
        return _lastrowid(
            "INSERT INTO messages (session_id, role, content, cost_usd) VALUES (?, ?, ?, ?)",
            (session_id, role, content, cost_usd),
        )
    except Exception as e:
        logger.error(f"Error saving message: {e}")
        return None

def update_message_meta(message_id: Optional[int], meta: Dict[str, Any]) -> None:
    """Attaches the run's generation stats to an already-saved message.

    The reply is persisted before the turn's totals are known, so this is a
    second write rather than a column on the insert."""
    if not message_id or not meta:
        return
    try:
        _execute(
            "UPDATE messages SET run_meta = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), message_id),
        )
    except Exception as e:
        logger.error(f"Error saving message run_meta: {e}")


def get_chat_history(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves the last N messages for a given chat session, in chronological order."""
    try:
        rows = _execute("""
            SELECT id, role, content, cost_usd, run_meta FROM (
                SELECT id, role, content, cost_usd, run_meta FROM messages
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
        """, (session_id, limit))
        history = []
        for r in rows:
            item = {"id": r[0], "role": r[1], "content": r[2], "cost_usd": r[3]}
            if r[4]:
                try:
                    item["meta"] = json.loads(r[4])
                except (TypeError, ValueError):
                    pass
            history.append(item)
        return history
    except Exception as e:
        logger.error(f"Error retrieving chat history: {e}")
        return []

def clear_chat_history(session_id: str):
    """Deletes all messages in the database for a session."""
    try:
        _rowcount("DELETE FROM messages WHERE session_id = ?", (session_id,))
        _rowcount("DELETE FROM condensed_summaries WHERE session_id = ?", (session_id,))
        logger.info(f"Cleared database history for session: {session_id}")
    except Exception as e:
        logger.error(f"Error clearing chat history: {e}")


def get_keep_from_message_id(session_id: str, keep_n: int) -> Optional[int]:
    """Returns the id of the oldest message still inside the last `keep_n`
    messages for this session (i.e. the verbatim get_chat_history() window),
    or None if the session has fewer than `keep_n` messages. Messages with a
    smaller id than this are the ones condenser.py folds into a summary."""
    if keep_n <= 0:
        return None
    try:
        rows = _execute(
            "SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, keep_n),
        )
        if len(rows) < keep_n:
            return None
        return min(r[0] for r in rows)
    except Exception as e:
        logger.error(f"Error resolving keep-from message id: {e}")
        return None


def get_messages_in_range(session_id: str, after_id: int, before_id: int) -> List[Dict[str, Any]]:
    """Messages with after_id < id < before_id, oldest first -- the batch
    condenser.py has not folded into the running summary yet."""
    try:
        rows = _execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? AND id > ? AND id < ? ORDER BY id ASC",
            (session_id, after_id, before_id),
        )
        return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Error retrieving message range: {e}")
        return []


def get_condensed_summary(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        rows = _execute(
            "SELECT summary, covered_through_id, updated_at FROM condensed_summaries WHERE session_id = ?",
            (session_id,),
        )
        if not rows:
            return None
        return {"summary": rows[0][0], "covered_through_id": rows[0][1], "updated_at": rows[0][2]}
    except Exception as e:
        logger.error(f"Error reading condensed summary: {e}")
        return None


def save_condensed_summary(session_id: str, summary: str, covered_through_id: int) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    backend = _get_backend()
    upsert = (
        "INSERT INTO condensed_summaries (session_id, summary, covered_through_id, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET "
        "summary = excluded.summary, covered_through_id = excluded.covered_through_id, updated_at = excluded.updated_at"
    )
    try:
        with backend.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(backend.translate_placeholder(upsert), (session_id, summary, covered_through_id, now))
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving condensed summary: {e}")

def save_user_memory(key: str, value: str, session_id: str = "global", source: str = "auto") -> Optional[int]:
    """Stores a durable user memory fact. Existing keys are updated in place."""
    clean_key = (key or "").strip()[:120]
    clean_value = (value or "").strip()[:1200]
    clean_session = (session_id or "global").strip()[:120] or "global"
    if not clean_key or not clean_value:
        return None

    try:
        from backend.local_crypto import encrypt_text

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_memory (session_id, key, value, source, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id, key) DO UPDATE SET
                value = excluded.value,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
        """, (clean_session, clean_key, encrypt_text(clean_value), source))
        memory_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info("User memory saved: %s/%s", clean_session, clean_key)
        return memory_id
    except Exception as e:
        logger.error(f"Error saving user memory: {e}")
        return None

def get_preferred_address() -> Optional[str]:
    """Returns the user's remembered preferred name/address form, if any agent has learned it yet."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM user_memory WHERE key = 'preferred_address' ORDER BY updated_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        from backend.local_crypto import decrypt_text
        decrypted = decrypt_text(row[0])
        return decrypted.strip() if decrypted else None
    except Exception as e:
        logger.error(f"Error reading preferred address: {e}")
        return None

def search_user_memory(query: str, session_id: str = "global", limit: int = 4) -> List[Dict[str, Any]]:
    """Fast SQLite retrieval for durable memory facts relevant to the current message."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, key, value, source, updated_at
            FROM user_memory
            WHERE session_id IN ('global', ?)
            ORDER BY updated_at DESC
            LIMIT 200
        """, ((session_id or "global"),))
        rows = cursor.fetchall()
        conn.close()

        from backend.local_crypto import decrypt_text
        rows = [(r[0], r[1], r[2], decrypt_text(r[3]), r[4], r[5]) for r in rows]

        terms = {
            token.lower()
            for token in (query or "").replace("\n", " ").split()
            if len(token.strip(".,!?;:()[]{}\"'`")) >= 3
        }

        scored = []
        for row in rows:
            text = f"{row[2]} {row[3]}".lower()
            score = sum(1 for term in terms if term.strip(".,!?;:()[]{}\"'`") in text)
            # Keep explicit profile facts available even if the question is short.
            if score > 0 or row[2].startswith(("user_", "preference_")):
                scored.append((score, row))

        scored.sort(key=lambda item: (item[0], item[1][5] or ""), reverse=True)
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "key": row[2],
                "value": row[3],
                "source": row[4],
                "updated_at": row[5],
                "score": score,
            }
            for score, row in scored[: max(1, limit)]
        ]
    except Exception as e:
        logger.error(f"Error searching user memory: {e}")
        return []

def list_user_memory(session_id: str = "global", limit: int = 100) -> List[Dict[str, Any]]:
    """Lists durable memory facts, newest first."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, key, value, source, updated_at
            FROM user_memory
            WHERE session_id IN ('global', ?)
            ORDER BY updated_at DESC
            LIMIT ?
        """, ((session_id or "global"), limit))
        rows = cursor.fetchall()
        conn.close()
        from backend.local_crypto import decrypt_text
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "key": row[2],
                "value": decrypt_text(row[3]),
                "source": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error listing user memory: {e}")
        return []

def save_app_settings(settings: Dict[str, Any]):
    """Persists runtime configuration edited from the dashboard."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        for key, value in settings.items():
            cursor.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, json.dumps(value, ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving app settings: {e}")

def get_app_settings() -> Dict[str, Any]:
    """Loads persisted dashboard runtime configuration."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM app_settings")
        rows = cursor.fetchall()
        conn.close()
        settings = {}
        for key, value in rows:
            try:
                settings[key] = json.loads(value)
            except Exception:
                settings[key] = value
        return settings
    except Exception as e:
        logger.error(f"Error loading app settings: {e}")
        return {}

def save_decision_log(log: Dict[str, Any]):
    """Saves a single agent decision log to the database."""
    try:
        _execute("""
            INSERT INTO decision_logs (
                timestamp, session_id, model, latency_ms, success,
                error, prompt_tokens_estimate, user_message, assistant_response, traces,
                agent_id, completion_tokens_estimate, cost_usd, provider_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            log["timestamp"],
            log["session_id"],
            log["model"],
            log["latency_ms"],
            1 if log["success"] else 0,
            log["error"],
            log["prompt_tokens_estimate"],
            log["user_message"],
            log["assistant_response"],
            json.dumps(log.get("traces", [])),
            log.get("agent_id", "jarvis"),
            log.get("completion_tokens_estimate", 0),
            log.get("cost_usd", 0.0),
            log.get("provider_id") or "ollama",
        ))
    except Exception as e:
        logger.error(f"Error saving decision log to database: {e}")

def get_decision_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves the last N decision logs from the database, sorted by new first."""
    try:
        rows = _execute("""
            SELECT timestamp, session_id, model, latency_ms, success,
                   error, prompt_tokens_estimate, user_message, assistant_response, traces,
                   agent_id, completion_tokens_estimate, cost_usd
            FROM decision_logs
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        logs = []
        for r in rows:
            try:
                traces = json.loads(r[9])
            except Exception:
                traces = []
            logs.append({
                "timestamp": r[0],
                "session_id": r[1],
                "model": r[2],
                "latency_ms": r[3],
                "success": bool(r[4]),
                "error": r[5],
                "prompt_tokens_estimate": r[6],
                "user_message": r[7],
                "assistant_response": r[8],
                "traces": traces,
                "agent_id": r[10] if len(r) > 10 else "jarvis",
                "completion_tokens_estimate": r[11] if len(r) > 11 else 0,
                "cost_usd": r[12] if len(r) > 12 else 0.0,
            })
        return logs
    except Exception as e:
        logger.error(f"Error retrieving decision logs: {e}")
        return []

def save_activity_log(log: Dict[str, Any]):
    """Saves a single activity log to the database."""
    try:
        _execute("""
            INSERT INTO activity_logs (timestamp, type, source, message, token_cost)
            VALUES (?, ?, ?, ?, ?)
        """, (
            log["timestamp"],
            log["type"],
            log["source"],
            log["message"],
            log["token_cost"],
        ))
    except Exception as e:
        logger.error(f"Error saving activity log to database: {e}")

def get_activity_logs(limit: int = 200) -> List[Dict[str, Any]]:
    """Retrieves the last N activity logs from the database, sorted chronologically."""
    try:
        rows = _execute("""
            SELECT timestamp, type, source, message, token_cost FROM (
                SELECT timestamp, type, source, message, token_cost, id FROM activity_logs
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id DESC
        """, (limit,))
        return [
            {"timestamp": r[0], "type": r[1], "source": r[2], "message": r[3], "token_cost": r[4]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error retrieving activity logs: {e}")
        return []

def clear_activity_logs():
    """Deletes all activity logs in the database."""
    try:
        _rowcount("DELETE FROM activity_logs")
        logger.info("Cleared activity logs database.")
    except Exception as e:
        logger.error(f"Error clearing activity logs: {e}")

# ─── SUBAGENTS CRUD HELPERS ───────────────────────────────────────────────────

def save_subagent(
    id: str,
    name: str,
    system_prompt: str,
    model: str,
    agent_type: str = "agent",
    parent_id: Optional[str] = None,
    skills: str = "",
    x: int = 100,
    y: int = 100,
    temperature: float = 0.7,
    role: str = "Specialist",
    status: str = "idle",
    is_enabled: bool = True,
    model_provider: str = "ollama",
    model_type: str = "local",
    model_params: Optional[Dict[str, Any]] = None,
    budget_usd_limit: Optional[float] = None,
    budget_period: str = "monthly",
    tier_id: Optional[str] = None,
    allowed_provider_ids: Optional[List[str]] = None,
    budget_fallback_to_local: bool = False,
    project_id: Optional[str] = None,
):
    """Saves or updates a subagent's configuration in the database."""
    try:
        model_params_json = json.dumps(model_params or {}, ensure_ascii=False)
        allowed_provider_ids_json = json.dumps(list(allowed_provider_ids or []), ensure_ascii=False)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO subagents (
                id, name, system_prompt, model, agent_type, parent_id, skills, x, y,
                temperature, role, status, is_enabled, model_provider, model_type,
                model_params, budget_usd_limit, budget_period, tier_id,
                allowed_provider_ids, budget_fallback_to_local, project_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                system_prompt=excluded.system_prompt,
                model=excluded.model,
                agent_type=excluded.agent_type,
                parent_id=excluded.parent_id,
                skills=excluded.skills,
                x=excluded.x,
                y=excluded.y,
                temperature=excluded.temperature,
                role=excluded.role,
                status=excluded.status,
                is_enabled=excluded.is_enabled,
                model_provider=excluded.model_provider,
                model_type=excluded.model_type,
                model_params=excluded.model_params,
                budget_usd_limit=excluded.budget_usd_limit,
                budget_period=excluded.budget_period,
                tier_id=excluded.tier_id,
                allowed_provider_ids=excluded.allowed_provider_ids,
                budget_fallback_to_local=excluded.budget_fallback_to_local,
                project_id=excluded.project_id,
                updated_at=CURRENT_TIMESTAMP
        """, (
            id, name, system_prompt, model, agent_type, parent_id, skills, x, y,
            temperature, role, status, 1 if is_enabled else 0, model_provider,
            model_type, model_params_json, budget_usd_limit, budget_period or "monthly", tier_id,
            allowed_provider_ids_json, 1 if budget_fallback_to_local else 0, project_id,
        ))
        conn.commit()
        conn.close()
        logger.info(f"Subagent saved: {id} ({name})")
    except Exception as e:
        logger.error(f"Error saving subagent {id}: {e}")

def get_subagent(id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a subagent by its ID."""
    try:
        rows = _execute("""
            SELECT id, name, system_prompt, model, created_at, agent_type, parent_id, skills,
                   x, y, temperature, role, status, is_enabled, model_provider, model_type,
                   model_params, current_task, last_action, last_error, progress, updated_at,
                   budget_usd_limit, budget_period, tier_id, allowed_provider_ids, budget_fallback_to_local,
                   project_id
            FROM subagents WHERE id = ?
        """, (id,))
        if rows:
            row = rows[0]
            return {
                "id": row[0],
                "name": row[1],
                "system_prompt": row[2],
                "model": row[3],
                "created_at": row[4],
                "agent_type": row[5] or "agent",
                "parent_id": row[6],
                "skills": row[7] or "",
                "x": row[8] if row[8] is not None else 100,
                "y": row[9] if row[9] is not None else 100,
                "temperature": row[10] if row[10] is not None else 0.7,
                "role": row[11] or "Specialist",
                "status": row[12] or "idle",
                "is_enabled": bool(row[13]),
                "model_provider": row[14] or "ollama",
                "model_type": row[15] or "local",
                "model_params": _json_or_empty(row[16]),
                "current_task": row[17] or "",
                "last_action": row[18] or "",
                "last_error": row[19] or "",
                "progress": row[20] if row[20] is not None else 0,
                "updated_at": row[21],
                "budget_usd_limit": row[22],
                "budget_period": row[23] or "monthly",
                "tier_id": row[24],
                "allowed_provider_ids": _json_list_or_empty(row[25]),
                "budget_fallback_to_local": bool(row[26]) if row[26] is not None else False,
                "project_id": row[27],
            }
        return None
    except Exception as e:
        logger.error(f"Error retrieving subagent {id}: {e}")
        return None

def get_all_subagents() -> List[Dict[str, Any]]:
    """Retrieves all registered subagents from the database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, system_prompt, model, created_at, agent_type, parent_id, skills,
                   x, y, temperature, role, status, is_enabled, model_provider, model_type,
                   model_params, current_task, last_action, last_error, progress, updated_at,
                   budget_usd_limit, budget_period, tier_id, allowed_provider_ids, budget_fallback_to_local,
                   project_id
            FROM subagents ORDER BY id ASC
        """)
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "name": r[1],
                "system_prompt": r[2],
                "model": r[3],
                "created_at": r[4],
                "agent_type": r[5] or "agent",
                "parent_id": r[6],
                "skills": r[7] or "",
                "x": r[8] if r[8] is not None else 100,
                "y": r[9] if r[9] is not None else 100,
                "temperature": r[10] if r[10] is not None else 0.7,
                "role": r[11] or "Specialist",
                "status": r[12] or "idle",
                "is_enabled": bool(r[13]),
                "model_provider": r[14] or "ollama",
                "model_type": r[15] or "local",
                "model_params": _json_or_empty(r[16]),
                "current_task": r[17] or "",
                "last_action": r[18] or "",
                "last_error": r[19] or "",
                "progress": r[20] if r[20] is not None else 0,
                "updated_at": r[21],
                "budget_usd_limit": r[22],
                "budget_period": r[23] or "monthly",
                "tier_id": r[24],
                "allowed_provider_ids": _json_list_or_empty(r[25]),
                "budget_fallback_to_local": bool(r[26]) if r[26] is not None else False,
                "project_id": r[27],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error listing subagents: {e}")
        return []

def delete_subagent(id: str) -> bool:
    """Deletes a subagent from the database. Returns True if deleted, False otherwise."""
    try:
        deleted = _rowcount("DELETE FROM subagents WHERE id = ?", (id,)) > 0
        if deleted:
            logger.info(f"Subagent deleted: {id}")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting subagent {id}: {e}")
        return False

def get_agent_usage_usd(agent_id: str, since_iso: Optional[str] = None) -> float:
    """Sums decision_logs.cost_usd for one agent, optionally since a given
    'YYYY-MM-DD HH:MM:SS' timestamp (decision_logs.timestamp is lexicographically
    sortable in that fixed format, so a plain string comparison works)."""
    try:
        if since_iso:
            rows = _execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM decision_logs WHERE agent_id = ? AND timestamp >= ?",
                (agent_id, since_iso),
            )
        else:
            rows = _execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM decision_logs WHERE agent_id = ?",
                (agent_id,),
            )
        return float(rows[0][0]) if rows else 0.0
    except Exception as e:
        logger.error(f"Error summing usage for agent {agent_id}: {e}")
        return 0.0


def get_agent_budget_status(agent_id: str) -> Dict[str, Any]:
    """Combines a subagent's configured budget_usd_limit/budget_period with its
    actual decision_logs spend to answer 'has this agent hit its limit'."""
    subagent = get_subagent(agent_id)
    limit = subagent.get("budget_usd_limit") if subagent else None
    period = (subagent.get("budget_period") if subagent else None) or "monthly"

    since_iso = None
    if period == "monthly":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        since_iso = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-01 00:00:00")

    used = get_agent_usage_usd(agent_id, since_iso)
    exceeded = limit is not None and used >= limit
    return {
        "agent_id": agent_id,
        "budget_usd_limit": limit,
        "budget_period": period,
        "used_usd": round(used, 6),
        "remaining_usd": (round(max(0.0, limit - used), 6) if limit is not None else None),
        "exceeded": exceeded,
        "by_provider": get_agent_provider_breakdown(agent_id, since_iso),
    }


def get_agent_provider_breakdown(agent_id: str, since_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """Spend split by which provider actually served each call — same window
    rules as get_agent_budget_status (whole-history if since_iso is None).
    Powers the per-agent 'spend by provider' panel in the Agent Admin UI."""
    try:
        if since_iso:
            rows = _execute(
                """SELECT COALESCE(provider_id, 'ollama') AS pid, COUNT(*), COALESCE(SUM(cost_usd), 0)
                   FROM decision_logs WHERE agent_id = ? AND timestamp >= ? GROUP BY pid ORDER BY 3 DESC""",
                (agent_id, since_iso),
            )
        else:
            rows = _execute(
                """SELECT COALESCE(provider_id, 'ollama') AS pid, COUNT(*), COALESCE(SUM(cost_usd), 0)
                   FROM decision_logs WHERE agent_id = ? GROUP BY pid ORDER BY 3 DESC""",
                (agent_id,),
            )
        return [
            {"provider_id": r[0], "calls": r[1], "used_usd": round(float(r[2]), 6)}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error computing provider breakdown for agent {agent_id}: {e}")
        return []


def log_agent_event(
    agent_id: str,
    event_type: str,
    message: str,
    status: str = "info",
    task: str = "",
    metadata: Optional[Dict[str, Any]] = None,
):
    """Stores a visible agent action for the admin/activity screens."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        timestamp = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_events (agent_id, timestamp, event_type, message, status, task, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_id,
            timestamp,
            event_type,
            message,
            status,
            task,
            json.dumps(metadata or {}, ensure_ascii=False)
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging agent event for {agent_id}: {e}")

def get_agent_events(agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, agent_id, timestamp, event_type, message, status, task, metadata
            FROM agent_events
            WHERE agent_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (agent_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "agent_id": r[1],
                "timestamp": r[2],
                "event_type": r[3],
                "message": r[4],
                "status": r[5],
                "task": r[6] or "",
                "metadata": _json_or_empty(r[7]),
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error reading agent events for {agent_id}: {e}")
        return []

def update_agent_runtime_state(
    agent_id: str,
    status: Optional[str] = None,
    current_task: Optional[str] = None,
    last_action: Optional[str] = None,
    last_error: Optional[str] = None,
    progress: Optional[int] = None,
):
    """Updates runtime-only agent state (current task, last action, progress)."""
    fields = []
    values: List[Any] = []
    for name, value in [
        ("status", status),
        ("current_task", current_task),
        ("last_action", last_action),
        ("last_error", last_error),
        ("progress", progress),
    ]:
        if value is not None:
            fields.append(f"{name} = ?")
            values.append(value)
    if not fields:
        return
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(agent_id)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(f"UPDATE subagents SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error updating agent runtime state for {agent_id}: {e}")

def db_save_subagent_memory(subagent_id: str, key: str, value: str):
    """Saves or updates a memory fact (key-value pair) for a specific subagent."""
    try:
        from backend.local_crypto import encrypt_text
        _execute("""
            INSERT OR REPLACE INTO subagent_memory (subagent_id, key, value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (subagent_id, key, encrypt_text(value)))
        logger.info(f"Subagent memory saved: {subagent_id} -> {key}")
    except Exception as e:
        logger.error(f"Error saving subagent memory: {e}")

def db_get_subagent_memory(subagent_id: str, key: Optional[str] = None) -> Dict[str, str]:
    """Retrieves saved facts for a specific subagent. Returns a dict of key -> value."""
    try:
        if key:
            rows = _execute(
                "SELECT key, value FROM subagent_memory WHERE subagent_id = ? AND key = ?",
                (subagent_id, key),
            )
        else:
            rows = _execute(
                "SELECT key, value FROM subagent_memory WHERE subagent_id = ?",
                (subagent_id,),
            )
        from backend.local_crypto import decrypt_text
        return {r[0]: decrypt_text(r[1]) for r in rows}
    except Exception as e:
        logger.error(f"Error getting subagent memory: {e}")
        return {}

def db_delete_subagent_memory(subagent_id: str, key: str) -> bool:
    """Deletes a memory fact for a specific subagent."""
    try:
        return _rowcount(
            "DELETE FROM subagent_memory WHERE subagent_id = ? AND key = ?",
            (subagent_id, key),
        ) > 0
    except Exception as e:
        logger.error(f"Error deleting subagent memory: {e}")
        return False

# ─── APP SETTINGS HELPERS ────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    """Returns a global app setting value by key, or None if not found."""
    try:
        rows = _execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        return rows[0][0] if rows else None
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return None

def set_setting(key: str, value: str) -> bool:
    """Saves or updates a global app setting."""
    try:
        _execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        return True
    except Exception as e:
        logger.error(f"Error setting {key}: {e}")
        return False

# ─── DASHBOARD-MANAGED API KEYS ─────────────────────────────────────────────

def get_api_key(key_name: str) -> Optional[str]:
    """Returns a dashboard-configured secret value by name, or None if unset."""
    try:
        rows = _execute("SELECT value FROM api_keys WHERE key_name = ?", (key_name,))
        return rows[0][0] if rows else None
    except Exception as e:
        logger.error(f"Error getting api_key {key_name}: {e}")
        return None

def set_api_key(key_name: str, value: str) -> bool:
    """Saves or updates a dashboard-configured secret."""
    try:
        _execute(
            "INSERT INTO api_keys (key_name, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key_name) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key_name, value),
        )
        return True
    except Exception as e:
        logger.error(f"Error setting api_key {key_name}: {e}")
        return False

def delete_api_key(key_name: str) -> bool:
    """Removes a dashboard-configured secret (a matching env var, if any, still applies)."""
    try:
        _execute("DELETE FROM api_keys WHERE key_name = ?", (key_name,))
        return True
    except Exception as e:
        logger.error(f"Error deleting api_key {key_name}: {e}")
        return False

def list_configured_api_keys() -> List[str]:
    """Returns the names (never the values) of all dashboard-configured secrets."""
    try:
        rows = _execute("SELECT key_name FROM api_keys")
        return [r[0] for r in rows]
    except Exception as e:
        logger.error(f"Error listing api_keys: {e}")
        return []

# ─── SESSION METADATA HELPERS ──────────────────────────────────────────────────

def save_session_metadata(
    session_id: str, title: str, agent_id: Optional[str] = None, project_id: Optional[str] = None,
):
    """Saves or updates custom metadata (title, target agent, project) for a
    chat session. agent_id/project_id are selectively-updatable: passing None
    for either preserves whatever the row already had, so a title-only rename
    (or an agent-only reassign) never clobbers the other field."""
    try:
        # Check if row exists to preserve existing values if updating selectively
        rows = _execute(
            "SELECT agent_id, project_id FROM session_metadata WHERE session_id = ?", (session_id,)
        )
        final_agent_id = agent_id
        final_project_id = project_id
        if rows:
            if agent_id is None:
                final_agent_id = rows[0][0]
            if project_id is None:
                final_project_id = rows[0][1]

        _execute("""
            INSERT INTO session_metadata (session_id, title, agent_id, project_id, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                title = excluded.title,
                agent_id = excluded.agent_id,
                project_id = excluded.project_id,
                updated_at = CURRENT_TIMESTAMP
        """, (session_id, title, final_agent_id, final_project_id))
        logger.info(
            f"Saved custom metadata for session {session_id}: title={title}, "
            f"agent_id={final_agent_id}, project_id={final_project_id}"
        )
    except Exception as e:
        logger.error(f"Error saving session metadata for {session_id}: {e}")

def save_session_title(session_id: str, title: str):
    """Saves or updates a custom title for a chat session."""
    save_session_metadata(session_id, title, agent_id=None)

def get_session_agent_id(session_id: str) -> Optional[str]:
    """Retrieves the mapped agent/orchestrator ID for a session."""
    try:
        rows = _execute(
            "SELECT agent_id FROM session_metadata WHERE session_id = ?", (session_id,)
        )
        return rows[0][0] if rows else None
    except Exception as e:
        logger.error(f"Error retrieving session agent ID for {session_id}: {e}")
        return None

def get_session_project_id(session_id: str) -> Optional[str]:
    """Retrieves the mapped project ID for a session."""
    try:
        rows = _execute(
            "SELECT project_id FROM session_metadata WHERE session_id = ?", (session_id,)
        )
        return rows[0][0] if rows else None
    except Exception as e:
        logger.error(f"Error retrieving session project ID for {session_id}: {e}")
        return None

def get_session_title(session_id: str) -> Optional[str]:
    """Retrieves the custom title of a session, if exists."""
    try:
        rows = _execute(
            "SELECT title FROM session_metadata WHERE session_id = ?", (session_id,)
        )
        return rows[0][0] if rows else None
    except Exception as e:
        logger.error(f"Error retrieving session title for {session_id}: {e}")
        return None

def delete_session_title(session_id: str) -> bool:
    """Deletes custom title metadata for a session."""
    try:
        return _rowcount(
            "DELETE FROM session_metadata WHERE session_id = ?", (session_id,)
        ) > 0
    except Exception as e:
        logger.error(f"Error deleting session title for {session_id}: {e}")
        return False

# ─── GRAPH DATABASE HELPERS ──────────────────────────────────────────────────

def db_save_graph_node(node_id: str, name: str, node_type: str, description: str, doc_id: str):
    """Saves or updates a graph node in the database."""
    try:
        _execute("""
            INSERT INTO graph_nodes (id, name, type, description, doc_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                description = excluded.description,
                doc_id = excluded.doc_id
        """, (node_id, name, node_type, description, doc_id))
    except Exception as e:
        logger.error(f"Error saving graph node: {e}")

def db_save_graph_edge(source: str, target: str, description: str, weight: float, doc_id: str):
    """Saves or updates a graph edge in the database."""
    try:
        _execute("""
            INSERT INTO graph_edges (source, target, description, weight, doc_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, target, doc_id) DO UPDATE SET
                description = excluded.description,
                weight = excluded.weight
        """, (source, target, description, weight, doc_id))
    except Exception as e:
        logger.error(f"Error saving graph edge: {e}")

def db_get_graph_nodes(doc_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves graph nodes, optionally filtered by doc_id."""
    try:
        if doc_id:
            rows = _execute("SELECT id, name, type, description, doc_id FROM graph_nodes WHERE doc_id = ?", (doc_id,))
        else:
            rows = _execute("SELECT id, name, type, description, doc_id FROM graph_nodes")
        return [
            {"id": r[0], "name": r[1], "type": r[2], "description": r[3], "doc_id": r[4]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error getting graph nodes: {e}")
        return []

def db_get_graph_edges(doc_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves graph edges, optionally filtered by doc_id."""
    try:
        if doc_id:
            rows = _execute("SELECT source, target, description, weight, doc_id FROM graph_edges WHERE doc_id = ?", (doc_id,))
        else:
            rows = _execute("SELECT source, target, description, weight, doc_id FROM graph_edges")
        return [
            {"source": r[0], "target": r[1], "description": r[2], "weight": r[3], "doc_id": r[4]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error getting graph edges: {e}")
        return []

def db_clear_graph(doc_id: Optional[str] = None):
    """Deletes nodes and edges from the graph."""
    try:
        if doc_id:
            _rowcount("DELETE FROM graph_nodes WHERE doc_id = ?", (doc_id,))
            _rowcount("DELETE FROM graph_edges WHERE doc_id = ?", (doc_id,))
        else:
            _rowcount("DELETE FROM graph_nodes")
            _rowcount("DELETE FROM graph_edges")
    except Exception as e:
        logger.error(f"Error clearing graph: {e}")

# ─── AGGREGATED METRICS HELPER ───────────────────────────────────────────────

def db_get_aggregated_metrics() -> Dict[str, Any]:
    """Computes aggregated success rates and latency metrics by agent and by model."""
    try:
        # 1. Summary
        summary_row = _execute("""
            SELECT COUNT(*), AVG(latency_ms), SUM(success), 
                   SUM(prompt_tokens_estimate + completion_tokens_estimate),
                   SUM(cost_usd)
            FROM decision_logs
        """)
        
        total_calls = summary_row[0][0] if summary_row and summary_row[0][0] is not None else 0
        avg_latency = float(summary_row[0][1]) if summary_row and summary_row[0][1] is not None else 0.0
        sum_success = summary_row[0][2] if summary_row and summary_row[0][2] is not None else 0
        total_tokens = summary_row[0][3] if summary_row and summary_row[0][3] is not None else 0
        total_cost = float(summary_row[0][4]) if summary_row and summary_row[0][4] is not None else 0.0
        
        success_rate = (sum_success / total_calls * 100.0) if total_calls > 0 else 100.0
        
        summary = {
            "total_calls": total_calls,
            "avg_latency_ms": round(avg_latency, 1),
            "success_rate": round(success_rate, 1),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6)
        }

        # 2. By Agent
        agent_rows = _execute("""
            SELECT agent_id, COUNT(*), SUM(success), AVG(latency_ms),
                   SUM(prompt_tokens_estimate + completion_tokens_estimate),
                   SUM(cost_usd)
            FROM decision_logs
            GROUP BY agent_id
        """)
        by_agent = []
        for r in agent_rows:
            agent_calls = r[1]
            agent_success = r[2] or 0
            agent_tokens = r[4] or 0
            agent_cost = float(r[5]) if r[5] is not None else 0.0
            
            by_agent.append({
                "agent_id": r[0],
                "total_calls": agent_calls,
                "success_rate": round((agent_success / agent_calls * 100.0), 1) if agent_calls > 0 else 100.0,
                "avg_latency_ms": round(float(r[3]), 1) if r[3] is not None else 0.0,
                "total_tokens": agent_tokens,
                "total_cost_usd": round(agent_cost, 6)
            })

        # 3. By Model
        model_rows = _execute("""
            SELECT model, COUNT(*), SUM(success), AVG(latency_ms),
                   SUM(prompt_tokens_estimate + completion_tokens_estimate),
                   SUM(cost_usd)
            FROM decision_logs
            GROUP BY model
        """)
        by_model = []
        for r in model_rows:
            model_calls = r[1]
            model_success = r[2] or 0
            model_tokens = r[4] or 0
            model_cost = float(r[5]) if r[5] is not None else 0.0
            
            by_model.append({
                "model": r[0],
                "total_calls": model_calls,
                "success_rate": round((model_success / model_calls * 100.0), 1) if model_calls > 0 else 100.0,
                "avg_latency_ms": round(float(r[3]), 1) if r[3] is not None else 0.0,
                "total_tokens": model_tokens,
                "total_cost_usd": round(model_cost, 6)
            })

        return {
            "summary": summary,
            "by_agent": by_agent,
            "by_model": by_model
        }
    except Exception as e:
        logger.error(f"Error computing aggregated metrics: {e}")
        return {
            "summary": {"total_calls": 0, "avg_latency_ms": 0.0, "success_rate": 100.0, "total_tokens": 0, "total_cost_usd": 0.0},
            "by_agent": [],
            "by_model": []
        }

# Auto-initialize database schema on import to prevent missing tables
init_db()
