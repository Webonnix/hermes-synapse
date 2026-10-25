import os
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from backend.agent import VexaAgent, DEFAULT_SYSTEM_PROMPT
from backend import database

@pytest.fixture(autouse=True)
def pin_pre_flight_routers():
    """Keep the two pre-flight routers out of the mocked call sequence.

    Both the complexity router and the delegation classifier make their own
    cheap LLM call before the turn proper. Whenever the ambient environment
    left either of them enabled, that call swallowed the first response in a
    test's `side_effect` list and every assertion downstream shifted by one —
    tests that pass or fail depending on the box they run on. Pin them here so
    a mocked sequence means exactly what it says.
    """
    with patch.dict(os.environ, {"COMPLEXITY_ROUTING": "keyword",
                                 "ROUTER_DELEGATION_MODE": "always_local"}):
        yield


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    # Set up a temporary database file for the agent tests
    original_db_path = database.DB_PATH
    original_db_dir = database.DB_DIR
    
    test_db = tmp_path / "test_hermes_agent.db"
    database.DB_PATH = str(test_db)
    database.DB_DIR = str(tmp_path)
    
    database.init_db()
    
    yield
    
    # Restore original paths
    database.DB_PATH = original_db_path
    database.DB_DIR = original_db_dir

@pytest.fixture
def agent():
    # Instantiate agent for testing
    agent_inst = VexaAgent()
    agent_inst.provider = "openrouter"
    agent_inst.api_key = "test_key"
    agent_inst.api_base = "https://openrouter.ai/api/v1"
    return agent_inst

def test_agent_initial_state(agent):
    import os
    assert agent.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert agent.model == os.getenv("LLM_MODEL", "qwen3.8-27b")

def test_update_system_prompt(agent):
    new_prompt = "New prompt content"
    agent.update_system_prompt(new_prompt)
    assert agent.system_prompt == new_prompt


def test_provider_switch_restores_provider_specific_endpoint(agent):
    agent.provider = "openrouter"
    agent.api_base = "https://openrouter.example/v1"
    agent.openai_api_base = "https://openrouter.example/v1"
    agent.local_base_url = "http://hermes-llama:8080"

    agent.update_runtime_config(provider="llamacpp")
    assert agent.api_base == "http://hermes-llama:8080"

    agent.update_runtime_config(provider="openrouter")
    assert agent.api_base == "https://openrouter.example/v1"


def test_locked_provider_endpoint_ignores_persisted_dashboard_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "llamacpp")
    monkeypatch.setenv("LLAMACPP_BASE_URL", "http://hermes-llama:8080")
    monkeypatch.setenv("LLM_LOCK_PROVIDER_ENDPOINT", "true")
    database.save_app_settings({
        "provider": "llamacpp",
        "api_base": "http://127.0.0.1:8080",
        "local_base_url": "http://127.0.0.1:8080",
    })

    locked_agent = VexaAgent()

    assert locked_agent.provider == "llamacpp"
    assert locked_agent.api_base == "http://hermes-llama:8080"
    assert locked_agent.local_base_url == "http://hermes-llama:8080"
    assert locked_agent.get_runtime_config()["provider_endpoint_locked"] is True

