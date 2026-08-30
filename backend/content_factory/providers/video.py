"""Video generation providers (§25).

All four vendors here work the same way — submit a prompt, poll a task id,
collect a URL — so the HTTP mechanics, error classification, timeouts and
cost accounting live in `SubmittedVideoProvider`, and each vendor class is only
its endpoints and its request/response field names.

**Endpoint shapes are vendor-versioned and change.** Every path, header and
field name below is overridable through the provider's config so a vendor's API
revision is a configuration change, not a code change — and so a deployment can
correct one without waiting on a release. Verify them against the vendor's
current documentation before enabling a provider in production; a wrong field
name surfaces as PROVIDER_ERROR on the first submit, not silently.

The engine never blocks on these: `submit()` returns a job id that is persisted
before the first poll (see `NodeResult.external_job_id`), so a backend restart
resumes the same paid job instead of paying for a second one.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.providers.base import (
    GeneratedAsset,
    JobStatus,
    Provider,
    ProviderCategory,
    ProviderJob,
    ProviderResult,
    Usage,
    classify_http_error,
)

logger = logging.getLogger("hermes.content_factory.providers.video")

_DEFAULT_TIMEOUT = 60.0


class SubmittedVideoProvider(Provider):
    """Base for submit/poll video APIs."""

    category = ProviderCategory.VIDEO

    # Overridden per vendor; every one of these may also be set from config.
    api_base = ""
    submit_path = ""
    status_path = ""  # formatted with {job_id}
    cancel_path = ""
    auth_header = "Authorization"
    auth_scheme = "Bearer"
    extra_headers: dict[str, str] = {}
    # USD per second of generated video; 0 until the owner sets a real rate,
    # so an unpriced provider under-reports rather than inventing charges.
    usd_per_second = 0.0

    def __init__(
        self,
        provider_id: str,
        *,
        api_key: str = "",
        model: str = "",
        binding_id: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(provider_id, model=model, config=config)
        self._api_key = api_key
        self.binding_id = binding_id
        for attribute in ("api_base", "submit_path", "status_path", "cancel_path", "auth_header", "auth_scheme"):
            override = self.config.get(attribute)
            if override:
                setattr(self, attribute, override)
        self.usd_per_second = float(self.config.get("usdPerSecond", self.usd_per_second))
        self.timeout = float(self.config.get("timeout", _DEFAULT_TIMEOUT))

    @property
    def is_async_job(self) -> bool:
        return True

    # ── HTTP plumbing ────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        key = self._resolve_key()
        headers = {"Content-Type": "application/json", **self.extra_headers}
        headers[self.auth_header] = f"{self.auth_scheme} {key}".strip()
        return headers

    def _resolve_key(self) -> str:
        if self.binding_id:
            from backend.provider_governance import resolve_binding_credentials

            resolved = resolve_binding_credentials(self.binding_id)
            if not resolved:
                raise ContentFactoryError(
                    ErrorCode.PROVIDER_AUTH,
                    f"Provider '{self.id}' has no usable credentials",
                    retryable=False,
                )
            return resolved[1]
        if not self._api_key:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH, f"Provider '{self.id}' has no API key", retryable=False
            )
        return self._api_key

    async def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = f"{self.api_base.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, headers=self._headers(), json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
        except ContentFactoryError:
            raise
        except Exception as exc:  # httpx and JSON decoding alike
            raise self.normalize_error(exc) from exc

    # ── Contract ─────────────────────────────────────────────────────────
    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        """Inline execution is not offered: a video job runs for minutes and
        would hold a worker (§91). The engine parks the node instead."""
        raise ContentFactoryError(
            ErrorCode.INTERNAL,
            f"{self.id} is a submitted-job provider; use submit()/get_status()/get_result()",
            retryable=False,
        )

    def build_submit_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def parse_submit(self, body: dict[str, Any]) -> str:
        raise NotImplementedError

    def parse_status(self, body: dict[str, Any]) -> ProviderJob:
        raise NotImplementedError

    def parse_result(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """(video_url, metadata)."""
        raise NotImplementedError

    async def submit(self, request: dict[str, Any]) -> ProviderJob:
        body = await self._request("POST", self.submit_path, self.build_submit_payload(request))
        job_id = self.parse_submit(body)
        if not job_id:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, f"{self.id} did not return a job id", retryable=False
            )
        return ProviderJob(external_job_id=job_id, status=JobStatus.PENDING, metadata={"request": request})

    async def get_status(self, job_id: str) -> ProviderJob:
        body = await self._request("GET", self.status_path.format(job_id=job_id))
        job = self.parse_status(body)
        job.external_job_id = job_id
        return job

    async def get_result(self, job_id: str) -> ProviderResult:
        body = await self._request("GET", self.status_path.format(job_id=job_id))
        url, metadata = self.parse_result(body)
        if not url:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, f"{self.id} job {job_id} finished without an output URL"
            )
        duration = float(metadata.get("duration") or 0.0)
        usage = Usage(video_seconds=duration)
        return ProviderResult(
            output={"url": url, "duration": duration},
            assets=[
                GeneratedAsset(
                    type="VIDEO",
                    mime_type="video/mp4",
                    source_url=url,
                    filename=f"{job_id}.mp4",
                    duration=duration or None,
                    width=metadata.get("width"),
                    height=metadata.get("height"),
                    metadata=metadata,
                )
            ],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=self.model,
            raw_metadata=metadata,
        )

    async def cancel(self, job_id: str) -> None:
        if not self.cancel_path:
            return
        try:
            await self._request("DELETE", self.cancel_path.format(job_id=job_id))
        except Exception:
            # §94: a cancelled run must complete its own cancellation even if
            # the vendor refuses. The unstopped job is logged, not raised.
            logger.warning("%s: could not cancel job %s", self.id, job_id, exc_info=True)

    def calculate_cost(self, usage: Usage) -> float:
        return round(usage.video_seconds * self.usd_per_second, 6)

    def normalize_error(self, exc: Exception) -> ContentFactoryError:
        if isinstance(exc, ContentFactoryError):
            return exc
        return classify_http_error(exc)


class RunwayProvider(SubmittedVideoProvider):
    api_base = "https://api.dev.runwayml.com"
    submit_path = "/v1/image_to_video"
    status_path = "/v1/tasks/{job_id}"
    cancel_path = "/v1/tasks/{job_id}"
    extra_headers = {"X-Runway-Version": "2024-11-06"}
    usd_per_second = 0.05

    def build_submit_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": request.get("model") or self.model or "gen3a_turbo",
            "promptText": request.get("prompt", ""),
            "duration": int(request.get("duration", 5)),
            "ratio": request.get("ratio", "768:1280"),
        }
        image_url = request.get("image_url")
        if image_url:
            payload["promptImage"] = image_url
        return payload

    def parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or "")

    def parse_status(self, body: dict[str, Any]) -> ProviderJob:
        state = str(body.get("status") or "").upper()
        mapping = {
            "PENDING": JobStatus.PENDING,
            "THROTTLED": JobStatus.PENDING,
            "RUNNING": JobStatus.RUNNING,
            "SUCCEEDED": JobStatus.COMPLETED,
            "FAILED": JobStatus.FAILED,
            "CANCELLED": JobStatus.CANCELLED,
        }
        job = ProviderJob(external_job_id="", status=mapping.get(state, JobStatus.RUNNING))
        job.progress = int(float(body.get("progress") or 0) * 100)
        if job.status == JobStatus.FAILED:
            job.error = ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, str(body.get("failure") or "Runway job failed")
            )
        return job

    def parse_result(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        outputs = body.get("output") or []
        url = outputs[0] if isinstance(outputs, list) and outputs else str(outputs or "")
        return url, {"duration": body.get("duration"), "vendorStatus": body.get("status")}


class KlingProvider(SubmittedVideoProvider):
    """Kling authenticates with a JWT signed from an access key/secret pair.

    The signing helper is intentionally small and local: pulling in a JWT
    library for one HS256 token would add a dependency to the shared image for
    no other consumer.
    """

    api_base = "https://api.klingai.com"
    submit_path = "/v1/videos/text2video"
    status_path = "/v1/videos/text2video/{job_id}"
    usd_per_second = 0.07

    def _resolve_key(self) -> str:
        access_key = self.config.get("accessKey") or os.environ.get("KLING_ACCESS_KEY", "")
        secret = super()._resolve_key()
        if not access_key:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH, "Kling requires an access key id", retryable=False
            )
        return _sign_jwt(access_key, secret)

    def build_submit_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "model_name": request.get("model") or self.model or "kling-v1",
            "prompt": request.get("prompt", ""),
            "duration": str(int(request.get("duration", 5))),
            "aspect_ratio": request.get("aspect_ratio", "9:16"),
        }

    def parse_submit(self, body: dict[str, Any]) -> str:
        return str((body.get("data") or {}).get("task_id") or "")

    def parse_status(self, body: dict[str, Any]) -> ProviderJob:
        data = body.get("data") or {}
        state = str(data.get("task_status") or "").lower()
        mapping = {
            "submitted": JobStatus.PENDING,
            "processing": JobStatus.RUNNING,
            "succeed": JobStatus.COMPLETED,
            "failed": JobStatus.FAILED,
        }
        job = ProviderJob(external_job_id="", status=mapping.get(state, JobStatus.RUNNING))
        if job.status == JobStatus.FAILED:
            job.error = ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, str(data.get("task_status_msg") or "Kling job failed")
            )
        return job

    def parse_result(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        data = body.get("data") or {}
        videos = (data.get("task_result") or {}).get("videos") or []
        if not videos:
            return "", {}
        video = videos[0]
        return str(video.get("url") or ""), {"duration": video.get("duration"), "vendorId": video.get("id")}


class PikaProvider(SubmittedVideoProvider):
    api_base = "https://api.pika.art"
    submit_path = "/v1/generate"
    status_path = "/v1/videos/{job_id}"
    usd_per_second = 0.06

    def build_submit_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "promptText": request.get("prompt", ""),
            "model": request.get("model") or self.model or "pika-1.5",
            "aspectRatio": request.get("aspect_ratio", "9:16"),
            "duration": int(request.get("duration", 5)),
        }

    def parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or body.get("video_id") or "")

    def parse_status(self, body: dict[str, Any]) -> ProviderJob:
        state = str(body.get("status") or "").lower()
        mapping = {
            "queued": JobStatus.PENDING,
            "pending": JobStatus.PENDING,
            "generating": JobStatus.RUNNING,
            "processing": JobStatus.RUNNING,
            "finished": JobStatus.COMPLETED,
            "completed": JobStatus.COMPLETED,
            "failed": JobStatus.FAILED,
        }
        job = ProviderJob(external_job_id="", status=mapping.get(state, JobStatus.RUNNING))
        job.progress = int(body.get("progress") or 0)
        if job.status == JobStatus.FAILED:
            job.error = ContentFactoryError(ErrorCode.PROVIDER_ERROR, "Pika job failed")
        return job

    def parse_result(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return str(body.get("resultUrl") or body.get("url") or ""), {"duration": body.get("duration")}


class ReplicateVideoProvider(SubmittedVideoProvider):
    """Replicate fronts many video models behind one prediction API, which makes
    it the cheapest way to add a fifth or sixth model without a new adapter."""

    api_base = "https://api.replicate.com"
    submit_path = "/v1/predictions"
    status_path = "/v1/predictions/{job_id}"
    cancel_path = "/v1/predictions/{job_id}/cancel"
    auth_scheme = "Bearer"
    usd_per_second = 0.04

    def build_submit_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        version = request.get("version") or self.config.get("version") or self.model
        if not version:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "Replicate needs a model version id in the node config",
                retryable=False,
            )
        return {
            "version": version,
            "input": {
                "prompt": request.get("prompt", ""),
                "aspect_ratio": request.get("aspect_ratio", "9:16"),
                "duration": int(request.get("duration", 5)),
                **(request.get("input") or {}),
            },
        }

    def parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or "")

    def parse_status(self, body: dict[str, Any]) -> ProviderJob:
        state = str(body.get("status") or "").lower()
        mapping = {
            "starting": JobStatus.PENDING,
            "processing": JobStatus.RUNNING,
            "succeeded": JobStatus.COMPLETED,
            "failed": JobStatus.FAILED,
            "canceled": JobStatus.CANCELLED,
        }
        job = ProviderJob(external_job_id="", status=mapping.get(state, JobStatus.RUNNING))
        if job.status == JobStatus.FAILED:
            job.error = ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, str(body.get("error") or "Replicate prediction failed")
            )
        return job

    def parse_result(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        output = body.get("output")
        if isinstance(output, list):
            url = str(output[0]) if output else ""
        else:
            url = str(output or "")
        metrics = body.get("metrics") or {}
        return url, {"duration": metrics.get("predict_time"), "vendorStatus": body.get("status")}


_VENDORS: dict[str, type[SubmittedVideoProvider]] = {
    "runway": RunwayProvider,
    "kling": KlingProvider,
    "pika": PikaProvider,
    "replicate": ReplicateVideoProvider,
}


def build_video_providers() -> list[Provider]:
    """One provider per vendor that has a usable key.

    Keys and per-second prices come from the integrations store — the
    Integrations screen writes there, and the `*_API_KEY` / `*_USD_PER_SECOND`
    environment variables remain the fallback, so an existing deployment keeps
    the providers it already had.
    """
    from backend.content_factory.integrations import resolve_media_provider

    providers: list[Provider] = []
    for vendor, factory in _VENDORS.items():
        resolved = resolve_media_provider(vendor)
        if resolved is None:
            continue
        providers.append(
            factory(vendor, api_key=resolved.api_key, model=resolved.model, config=resolved.config)
        )
    return providers


def _sign_jwt(access_key: str, secret: str) -> str:
    """Minimal HS256 JWT with the 30-minute validity Kling expects."""
    import base64
    import hashlib
    import hmac
    import json
    import time

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(
        json.dumps(
            {"iss": access_key, "exp": now + 1800, "nbf": now - 5}, separators=(",", ":")
        ).encode()
    )
    signing_input = header + b"." + payload
    signature = b64(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + signature).decode()
