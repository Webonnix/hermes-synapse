"""The per-turn model override in backend/agent.py.

The agent is a process-wide singleton: one WebSocket connection picking a
OmniRoute model for a message must not move the model out from under the Telegram
bot, a scheduler run, or another chat that is mid-turn. These tests pin that
the override is visible everywhere inside the turn that asked for it, and
nowhere else — including in a concurrently running turn.
"""

import asyncio
from unittest.mock import patch

import pytest

from backend.agent import VexaAgent
from backend import database


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    original_db_path, original_db_dir = database.DB_PATH, database.DB_DIR
    database.DB_PATH = str(tmp_path / "test_override.db")
    database.DB_DIR = str(tmp_path)
    database.init_db()
    yield
    database.DB_PATH, database.DB_DIR = original_db_path, original_db_dir


@pytest.fixture
def agent():
    inst = VexaAgent()
    inst.provider = "llamacpp"
    inst.api_key = ""
    inst.api_base = "http://hermes-llama:8080"
    inst.model = "qwen-mtp:latest"
    return inst


ROUTER_OVERRIDE = {
    "source": "router",
    "model": "kimi/kimi-k3",
    "api_base": "http://omniroute:20128/v1",
    "api_key": "rtr_key",
    "provider": "openai_compatible",
}


def test_reads_the_configured_model_with_no_override(agent):
    assert agent.model == "qwen-mtp:latest"
    assert agent.provider == "llamacpp"
    assert agent.api_base == "http://hermes-llama:8080"


@pytest.mark.asyncio
async def test_override_is_visible_to_the_whole_turn(agent):
    seen = {}

    async def fake_respond(self, user_message, session_id=None, stream_callback=None, images=None):
        # Everything the turn's call tree reads goes through these four.
        seen.update(model=agent.model, provider=agent.provider,
                    api_base=agent.api_base, api_key=agent.api_key)
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("привет", session_id="s1", llm_override=ROUTER_OVERRIDE)

    assert seen == {
        "model": "kimi/kimi-k3",
        "provider": "openai_compatible",
        "api_base": "http://omniroute:20128/v1",
        "api_key": "rtr_key",
    }


@pytest.mark.asyncio
async def test_override_is_gone_once_the_turn_ends(agent):
    async def fake_respond(*args, **kwargs):
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("привет", session_id="s1", llm_override=ROUTER_OVERRIDE)

    assert agent.model == "qwen-mtp:latest"
    assert agent.provider == "llamacpp"
    assert agent.api_base == "http://hermes-llama:8080"


@pytest.mark.asyncio
async def test_override_is_gone_even_when_the_turn_raises(agent):
    async def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    with patch.object(VexaAgent, "_respond", boom):
        with pytest.raises(RuntimeError):
            await agent.respond("привет", session_id="s1", llm_override=ROUTER_OVERRIDE)

    assert agent.model == "qwen-mtp:latest"


@pytest.mark.asyncio
async def test_two_concurrent_turns_keep_their_own_model(agent):
    """The reason this is a ContextVar and not an attribute swap."""
    observed = {}

    async def fake_respond(self, user_message, session_id=None, stream_callback=None, images=None):
        # Yield between the read and the record, so an attribute-based
        # implementation would have had the other turn's value by now.
        await asyncio.sleep(0)
        model = agent.model
        await asyncio.sleep(0)
        observed[session_id] = (model, agent.model)
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond):
        await asyncio.gather(
            agent.respond("a", session_id="router-chat", llm_override=ROUTER_OVERRIDE),
            agent.respond("b", session_id="local-chat"),
        )

    assert observed["router-chat"] == ("kimi/kimi-k3", "kimi/kimi-k3")
    assert observed["local-chat"] == ("qwen-mtp:latest", "qwen-mtp:latest")


@pytest.mark.asyncio
async def test_override_reaches_a_worker_thread(agent):
    """Tools are dispatched with asyncio.to_thread, which copies the context."""
    seen = {}

    async def fake_respond(*args, **kwargs):
        seen["model"] = await asyncio.to_thread(lambda: agent.model)
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("привет", session_id="s1", llm_override=ROUTER_OVERRIDE)

    assert seen["model"] == "kimi/kimi-k3"


@pytest.mark.asyncio
async def test_an_empty_api_key_override_is_honoured(agent):
    """A local model legitimately has no key; the override must not fall
    through to the configured cloud key just because the value is falsy."""
    agent.api_key = "openrouter-key"
    seen = {}

    async def fake_respond(*args, **kwargs):
        seen["api_key"] = agent.api_key
        return "ok"

    override = {"model": "llama3:70b", "api_base": "http://hermes-llama:8080",
                "api_key": "", "provider": "llamacpp"}
    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("привет", session_id="s1", llm_override=override)

    assert seen["api_key"] == ""
    assert agent.api_key == "openrouter-key"


def test_runtime_config_still_reports_the_configured_model(agent):
    # Outside a turn there is no override, so the dashboard's config view is
    # unaffected by whatever any chat happens to be pinned to.
    assert agent.get_runtime_config()["model"] == "qwen-mtp:latest"
    assert agent.get_runtime_config()["provider"] == "llamacpp"


