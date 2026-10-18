"""Image generation providers (§26).

Stability is already wired into the app as the `generate_image` tool; this
adapter talks to the same endpoint but returns bytes instead of writing a file
into `backend/data/generated_images`. That difference is the point: a Content
Factory asset belongs in object storage under its run's prefix (§44), with a
row in `cf_assets` recording provider, cost and checksum — not on the backend's
local disk where nothing can trace it back to the video it ended up in.

Adding a vendor is one subclass: a payload builder, a response reader, and a
per-image price.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.providers.base import (
    GeneratedAsset,
    Provider,
    ProviderCategory,
    ProviderResult,
    Usage,
    classify_http_error,
)

logger = logging.getLogger("hermes.content_factory.providers.image")

_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "3:2", "2:3", "4:5", "5:4", "21:9", "9:21"}


class ImageProvider(Provider):
    category = ProviderCategory.IMAGE
    usd_per_image = 0.0

    def __init__(
        self,
        provider_id: str,
        *,
        api_key: str = "",
        model: str = "",
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(provider_id, model=model, config=config)
        self._api_key = api_key
        self.usd_per_image = float(self.config.get("usdPerImage", self.usd_per_image))
        self.timeout = float(self.config.get("timeout", 60.0))

    def calculate_cost(self, usage: Usage) -> float:
        return round(usage.images_generated * self.usd_per_image, 6)

    def normalize_error(self, exc: Exception) -> ContentFactoryError:
        if isinstance(exc, ContentFactoryError):
            return exc
        return classify_http_error(exc)

    @staticmethod
    def _aspect_ratio(request: dict[str, Any]) -> str:
        ratio = str(request.get("aspect_ratio") or "")
        if ratio in _ASPECT_RATIOS:
            return ratio
        width, height = int(request.get("width", 1080)), int(request.get("height", 1920))
        return "9:16" if height > width else ("1:1" if height == width else "16:9")


class StabilityImageProvider(ImageProvider):
    usd_per_image = 0.03  # Stable Image Core, 3 credits @ $0.01

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "prompt is required", retryable=False)
        if not self._api_key:
            raise ContentFactoryError(ErrorCode.PROVIDER_AUTH, "STABILITY_API_KEY is not set", retryable=False)

        data: dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": self._aspect_ratio(request),
            "output_format": "png",
        }
        if request.get("style_preset"):
            data["style_preset"] = request["style_preset"]
        if request.get("negative_prompt"):
            data["negative_prompt"] = request["negative_prompt"]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # v2beta requires multipart even with no files; an empty `files`
                # dict is what makes httpx encode it that way.
                response = await client.post(
                    "https://api.stability.ai/v2beta/stable-image/generate/core",
                    headers={"authorization": f"Bearer {self._api_key}", "accept": "image/*"},
                    data=data,
                    files={"none": (None, "")},
                )
                response.raise_for_status()
                content = response.content
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        usage = Usage(images_generated=1)
        return ProviderResult(
            output={"prompt": prompt},
            assets=[
                GeneratedAsset(
                    type="IMAGE",
                    mime_type="image/png",
                    data=content,
                    filename="image.png",
                    metadata={"aspectRatio": data["aspect_ratio"]},
                )
            ],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model or "stable-image-core",
        )


class OpenAIImageProvider(ImageProvider):
    usd_per_image = 0.04

    def _api_base(self) -> str:
        return str(self.config.get("apiBase") or "https://api.openai.com").rstrip("/")

    def _payload(self, prompt: str, request: dict[str, Any]) -> dict[str, Any]:
        """Split out so a sibling endpoint can change the request body without
        re-implementing the response handling below."""
        return {
            "model": self.model or "gpt-image-1",
            "prompt": prompt,
            "size": request.get("size") or _openai_size(request),
            "n": 1,
        }

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        import base64

        prompt = str(request.get("prompt", "")).strip()
        if not prompt:
            raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "prompt is required", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self._api_base()}/v1/images/generations",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=self._payload(prompt, request),
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        entries = body.get("data") or []
        if not entries:
            raise ContentFactoryError(ErrorCode.PROVIDER_ERROR, "Image API returned no data")
        entry = entries[0]
        encoded = entry.get("b64_json")
        asset = GeneratedAsset(
            type="IMAGE",
            mime_type="image/png",
            data=base64.b64decode(encoded) if encoded else None,
            source_url="" if encoded else str(entry.get("url") or ""),
            filename="image.png",
        )
        usage = Usage(images_generated=1)
        return ProviderResult(
            output={"prompt": prompt},
            assets=[asset],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model or "gpt-image-1",
        )


class RouterImageProvider(OpenAIImageProvider):
    """The same OpenAI image contract, served by the OmniRoute sidecar.

    Worth its own entry rather than "point OpenAI Images at another base URL"
    for two reasons. It authenticates with `ROUTER_API_KEY` — already stored,
    so this provider configures itself the moment the sidecar has an upstream
    account — and it must not send `size`: OmniRoute forwards the payload to
    whichever vendor owns the model, and Gemini's image models reject the
    parameter outright rather than ignoring it. Aspect ratio for those goes in
    the prompt, which is what `_aspect_hint` appends.

    The model is a full OmniRoute id — `gemini/gemini-3-pro-image-preview`
    (Nano Banana Pro), `gemini/gemini-2.5-flash-image` — so which vendor
    actually renders the image is a settings choice, not a code change.
    """

    # Rides whatever plan the connected upstream account has; a per-image price
    # is set on the Integrations screen when that account is metered.
    usd_per_image = 0.0

    def _api_base(self) -> str:
        configured = str(self.config.get("apiBase") or "").strip()
        if configured:
            return configured.rstrip("/")
        return os.environ.get("ROUTER_API_BASE", "http://omniroute:20128").rstrip("/")

    def _payload(self, prompt: str, request: dict[str, Any]) -> dict[str, Any]:
        hint = _aspect_hint(request)
        return {
            "model": self.model or "gemini/gemini-2.5-flash-image",
            "prompt": f"{prompt}\n\n{hint}" if hint else prompt,
            "n": 1,
        }


def _aspect_hint(request: dict[str, Any]) -> str:
    """Aspect ratio as words, for an endpoint that takes no size parameter."""
    ratio = str(request.get("aspect_ratio") or "").strip()
    if ratio in _ASPECT_RATIOS:
        return f"Aspect ratio: {ratio}."
    width, height = int(request.get("width", 0) or 0), int(request.get("height", 0) or 0)
    if width and height:
        return f"Aspect ratio: {width}:{height}."
    return ""


def _openai_size(request: dict[str, Any]) -> str:
    width, height = int(request.get("width", 1024)), int(request.get("height", 1536))
    if height > width:
        return "1024x1536"
    if width > height:
        return "1536x1024"
    return "1024x1024"


class ReplicateImageProvider(ImageProvider):
    """Covers Flux and everything else Replicate hosts. Synchronous mode (`Prefer:
    wait`) keeps this an immediate provider — images finish in seconds, unlike
    video, so parking a node for them would cost more in bookkeeping than it
    saves."""

    usd_per_image = 0.03

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        version = request.get("version") or self.config.get("version") or self.model
        if not version:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "Replicate needs a model version id in the node config",
                retryable=False,
            )
        payload = {
            "version": version,
            "input": {
                "prompt": str(request.get("prompt", "")),
                "aspect_ratio": self._aspect_ratio(request),
                **(request.get("input") or {}),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.replicate.com/v1/predictions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Prefer": "wait",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        if body.get("status") == "failed":
            raise ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, str(body.get("error") or "Replicate prediction failed")
            )
        output = body.get("output")
        url = output[0] if isinstance(output, list) and output else str(output or "")
        if not url:
            raise ContentFactoryError(ErrorCode.PROVIDER_ERROR, "Replicate returned no image URL")

        usage = Usage(images_generated=1)
        return ProviderResult(
            output={"prompt": payload["input"]["prompt"]},
            assets=[GeneratedAsset(type="IMAGE", mime_type="image/png", source_url=url, filename="image.png")],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=str(version),
        )


_IMAGE_FACTORIES: dict[str, type[ImageProvider]] = {
    "stability": StabilityImageProvider,
    "openai-image": OpenAIImageProvider,
    "replicate-image": ReplicateImageProvider,
    "router-image": RouterImageProvider,
}


def build_image_providers() -> list[Provider]:
    """One provider per vendor that has a usable key.

    Keys, models and per-image prices come from the integrations store
    (Integrations screen first, `.env` as the fallback), so connecting Stability
    is a form submission rather than an edit to a file on the host followed by a
    restart. A vendor the owner switched off is simply absent, which is what a
    fallback chain needs in order to skip it instead of burning an attempt.
    """
    from backend.content_factory.integrations import resolve_media_provider

    providers: list[Provider] = []
    for provider_id, factory in _IMAGE_FACTORIES.items():
        resolved = resolve_media_provider(provider_id)
        if resolved is None:
            continue
        providers.append(
            factory(provider_id, api_key=resolved.api_key, model=resolved.model, config=resolved.config)
        )
    return providers