@pytest.mark.asyncio
async def test_clear_history(agent):
    session_id = "user_123"
    database.save_message(session_id, "user", "Hi")

    assert len(await agent.get_history(session_id)) == 1
    agent.clear_history(session_id)
    assert len(await agent.get_history(session_id)) == 0

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_respond_success(mock_post, agent):
    # Setup mock response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Здравствуйте, Сэр. Чем могу помочь?"
                }
            }
        ]
    }
    mock_post.return_value = mock_response

    response = await agent.respond("Привет", session_id="test_session")
    
    assert response == "Здравствуйте, Сэр. Чем могу помочь?"
    
    # Check if history is updated in the database
    history = await agent.get_history("test_session")
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Привет"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Здравствуйте, Сэр. Чем могу помочь?"

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_respond_retries_when_cleaned_reasoning_is_empty(mock_post, agent):
    reasoning_only_response = MagicMock()
    reasoning_only_response.status_code = 200
    reasoning_only_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "<think>\nНужно кратко ответить на приветствие.\n</think>"
                },
                "finish_reason": "stop"
            }
        ]
    }

    final_response = MagicMock()
    final_response.status_code = 200
    final_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Дела хорошо, Сэр. Готова помочь."
                }
            }
        ]
    }
    mock_post.side_effect = [reasoning_only_response, final_response]

    response = await agent.respond("Как дела?", session_id="test_session_reasoning_retry")

    assert response == "Дела хорошо, Сэр. Готова помочь."
    assert mock_post.call_count == 2
    retry_payload = mock_post.call_args_list[1].kwargs["json"]
    assert "видимого финального ответа" in retry_payload["messages"][-1]["content"]

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_respond_retries_when_provider_content_is_empty(mock_post, agent):
    empty_response = MagicMock()
    empty_response.status_code = 200
    empty_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None
                },
                "finish_reason": "stop"
            }
        ]
    }

    final_response = MagicMock()
    final_response.status_code = 200
    final_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Всё в порядке, Сэр."
                }
            }
        ]
    }
    mock_post.side_effect = [empty_response, final_response]

    response = await agent.respond("Как дела?", session_id="test_session_empty_retry")

    assert response == "Всё в порядке, Сэр."
    assert mock_post.call_count == 2

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_respond_http_error(mock_post, agent):
    # Setup mock error response
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_post.return_value = mock_response

    response = await agent.respond("Привет", session_id="test_session")
    assert "Провайдер модели временно недоступен" in response

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_respond_openmodel_success(mock_post, agent):
    agent.api_base = "https://api.openmodel.ai/v1"
    
    # Setup mock response in Anthropic style
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "Здравствуйте, Сэр. Чем могу помочь из OpenModel?"
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 15
        }
    }
    mock_post.return_value = mock_response

    response = await agent.respond("Привет", session_id="test_session_openmodel")
    assert response == "Здравствуйте, Сэр. Чем могу помочь из OpenModel?"

def test_calculate_cost():
    from backend.agent import calculate_cost
    # gemini-2.5-pro
    cost = calculate_cost("google/gemini-2.5-pro", 1000, 2000)
    assert cost == pytest.approx((1000 * 0.075 / 1_000_000) + (2000 * 0.30 / 1_000_000))
    
    # gemini-2.5-flash
    cost_flash = calculate_cost("google/gemini-2.5-flash", 1000, 2000)
    assert cost_flash == pytest.approx((1000 * 0.0375 / 1_000_000) + (2000 * 0.15 / 1_000_000))

def test_local_provider_message_sanitization_and_qwen_reasoning_cleanup():
    from backend.agent import (
        _clean_model_output,
        _normalize_tool_calls,
        _parse_tool_arguments,
        _sanitize_message_for_provider,
    )

    assert _clean_model_output("<think>\nreasoning only") == ""
    assert _clean_model_output("<think>hidden</think>\nГотово, Сэр.") == "Готово, Сэр."

    raw_call = {
        "id": "abc",
        "function": {
            "name": "get_weather",
            "arguments": {"location": "Minsk", "days_ahead": 0},
        },
    }
    normalized = _normalize_tool_calls([raw_call])
    assert isinstance(normalized[0]["function"]["arguments"], str)
    assert _parse_tool_arguments(normalized[0]["function"]["arguments"]) == {"location": "Minsk", "days_ahead": 0}

    sanitized = _sanitize_message_for_provider({
        "role": "assistant",
        "content": None,
        "reasoning_content": "private local-model trace",
        "thinking": "qwen trace",
        "tool_calls": [raw_call],
    })
    assert "reasoning_content" not in sanitized
    assert "thinking" not in sanitized
    assert sanitized["content"] == ""
    assert sanitized["tool_calls"][0]["function"]["arguments"] == '{"location": "Minsk", "days_ahead": 0}'


def test_visible_answer_retry_disables_thinking_and_has_safe_budget():
    from backend.agent import _visible_answer_retry_budget, _visible_answer_retry_options

    options = _visible_answer_retry_options({"provider": "llamacpp", "think": "true", "num_ctx": 131072})

    assert options["think"] is False
    assert options["num_ctx"] == 131072
    assert _visible_answer_retry_budget(256) == 512
    assert _visible_answer_retry_budget(2048) == 2048
    assert _visible_answer_retry_budget(9000) == 4096
    # A call that already died on `length` must not be recovered under the same
    # ceiling — that just reproduces the truncation.
    assert _visible_answer_retry_budget(2048, "length") == 4096
    assert _visible_answer_retry_budget(256, "length") == 2048
    assert _visible_answer_retry_budget(9000, "length") == 8192


