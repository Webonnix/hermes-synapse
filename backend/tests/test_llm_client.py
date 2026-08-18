"""Unit + integration tests for the normalized LLM client (P0)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend import llm_client as lc
from backend.llm_client import (
    NormalizedLLMResponse,
    call_llm_normalized,
    mask_secrets,
    normalize_openai_response,
    normalize_stream_chunks,
)


def _make_body(content=None, tool_calls=None, finish_reason="stop", usage=None,
               refusal=None, reasoning=None):
    message = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if refusal is not None:
        message["refusal"] = refusal
    if reasoning is not None:
        message["reasoning"] = reasoning
    body = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage
    return body


# ── normalize_openai_response: response variants ──────────────────────────────

def test_normalize_plain_text_success():
    r = normalize_openai_response(
        _make_body("Готово, Сэр.", usage={"prompt_tokens": 10, "completion_tokens": 5}),
        provider="openrouter", model="m",
    )
    assert r.status == lc.STATUS_SUCCESS
    assert r.content == "Готово, Сэр."
    assert r.usage.input_tokens == 10
    assert r.usage.output_tokens == 5


def test_normalize_content_block_array():
    r = normalize_openai_response(
        _make_body([{"type": "text", "text": "Часть 1"}, {"type": "text", "text": "Часть 2"}]),
        provider="p", model="m",
    )
    assert r.status == lc.STATUS_SUCCESS
    assert "Часть 1" in r.content and "Часть 2" in r.content


def test_normalize_tool_call_without_text_is_tool_call_not_empty():
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "get_weather", "arguments": {"location": "Minsk"}}}]
    r = normalize_openai_response(_make_body(None, tool_calls=tool_calls),
                                  provider="p", model="m")
    assert r.status == lc.STATUS_TOOL_CALL
    assert r.has_tool_calls
    assert r.status != lc.STATUS_EMPTY


def test_normalize_empty_content():
    r = normalize_openai_response(_make_body(None), provider="p", model="m")
    assert r.status == lc.STATUS_EMPTY
    assert r.error_message


def test_normalize_reasoning_only_is_empty_but_keeps_reasoning():
    r = normalize_openai_response(_make_body(None, reasoning="думаю..."),
                                  provider="p", model="m")
    assert r.status == lc.STATUS_EMPTY
    assert r.reasoning == "думаю..."


def test_normalize_refusal_field():
    r = normalize_openai_response(_make_body(None, refusal="I can't help with that."),
                                  provider="p", model="m")
    assert r.status == lc.STATUS_REFUSAL
    assert r.content == "I can't help with that."


def test_normalize_content_filter_finish_reason():
    r = normalize_openai_response(_make_body(None, finish_reason="content_filter"),
                                  provider="p", model="m")
    assert r.status == lc.STATUS_REFUSAL


def test_normalize_length_truncation_is_empty_with_reason():
    r = normalize_openai_response(_make_body(None, finish_reason="length"),
                                  provider="p", model="m")
    assert r.status == lc.STATUS_EMPTY
    assert "truncat" in (r.error_message or "").lower()


def test_normalize_malformed_no_choices_is_parse_error():
    r = normalize_openai_response({"id": "x"}, provider="p", model="m")
    assert r.status == lc.STATUS_PARSE_ERROR


def test_reasoning_only_visible_answer_kept():
    # An unfinished <think> block with a trailing visible answer keeps the answer.
    r = normalize_openai_response(
        _make_body("<think>план...\nОтвет: Привет, Сэр."),
        provider="p", model="m",
    )
    # cleanup keeps text after unfinished think; may or may not strip marker but
    # must not be empty.
    assert r.status in (lc.STATUS_SUCCESS,)
    assert r.content.strip()


# ── Secret masking ────────────────────────────────────────────────────────────

def test_mask_secrets_bearer_and_keys():
    assert "REDACTED" in mask_secrets("Authorization: Bearer sk-or-abcdef123456")
    assert "sk-or-abcdef123456" not in mask_secrets("Bearer sk-or-abcdef123456")
    assert "REDACTED" in mask_secrets('{"api_key": "supersecretvalue"}')


@pytest.fixture(autouse=True)
def _forget_parameter_refusals():
    """Refused-parameter memory is process-wide by design (it must survive
    between user turns), so tests have to reset it or they leak into each other."""
    lc._REFUSED_PARAMS.clear()
    yield
    lc._REFUSED_PARAMS.clear()


# ── call_llm_normalized: HTTP integration (mocked) ────────────────────────────

def _mock_response(status_code=200, json_body=None, headers=None, text=""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


@pytest.mark.asyncio
async def test_call_success():
    resp = _mock_response(200, _make_body("Привет, Сэр.", usage={"prompt_tokens": 3, "completion_tokens": 2}),
                          headers={"x-request-id": "req_123"})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=resp)):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=0,
        )
    assert r.status == lc.STATUS_SUCCESS
    assert r.content == "Привет, Сэр."
    assert r.request_id == "req_123"
    assert r.latency_ms is not None


@pytest.mark.asyncio
async def test_call_ollama_uses_native_provider_and_stream_callback():
    from backend.ollama_client import OllamaChatResult

    callback = AsyncMock()
    result = OllamaChatResult(
        model="qwen3:8b",
        content="Native answer",
        thinking="plan",
        done_reason="stop",
        prompt_tokens=7,
        completion_tokens=3,
    )
    with patch("backend.ollama_client.OllamaClient.chat", new=AsyncMock(return_value=result)) as chat:
        response = await call_llm_normalized(
            api_base="http://ollama:11434",
            api_key="",
            model="qwen3:8b",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "clock", "parameters": {"type": "object"}}}],
            stream_callback=callback,
            provider_options={"num_ctx": 16384, "keep_alive": "10m", "think": True},
            max_retries=0,
        )
    assert response.status == lc.STATUS_SUCCESS
    assert response.provider == "ollama"
    assert response.usage.input_tokens == 7
    assert chat.await_args.kwargs["tools"]
    assert chat.await_args.kwargs["num_ctx"] == 16384
    assert chat.await_args.kwargs["stream_callback"] is callback


@pytest.mark.asyncio
async def test_call_timeout_retries_then_fails():
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.TimeoutException("t"))), \
         patch("backend.llm_client._backoff_delay", return_value=0):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=2,
        )
    assert r.status == lc.STATUS_TIMEOUT
    assert r.retry_count == 2


@pytest.mark.asyncio
async def test_call_provider_error_non_retryable_fails_fast():
    resp = _mock_response(400, {}, text="bad request Bearer sk-secret123456")
    post = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.post", new=post):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=3,
        )
    assert r.status == lc.STATUS_PROVIDER_ERROR
    assert post.call_count == 1  # 400 is not retried
    # error message must not leak the body/secret
    assert "sk-secret" not in (r.error_message or "")


@pytest.mark.asyncio
async def test_call_5xx_retries():
    resp = _mock_response(503, {}, text="unavailable")
    post = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.post", new=post), \
         patch("backend.llm_client._backoff_delay", return_value=0):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=2,
        )
    assert r.status == lc.STATUS_PROVIDER_ERROR
    assert post.call_count == 3  # 1 + 2 retries


@pytest.mark.asyncio
async def test_call_parse_error():
    resp = _mock_response(200)
    resp.json.side_effect = ValueError("no json")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=resp)):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=0,
        )
    assert r.status == lc.STATUS_PARSE_ERROR


@pytest.mark.asyncio
async def test_call_cancellation_propagates():
    async def _slow(*a, **k):
        await asyncio.sleep(10)

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_slow)):
        task = asyncio.create_task(call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=0,
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_public_dict_has_no_raw_body():
    r = NormalizedLLMResponse(status=lc.STATUS_SUCCESS, provider="p", model="m",
                              content="x", raw_response={"secret": "body"})
    public = r.to_public_dict()
    assert "raw_response" not in public
    assert public["status"] == lc.STATUS_SUCCESS


def test_normalize_streaming_chunks():
    result = normalize_stream_chunks([
        {"choices": [{"delta": {"content": "При"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "вет"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 2}},
    ], provider="p", model="m")
    assert result.status == lc.STATUS_SUCCESS
    assert result.content == "Привет"
    assert result.usage.total_tokens is None


def test_interrupted_stream_is_provider_error():
    result = normalize_stream_chunks([
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
    ], provider="p", model="m")
    assert result.status == lc.STATUS_PROVIDER_ERROR
    assert result.content == "partial"


# ── Redaction gateway (call_llm_normalized) ────────────────────────────────────

@pytest.mark.asyncio
async def test_call_redacts_secret_before_send_and_restores_in_response():
    """A secret in an outbound message to an external provider must never hit
    the wire, and a placeholder the provider echoes back must be restored to
    the real value before the caller ever sees it."""
    secret = "sk-" + "a" * 20
    sent = {}

    async def fake_post(url, json=None, headers=None):
        sent["messages"] = json["messages"]
        placeholder = sent["messages"][0]["content"]
        return _mock_response(200, _make_body(f"Использую ключ {placeholder}"))

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
         patch("backend.redaction._local_model_confirm", new=AsyncMock(return_value=[])):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": secret}], max_retries=0,
        )

    assert secret not in sent["messages"][0]["content"]
    assert sent["messages"][0]["content"].startswith("[SECRET_")
    assert secret in r.content
    assert "[SECRET_" not in r.content


@pytest.mark.asyncio
async def test_call_does_not_redact_for_local_provider():
    """The local model is the trust boundary — nothing gets rewritten on the
    way to Ollama, and no local-model confirmation call is made for it."""
    from backend.ollama_client import OllamaChatResult

    secret = "sk-" + "b" * 20
    result = OllamaChatResult(model="qwen3:8b", content="ok", thinking=None,
                              done_reason="stop", prompt_tokens=1, completion_tokens=1)
    with patch("backend.ollama_client.OllamaClient.chat", new=AsyncMock(return_value=result)) as chat, \
         patch("backend.redaction.detect_and_redact") as spy:
        await call_llm_normalized(
            api_base="http://ollama:11434", api_key="", model="qwen3:8b",
            messages=[{"role": "user", "content": secret}], max_retries=0,
        )
    assert chat.await_args.kwargs["messages"][0]["content"] == secret
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_redaction_scans_each_message_at_most_once_across_calls():
    """A tool loop reuses the same `messages` list across many calls as it
    grows; each message must only ever pass through the (expensive,
    local-model-backed) redaction pass once, not on every iteration."""
    secret = "sk-" + "c" * 20
    messages = [{"role": "user", "content": secret}]

    async def fake_post(url, json=None, headers=None):
        return _mock_response(200, _make_body("ok"))

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
         patch("backend.redaction._local_model_confirm", new=AsyncMock(return_value=[])) as confirm:
        await call_llm_normalized(api_base="https://openrouter.ai/api/v1", api_key="k",
                                  model="m", messages=messages, max_retries=0)
        messages.append({"role": "assistant", "content": "first reply"})
        messages.append({"role": "tool", "content": "unrelated tool output", "tool_call_id": "c1"})
        await call_llm_normalized(api_base="https://openrouter.ai/api/v1", api_key="k",
                                  model="m", messages=messages, max_retries=0)

    # 3 distinct message contents ever appeared across both calls -> exactly 3
    # local-model confirmation calls, never re-scanning the first message.
    assert confirm.await_count == 3


# ── SSE-framed bodies from gateways that ignore the non-streaming request ─────

def _sse_mock_response(text):
    """A response whose .json() raises, exactly like httpx on a trailing frame."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = {"content-type": "text/event-stream"}
    resp.json.side_effect = ValueError("Extra data")
    resp.text = text
    return resp


