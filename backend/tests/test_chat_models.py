"""Unit tests for backend/chat_models.py — the chat model picker's catalogue and,
more importantly, the resolution of a picked entry back into an endpoint.

The resolution half is a security boundary: a chat frame names a model *id*,
never a URL, and only ids the server itself has just seen in the live catalogue
may resolve. Anything else falls back to the configured default rather than
sending the conversation (and an API key) somewhere the client chose.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend import chat_models


LOCAL_MODELS = [
    {"id": "qwen3.8-27b", "object": "model", "owned_by": "llamacpp",
     "meta": {"size": 17_678_000_000, "n_params": 27_000_000_000, "n_ctx_train": 262144}},
]

PROPS = {"default_generation_settings": {"n_ctx": 196608}, "total_slots": 1}

ROUTER_MODELS = {
    "data": [
        {"id": "kimi/kimi-k3", "owned_by": "kimi", "capabilities": {"tools": True}},
        {"id": "ds/deepseek-chat", "owned_by": "ds"},
        {"id": "kimi/kimi-k3", "owned_by": "kimi"},  # duplicate — collapsed
    ],
}

LOCAL_BASE = "http://hermes-llama:8080"


def _resp(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


@pytest.fixture(autouse=True)
def _clear_cache():
    chat_models.invalidate_cache()
    yield
    chat_models.invalidate_cache()


def _patch_sources(*, local=LOCAL_MODELS, local_error=None, props=PROPS, props_error=None,
                   router=ROUTER_MODELS, router_key="rtr_key", router_status=200, router_error=None):
    """Both upstreams stubbed at their client boundary."""
    get = AsyncMock(side_effect=router_error) if router_error else AsyncMock(
        return_value=_resp(router_status, router),
    )
    list_models = AsyncMock(side_effect=local_error) if local_error else AsyncMock(return_value=local)
    props_mock = AsyncMock(side_effect=props_error) if props_error else AsyncMock(return_value=props)
    return (
        patch("backend.llamacpp_client.LlamaCppClient.list_models", new=list_models),
        patch("backend.llamacpp_client.LlamaCppClient.props", new=props_mock),
        patch("backend.database.get_api_key", return_value=router_key),
        patch("httpx.AsyncClient.get", new=get),
    )


async def _catalogue(**kwargs):
    a, b, c, d = _patch_sources(**kwargs)
    with a, b, c, d:
        return await chat_models.list_chat_models(
            local_base_url=LOCAL_BASE,
            default_model="qwen3.8-27b",
            default_provider="llamacpp",
        )


@pytest.mark.asyncio
async def test_lists_both_sources_separately():
    result = await _catalogue()

    assert [m["id"] for m in result["local"]] == ["qwen3.8-27b"]
    # Router entries are deduplicated and sorted by id.
    assert [m["id"] for m in result["router"]] == ["ds/deepseek-chat", "kimi/kimi-k3"]
    assert result["local_error"] is None and result["router_error"] is None
    assert result["local_hidden"] == []


@pytest.mark.asyncio
async def test_local_entry_carries_the_server_details_the_picker_shows():
    result = await _catalogue()

    qwen = result["local"][0]
    assert qwen["source"] == "local"
    assert qwen["owned_by"] == "llama.cpp"
    assert qwen["parameter_size"] == "27.0B"
    assert qwen["size_label"] == "16.5 GB"
    # The context the server actually allocated, not the model's training max.
    assert qwen["context_length"] == 196608


@pytest.mark.asyncio
async def test_the_served_model_is_always_resident():
    result = await _catalogue()
    assert all(m["loaded"] is True for m in result["local"])


@pytest.mark.asyncio
async def test_a_dead_props_endpoint_costs_only_the_context_badge():
    result = await _catalogue(props_error=OSError("refused"))

    assert [m["id"] for m in result["local"]] == ["qwen3.8-27b"]
    assert result["local"][0]["context_length"] is None
    assert result["local_error"] is None


@pytest.mark.asyncio
async def test_router_entry_keeps_the_namespaced_id_but_shows_the_short_name():
    result = await _catalogue()

    kimi = next(m for m in result["router"] if m["id"] == "kimi/kimi-k3")
    # The id must survive verbatim — 9Router 404s on an unprefixed model.
    assert kimi["id"] == "kimi/kimi-k3"
    assert kimi["name"] == "kimi-k3"
    assert kimi["owned_by"] == "kimi"
    assert kimi["capabilities"] == {"tools": True}


@pytest.mark.asyncio
async def test_one_dead_source_does_not_take_the_other_down():
    result = await _catalogue(router_error=httpx.ConnectError("refused"))

    assert [m["id"] for m in result["local"]] == ["qwen3.8-27b"]
    assert result["router"] == []
    assert "OmniRoute" in result["router_error"]


@pytest.mark.asyncio
async def test_missing_router_key_is_reported_not_raised():
    result = await _catalogue(router_key=None)

    assert result["router"] == []
    assert "ROUTER_API_KEY" in result["router_error"]


@pytest.mark.asyncio
async def test_local_server_failure_is_reported_without_losing_the_router_half():
    result = await _catalogue(local_error=OSError("connection refused"))

    assert result["local"] == []
    assert "llama.cpp" in result["local_error"]
    assert len(result["router"]) == 2


@pytest.mark.asyncio
async def test_default_entry_is_annotated_with_the_source_it_was_found_in():
    result = await _catalogue()
    assert result["default"] == {"id": "qwen3.8-27b", "source": "local", "name": "qwen3.8-27b"}


# ─── Resolution ─────────────────────────────────────────────────────────────

async def _resolve(source, model_id, **kwargs):
    a, b, c, d = _patch_sources(**kwargs)
    with a, b, c, d:
        return await chat_models.resolve_chat_model(
            source, model_id, local_base_url=LOCAL_BASE, api_key="local-key",
        )


@pytest.mark.asyncio
async def test_local_pick_resolves_to_the_llamacpp_endpoint():
    resolved = await _resolve("local", "qwen3.8-27b")

    assert resolved == {
        "source": "local",
        "model": "qwen3.8-27b",
        "api_base": LOCAL_BASE,
        "api_key": "local-key",
        "provider": "llamacpp",
    }


@pytest.mark.asyncio
async def test_router_pick_resolves_to_the_sidecar_with_its_own_key(monkeypatch):
    monkeypatch.setenv("ROUTER_API_BASE", "http://9router:20128")

    resolved = await _resolve("router", "kimi/kimi-k3")

    assert resolved["api_base"] == "http://9router:20128/v1"
    assert resolved["api_key"] == "rtr_key"
    assert resolved["provider"] == "openai_compatible"
    assert resolved["model"] == "kimi/kimi-k3"


@pytest.mark.asyncio
async def test_an_id_outside_the_catalogue_resolves_to_nothing():
    # The whole point of resolving server-side: a frame naming a model the
    # server has never seen falls back to the default instead of being trusted.
    assert await _resolve("router", "evil/anything") is None
    # An Ollama tag remembered in a browser from before the migration is just
    # another unknown id.
    assert await _resolve("local", "unsloth-mtp-q4km:latest") is None


@pytest.mark.asyncio
async def test_a_local_id_claimed_as_router_does_not_cross_over():
    # Sending the real local model with source=router must not reach the router
    # endpoint — the id has to exist in the source it claims.
    assert await _resolve("router", "qwen3.8-27b") is None


@pytest.mark.asyncio
async def test_no_pick_means_the_configured_default():
    assert await _resolve("local", "") is None
    assert await _resolve("", "") is None


@pytest.mark.asyncio
async def test_a_local_pick_resolves_to_nothing_while_the_server_is_down():
    assert await _resolve("local", "qwen3.8-27b", local_error=OSError("refused")) is None