@pytest.mark.asyncio
async def test_local_reasoning_only_retry_forces_visible_answer(agent):
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_EMPTY, STATUS_SUCCESS

    agent.provider = "llamacpp"
    agent.api_base = "http://hermes-llama:8080"
    agent.local_base_url = agent.api_base
    agent.local_think = "true"
    agent.max_tokens = 256
    agent.auto_rag = False
    first = NormalizedLLMResponse(
        status=STATUS_EMPTY,
        provider="llamacpp",
        model=agent.model,
        reasoning="internal trace",
        finish_reason="length",
        usage=LLMUsage(input_tokens=10, output_tokens=256),
        error_message="Model returned reasoning only, no visible answer.",
    )
    recovered = NormalizedLLMResponse(
        status=STATUS_SUCCESS,
        provider="llamacpp",
        model=agent.model,
        content="Привет. Я Vexa.",
        finish_reason="stop",
        usage=LLMUsage(input_tokens=20, output_tokens=8),
    )

    with patch("backend.llm_client.call_llm_normalized", new=AsyncMock(side_effect=[first, recovered])) as call:
        response = await agent.respond("Привет", session_id="local_reasoning_recovery")

    assert response == "Привет. Я Vexa."
    assert call.await_count == 2
    assert call.await_args_list[0].kwargs["provider_options"]["think"] == "true"
    assert call.await_args_list[1].kwargs["provider_options"]["think"] is False
    assert call.await_args_list[1].kwargs["max_tokens"] == 2048


@pytest.mark.asyncio
@patch("backend.agent.asyncio.to_thread", new_callable=AsyncMock)
async def test_empty_response_after_tool_execution_still_recovers(mock_to_thread, agent):
    """The regression this whole change exists for.

    The model runs a tool, then spends its entire budget writing the *next* tool
    call; the provider drops the truncated call, so the turn returns neither
    content nor tool calls. The recovery used to be skipped outright whenever a
    tool had run, and the user got a fallback claiming a retry had been made.
    """
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_EMPTY, STATUS_SUCCESS, STATUS_TOOL_CALL

    agent.provider = "llamacpp"
    agent.api_base = "http://hermes-llama:8080"
    agent.local_base_url = agent.api_base
    agent.local_think = False
    agent.tool_max_tokens = 2048
    agent.auto_rag = False

    tool_turn = NormalizedLLMResponse(
        status=STATUS_TOOL_CALL, provider="llamacpp", model=agent.model, finish_reason="stop",
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "get_system_stats", "arguments": "{}"}}],
        usage=LLMUsage(input_tokens=10, output_tokens=12),
    )
    def truncated():
        return NormalizedLLMResponse(
            status=STATUS_EMPTY, provider="llamacpp", model=agent.model, finish_reason="length",
            usage=LLMUsage(input_tokens=40, output_tokens=2048),
            error_message="Response truncated (max_tokens reached) before any text.",
        )
    recovered = NormalizedLLMResponse(
        status=STATUS_SUCCESS, provider="llamacpp", model=agent.model, finish_reason="stop",
        content="Сервер загружен на 12%.", usage=LLMUsage(input_tokens=45, output_tokens=9),
    )

    # Tool turn, a truncated turn, the doubled-budget retry of it (also
    # truncated), and only then the tool-free prose recovery.
    with patch("backend.tools.execute_tool", return_value={"cpu": 12}), \
         patch("backend.llm_client.call_llm_normalized",
               new=AsyncMock(side_effect=[tool_turn, truncated(), truncated(), recovered])) as call:
        response = await agent.respond("Как сервер?", session_id="empty_after_tools")

    assert response == "Сервер загружен на 12%."
    assert call.await_count == 4
    recovery = call.await_args_list[-1].kwargs
    # No tools on the recovery call: it cannot re-trigger a side effect, which is
    # exactly what makes it safe to run after a tool has already executed.
    assert not recovery.get("tools")
    assert recovery["max_tokens"] == 4096
    assert "Инструменты уже выполнены" in recovery["messages"][-1]["content"]


