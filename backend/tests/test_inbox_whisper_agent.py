"""The whisper, as the model actually receives it: inside the system message of the
next sub-agent turn, never in the user's turn, never in the stored history."""

from unittest.mock import AsyncMock, patch

import pytest

from backend import bot_access_gate, database, inbox
from backend.agent import agent_instance
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture


@pytest.mark.asyncio
async def test_the_whisper_reaches_the_model_only_through_the_system_message(inbox_env):
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_SUCCESS

    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Сколько стоит доставка?")
    whisper = "клиент постоянный — предложи бесплатную доставку"
    inbox.add_whisper(decision.conversation_id, whisper)
    captured = []

    async def fake_llm(*args, **kwargs):
        captured.append(kwargs.get("messages") or (args[3] if len(args) > 3 else None))
        return NormalizedLLMResponse(
            status=STATUS_SUCCESS, provider="llamacpp", model="llama3",
            content="Для вас доставка бесплатная.", finish_reason="stop",
            usage=LLMUsage(input_tokens=10, output_tokens=5),
        )

    subagent = database.get_subagent("agent-1")
    with patch.dict("os.environ", {"ROUTER_DELEGATION_MODE": "always_local"}), \
         patch("backend.llm_client.call_llm_normalized", new=AsyncMock(side_effect=fake_llm)):
        reply = await agent_instance._respond_as_subagent("Сколько стоит доставка?", subagent, chat_id=decision.session_id)

    assert reply == "Для вас доставка бесплатная."
    messages = next(m for m in captured if m)
    system = [m["content"] for m in messages if m["role"] == "system"]
    others = [m["content"] for m in messages if m["role"] != "system"]
    assert any(whisper in content for content in system)
    assert not any(whisper in (content or "") for content in others)
    assert not any(whisper in m["content"] for m in database.get_chat_history(decision.session_id))
    # Consumed: the following turn does not hear it again.
    assert inbox.consume_whisper_block(decision.session_id) == ""
