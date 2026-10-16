"""Generation stats for turns that bypass the single-agent tool loop.

Vexa in the simple chat is an orchestrator-type agent, so its replies go through
run_orchestration, which never measured tokens or decode time. From 09-09 on
every such reply was saved with run_meta = NULL and the chat had no speed
read-out to show. These tests pin that the numbers now come from the one call
site every path shares, and land on the saved message.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend import database, llm_client
from backend.agent import MAIN_AGENT_ID, VexaAgent
from backend.llm_client import LLMUsage, NormalizedLLMResponse, track_turn_usage


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    original_db_path, original_db_dir = database.DB_PATH, database.DB_DIR
    database.DB_PATH = str(tmp_path / "test_turn_usage.db")
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
    inst.model = "qwen3.8-27b"
    return inst


def _reply(input_tokens, output_tokens, eval_ms, prompt_ms, model="qwen3.8-27b"):
    return NormalizedLLMResponse(
        status="success",
        provider="llamacpp",
        model=model,
        content="ok",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        eval_ms=eval_ms,
        prompt_ms=prompt_ms,
    )


# A planner call and a synth call, the shape of an orchestrated turn.
PLANNER = _reply(1000, 10, 100, 500)
SYNTH = _reply(1200, 200, 2000, 300)


async def _call(**extra):
    return await llm_client.call_llm_normalized(api_base="http://x", api_key="", model="m", messages=[], **extra)


@pytest.mark.asyncio
async def test_tracker_sums_every_call_but_not_the_refused_param_retry():
    inner = AsyncMock(side_effect=[PLANNER, SYNTH, _reply(9, 9, 9, 9)])
    with patch.object(llm_client, "_call_llm_normalized", inner):
        with track_turn_usage() as usage:
            await _call()
            await _call()
            # The retry re-enters the public function; its outer call reports it.
            await _call(_drop_params={"temperature"})

    assert usage.calls == 2
    meta = usage.as_meta()
    meta.pop("generation_ms", None)  # wall time of a mock; zero is legitimately dropped
    assert meta == {
        "input_tokens": 2200,
        "output_tokens": 210,
        "decode_ms": 2100,
        "prompt_ms": 800,
        "model": "qwen3.8-27b",
    }


@pytest.mark.asyncio
async def test_orchestrator_agent_reply_is_saved_with_stats(agent):
    async def fake_orchestration(query, api_key, model, chat_id=None):
        await _call()
        await _call()
        return {"response": "План из семи шагов.", "traces": [], "steps": []}

    # A stale previous turn must not leak its numbers into this reply.
    agent.last_run_metadata[MAIN_AGENT_ID] = {"output_tokens": 999, "decode_ms": 1}

    with patch("backend.orchestrator.run_orchestration", fake_orchestration), \
         patch.object(llm_client, "_call_llm_normalized", AsyncMock(side_effect=[PLANNER, SYNTH])):
        text = await agent.respond("Составь бизнес-план", session_id=MAIN_AGENT_ID)

    assert text == "План из семи шагов."
    meta = agent.last_run_metadata[MAIN_AGENT_ID]
    assert meta["output_tokens"] == 210
    assert meta["decode_ms"] == 2100
    assert meta["model"] == "qwen3.8-27b"
    assert meta["latency_ms"] >= 0

    saved = database.get_chat_history(MAIN_AGENT_ID)[-1]
    assert saved["role"] == "assistant"
    assert saved["meta"]["output_tokens"] == 210
    assert saved["meta"]["input_tokens"] == 2200
    assert saved["meta"]["decode_ms"] == 2100
    assert saved["meta"]["prompt_ms"] == 800


@pytest.mark.asyncio
async def test_numbers_the_turn_measured_itself_are_kept(agent):
    async def fake_respond(self, user_message, session_id=None, stream_callback=None, images=None):
        await _call()
        msg_id = database.save_message(session_id, "assistant", "ok")
        self.last_saved_ids[session_id] = {"user": None, "assistant": msg_id}
        # A sub-agent reports tokens and latency but no decode time.
        self.last_run_metadata[session_id] = {"output_tokens": 50, "latency_ms": 4000, "model": "sub-model"}
        return "ok"

    with patch.object(VexaAgent, "_respond", fake_respond), \
         patch.object(llm_client, "_call_llm_normalized", AsyncMock(return_value=SYNTH)):
        await agent.respond("hi", session_id="s1")

    meta = agent.last_run_metadata["s1"]
    assert meta["output_tokens"] == 50
    assert meta["latency_ms"] == 4000
    assert meta["model"] == "sub-model"
    assert meta["decode_ms"] == 2000
    assert database.get_chat_history("s1")[-1]["meta"]["decode_ms"] == 2000


@pytest.mark.asyncio
async def test_a_turn_with_no_model_call_writes_nothing(agent):
    async def fake_respond(self, user_message, session_id=None, stream_callback=None, images=None):
        msg_id = database.save_message(session_id, "assistant", "Запомнил.")
        self.last_saved_ids[session_id] = {"user": None, "assistant": msg_id}
        return "Запомнил."

    with patch.object(VexaAgent, "_respond", fake_respond):
        await agent.respond("запомни", session_id="s2")

    assert "meta" not in database.get_chat_history("s2")[-1]