@pytest.mark.asyncio
@patch("backend.agent.asyncio.to_thread", new_callable=AsyncMock)
async def test_truncated_tool_turn_is_retried_once_with_a_bigger_budget(mock_to_thread, agent):
    """A tool call that outgrew num_predict is retried, not abandoned.

    The provider drops the half-written call, so the turn comes back with
    neither content nor tool calls. Nothing ran, so repeating the same move
    with more room is safe — and it is what actually gets the work done.
    """
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_EMPTY, STATUS_SUCCESS, STATUS_TOOL_CALL

    agent.provider = "llamacpp"
    agent.api_base = "http://hermes-llama:8080"
    agent.local_base_url = agent.api_base
    agent.local_think = False
    agent.tool_max_tokens = 2048
    agent.auto_rag = False

    truncated = NormalizedLLMResponse(
        status=STATUS_EMPTY, provider="llamacpp", model=agent.model, finish_reason="length",
        usage=LLMUsage(input_tokens=40, output_tokens=2048),
    )
    tool_turn = NormalizedLLMResponse(
        status=STATUS_TOOL_CALL, provider="llamacpp", model=agent.model, finish_reason="stop",
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "get_system_stats", "arguments": "{}"}}],
        usage=LLMUsage(input_tokens=45, output_tokens=900),
    )
    done = NormalizedLLMResponse(
        status=STATUS_SUCCESS, provider="llamacpp", model=agent.model, finish_reason="stop",
        content="Готово.", usage=LLMUsage(input_tokens=60, output_tokens=5),
    )

    with patch("backend.tools.execute_tool", return_value={"cpu": 12}), \
         patch("backend.llm_client.call_llm_normalized",
               new=AsyncMock(side_effect=[truncated, tool_turn, done])) as call:
        response = await agent.respond("Как сервер?", session_id="truncated_tool_turn")

    assert response == "Готово."
    budgets = [c.kwargs["max_tokens"] for c in call.await_args_list]
    # First attempt at the configured ceiling, the retry at double, and the
    # escalation is not applied a second time within the turn.
    assert budgets[0] == 2048
    assert budgets[1] == 4096
    assert all(c.kwargs.get("tools") for c in call.await_args_list[:2])


def test_tool_call_signature_ignores_ids_but_not_arguments():
    from backend.agent import _tool_call_signature

    def call(cid, name, args):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}

    # Same move, different call id each turn — must read as a repeat.
    assert _tool_call_signature([call("a", "dev_exec", '{"cmd": "ls"}')]) == \
           _tool_call_signature([call("b", "dev_exec", '{"cmd": "ls"}')])
    # Different arguments are a different move, not a repeat.
    assert _tool_call_signature([call("a", "dev_exec", '{"cmd": "ls"}')]) != \
           _tool_call_signature([call("a", "dev_exec", '{"cmd": "pwd"}')])
    # Order within one iteration is not significant.
    assert _tool_call_signature([call("a", "x", "{}"), call("b", "y", "{}")]) == \
           _tool_call_signature([call("c", "y", "{}"), call("d", "x", "{}")])


async def _run_until_loop_stops(agent, *, responses, question="Собери отчёт"):
    from unittest.mock import ANY  # noqa: F401  (kept local, mirrors other helpers)

    with patch("backend.tools.execute_tool", return_value={"ok": True}), \
         patch("backend.llm_client.call_llm_normalized",
               new=AsyncMock(side_effect=responses)) as call:
        text = await agent.respond(question, session_id="loop_guard")
    return text, call