@pytest.mark.asyncio
async def test_complete_body_with_sse_done_trailer_is_still_parsed():
    """9Router answers a plain (non-streaming) request with HTTP 200,
    content-type text/event-stream, and glues `data: [DONE]` straight onto the
    JSON. That trailer alone used to surface to the user as 'Provider response
    was not valid JSON' — an agent answering a Matrix message with an error."""
    body = json.dumps(_make_body("Привет! Дела отлично.")) + "data: [DONE]\n\n"
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_sse_mock_response(body))):
        r = await call_llm_normalized(
            api_base="http://9router:20128/v1", api_key="k", model="ds/deepseek-v4-pro",
            messages=[{"role": "user", "content": "привет"}], max_retries=0,
        )
    assert r.status == lc.STATUS_SUCCESS
    assert r.content == "Привет! Дела отлично."


@pytest.mark.asyncio
async def test_real_chunk_stream_is_merged_instead_of_truncated():
    """Several frames means a genuine delta stream — taking only the first one
    would silently answer with a fragment of the reply."""
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Привет"}}]},
        {"choices": [{"index": 0, "delta": {"content": ", Альберт"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_sse_mock_response(body))):
        r = await call_llm_normalized(
            api_base="http://9router:20128/v1", api_key="k", model="ds/deepseek-v4-pro",
            messages=[{"role": "user", "content": "привет"}], max_retries=0,
        )
    assert r.status == lc.STATUS_SUCCESS
    assert r.content == "Привет, Альберт"


@pytest.mark.asyncio
async def test_body_that_is_genuinely_not_json_still_fails_loudly():
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_sse_mock_response("<html>502</html>"))):
        r = await call_llm_normalized(
            api_base="http://9router:20128/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=0,
        )
    assert r.status == lc.STATUS_PARSE_ERROR


