"""Mock providers (§128).

Without these, exercising a pipeline end to end means paying a video vendor per
attempt, and the failure tests §129 demands (429, 500, timeout, invalid
response, duplicate webhook) could only be produced by getting a real provider
to misbehave. Every mock is deterministic given its config, so a test asserts
on exact output rather than on whatever a model felt like generating.

They are registered whenever `CONTENT_FACTORY_MOCK_PROVIDERS` is on, and are
the only providers a test ever touches.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Optional

from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.providers.base import (
    GeneratedAsset,
    JobStatus,
    Provider,
    ProviderCategory,
    ProviderJob,
    ProviderResult,
    Usage,
)
from backend.content_factory.providers.publishing import PublishingProvider


class _FaultConfig:
    """Shared failure-injection knobs, so §129's cases are one config away.

    `fail_times` fails the first N calls and then succeeds — that is what
    exercises retry (§20) and fallback (§21) without a real outage.
    """

    def __init__(self, config: dict[str, Any]):
        self.fail_times = int(config.get("failTimes", 0))
        self.fail_code = str(config.get("failCode", ErrorCode.PROVIDER_RATE_LIMIT))
        self.always_fail = bool(config.get("alwaysFail", False))
        self.latency = float(config.get("latency", 0.0))
        self.calls = 0

    async def tick(self, provider_id: str) -> None:
        self.calls += 1
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.always_fail or self.calls <= self.fail_times:
            raise ContentFactoryError(self.fail_code, f"{provider_id}: injected failure #{self.calls}")


def _seeded(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]


class MockLLMProvider(Provider):
    category = ProviderCategory.LLM

    def __init__(self, provider_id: str = "mock-llm", *, model: str = "mock-model",
                 config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, model=model, config=config)
        self.faults = _FaultConfig(self.config)

    @property
    def is_free(self) -> bool:
        return True

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        await self.faults.tick(self.id)
        schema = request.get("output_schema") or {}
        prompt = request.get("prompt") or ""
        # A canned response per schema keeps downstream nodes honest: they get a
        # shape that actually satisfies their contract, not free-form prose.
        canned = self.config.get("response")
        output = canned if canned is not None else _synthesize(schema, prompt)
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        usage = Usage(input_tokens=len(prompt) // 4, output_tokens=len(text) // 4)
        return ProviderResult(
            output={"text": text, "structured": None if isinstance(output, str) else output},
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model,
        )

    def calculate_cost(self, usage: Usage) -> float:
        return 0.0


def _synthesize(schema: dict[str, Any], prompt: str) -> Any:
    """Smallest value that satisfies a JSON Schema — enough for the structured
    contracts in §29/§30/§35 to validate."""
    if not schema:
        return f"mock response for: {prompt[:60]}"
    node_type = schema.get("type", "object")
    if node_type == "object":
        result: dict[str, Any] = {}
        properties = schema.get("properties", {})
        for key in schema.get("required", list(properties)):
            result[key] = _synthesize(properties.get(key, {}), prompt)
        return result
    if node_type == "array":
        return [_synthesize(schema.get("items", {}), prompt)]
    if node_type == "number":
        return 3.0
    if node_type == "integer":
        return 1
    if node_type == "boolean":
        return True
    enum = schema.get("enum")
    if enum:
        return enum[0]
    return f"mock-{_seeded(prompt)}"


class MockImageProvider(Provider):
    category = ProviderCategory.IMAGE

    def __init__(self, provider_id: str = "mock-image", *, model: str = "mock-diffusion",
                 config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, model=model, config=config)
        self.faults = _FaultConfig(self.config)

    @property
    def is_free(self) -> bool:
        return True

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        await self.faults.tick(self.id)
        prompt = str(request.get("prompt", ""))
        width = int(request.get("width", 1080))
        height = int(request.get("height", 1920))
        usage = Usage(images_generated=1)
        return ProviderResult(
            output={"prompt": prompt},
            assets=[
                GeneratedAsset(
                    type="IMAGE",
                    mime_type="image/png",
                    data=_PNG_1PX,
                    filename=f"mock-{_seeded(prompt)}.png",
                    width=width,
                    height=height,
                )
            ],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model,
        )


class MockVoiceProvider(Provider):
    category = ProviderCategory.VOICE

    def __init__(self, provider_id: str = "mock-voice", *, model: str = "mock-tts",
                 config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, model=model, config=config)
        self.faults = _FaultConfig(self.config)

    @property
    def is_free(self) -> bool:
        return True

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        await self.faults.tick(self.id)
        text = str(request.get("text", ""))
        # ~15 characters per second of speech: close enough that duration-driven
        # logic downstream (scene timing, §28) is exercised realistically.
        duration = round(max(len(text), 1) / 15.0, 2)
        usage = Usage(seconds_generated=duration)
        return ProviderResult(
            output={"duration": duration},
            assets=[
                GeneratedAsset(
                    type="VOICE",
                    mime_type="audio/wav",
                    data=_WAV_SILENCE,
                    filename=f"mock-{_seeded(text)}.wav",
                    duration=duration,
                )
            ],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model,
        )


class MockVideoProvider(Provider):
    """Submitted-job provider (§25), so the WAITING_EXTERNAL path is covered by
    tests without waiting on a real vendor for minutes."""

    category = ProviderCategory.VIDEO

    def __init__(self, provider_id: str = "mock-video", *, model: str = "mock-gen",
                 config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, model=model, config=config)
        self.faults = _FaultConfig(self.config)
        # Seconds a submitted job stays RUNNING before completing. 0 = ready on
        # the first poll, which is what most tests want.
        self.job_duration = float(self.config.get("jobDuration", 0.0))
        self._jobs: dict[str, dict[str, Any]] = {}

    @property
    def is_async_job(self) -> bool:
        return True

    @property
    def is_free(self) -> bool:
        return True

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        job = await self.submit(request)
        return await self.get_result(job.external_job_id)

    async def submit(self, request: dict[str, Any]) -> ProviderJob:
        await self.faults.tick(self.id)
        prompt = str(request.get("prompt", ""))
        job_id = f"mockjob-{_seeded(prompt, len(self._jobs))}"
        self._jobs[job_id] = {
            "prompt": prompt,
            "duration": float(request.get("duration", 4.0)),
            "ready_at": time.monotonic() + self.job_duration,
            "cancelled": False,
        }
        return ProviderJob(external_job_id=job_id, status=JobStatus.PENDING)

    async def get_status(self, job_id: str) -> ProviderJob:
        job = self._jobs.get(job_id)
        if not job:
            raise ContentFactoryError(ErrorCode.PROVIDER_ERROR, f"Unknown job {job_id}", retryable=False)
        if job["cancelled"]:
            return ProviderJob(external_job_id=job_id, status=JobStatus.CANCELLED)
        if time.monotonic() >= job["ready_at"]:
            return ProviderJob(external_job_id=job_id, status=JobStatus.COMPLETED, progress=100)
        return ProviderJob(external_job_id=job_id, status=JobStatus.RUNNING, progress=50)

    async def get_result(self, job_id: str) -> ProviderResult:
        job = self._jobs.get(job_id)
        if not job:
            raise ContentFactoryError(ErrorCode.PROVIDER_ERROR, f"Unknown job {job_id}", retryable=False)
        duration = job["duration"]
        usage = Usage(video_seconds=duration)
        return ProviderResult(
            output={"prompt": job["prompt"], "duration": duration},
            assets=[
                GeneratedAsset(
                    type="VIDEO",
                    mime_type="video/mp4",
                    data=b"\x00\x00\x00\x18ftypmp42mock",
                    filename=f"{job_id}.mp4",
                    duration=duration,
                    width=1080,
                    height=1920,
                )
            ],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model,
        )

    async def cancel(self, job_id: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["cancelled"] = True


class MockPublishingProvider(PublishingProvider):
    """Records what would have been published.

    Subclasses the real `PublishingProvider` rather than `Provider` on purpose:
    the dry-run short-circuit (§108) and the "already has an external id, do
    not upload again" check (§55) are the guard rails that must not regress, so
    the mock has to run through exactly the same ones the live adapters do. A
    mock that reimplemented `execute` would test the guard rails' absence.
    """

    platform = "MOCK"

    def __init__(self, provider_id: str = "mock-publish", *, model: str = "",
                 config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, config=config)
        self.model = model
        self.faults = _FaultConfig(self.config)
        self.published: list[dict[str, Any]] = []

    async def publish(self, request: dict[str, Any]) -> ProviderResult:
        await self.faults.tick(self.id)
        self.published.append(dict(request))
        external_id = f"mock-video-{_seeded(request.get('title', ''), len(self.published))}"
        return ProviderResult(
            output={
                "externalId": external_id,
                "externalUrl": self.external_url(external_id),
                "publishedAt": None,
            },
            provider=self.id,
        )

    def external_url(self, external_id: str) -> str:
        return f"https://example.invalid/{external_id}"

    async def get_analytics(self, external_id: str, credentials_ref: str = "") -> dict[str, Any]:
        return {"views": 0, "likes": 0, "comments": 0, "shares": 0}


# A 1x1 transparent PNG and a minimal silent WAV header: real enough for
# mime/type checks and ffprobe smoke tests, small enough to keep in source.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae426082"
)
_WAV_SILENCE = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x44\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)