@pytest.mark.asyncio
@patch("backend.agent.asyncio.to_thread", new_callable=AsyncMock)
async def test_loop_stops_on_repeated_identical_tool_calls(mock_to_thread, agent):
    """A model reissuing the same call gets cut off well before the step cap."""
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_SUCCESS, STATUS_TOOL_CALL
    from backend.agent import _REPEATED_TOOL_CALL_LIMIT

    agent.provider = "llamacpp"
    agent.api_base = "http://hermes-llama:8080"
    agent.local_base_url = agent.api_base
    agent.auto_rag = False
    agent.max_tool_iterations = 50

    def same_call():
        return NormalizedLLMResponse(
            status=STATUS_TOOL_CALL, provider="llamacpp", model=agent.model, finish_reason="stop",
            tool_calls=[{"id": "c", "type": "function",
                         "function": {"name": "get_system_stats", "arguments": "{}"}}],
            usage=LLMUsage(input_tokens=100, output_tokens=10),
        )
    wrap_up = NormalizedLLMResponse(
        status=STATUS_SUCCESS, provider="llamacpp", model=agent.model, finish_reason="stop",
        content="Собрал телеметрию, дальше не продвинулся.",
        usage=LLMUsage(input_tokens=120, output_tokens=12),
    )

    # Exactly the repeats the guard allows, then the wrap-up call it makes.
    text, call = await _run_until_loop_stops(
        agent,
        responses=[same_call() for _ in range(_REPEATED_TOOL_CALL_LIMIT)] + [wrap_up],
    )

    # Stopped at the repeat limit, not at max_tool_iterations (50).
    assert call.await_count == _REPEATED_TOOL_CALL_LIMIT + 1
    # The work done is reported, with an honest note about why it stopped.
    assert "Собрал телеметрию" in text
    assert "повторила" in text
    # The wrap-up call may not start anything new.
    assert not call.await_args_list[-1].kwargs.get("tools")


@pytest.mark.asyncio
@patch("backend.agent.asyncio.to_thread", new_callable=AsyncMock)
async def test_loop_stops_before_the_context_window_overflows(mock_to_thread, agent):
    """Stop while the window still holds the turn, rather than let it evict the system prompt."""
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_SUCCESS, STATUS_TOOL_CALL

    agent.provider = "llamacpp"
    agent.api_base = "http://hermes-llama:8080"
    agent.local_base_url = agent.api_base
    agent.auto_rag = False
    agent.max_tool_iterations = 50
    agent.local_num_ctx = 1000

    def call_at(prompt_tokens, name):
        return NormalizedLLMResponse(
            status=STATUS_TOOL_CALL, provider="llamacpp", model=agent.model, finish_reason="stop",
            tool_calls=[{"id": "c", "type": "function",
                         "function": {"name": "dev_exec", "arguments": '{"cmd": "%s"}' % name}}],
            usage=LLMUsage(input_tokens=prompt_tokens, output_tokens=10),
        )
    wrap_up = NormalizedLLMResponse(
        status=STATUS_SUCCESS, provider="llamacpp", model=agent.model, finish_reason="stop",
        content="Вот что успела собрать.", usage=LLMUsage(input_tokens=900, output_tokens=8),
    )

    text, call = await _run_until_loop_stops(
        agent,
        # 400 and 700 are under the 85% mark; 900 is over it and ends the loop.
        responses=[call_at(400, "a"), call_at(700, "b"), call_at(900, "c"), wrap_up],
    )

    assert call.await_count == 4
    assert "Вот что успела собрать." in text
    assert "контекст заполнен" in text.lower()


def test_length_fallback_does_not_invent_a_retry_or_blame_reasoning():
    from backend.agent import _empty_model_response_fallback

    text = _empty_model_response_fallback("Vexa", "length", retry_attempted=False, max_tokens=2048)
    assert "рассужден" not in text
    assert "повторная попытка" not in text.lower()
    assert "2048" in text

    tried = _empty_model_response_fallback("Vexa", "length", retry_attempted=True, max_tokens=4096)
    assert "повторная попытка" in tried.lower()

@pytest.mark.asyncio
@patch("backend.agent.asyncio.to_thread", new_callable=AsyncMock)
@patch("backend.tools.execute_tool")
@patch("httpx.AsyncClient.post")
async def test_respond_sanitizes_qwen_tool_messages_between_local_calls(mock_post, mock_execute_tool, mock_to_thread, agent):
    agent.api_base = "http://localhost:11434/v1"
    agent.model = "qwen3:6b"

    tool_response = MagicMock()
    tool_response.status_code = 200
    tool_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "I should call weather.",
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": {"location": "Minsk", "days_ahead": 0},
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
    }

    final_response = MagicMock()
    final_response.status_code = 200
    final_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "<think>hidden</think>\nВ Минске сейчас прохладно, Сэр.",
                }
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10},
    }

    mock_post.side_effect = [tool_response, final_response]
    mock_execute_tool.return_value = '{"status": "mock", "temperature": "+20°C"}'
    mock_to_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    response = await agent.respond("Какая погода в Минске?", session_id="local_qwen_tool")

    assert response == "В Минске сейчас прохладно, Сэр."
    assert mock_execute_tool.call_args.args[1] == {"location": "Minsk", "days_ahead": 0}

    second_payload = mock_post.call_args_list[1].kwargs["json"]
    assistant_message = next(msg for msg in second_payload["messages"] if msg["role"] == "assistant")
    assert "reasoning_content" not in assistant_message
    assert "thinking" not in assistant_message
    assert assistant_message["content"] == ""
    assert json.loads(assistant_message["tool_calls"][0]["function"]["arguments"]) == {"location": "Minsk", "days_ahead": 0}


