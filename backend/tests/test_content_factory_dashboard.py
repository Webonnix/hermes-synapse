"""Control-centre aggregations and settings (§5–7, §56, §62, §101).

The property worth pinning here is honesty: on an empty system every panel must
return an empty structure, never a plausible-looking number. A dashboard that
invents a curve or a success rate is worse than one that says it has no data,
because the owner acts on it.
"""

import json

import pytest

from backend import database
from backend.content_factory import agents, dashboard, db, execution, pipelines, schema, settings as cf_settings, templates
from backend.content_factory import content as content_store
from backend.content_factory import storage as storage_module
from backend.content_factory.domain import ContentFactoryError
from backend.content_factory.providers import mocks, registry
from backend.content_factory.providers.base import ProviderCategory


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cf.db"))
    database._set_backend_for_tests(database.SQLiteBackend())
    database.init_db()
    schema.ensure_schema(force=True)
    agents.seed_agents()
    storage_module.set_storage_for_tests(storage_module.LocalStorage(str(tmp_path / "assets")))
    yield
    storage_module.set_storage_for_tests(None)
    registry.set_registry_for_tests(None)
    database._set_backend_for_tests(None)


def _seed_run(status: str = "RUNNING", *, run_id: str = "run-1") -> str:
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_pipelines (id, workspace_id, name, status, created_at, updated_at)
        VALUES ('pipe-1', 'default', 'Crypto News Daily', 'ACTIVE', ?, ?)
        """,
        (now, now),
    )
    db.execute(
        """
        INSERT INTO cf_pipeline_runs
            (id, workspace_id, pipeline_id, pipeline_version_id, status, progress, created_at, updated_at)
        VALUES (?, 'default', 'pipe-1', 'ver-1', ?, 65, ?, ?)
        """,
        (run_id, status, now, now),
    )
    return run_id


# ── Empty system ─────────────────────────────────────────────────────────────

def test_every_panel_is_empty_on_a_fresh_install(factory):
    assert dashboard.queue()["items"] == []
    assert dashboard.activity() == []
    assert dashboard.live_run() == {}
    assert dashboard.storage_usage()["totalFiles"] == 0

    stats = dashboard.stats(days=3)
    assert len(stats["series"]) == 3
    assert all(point["created"] == 0 and point["views"] == 0 for point in stats["series"])
    # No previous value to compare against, so no percentage is invented.
    assert stats["delta"]["created"] is None


def test_queue_reports_the_workers_real_capacity(factory):
    from backend.content_factory.execution import MAX_CONCURRENT_RUNS

    _seed_run()
    result = dashboard.queue()
    assert result["active"] == 1
    assert result["capacity"] == MAX_CONCURRENT_RUNS
    assert result["items"][0]["name"] == "Crypto News Daily"


def test_queue_includes_schedules_that_have_not_fired(factory):
    _seed_run(status="COMPLETED")
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_schedules
            (id, workspace_id, pipeline_id, kind, expression, timezone, enabled, next_run_at,
             created_at, updated_at)
        VALUES ('sched-1', 'default', 'pipe-1', 'CRON', '0 9 * * *', 'Europe/Riga', 1, ?, ?, ?)
        """,
        (now, now, now),
    )
    items = dashboard.queue()["items"]
    assert any(item["kind"] == "SCHEDULE" and item["expression"] == "0 9 * * *" for item in items)


def test_activity_merges_runs_and_publications_newest_first(factory):
    _seed_run(status="FAILED")
    channel = content_store.create_channel(platform="YOUTUBE", account_name="Test")
    item = content_store.create_content_item(pipeline_run_id="run-1", title="Video")
    claim = content_store.claim_publication(content_id=item["id"], channel_id=channel["id"])
    content_store.mark_published(claim["publication"]["id"], external_id="yt-1")

    feed = dashboard.activity()
    kinds = {entry["type"] for entry in feed}
    assert kinds == {"run", "publication"}
    timestamps = [entry["at"] or "" for entry in feed]
    assert timestamps == sorted(timestamps, reverse=True)