@pytest.mark.asyncio
async def test_parameter_the_model_refuses_is_dropped_and_the_call_retried():
    """kimi-k3 answers `temperature: 0.7` with HTTP 400 "invalid temperature:
    only 1 is allowed for this model". The agent has no way to know that per
    model, so a permanent 400 over one field must not cost the whole turn."""
    calls = []

    async def fake_post(url, json=None, headers=None):
        calls.append(json)
        if "temperature" in json:
            return _mock_response(400, text='{"error":{"message":"invalid temperature: only 1 is allowed for this model"}}')
        return _mock_response(200, _make_body("51"))

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        r = await call_llm_normalized(
            api_base="http://9router:20128/v1", api_key="k", model="kimi/kimi-k3",
            messages=[{"role": "user", "content": "17*3"}], temperature=0.7, max_retries=0,
        )
    assert r.status == lc.STATUS_SUCCESS
    assert r.content == "51"
    assert len(calls) == 2 and "temperature" not in calls[1]


@pytest.mark.asyncio
async def test_refused_parameter_retry_happens_only_once():
    """A gateway that keeps blaming fields must not put us in a strip-and-retry
    loop that walks the whole payload apart."""
    calls = []

    async def fake_post(url, json=None, headers=None):
        calls.append(json)
        return _mock_response(400, text='{"error":{"message":"invalid temperature"}}')

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        r = await call_llm_normalized(
            api_base="http://9router:20128/v1", api_key="k", model="kimi/kimi-k3",
            messages=[{"role": "user", "content": "hi"}], temperature=0.7, max_retries=0,
        )
    assert r.status == lc.STATUS_PROVIDER_ERROR
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_agent_generation_params_reach_the_request_body():
    """model_params used to be written by the UI and read by nobody."""
    from backend.agent import _agent_generation_params

    params = _agent_generation_params({"model_params": '{"top_p": 0.5, "seed": 7, "nonsense": 1, "model": "evil"}'})
    assert params == {"top_p": 0.5, "seed": 7}

    sent = {}

    async def fake_post(url, json=None, headers=None):
        sent.update(json)
        return _mock_response(200, _make_body("ok"))

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}], max_retries=0,
            extra_payload={**params, "model": "hijacked"},
        )
    assert sent["top_p"] == 0.5 and sent["seed"] == 7
    assert sent["model"] == "m"  # extra_payload can never rewrite the routed model


@pytest.mark.asyncio
async def test_a_refusal_is_remembered_so_the_next_call_never_repeats_it():
    """The retry alone is not enough: the upstream that rejected the field also
    throttles the request that carried it ("reset after 30s"), so the immediate
    retry can fail too. Every later call must leave the field out from the start."""
    calls = []

    async def fake_post(url, json=None, headers=None):
        calls.append(json)
        if "temperature" in json:
            return _mock_response(400, text='{"error":{"message":"invalid temperature: only 1 is allowed"}}')
        return _mock_response(200, _make_body("51"))

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        for _ in range(3):
            r = await call_llm_normalized(
                api_base="http://9router:20128/v1", api_key="k", model="kimi/kimi-k3",
                messages=[{"role": "user", "content": "17*3"}], temperature=0.7, max_retries=0,
            )
            assert r.status == lc.STATUS_SUCCESS

    # first call: rejected + retry = 2 requests; the two after it: 1 each.
    assert len(calls) == 4
    assert all("temperature" not in payload for payload in calls[1:])
    # Scoped to that model — a different one still gets the owner's temperature.
    assert lc.remembered_refusals("http://9router:20128/v1", "ds/deepseek-v4-pro") == set()