@pytest.mark.asyncio
async def test_generate_image_tool_call_is_captured_in_last_generated_images(agent):
    """The image URL a generate_image tool call actually returns must be
    recorded on the agent (last_generated_images), independent of whatever
    the model's own final text says -- Telegram delivery (bot.py/agent_bot.py)
    relies on this rather than re-parsing the model's reply, since a model
    echoing back a long generated filename is exactly the kind of string an
    LLM can drop or transpose a character in."""
    from backend.llm_client import LLMUsage, NormalizedLLMResponse, STATUS_SUCCESS

    tool_call_response = NormalizedLLMResponse(
        status=STATUS_SUCCESS, provider="openrouter", model=agent.model,
        content="",
        tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "generate_image", "arguments": json.dumps({"prompt": "a robot"})},
        }],
        finish_reason="tool_calls",
        usage=LLMUsage(input_tokens=10, output_tokens=5),
    )
    final_response = NormalizedLLMResponse(
        status=STATUS_SUCCESS, provider="openrouter", model=agent.model,
        content="Готово! ![Робот](/api/generated-images/abc123.png)",
        finish_reason="stop",
        usage=LLMUsage(input_tokens=20, output_tokens=10),
    )
    tool_result = json.dumps({"image_url": "/api/generated-images/abc123.png", "cost_usd": 0.04})

    with patch.dict("os.environ", {"COMPLEXITY_ROUTING": "always_direct"}), \
         patch("backend.llm_client.call_llm_normalized", new=AsyncMock(side_effect=[tool_call_response, final_response])), \
         patch("backend.control_plane.execute_governed_tool", return_value=tool_result):
        response = await agent.respond("Нарисуй робота", session_id="img_test")

    assert response == "Готово! ![Робот](/api/generated-images/abc123.png)"
    assert agent.last_generated_images["img_test"] == ["/api/generated-images/abc123.png"]

@pytest.mark.asyncio
async def test_classify_complexity():
    from backend.agent import classify_complexity
    
    # Test env overrides
    with patch.dict("os.environ", {"COMPLEXITY_ROUTING": "always_direct"}):
        res = await classify_complexity("test message", "api_key", "api_base")
        assert res == "direct"
        
    with patch.dict("os.environ", {"COMPLEXITY_ROUTING": "always_agent"}):
        res = await classify_complexity("test message", "api_key", "api_base")
        assert res == "agent"
        
    # Normal message with auto
    with patch.dict("os.environ", {"COMPLEXITY_ROUTING": "auto"}):
        # Test keyword matching fallback (on exception/failure)
        with patch("httpx.AsyncClient.post", side_effect=Exception("network error")):
            # Message has keyword matching 'orchestrate'
            res = await classify_complexity("сравни Bitcoin и Ethereum", "api_key", "api_base")
            assert res == "orchestrate"
            
            # Message has keyword matching 'agent'
            res = await classify_complexity("найди курс биткоина", "api_key", "api_base")
            assert res == "agent"
            
            # Normal message (direct fallback)
            res = await classify_complexity("Привет, как дела?", "api_key", "api_base")
            assert res == "direct"

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_classify_complexity_llm_success(mock_post):
    from backend.agent import classify_complexity
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "agent"
                }
            }
        ]
    }
    mock_post.return_value = mock_resp
    
    with patch.dict("os.environ", {"COMPLEXITY_ROUTING": "auto"}):
        res = await classify_complexity("hi", "key", "base")
        assert res == "agent"

