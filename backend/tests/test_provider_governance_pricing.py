"""Tests for provider_bindings' optional per-provider pricing override
(backend/provider_governance.py) — billing-accuracy feature alongside
per-agent provider access control."""

import pytest

from backend import database, provider_governance


@pytest.fixture()
def governance_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "governance.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(provider_governance, "DB_PATH", db_path)
    database.init_db()
    provider_governance._init_schema()

    store: dict[str, str] = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: store.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: store.get(k))
    monkeypatch.setattr("backend.control_plane.create_review_task", lambda **kw: {"id": "task-1"})
    return db_path


def test_create_binding_with_pricing_override(governance_db):
    proposal = provider_governance.create_binding_proposal(
        "ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x",
        cost_per_1m_input=0.5, cost_per_1m_output=1.0,
    )
    binding = provider_governance.get_binding(proposal["id"])
    assert binding["cost_per_1m_input"] == 0.5
    assert binding["cost_per_1m_output"] == 1.0


def test_create_binding_without_pricing_defaults_to_null(governance_db):
    proposal = provider_governance.create_binding_proposal("ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x")
    binding = provider_governance.get_binding(proposal["id"])
    assert binding["cost_per_1m_input"] is None
    assert binding["cost_per_1m_output"] is None


def test_create_binding_rejects_negative_pricing(governance_db):
    with pytest.raises(ValueError):
        provider_governance.create_binding_proposal(
            "ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x", cost_per_1m_input=-1.0,
        )


def test_update_binding_pricing(governance_db):
    proposal = provider_governance.create_binding_proposal("ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x")
    updated = provider_governance.update_binding_pricing(proposal["id"], 2.0, 4.0)
    assert updated["cost_per_1m_input"] == 2.0
    assert updated["cost_per_1m_output"] == 4.0

    cleared = provider_governance.update_binding_pricing(proposal["id"], None, None)
    assert cleared["cost_per_1m_input"] is None
    assert cleared["cost_per_1m_output"] is None


def test_update_binding_pricing_missing_binding_raises(governance_db):
    with pytest.raises(KeyError):
        provider_governance.update_binding_pricing("prov-ghost", 1.0, 2.0)


def test_update_binding_pricing_rejects_negative(governance_db):
    proposal = provider_governance.create_binding_proposal("ds", "openai_compatible", "https://api.deepseek.com/v1", "sk-x")
    with pytest.raises(ValueError):
        provider_governance.update_binding_pricing(proposal["id"], -5.0, 1.0)
