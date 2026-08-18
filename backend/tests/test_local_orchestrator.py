import pytest

from backend import database, local_orchestrator, provider_governance, router_tiers, router_usage
from backend.llm_client import STATUS_PROVIDER_ERROR, STATUS_SUCCESS, LLMUsage, NormalizedLLMResponse


@pytest.fixture()
def orchestrator_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "orch.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(provider_governance, "DB_PATH", db_path)
    monkeypatch.setattr(router_tiers, "DB_PATH", db_path)
    monkeypatch.setattr(router_usage, "DB_PATH", db_path)
    database.init_db()
    provider_governance._init_schema()
    router_tiers._init_schema()
    router_usage._init_schema()

    store: dict[str, str] = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: store.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: store.get(k))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda k: store.pop(k, None))
    monkeypatch.setattr("backend.control_plane.create_review_task", lambda **kw: {"id": "task-1"})
    monkeypatch.setenv("ROUTER_DELEGATION_MODE", "always_delegate")
    return db_path


def _binding_tier(label: str, rank: int, api_base: str, quota_limit=None):
    proposal = provider_governance.create_binding_proposal(label, "openai_compatible", api_base, f"sk-{label}")
    with provider_governance._connect() as conn:
        conn.execute("UPDATE provider_bindings SET status='active' WHERE id=?", (proposal["id"],))
    from backend.valkey_client import set_value
    set_value(f"provider_secret:{proposal['id']}", f"sk-{label}")
    return router_tiers.create_tier(
        label=label, tier_rank=rank, kind="binding", provider_binding_id=proposal["id"],
        model_override=f"{label}-model", quota_limit=quota_limit,
    )


def _ok(api_base: str) -> NormalizedLLMResponse:
    return NormalizedLLMResponse(status=STATUS_SUCCESS, provider="openai_compatible", model="m", content=f"ok from {api_base}", usage=LLMUsage(input_tokens=10, output_tokens=5))


def _fail(api_base: str) -> NormalizedLLMResponse:
    return NormalizedLLMResponse(status=STATUS_PROVIDER_ERROR, provider="openai_compatible", model="m", error_message="boom")


@pytest.mark.asyncio
async def test_no_tiers_configured_passthrough_to_local(orchestrator_db, monkeypatch):
    calls = []

    async def fake_call_llm_normalized(*, api_base, **kw):
        calls.append(api_base)
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "hi"}],
    )
    assert calls == ["http://ollama:11434"]
    assert result.content == "ok from http://ollama:11434"
    assert route_state["tier"]["rank"] == 0


@pytest.mark.asyncio
async def test_delegates_to_first_tier_on_success(orchestrator_db, monkeypatch):
    _binding_tier("premium", 1, "https://premium.example/v1")
    calls = []

    async def fake_call_llm_normalized(*, api_base, **kw):
        calls.append(api_base)
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    assert calls == ["https://premium.example/v1"]
    assert route_state["tier"]["label"] == "premium"
    assert result.content == "ok from https://premium.example/v1"


@pytest.mark.asyncio
async def test_escalates_past_failing_tier(orchestrator_db, monkeypatch):
    _binding_tier("flaky", 1, "https://flaky.example/v1")
    _binding_tier("backup", 2, "https://backup.example/v1")

    async def fake_call_llm_normalized(*, api_base, **kw):
        if "flaky" in api_base:
            return _fail(api_base)
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    assert route_state["tier"]["label"] == "backup"
    assert result.content == "ok from https://backup.example/v1"


@pytest.mark.asyncio
async def test_falls_back_to_local_when_every_tier_fails(orchestrator_db, monkeypatch):
    _binding_tier("a", 1, "https://a.example/v1")
    _binding_tier("b", 2, "https://b.example/v1")

    async def fake_call_llm_normalized(*, api_base, **kw):
        if api_base == "http://ollama:11434":
            return _ok(api_base)
        return _fail(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    assert route_state["tier"]["rank"] == 0
    assert result.status == STATUS_SUCCESS
    assert result.content == "ok from http://ollama:11434"


@pytest.mark.asyncio
async def test_exhausted_quota_tier_is_skipped(orchestrator_db, monkeypatch):
    _binding_tier("limited", 1, "https://limited.example/v1", quota_limit=1)
    _binding_tier("open", 2, "https://open.example/v1")

    async def fake_call_llm_normalized(*, api_base, **kw):
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    # Use up the "limited" tier's quota first.
    router_usage.log_usage(router_tiers.list_tiers()[0]["id"], "limited", 1, "binding", success=True)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    assert route_state["tier"]["label"] == "open"


@pytest.mark.asyncio
async def test_route_state_pins_tier_across_calls_in_one_turn(orchestrator_db, monkeypatch):
    _binding_tier("premium", 1, "https://premium.example/v1")
    calls = []

    async def fake_call_llm_normalized(*, api_base, **kw):
        calls.append(api_base)
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    # A second call in the same turn (route_state already decided) must not
    # re-run classification — it goes straight back to the pinned tier.
    await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "продолжай"}],
    )
    assert calls == ["https://premium.example/v1", "https://premium.example/v1"]