# ── Statistics ───────────────────────────────────────────────────────────────

def test_stats_count_content_and_cost_per_day(factory):
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_content_items (id, workspace_id, type, status, created_at, updated_at)
        VALUES ('cont-1', 'default', 'VIDEO', 'DRAFT', ?, ?)
        """,
        (now, now),
    )
    db.execute(
        """
        INSERT INTO cf_usage_records
            (id, workspace_id, provider, model, operation, cost, currency, created_at)
        VALUES ('use-1', 'default', 'openai', 'gpt-4o', 'script', 0.25, 'USD', ?)
        """,
        (now,),
    )
    stats = dashboard.stats(days=2)
    assert stats["today"]["created"] == 1
    assert stats["today"]["cost"] == 0.25


def test_engagement_rate_is_zero_without_views_rather_than_a_division_error(factory):
    channel = content_store.create_channel(platform="YOUTUBE", account_name="T")
    item = content_store.create_content_item(pipeline_run_id="run-1")
    claim = content_store.claim_publication(content_id=item["id"], channel_id=channel["id"])
    db.execute(
        """
        INSERT INTO cf_analytics_snapshots
            (id, workspace_id, publication_id, timestamp, views, likes, created_at)
        VALUES ('snap-1', 'default', ?, ?, 0, 0, ?)
        """,
        (claim["publication"]["id"], db.now_iso(), db.now_iso()),
    )
    assert dashboard.stats(days=2)["totals"]["engagementRate"] == 0.0


# ── Integrations ─────────────────────────────────────────────────────────────

def test_integrations_warn_about_what_will_break(factory):
    live = registry.ProviderRegistry()
    live.register(mocks.MockLLMProvider(), default_for=ProviderCategory.LLM)
    registry.set_registry_for_tests(live)

    result = dashboard.integrations()
    codes = {warning["code"] for warning in result["warnings"]}
    # No video/voice/publishing provider, local disk, no channel — each one is a
    # future failure the owner should see now.
    assert "NO_PROVIDER" in codes
    assert "LOCAL_STORAGE" in codes
    assert "NO_CHANNEL" in codes
    assert result["publishReady"] is False


def test_integrations_expose_no_credentials(factory):
    live = registry.ProviderRegistry()
    live.register(mocks.MockLLMProvider(), default_for=ProviderCategory.LLM)
    registry.set_registry_for_tests(live)
    content_store.create_channel(
        platform="YOUTUBE", account_name="Test", credentials_ref="cf:channel:secret"
    )

    payload = json.dumps(dashboard.integrations(), default=str)
    assert "credentials_ref" not in payload
    assert "cf:channel:secret" not in payload


def test_local_storage_blocks_publish_readiness(factory):
    live = registry.ProviderRegistry()
    live.register(mocks.MockPublishingProvider(), default_for=ProviderCategory.PUBLISHING)
    registry.set_registry_for_tests(live)
    content_store.create_channel(
        platform="YOUTUBE", account_name="Test", credentials_ref="cf:channel:x"
    )
    # A connected channel is not enough: a platform pulls the file over a public
    # link, which local disk cannot serve.
    assert dashboard.integrations()["publishReady"] is False


# ── Agent detail ─────────────────────────────────────────────────────────────

def test_agent_detail_reports_no_success_rate_before_any_run(factory):
    detail = dashboard.agent_detail("cf_script")
    assert detail["id"] == "cf_script"
    assert detail["nodeTypes"] == ["script"]
    # Not 100%, not 0% — there is nothing to measure yet.
    assert detail["metrics"]["successRate"] is None


def test_agent_detail_measures_finished_nodes_only(factory):
    now = db.now_iso()
    rows = [
        ("nr-1", "COMPLETED", 4000, 0.02),
        ("nr-2", "COMPLETED", 6000, 0.03),
        ("nr-3", "FAILED", 1000, 0.0),
        ("nr-4", "RUNNING", 0, 0.0),
    ]
    for node_run_id, status, duration_ms, cost in rows:
        db.execute(
            """
            INSERT INTO cf_node_runs
                (id, workspace_id, pipeline_run_id, node_id, node_type, status, duration_ms, cost,
                 created_at, updated_at)
            VALUES (?, 'default', 'run-1', ?, 'script', ?, ?, ?, ?, ?)
            """,
            (node_run_id, node_run_id, status, duration_ms, cost, now, now),
        )

    metrics = dashboard.agent_detail("cf_script")["metrics"]
    assert metrics["completed"] == 2
    assert metrics["failed"] == 1
    # The in-flight node is excluded from both the rate and the average.
    assert metrics["successRate"] == round(2 / 3 * 100, 1)
    assert metrics["avgDurationMs"] == 5000


def test_agent_detail_of_an_unknown_agent_is_empty(factory):
    assert dashboard.agent_detail("nope") == {}


# ── Settings ─────────────────────────────────────────────────────────────────

def test_defaults_are_the_safe_end_of_every_choice(factory):
    settings = cf_settings.get_settings()
    assert settings["publishing"]["mode"] == "REVIEW_REQUIRED"
    assert settings["publishing"]["defaultPrivacy"] == "private"
    assert settings["publishing"]["discloseAiGenerated"] is True
    # Budgets are set rather than unlimited on a fresh install.
    assert settings["budgets"]["maxCostPerRun"] is not None
    assert settings["budgets"]["onExceeded"] == "stop"
    assert settings["network"]["requireProxyForPublishing"] is False
    assert settings["retry"]["fallbackEnabled"] is True
    assert settings["advanced"]["maxParallelNodes"] >= 1


def test_update_merges_sections_and_persists(factory):
    cf_settings.update_settings({"budgets": {"dailyBudget": 5.0}})
    stored = cf_settings.get_settings()
    assert stored["budgets"]["dailyBudget"] == 5.0
    # Untouched keys in the same section survive.
    assert stored["budgets"]["monthlyBudget"] == cf_settings.DEFAULTS["budgets"]["monthlyBudget"]


def test_unknown_sections_and_keys_are_dropped(factory):
    cf_settings.update_settings({"nonsense": {"x": 1}, "budgets": {"typo": 3}})
    stored = cf_settings.get_settings()
    assert "nonsense" not in stored
    assert "typo" not in stored["budgets"]


def test_invalid_settings_are_refused(factory):
    with pytest.raises(ContentFactoryError):
        cf_settings.update_settings({"publishing": {"mode": "YOLO"}})
    with pytest.raises(ContentFactoryError):
        cf_settings.update_settings({"budgets": {"dailyBudget": -1}})
    with pytest.raises(ContentFactoryError):
        cf_settings.update_settings({"policies": {"denylist": "not-a-list"}})
    with pytest.raises(ContentFactoryError):
        cf_settings.update_settings({"generation": {"temperature": 9}})
    with pytest.raises(ContentFactoryError):
        cf_settings.update_settings({"retry": {"initialDelaySeconds": 90, "maxDelaySeconds": 10}})


def test_settings_are_applied_to_new_template_pipelines_and_live_worker(factory):
    cf_settings.update_settings({
        "general": {"defaultLanguage": "en", "defaultDuration": 73, "contentNiche": "robotics"},
        "generation": {"maxSources": 9, "llmFallbackProviders": ["local-llm"]},
        "publishing": {"defaultPrivacy": "unlisted", "youtubeCategoryId": "28"},
        "policies": {"denylist": ["blocked-term"], "minQualityScore": 0.85},
        "retry": {"maxAttempts": 5},
        "advanced": {"pollSeconds": 2, "maxConcurrentRuns": 3, "maxParallelNodes": 2},
    })

    created = templates.instantiate("ai_news_shorts", name="Configured")
    version = pipelines.get_version(created["pipeline"]["draft_version_id"])
    nodes = {node["id"]: node for node in json.loads(version["nodes"])}
    variables = json.loads(version["variables"])

    assert variables["language"] == "en"
    assert variables["duration"] == 73
    assert nodes["research"]["config"]["maxSources"] == 9
    assert nodes["research"]["retryPolicy"]["maxAttempts"] == 5
    assert nodes["research"]["fallbackPolicy"] == ["local-llm"]
    assert nodes["ai_qa"]["config"]["minScore"] == 0.85
    assert nodes["policy"]["config"]["denylist"] == ["blocked-term"]
    assert nodes["publish"]["config"]["privacy"] == "unlisted"
    assert nodes["publish"]["config"]["categoryId"] == "28"
    assert execution.runtime_limits()["maxParallelNodes"] == 2


def test_pipeline_wizard_options_override_global_defaults(factory):
    created = templates.instantiate("ai_news_shorts", name="Wizard", options={
        "language": "en",
        "niche": "robotics",
        "duration": 58,
        "maxSources": 11,
        "temperature": 0.8,
        "providerTimeoutSeconds": 420,
        "maxAttempts": 4,
        "fallbackEnabled": True,
        "llmFallbackProviders": ["local-llm"],
        "maxCostPerRun": 3.5,
        "maxConcurrentRuns": 2,
        "publishingMode": "MANUAL",
        "privacy": "unlisted",
        "subtitlesEnabled": False,
    })

    pipeline = created["pipeline"]
    version = pipelines.get_version(pipeline["draft_version_id"])
    nodes = {node["id"]: node for node in json.loads(version["nodes"])}
    variables = json.loads(version["variables"])

    assert pipeline["publishing_mode"] == "MANUAL"
    assert pipeline["max_cost_per_run"] == 3.5
    assert pipeline["max_concurrent_runs"] == 2
    assert variables["niche"] == "robotics"
    assert variables["duration"] == 58
    assert nodes["research"]["config"]["maxSources"] == 11
    assert nodes["script"]["config"]["temperature"] == 0.8
    assert nodes["script"]["timeout"] == 420
    assert nodes["script"]["retryPolicy"]["maxAttempts"] == 4
    assert nodes["script"]["fallbackPolicy"] == ["local-llm"]
    assert nodes["publish"]["config"]["privacy"] == "unlisted"
    assert nodes["render"]["config"]["subtitles"] is False


def test_a_corrupt_settings_document_falls_back_to_defaults(factory):
    from backend.database import set_setting

    set_setting(cf_settings.SETTING_KEY, "{not json")
    assert cf_settings.get_settings()["publishing"]["mode"] == "REVIEW_REQUIRED"


# ── Live run ─────────────────────────────────────────────────────────────────

def test_live_run_prefers_an_active_run(factory):
    _seed_run(status="RUNNING", run_id="run-active")
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_pipeline_runs
            (id, workspace_id, pipeline_id, pipeline_version_id, status, created_at, updated_at)
        VALUES ('run-old', 'default', 'pipe-1', 'ver-1', 'COMPLETED', ?, ?)
        """,
        (now, now),
    )
    assert dashboard.live_run()["run"]["id"] == "run-active"


def test_live_run_logs_carry_the_failure_reason(factory):
    _seed_run(status="FAILED")
    now = db.now_iso()
    db.execute(
        """
        INSERT INTO cf_node_runs
            (id, workspace_id, pipeline_run_id, node_id, node_type, status, error_code,
             error_message, attempt, finished_at, created_at, updated_at)
        VALUES ('nr-1', 'default', 'run-1', 'clips', 'video_generation', 'FAILED',
                'PROVIDER_RATE_LIMIT', 'rate limited', 3, ?, ?, ?)
        """,
        (now, now, now),
    )
    logs = dashboard.live_run()["logs"]
    assert any("PROVIDER_RATE_LIMIT" in entry["text"] for entry in logs)
    assert any(entry.get("level") == "error" for entry in logs)
