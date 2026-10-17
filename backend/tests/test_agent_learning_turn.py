"""A sub-agent turn actually carries that agent's learned notes to the model —
and does so even with the shared auto-RAG switched off."""

from unittest.mock import AsyncMock, patch

import pytest

from backend import database
from backend.agent import agent_instance
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture


@pytest.mark.asyncio
async def test_learned_notes_reach_the_model_for_their_own_agent(inbox_env, monkeypatch):
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_SUCCESS

    captured = []

    async def fake_llm(*args, **kwargs):
        captured.append(kwargs.get("messages"))
        return NormalizedLLMResponse(
            status=STATUS_SUCCESS, provider="llamacpp", model="llama3",
            content="ok", finish_reason="stop", usage=LLMUsage(input_tokens=1, output_tokens=1),
        )

    asked = []

    def fake_context(agent_id, query):
        asked.append(agent_id)
        return f"\n[learned note for {agent_id}]"

    monkeypatch.setattr("backend.agent_learning.context_for_agent", fake_context)
    monkeypatch.setattr(agent_instance, "auto_rag", False)
    subagent = database.get_subagent("agent-1")
    with patch.dict("os.environ", {"ROUTER_DELEGATION_MODE": "always_local"}), \
         patch("backend.llm_client.call_llm_normalized", new=AsyncMock(side_effect=fake_llm)):
        await agent_instance._respond_as_subagent("Как обновить зависимости?", subagent, chat_id="learn-1")

    assert asked == ["agent-1"]
    messages = next(m for m in captured if m)
    user_turns = [m["content"] for m in messages if m["role"] == "user"]
    assert any("[learned note for agent-1]" in (content or "") for content in user_turns)
    # The stored transcript keeps what the user said, not the reference block.
    assert not any("learned note" in m["content"] for m in database.get_chat_history("learn-1"))
