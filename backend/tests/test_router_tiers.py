import pytest

from backend import database, provider_governance, router_tiers


@pytest.fixture()
def tiers_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tiers.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(provider_governance, "DB_PATH", db_path)
    monkeypatch.setattr(router_tiers, "DB_PATH", db_path)
    database.init_db()
    provider_governance._init_schema()
    router_tiers._init_schema()

    stored_secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: stored_secrets.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: stored_secrets.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: stored_secrets.pop(key, None))
    monkeypatch.setattr("backend.control_plane.create_review_task", lambda **kw: {"id": "task-1"})
    return db_path


def _make_active_binding(name="deepseek") -> str:
    proposal = provider_governance.create_binding_proposal(name, "openai_compatible", "https://api.deepseek.com/v1", "sk-test")
    binding_id = proposal["id"]
    with provider_governance._connect() as conn:
        conn.execute("UPDATE provider_bindings SET status='active' WHERE id=?", (binding_id,))
    from backend.valkey_client import set_value
    set_value(f"provider_secret:{binding_id}", "sk-test")
    return binding_id


def test_create_local_tier(tiers_db):
    tier = router_tiers.create_tier(label="Локальная сильная модель", tier_rank=1, kind="local")
    assert tier["kind"] == "local"
    assert tier["provider_binding_id"] is None


def test_create_binding_tier_requires_valid_binding(tiers_db):
    with pytest.raises(ValueError):
        router_tiers.create_tier(label="ghost", tier_rank=1, kind="binding", provider_binding_id="no-such-id")


def test_create_and_resolve_binding_tier(tiers_db):
    binding_id = _make_active_binding()
    router_tiers.create_tier(
        label="Подписка", tier_rank=1, kind="binding", provider_binding_id=binding_id,
        model_override="deepseek-chat", quota_limit=200,
    )
    resolved = router_tiers.list_active_tiers_resolved()
    assert len(resolved) == 1
    assert resolved[0]["api_base"] == "https://api.deepseek.com/v1"
    assert resolved[0]["api_key"] == "sk-test"


def test_revoked_binding_silently_drops_from_chain(tiers_db):
    binding_id = _make_active_binding()
    router_tiers.create_tier(label="Подписка", tier_rank=1, kind="binding", provider_binding_id=binding_id)
    provider_governance.revoke_binding(binding_id)
    assert router_tiers.list_active_tiers_resolved() == []


def test_inactive_tier_excluded_from_resolution(tiers_db):
    binding_id = _make_active_binding()
    tier = router_tiers.create_tier(label="Подписка", tier_rank=1, kind="binding", provider_binding_id=binding_id)
    router_tiers.update_tier(tier["id"], is_active=False)
    assert router_tiers.list_active_tiers_resolved() == []


def test_tiers_ordered_by_rank(tiers_db):
    b1 = _make_active_binding("a")
    b2 = _make_active_binding("b")
    router_tiers.create_tier(label="cheap", tier_rank=2, kind="binding", provider_binding_id=b1)
    router_tiers.create_tier(label="expensive", tier_rank=1, kind="binding", provider_binding_id=b2)
    resolved = router_tiers.list_active_tiers_resolved()
    assert [t["label"] for t in resolved] == ["expensive", "cheap"]


def test_update_and_delete(tiers_db):
    binding_id = _make_active_binding()
    tier = router_tiers.create_tier(label="x", tier_rank=1, kind="binding", provider_binding_id=binding_id)
    updated = router_tiers.update_tier(tier["id"], label="y", quota_limit=50)
    assert updated["label"] == "y"
    assert updated["quota_limit"] == 50
    assert router_tiers.delete_tier(tier["id"]) is True
    assert router_tiers.get_tier(tier["id"]) is None


def test_rank_must_be_positive(tiers_db):
    with pytest.raises(ValueError):
        router_tiers.create_tier(label="x", tier_rank=0, kind="local")