# ─── Endpoint and model must travel together ────────────────────────────────
# The bug these pin: the override supplied api_base while the agent's DB row
# still supplied `model`, so a turn picked for a OmniRoute model posted the local
# tag `unsloth-mtp-q4km` to http://omniroute:20128/v1 and got back
# HTTP 404 "No active credentials for provider: openai".


def test_resolve_agent_llm_config_prefers_the_picked_model_over_the_row(agent):
    from backend.agent import _LLM_OVERRIDE, _resolve_agent_llm_config

    row = {"id": "vexa", "model": "unsloth-mtp-q4km", "model_provider": "local"}
    token = _LLM_OVERRIDE.set(ROUTER_OVERRIDE)
    try:
        api_base, api_key, model, provider, is_external, provider_id = _resolve_agent_llm_config(
            row, agent.api_base, agent.api_key, agent.model, agent.provider,
        )
    finally:
        _LLM_OVERRIDE.reset(token)

    assert (api_base, model) == ("http://omniroute:20128/v1", "kimi/kimi-k3")
    assert (api_key, provider) == ("rtr_key", "openai_compatible")
    # A router turn is billable and must be gated/costed like one.
    assert is_external is True and provider_id is None


def test_resolve_agent_llm_config_untouched_without_an_override(agent):
    from backend.agent import _resolve_agent_llm_config

    row = {"id": "vexa", "model": "unsloth-mtp-q4km", "model_provider": "local"}
    api_base, _key, model, provider, is_external, _pid = _resolve_agent_llm_config(
        row, agent.api_base, agent.api_key, agent.model, agent.provider,
    )

    assert (api_base, model, provider, is_external) == (
        "http://hermes-llama:8080", "unsloth-mtp-q4km", "llamacpp", False,
    )


@pytest.mark.asyncio
async def test_delegation_does_not_inherit_the_chats_model(agent):
    """tools.call_subagent re-enters respond() for another agent from inside the
    caller's context. That agent has its own model and its own endpoint, so the
    chat's pick must not follow it in."""
    seen = {}

    async def fake_respond(self, user_message, session_id=None, stream_callback=None, images=None):
        if session_id == "outer":
            seen["outer"] = (agent.model, agent.api_base)
            await agent.respond("delegated", session_id="inner")
            seen["outer_after"] = (agent.model, agent.api_base)
        else:
            seen["inner"] = (agent.model, agent.api_base)
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("привет", session_id="outer", llm_override=ROUTER_OVERRIDE)

    assert seen["outer"] == ("kimi/kimi-k3", "http://omniroute:20128/v1")
    assert seen["inner"] == ("qwen-mtp:latest", "http://hermes-llama:8080")
    assert seen["outer_after"] == ("kimi/kimi-k3", "http://omniroute:20128/v1")


@pytest.mark.asyncio
async def test_no_llm_override_also_drops_the_orchestration_endpoint(agent):
    from backend.agent import no_llm_override
    from backend.subagents import _active_llm_runtime, use_llm_runtime

    async def fake_respond(*args, **kwargs):
        use_llm_runtime("http://omniroute:20128/v1", {"provider": "openai_compatible"})
        assert _active_llm_runtime()[0] == "http://omniroute:20128/v1"
        with no_llm_override():
            # A delegated sub-agent's helper calls must not land on the
            # endpoint the outer chat happens to be pinned to.
            assert agent.model == "qwen-mtp:latest"
            assert _active_llm_runtime()[0] != "http://omniroute:20128/v1"
        assert _active_llm_runtime()[0] == "http://omniroute:20128/v1"
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("привет", session_id="s1", llm_override=ROUTER_OVERRIDE)


def test_configured_accessors_ignore_the_override(agent):
    from backend.agent import _LLM_OVERRIDE

    token = _LLM_OVERRIDE.set(ROUTER_OVERRIDE)
    try:
        # redaction.py's local confirmation pass reads these: it posts to the
        # local llama.cpp base URL, where a OmniRoute model id is a 404.
        assert agent.configured_model == "qwen-mtp:latest"
        assert agent.configured_api_base == "http://hermes-llama:8080"
        assert agent.configured_provider == "llamacpp"
        assert agent.configured_api_key == ""
    finally:
        _LLM_OVERRIDE.reset(token)


@pytest.mark.asyncio
async def test_per_turn_endpoint_does_not_leak_between_turns(agent):
    """use_llm_runtime is what _respond publishes for the orchestration
    helpers; the Telegram bot shares this process and must not be moved."""
    from backend.subagents import _active_llm_runtime, use_llm_runtime

    observed = {}

    async def one():
        use_llm_runtime("http://omniroute:20128/v1", {"provider": "openai_compatible"})
        await asyncio.sleep(0)
        observed["router-turn"] = _active_llm_runtime()[0]

    async def two():
        await asyncio.sleep(0)
        observed["other-turn"] = _active_llm_runtime()[0]

    await asyncio.gather(one(), two())

    assert observed["router-turn"] == "http://omniroute:20128/v1"
    assert observed["other-turn"] != "http://omniroute:20128/v1"
