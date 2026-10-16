import json

import httpx
import pytest

from backend.llamacpp_client import (
    LlamaCppClient,
    LlamaCppError,
    is_llamacpp_provider,
    is_local_provider_id,
    normalize_base_url,
    normalize_provider,
    think_template_kwargs,
)


def _sse(*events):
    lines = [f"data: {json.dumps(event)}\n\n" for event in events]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def test_base_url_provider_and_local_id_helpers(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLAMACPP_BASE_URL", raising=False)
    assert normalize_base_url("http://hermes-llama:8080/v1/") == "http://hermes-llama:8080"
    assert normalize_provider("OLLAMA") == "llamacpp"
    assert is_llamacpp_provider("https://provider.example/v1", "llamacpp")
    assert not is_llamacpp_provider("http://hermes-llama:8080", "openai_compatible")
    assert is_llamacpp_provider("http://hermes-llama:8080")
    assert not is_llamacpp_provider("https://openrouter.ai/api/v1")
    for value in ("", None, "local", "ollama", "LOCAL"):
        assert is_local_provider_id(value)
    assert not is_local_provider_id("prov-2a653eab8d1c")


@pytest.mark.parametrize("think,expected", [
    (False, {"enable_thinking": False}),
    ("false", {"enable_thinking": False}),
    (True, {"enable_thinking": True}),
    ("true", {"enable_thinking": True}),
    ("medium", {"enable_thinking": True, "reasoning_effort": "medium"}),
    ("high", {"enable_thinking": True, "reasoning_effort": "xhigh"}),
    ("whatever", {"enable_thinking": True}),
])
def test_think_maps_onto_template_switches(think, expected):
    assert think_template_kwargs(think) == expected


@pytest.mark.asyncio
async def test_non_streaming_chat_sends_template_kwargs_images_and_reads_timings():
    captured = {}

    async def handler(request: httpx.Request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "qwen3.8-27b",
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Red", "reasoning_content": "it is red"},
            }],
            "usage": {"prompt_tokens": 40, "completion_tokens": 2},
            "timings": {"prompt_ms": 120.5, "predicted_ms": 30.0, "cache_n": 12},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await LlamaCppClient("http://hermes-llama:8080/v1", client=http_client).chat(
            model="qwen3.8-27b",
            messages=[{"role": "user", "content": "colour?", "images": ["iVBORw0KGgo="]}],
            think="low",
            max_tokens=16,
            extra_payload={"top_p": 0.9, "enable_thinking": False, "stream": True},
        )

    body = captured["body"]
    assert captured["path"] == "/v1/chat/completions"
    assert body["stream"] is False
    assert body["top_p"] == 0.9
    # a per-agent switch overrides the configured think level
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "reasoning_effort": "low"}
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    assert parts[1] == {"type": "text", "text": "colour?"}
    assert result.content == "Red"
    assert result.thinking == "it is red"
    assert result.prompt_tokens == 40 and result.completion_tokens == 2
    assert result.eval_ms == 30.0 and result.prompt_ms == 120.5
    assert result.cached_tokens == 12


@pytest.mark.asyncio
async def test_streaming_emits_chunks_and_assembles_tool_calls():
    body = _sse(
        {"choices": [{"delta": {"reasoning_content": "thinking "}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                "function": {"name": "get_weather", "arguments": "{\"city\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \"Berlin\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "timings": {"prompt_ms": 10.0, "predicted_ms": 50.0, "prompt_n": 5, "predicted_n": 9}},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 9}},
    )

    async def handler(request: httpx.Request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["tools"][0]["function"]["name"] == "get_weather"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    chunks = []

    async def on_chunk(chunk):
        chunks.append(chunk)

    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await LlamaCppClient("http://hermes-llama:8080", client=http_client).chat(
            model="qwen3.8-27b", messages=[{"role": "user", "content": "weather?"}],
            tools=tools, stream_callback=on_chunk,
        )

    assert [c["thinking"] for c in chunks if c["thinking"]] == ["thinking "]
    assert [c["content"] for c in chunks if c["content"]] == ["Hello"]
    assert chunks[-1]["done"] is True
    assert chunks[-1]["tool_calls"] == result.tool_calls
    assert result.tool_calls == [{
        "id": "call_1", "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\": \"Berlin\"}"},
    }]
    assert result.done_reason == "tool_calls"
    assert result.prompt_tokens == 20 and result.completion_tokens == 9
    assert result.eval_ms == 50.0


@pytest.mark.asyncio
async def test_stream_without_final_event_is_an_interrupted_stream():
    body = f"data: {json.dumps({'choices': [{'delta': {'content': 'partial'}}]})}\n\n".encode()

    async def handler(request: httpx.Request):
        return httpx.Response(200, content=body)

    async def on_chunk(chunk):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(LlamaCppError) as exc:
            await LlamaCppClient("http://hermes-llama:8080", client=http_client).chat(
                model="m", messages=[{"role": "user", "content": "x"}], stream_callback=on_chunk,
            )
    assert exc.value.code == "interrupted_stream"


@pytest.mark.asyncio
async def test_loading_server_is_reported_not_raised():
    async def handler(request: httpx.Request):
        if request.url.path == "/health":
            return httpx.Response(503, json={"error": {"message": "Loading model", "code": 503}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        status = await LlamaCppClient("http://hermes-llama:8080", client=http_client).status()
    assert status["available"] is False
    assert status["health"] == "loading"
    assert status["code"] == "loading"


@pytest.mark.asyncio
async def test_status_reports_model_context_and_slots():
    async def handler(request: httpx.Request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b", "meta": {"size": 17000, "n_params": 27e9, "n_ctx_train": 262144}}]})
        if request.url.path == "/props":
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 196608}, "total_slots": 1,
                "model_path": "/models/qwen.gguf", "build_info": "b10920", "modalities": {"vision": True},
            })
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        status = await LlamaCppClient("http://hermes-llama:8080", client=http_client).status()
    assert status["available"] is True
    assert status["model"] == "qwen3.8-27b"
    assert status["n_ctx"] == 196608
    assert status["total_slots"] == 1
    assert status["vision"] is True
    assert status["build"] == "b10920"


@pytest.mark.asyncio
async def test_connection_refused_maps_to_connection_error():
    async def handler(request: httpx.Request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(LlamaCppError) as exc:
            await LlamaCppClient("http://hermes-llama:8080", client=http_client).model_ids()
    assert exc.value.code == "connection_error"
    assert exc.value.status_code == 503