def test_translate_to_anthropic_payload():
    from backend.agent import translate_to_anthropic_payload
    import json
    
    openai_payload = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi", "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location": "Tel Aviv"}'
                    }
                }
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather info",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"]
                    }
                }
            }
        ],
        "temperature": 0.5,
        "max_tokens": 123
    }
    
    anthropic = translate_to_anthropic_payload(openai_payload)
    
    assert anthropic["model"] == "claude-3-5-sonnet"
    assert anthropic["system"] == "You are a helpful assistant."
    assert anthropic["temperature"] == 0.5
    assert anthropic["max_tokens"] == 123
    assert len(anthropic["messages"]) == 3
    
    # Check messages translation
    assert anthropic["messages"][0] == {"role": "user", "content": "Hello"}
    assert anthropic["messages"][1]["role"] == "assistant"
    assert anthropic["messages"][1]["content"][0] == {"type": "text", "text": "Hi"}
    assert anthropic["messages"][1]["content"][1]["type"] == "tool_use"
    assert anthropic["messages"][1]["content"][1]["name"] == "get_weather"
    
    assert anthropic["messages"][2]["role"] == "user"
    assert anthropic["messages"][2]["content"][0]["type"] == "tool_result"
    assert anthropic["messages"][2]["content"][0]["tool_use_id"] == "call_1"
    assert anthropic["messages"][2]["content"][0]["content"] == "sunny"
    
    # Check tools translation
    assert len(anthropic["tools"]) == 1
    assert anthropic["tools"][0]["name"] == "get_weather"
    assert anthropic["tools"][0]["input_schema"]["properties"] == {"location": {"type": "string"}}

def test_translate_to_openai_response():
    from backend.agent import translate_to_openai_response
    import json
    
    anthropic_response = {
        "content": [
            {"type": "text", "text": "Let me check that."},
            {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"location": "Haifa"}}
        ],
        "usage": {
            "input_tokens": 15,
            "output_tokens": 25
        }
    }
    
    openai_resp = translate_to_openai_response(anthropic_response)
    
    assert openai_resp["choices"][0]["message"]["content"] == "Let me check that."
    tool_calls = openai_resp["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_123"
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"location": "Haifa"}
    assert openai_resp["usage"]["prompt_tokens"] == 15
    assert openai_resp["usage"]["completion_tokens"] == 25

@pytest.mark.asyncio
@patch("backend.agent.asyncio.to_thread", new_callable=AsyncMock)
@patch("backend.tools.execute_tool")
@patch("httpx.AsyncClient.post")
async def test_respond_caps_tool_loop_iterations(mock_post, mock_execute_tool, mock_to_thread, agent):
    """A model that keeps requesting tools must be stopped at the iteration cap
    instead of looping forever (audit P0 runaway-loop guard)."""
    agent.max_tool_iterations = 3

    def _tool_call_response(*args, **kwargs):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_loop",
                        "type": "function",
                        "function": {"name": "get_weather",
                                     "arguments": {"location": "Minsk", "days_ahead": 0}},
                    }],
                }
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        return r

    mock_post.side_effect = _tool_call_response
    mock_execute_tool.return_value = '{"status": "ok"}'
    mock_to_thread.return_value = '{"status": "ok"}'

    response = await agent.respond("погода?", session_id="loop_cap")

    # The loop must stop; the model was called at most cap+1 times.
    assert mock_post.call_count <= agent.max_tool_iterations + 1
    assert "остановлен" in response.lower() or response.strip()


@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_respond_timeout_returns_graceful_message(mock_post, agent):
    import httpx as _httpx
    mock_post.side_effect = _httpx.TimeoutException("timed out")
    response = await agent.respond("Привет", session_id="timeout_sess")
    # No stack trace leaks to the user; a graceful message is returned.
    assert "Traceback" not in response
    assert response.strip()


def test_calculate_cost_delegates_to_cost_module():
    from backend.agent import calculate_cost
    from backend.cost import calculate_cost as cost_calc
    assert calculate_cost("google/gemini-2.5-pro", 1000, 2000) == cost_calc(
        "google/gemini-2.5-pro", 1000, 2000
    )


def test_keyword_route_precedence():
    from backend.agent import _keyword_route
    assert _keyword_route("сравни Bitcoin и Ethereum") == "orchestrate"
    assert _keyword_route("найди курс биткоина") == "agent"
    assert _keyword_route("Привет, как дела?") == "direct"


