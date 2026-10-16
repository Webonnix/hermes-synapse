"""Image generation routed through the 9Router sidecar.

The point of the provider is that *which vendor renders the image* stops being
a code change: the model id carries it (`gemini/gemini-3-pro-image-preview`,
`gemini/gemini-2.5-flash-image`), and 9Router forwards to whichever upstream
account is connected. Two request-shape rules make that work and are what these
tests pin, because both fail as an opaque vendor 400 several layers away:

* no `size` parameter — Gemini's image models reject it outright rather than
  ignoring it, so aspect ratio goes into the prompt instead;
* the base URL comes from ROUTER_API_BASE unless the screen overrode it, so a
  deployment that renamed the sidecar does not have to re-enter it here.
"""

import httpx
import pytest

from backend.content_factory.providers.image import (
    OpenAIImageProvider,
    RouterImageProvider,
)


def _provider(**config) -> RouterImageProvider:
    return RouterImageProvider(
        "router-image", api_key="sk-router", model="gemini/gemini-2.5-flash-image", config=config,
    )


def test_payload_carries_no_size_parameter():
    payload = _provider()._payload("a cat", {"width": 1024, "height": 1536})
    assert "size" not in payload
    assert payload["model"] == "gemini/gemini-2.5-flash-image"


def test_aspect_ratio_goes_into_the_prompt():
    payload = _provider()._payload("a cat", {"aspect_ratio": "9:16"})
    assert payload["prompt"].startswith("a cat")
    assert "9:16" in payload["prompt"]


def test_pixel_dimensions_become_an_aspect_hint():
    payload = _provider()._payload("a cat", {"width": 1024, "height": 1536})
    assert "1024:1536" in payload["prompt"]


def test_a_prompt_with_no_size_information_is_left_alone():
    assert _provider()._payload("a cat", {})["prompt"] == "a cat"


def test_base_url_comes_from_the_environment_by_default(monkeypatch):
    monkeypatch.setenv("ROUTER_API_BASE", "http://9router:20128")
    assert _provider()._api_base() == "http://9router:20128"


def test_a_configured_base_url_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("ROUTER_API_BASE", "http://9router:20128")
    assert _provider(apiBase="http://elsewhere:9000/")._api_base() == "http://elsewhere:9000"


def test_the_openai_provider_still_sends_a_size():
    """The hook this provider overrides must not have changed the vendor it was
    split out of."""
    provider = OpenAIImageProvider("openai-image", api_key="sk", model="gpt-image-1")
    assert provider._payload("a cat", {"width": 1024, "height": 1536})["size"] == "1024x1536"


@pytest.mark.asyncio
async def test_generated_image_bytes_are_returned_as_an_asset(monkeypatch):
    import base64

    encoded = base64.b64encode(b"png-bytes").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer sk-router"
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await _provider(apiBase="http://9router:20128").execute({"prompt": "a cat"})
    assert result.assets[0].data == b"png-bytes"
    assert result.provider == "router-image"


def test_it_is_free_by_default():
    """It rides whatever plan the connected upstream account has; a per-image
    price is set on the Integrations screen when that account is metered."""
    assert _provider().usd_per_image == 0.0