@pytest.mark.asyncio
async def test_pinned_tier_degrading_mid_turn_escalates_onward(orchestrator_db, monkeypatch):
    _binding_tier("premium", 1, "https://premium.example/v1")
    _binding_tier("backup", 2, "https://backup.example/v1")
    call_count = {"premium": 0}

    async def fake_call_llm_normalized(*, api_base, **kw):
        if "premium" in api_base:
            call_count["premium"] += 1
            return _ok(api_base) if call_count["premium"] == 1 else _fail(api_base)
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    first = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    assert route_state["tier"]["label"] == "premium"

    second = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "продолжай"}],
    )
    assert route_state["tier"]["label"] == "backup"
    assert second.content == "ok from https://backup.example/v1"


@pytest.mark.asyncio
async def test_allowed_binding_ids_skips_non_allowed_tier(orchestrator_db, monkeypatch):
    """A per-agent allowlist (backend/agent_provider_access.py) narrows the
    global chain: tier 'a' is rank 1 (would normally win) but isn't in the
    agent's allowed set, so the chain must skip straight to 'b'."""
    tier_a = _binding_tier("a", 1, "https://a.example/v1")
    tier_b = _binding_tier("b", 2, "https://b.example/v1")

    async def fake_call_llm_normalized(*, api_base, **kw):
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
        allowed_binding_ids={tier_b["provider_binding_id"]},
    )
    assert route_state["tier"]["label"] == "b"
    assert result.content == "ok from https://b.example/v1"


@pytest.mark.asyncio
async def test_allowed_binding_ids_empty_set_forces_local(orchestrator_db, monkeypatch):
    """An empty allowed set (agent's tier forbids external providers outright,
    see agent_provider_access.allowed_binding_ids_for_chain) must never
    escalate to any binding tier, even though the classifier says to delegate."""
    _binding_tier("premium", 1, "https://premium.example/v1")
    calls = []

    async def fake_call_llm_normalized(*, api_base, **kw):
        calls.append(api_base)
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
        allowed_binding_ids=set(),
    )
    assert calls == ["http://ollama:11434"]
    assert route_state["tier"]["rank"] == 0
    assert result.content == "ok from http://ollama:11434"


@pytest.mark.asyncio
async def test_allowed_binding_ids_none_is_unrestricted(orchestrator_db, monkeypatch):
    """The default (no allowlist passed) must behave exactly like before this
    parameter existed — full chain, highest rank wins."""
    _binding_tier("premium", 1, "https://premium.example/v1")

    async def fake_call_llm_normalized(*, api_base, **kw):
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    assert route_state["tier"]["label"] == "premium"
    assert result.content == "ok from https://premium.example/v1"


@pytest.mark.asyncio
async def test_router_usage_log_records_cost_for_binding_tier(orchestrator_db, monkeypatch):
    """Regression test for the pre-existing gap where router_usage_log.cost_usd
    was always NULL for binding tiers even though the column existed —
    _log() must now actually price a successful binding call, and keep local
    calls at exactly $0."""
    _binding_tier("premium", 1, "https://premium.example/v1")

    async def fake_call_llm_normalized(*, api_base, **kw):
        return NormalizedLLMResponse(
            status=STATUS_SUCCESS, provider="openai_compatible", model="gpt-4o",
            content="ok", usage=LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "напиши код"}],
    )
    with router_usage._connect() as conn:
        rows = conn.execute("SELECT kind, cost_usd FROM router_usage_log ORDER BY id").fetchall()
    kinds = {r["kind"]: r["cost_usd"] for r in rows}
    assert kinds["binding"] == pytest.approx(2.50 + 10.00)  # gpt-4o rates, 1M in + 1M out


@pytest.mark.asyncio
async def test_classifier_says_no_stays_local(orchestrator_db, monkeypatch):
    monkeypatch.delenv("ROUTER_DELEGATION_MODE", raising=False)
    _binding_tier("premium", 1, "https://premium.example/v1")
    calls = []

    async def fake_call_llm_normalized(*, api_base, messages, **kw):
        calls.append(api_base)
        if api_base == "http://ollama:11434" and any("routing gate" in (m.get("content") or "") for m in messages):
            return NormalizedLLMResponse(status=STATUS_SUCCESS, provider="ollama", model="m", content="no")
        return _ok(api_base)

    monkeypatch.setattr("backend.llm_client.call_llm_normalized", fake_call_llm_normalized)

    route_state = {}
    result = await local_orchestrator.route_llm_call(
        route_state=route_state, local_api_base="http://ollama:11434", local_api_key="", local_model="qwen3:8b",
        local_provider="ollama", messages=[{"role": "user", "content": "привет"}],
    )
    assert route_state["tier"]["rank"] == 0
    assert result.content == "ok from http://ollama:11434"