def test_suppress_tts(agent):
    session_id = "test_sess"
    assert agent.check_and_clear_suppress_tts(session_id) is False
    agent.suppress_tts_sessions.add(session_id)
    assert agent.check_and_clear_suppress_tts(session_id) is True
    assert agent.check_and_clear_suppress_tts(session_id) is False


def _selected(message):
    from backend.agent import _select_tools_for_query
    from backend.tools import TOOLS_SCHEMA
    return {t["function"]["name"] for t in _select_tools_for_query(message, TOOLS_SCHEMA)}


def test_dev_request_offers_the_whole_dev_bundle():
    """Before the bundles existed, dev_* and git_* had no keywords at all — the
    main agent never saw them, so "разрабатывать" was unreachable by phrasing."""
    names = _selected("почини баг в коде и прогони тесты")
    assert {"dev_read_file", "dev_patch", "dev_run_tests", "git_commit"} <= names


def test_agent_management_request_offers_delegation_tools():
    names = _selected("делегируй эту задачу агенту")
    assert {"list_subagents", "call_subagent", "create_subagent"} <= names


@pytest.mark.parametrize("message", [
    "Запиши встречу с Ивановым на завтра в 15:00",
    "Запиши созвон с бухгалтером на пятницу",
    "Поставь встречу с Мариной на 28 октября в 18:30",
])
def test_owner_meeting_request_offers_add_calendar_event(message):
    """"Запиши встречу" matched only get_calendar_events, so the model searched with
    list_tools and sometimes picked the call-only propose_meeting instead."""
    assert "add_calendar_event" in _selected(message)


def test_phone_meeting_tools_say_they_are_call_only():
    from backend.phone_context import TOOLS_SCHEMA as PHONE_TOOLS
    descriptions = {t["function"]["name"]: t["function"]["description"] for t in PHONE_TOOLS}
    for name in ("propose_meeting", "calendar_free_slots"):
        assert descriptions[name].startswith("Только во время телефонного звонка")


def test_google_phrasings_reach_web_search():
    for message in ("загугли что нового в Qwen", "look up the release notes"):
        assert "web_search" in _selected(message), message


def test_plain_chat_still_selects_no_tool():
    assert _selected("Привет, как настроение?") == set()


def test_no_think_is_dropped_when_thinking_is_enabled():
    """`/no_think` next to local_think='medium' fought the owner's own setting
    and stripped the planning that tool chains need."""
    from backend.agent import _local_model_system_hint

    off = _local_model_system_hint("qwen-quality:latest", "http://hermes-llama:8080", "false")
    assert "/no_think" in off

    on = _local_model_system_hint("qwen-quality:latest", "http://hermes-llama:8080", "medium")
    assert on and "/no_think" not in on

    # Non-qwen and remote endpoints are untouched, as before.
    assert _local_model_system_hint("hermes-brain:latest", "http://hermes-llama:8080", "medium") == ""
    assert _local_model_system_hint("qwen/qwen3-max", "https://openrouter.ai/api/v1", "medium") == ""


def test_runtime_config_allows_a_larger_answer_budget(agent):
    agent.update_runtime_config(max_tokens=12000, tool_max_tokens=12000)
    assert agent.max_tokens == 12000
    assert agent.tool_max_tokens == 12000
    agent.update_runtime_config(max_tokens=99999)
    assert agent.max_tokens == 16384


def test_code_requests_stay_on_the_owner_agent():
    """Subagents have no dev_*/git_* tools, so a code request must not be routed
    to the research/orchestrator path just because it also sounds analytical."""
    from backend.agent import _keyword_route
    assert _keyword_route("проанализируй код в проекте") == "direct"
    assert _keyword_route("почини баг и закоммить") == "direct"
    # Non-code analysis is untouched.
    assert _keyword_route("сравни Bitcoin и Ethereum") == "orchestrate"


def test_work_discipline_only_covers_the_tools_on_the_turn():
    from backend.agent import _work_discipline

    weather = _work_discipline({"get_weather", "get_current_time_israel"})
    assert "git_commit" not in weather and "call_subagent" not in weather
    assert "Многошаговую задачу" in weather

    dev = _work_discipline({"dev_patch", "dev_run_tests"})
    assert "dev_run_tests" in dev and "call_subagent" not in dev

    delegation = _work_discipline({"call_subagent"})
    assert "call_subagent" in delegation
