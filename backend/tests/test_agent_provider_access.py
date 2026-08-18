import pytest

from backend import agent_provider_access, agent_tiers, database, provider_governance


@pytest.fixture()
def access_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "access.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(provider_governance, "DB_PATH", db_path)
    monkeypatch.setattr(agent_tiers, "DB_PATH", db_path)
    database.init_db()
    provider_governance._init_schema()
    agent_tiers._init_schema()

    store: dict[str, str] = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: store.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: store.get(k))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda k: store.pop(k, None))
    monkeypatch.setattr("backend.control_plane.create_review_task", lambda **kw: {"id": "task-1"})
    return db_path


def _active_binding(name: str) -> str:
    proposal = provider_governance.create_binding_proposal(name, "openai_compatible", f"https://{name}.example/v1", f"sk-{name}")
    with provider_governance._connect() as conn:
        conn.execute("UPDATE provider_bindings SET status='active' WHERE id=?", (proposal["id"],))
    from backend.valkey_client import set_value
    set_value(f"provider_secret:{proposal['id']}", f"sk-{name}")
    return proposal["id"]


# ── database.py persistence ─────────────────────────────────────────────────

def test_allowed_provider_ids_round_trip(access_db):
    database.save_subagent(
        "researcher", "Researcher", "sp", "qwen3:8b",
        allowed_provider_ids=["prov-a", "prov-b"], budget_fallback_to_local=True,
    )
    row = database.get_subagent("researcher")
    assert row["allowed_provider_ids"] == ["prov-a", "prov-b"]
    assert row["budget_fallback_to_local"] is True

    listed = database.get_all_subagents()
    # init_db() seeds its own default agents alongside "researcher", so find
    # it by id rather than assuming it's first in id-sorted order.
    researcher = next(a for a in listed if a["id"] == "researcher")
    assert researcher["allowed_provider_ids"] == ["prov-a", "prov-b"]


def test_allowed_provider_ids_default_empty(access_db):
    database.save_subagent("plain", "Plain", "sp", "qwen3:8b")
    row = database.get_subagent("plain")
    assert row["allowed_provider_ids"] == []
    assert row["budget_fallback_to_local"] is False


# ── agent_provider_access.py resolution ─────────────────────────────────────

def test_resolve_candidates_primary_first_then_allowlist(access_db):
    row = {"model_provider": "prov-a", "allowed_provider_ids": ["prov-b", "prov-a", "prov-c"]}
    # prov-a (primary) must not be duplicated even though it also appears in the allowlist.
    assert agent_provider_access.resolve_provider_candidates(row) == ["prov-a", "prov-b", "prov-c"]


def test_resolve_candidates_ollama_primary_uses_only_allowlist(access_db):
    row = {"model_provider": "ollama", "allowed_provider_ids": ["prov-b"]}
    assert agent_provider_access.resolve_provider_candidates(row) == ["prov-b"]


def test_resolve_best_provider_skips_unresolvable_to_next_candidate(access_db):
    good = _active_binding("good")
    row = {"model_provider": "prov-ghost", "allowed_provider_ids": [good]}
    provider_id, creds = agent_provider_access.resolve_best_provider(row)
    assert provider_id == good
    assert creds == ("https://good.example/v1", "sk-good")


def test_resolve_best_provider_none_when_nothing_resolves(access_db):
    row = {"model_provider": "prov-ghost", "allowed_provider_ids": ["prov-also-ghost"]}
    assert agent_provider_access.resolve_best_provider(row) == (None, None)


def test_resolve_best_provider_local_default_returns_none(access_db):
    assert agent_provider_access.resolve_best_provider({"model_provider": "ollama"}) == (None, None)


# ── Tier ceiling ─────────────────────────────────────────────────────────────

def test_tier_forbidding_external_blocks_all_candidates(access_db):
    good = _active_binding("good")
    tier = agent_tiers.create_tier("Locked down", allow_external_provider=False)
    row = {"model_provider": good, "allowed_provider_ids": [], "tier_id": tier["id"]}
    assert agent_provider_access.resolve_provider_candidates(row) == []
    assert agent_provider_access.resolve_best_provider(row) == (None, None)
    assert agent_provider_access.allowed_binding_ids_for_chain(row) == set()


def test_tier_permitting_external_is_unaffected(access_db):
    good = _active_binding("good")
    tier = agent_tiers.create_tier("Open", allow_external_provider=True)
    row = {"model_provider": good, "allowed_provider_ids": [], "tier_id": tier["id"]}
    assert agent_provider_access.resolve_provider_candidates(row) == [good]


def test_no_tier_permits_external_by_default(access_db):
    good = _active_binding("good")
    row = {"model_provider": good, "allowed_provider_ids": []}
    assert agent_provider_access.resolve_provider_candidates(row) == [good]


# ── Chain-narrowing helper ───────────────────────────────────────────────────

def test_allowed_binding_ids_for_chain_none_when_unrestricted(access_db):
    row = {"model_provider": "ollama", "allowed_provider_ids": []}
    assert agent_provider_access.allowed_binding_ids_for_chain(row) is None


def test_allowed_binding_ids_for_chain_returns_set(access_db):
    row = {"model_provider": "ollama", "allowed_provider_ids": ["prov-a", "prov-b"]}
    assert agent_provider_access.allowed_binding_ids_for_chain(row) == {"prov-a", "prov-b"}
