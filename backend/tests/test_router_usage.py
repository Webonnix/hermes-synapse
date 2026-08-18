import pytest

from backend import database, provider_governance, router_tiers, router_usage


@pytest.fixture()
def usage_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "usage.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(provider_governance, "DB_PATH", db_path)
    monkeypatch.setattr(router_tiers, "DB_PATH", db_path)
    monkeypatch.setattr(router_usage, "DB_PATH", db_path)
    database.init_db()
    provider_governance._init_schema()
    router_tiers._init_schema()
    router_usage._init_schema()
    return db_path


def test_log_and_quota_status_counts_only_success(usage_db):
    router_usage.log_usage("t1", "Tier1", 1, "binding", success=True)
    router_usage.log_usage("t1", "Tier1", 1, "binding", success=True)
    router_usage.log_usage("t1", "Tier1", 1, "binding", success=False, error="timeout")

    status = router_usage.tier_quota_status("t1", quota_limit=10, quota_window_hours=24)
    assert status["used"] == 2
    assert status["limit"] == 10
    assert status["pct"] == 20.0
    assert status["exhausted"] is False


def test_quota_exhausted_when_limit_reached(usage_db):
    for _ in range(3):
        router_usage.log_usage("t1", "Tier1", 1, "binding", success=True)
    status = router_usage.tier_quota_status("t1", quota_limit=3, quota_window_hours=24)
    assert status["exhausted"] is True
    assert status["pct"] == 100.0


def test_unlimited_quota_has_no_pct(usage_db):
    router_usage.log_usage("t1", "Tier1", 1, "binding", success=True)
    status = router_usage.tier_quota_status("t1", quota_limit=None, quota_window_hours=24)
    assert status["pct"] is None
    assert status["exhausted"] is False


def test_overview_stats_savings_and_requests(usage_db):
    # 1 request on the top tier (rank 2), 3 requests handled cheaper (local + rank 1)
    router_usage.log_usage(None, "local", 0, "local", success=True)
    router_usage.log_usage(None, "local", 0, "local", success=True)
    router_usage.log_usage("t1", "cheap", 1, "binding", success=True)
    router_usage.log_usage("t2", "premium", 2, "binding", success=True)

    stats = router_usage.overview_stats()
    assert stats["requests_24h"] == 4
    # 3 of 4 successful requests stayed off the top (rank 2) tier
    assert stats["token_savings_pct"] == 75.0


def test_overview_stats_empty_state(usage_db):
    stats = router_usage.overview_stats()
    assert stats["requests_24h"] == 0
    assert stats["token_savings_pct"] is None
    assert stats["active_combo_label"] == "Локальная модель"
    assert stats["active_combo_tier_count"] == 1


def test_overview_reflects_configured_tiers(usage_db, monkeypatch):
    vk_store: dict[str, str] = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: vk_store.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: vk_store.get(k))
    monkeypatch.setattr("backend.control_plane.create_review_task", lambda **kw: {"id": "task-1"})

    proposal = provider_governance.create_binding_proposal("ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x")
    with provider_governance._connect() as conn:
        conn.execute("UPDATE provider_bindings SET status='active' WHERE id=?", (proposal["id"],))
    from backend.valkey_client import set_value
    set_value(f"provider_secret:{proposal['id']}", "sk-x")

    router_tiers.create_tier(label="Подписка", tier_rank=1, kind="binding", provider_binding_id=proposal["id"])
    stats = router_usage.overview_stats()
    assert stats["active_combo_label"] == "Подписка"
    assert stats["active_combo_tier_count"] == 2
    assert stats["providers_total"] == 1
    assert stats["providers_healthy"] == 1
