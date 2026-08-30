"""Provider abstraction (§23).

Every external service — LLM, image, video, voice, publishing, analytics —
reaches the pipeline through one of these. A node never imports a vendor SDK
and never sees a vendor's response shape (§24): it asks a provider for a
result, and gets either a `ProviderResult` or a `ContentFactoryError` with a
code it can act on.

Two execution shapes, because paid media APIs come in both:

* **Immediate** — `execute()` returns the result. LLM calls, TTS, most image
  APIs.
* **Submitted** — `submit()` returns a job id, `get_status()` is polled and
  `get_result()` collects the output (§25). Video generation and rendering can
  run for minutes; holding a worker (or an HTTP request, §91) open for that is
  not an option. The job id is persisted by the engine before the first poll,
  so a restart resumes the same paid job instead of starting a second one.

Cost (§59) is the provider's own responsibility: only the adapter knows whether
it is billed per token, per second of audio, per image or per second of video.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.content_factory.domain import ContentFactoryError, ErrorCode

logger = logging.getLogger("hermes.content_factory.providers")


class ProviderCategory:
    """§23."""

    LLM = "LLM"
    SEARCH = "SEARCH"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    VOICE = "VOICE"
    MUSIC = "MUSIC"
    MODERATION = "MODERATION"
    PUBLISHING = "PUBLISHING"
    ANALYTICS = "ANALYTICS"
    STORAGE = "STORAGE"


class JobStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class ProviderJob:
    """§25."""

    external_job_id: str
    status: str = JobStatus.PENDING
    progress: int = 0
    estimated_completion: Optional[str] = None
    error: Optional[ContentFactoryError] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedAsset:
    """One produced artifact. Either `data` (bytes we already hold) or
    `source_url` (bytes the provider is hosting and we must fetch) is set —
    never neither. The engine is what moves it into object storage; a provider
    never writes to the asset store itself, so storage stays swappable."""

    type: str
    mime_type: str = ""
    data: Optional[bytes] = None
    source_url: str = ""
    filename: str = ""
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    """What was consumed, in whichever unit this provider bills (§59)."""

    input_tokens: int = 0
    output_tokens: int = 0
    seconds_generated: float = 0.0
    images_generated: int = 0
    video_seconds: float = 0.0
    requests: int = 1


@dataclass
class ProviderResult:
    output: dict[str, Any] = field(default_factory=dict)
    assets: list[GeneratedAsset] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    provider: str = ""
    model: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    """Base for every adapter.

    `id` is what a pipeline node references (§21 fallback lists are lists of
    these), so it must stay stable across restarts and deployments.
    """

    category: str = ""

    def __init__(self, provider_id: str, *, model: str = "", config: Optional[dict[str, Any]] = None):
        self.id = provider_id
        self.model = model
        self.config = config or {}

    # ── Capability flags ─────────────────────────────────────────────────
    @property
    def is_async_job(self) -> bool:
        """True for submit/poll providers (§25). The engine parks the node in
        WAITING_EXTERNAL for these instead of awaiting inline."""
        return False

    @property
    def is_free(self) -> bool:
        """Local/self-hosted providers cost nothing and are exempt from budget
        checks — the same distinction `llm_client._is_local_provider` makes."""
        return False

    # ── Execution ────────────────────────────────────────────────────────
    @abstractmethod
    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        """Immediate execution. Async-job providers implement this as
        submit-then-error-if-called-inline; the engine uses submit() for them."""

    async def submit(self, request: dict[str, Any]) -> ProviderJob:
        raise NotImplementedError(f"{self.id} does not support submitted jobs")

    async def get_status(self, job_id: str) -> ProviderJob:
        raise NotImplementedError(f"{self.id} does not support submitted jobs")

    async def get_result(self, job_id: str) -> ProviderResult:
        raise NotImplementedError(f"{self.id} does not support submitted jobs")

    async def cancel(self, job_id: str) -> None:
        """Best-effort cancellation (§94). Never raises: a run being cancelled
        must not fail because the provider refused to acknowledge it."""
        logger.info("Provider %s: cancel(%s) is a no-op", self.id, job_id)

    # ── Cost & errors ────────────────────────────────────────────────────
    def calculate_cost(self, usage: Usage) -> float:
        """USD for this usage. Zero by default so a misconfigured adapter
        under-reports rather than inventing charges; real adapters override."""
        return 0.0

    def normalize_error(self, exc: Exception) -> ContentFactoryError:
        """Vendor exception → an error code the UI can switch on (§76, §103)."""
        if isinstance(exc, ContentFactoryError):
            return exc
        return classify_http_error(exc)

    async def health(self) -> dict[str, Any]:
        """Cheap liveness signal for the Providers settings page (§62). Must not
        perform a billable generation."""
        return {"provider": self.id, "status": "unknown"}


def classify_http_error(exc: Exception) -> ContentFactoryError:
    """Maps httpx / generic transport failures onto §76 error codes.

    Kept as a free function so every adapter classifies the same way — a rate
    limit must look identical whether it came from Runway or ElevenLabs, since
    §93 decides between WAITING and FAILED on exactly this distinction.
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return ContentFactoryError(ErrorCode.PROVIDER_TIMEOUT, "Provider request timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return ContentFactoryError(
                ErrorCode.PROVIDER_RATE_LIMIT,
                "Provider rate limit exceeded",
                details=_retry_after(exc.response),
            )
        if status in (401, 403):
            return ContentFactoryError(
                ErrorCode.PROVIDER_AUTH, "Provider rejected the credentials", retryable=False
            )
        if status in (402, 413, 422):
            return ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, f"Provider rejected the request (HTTP {status})", retryable=False
            )
        if status >= 500:
            return ContentFactoryError(ErrorCode.PROVIDER_UNAVAILABLE, f"Provider error (HTTP {status})")
        return ContentFactoryError(ErrorCode.PROVIDER_ERROR, f"Provider error (HTTP {status})", retryable=False)
    if isinstance(exc, httpx.HTTPError):
        return ContentFactoryError(ErrorCode.PROVIDER_UNAVAILABLE, "Provider is unreachable")
    return ContentFactoryError(ErrorCode.INTERNAL, "Unexpected provider failure", retryable=False)


def _retry_after(response: Any) -> dict[str, Any]:
    raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
    try:
        return {"retryAfter": float(raw)} if raw else {}
    except (TypeError, ValueError):
        return {}
