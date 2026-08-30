"""Publishing providers (§51–53).

The contract (§53) is small on purpose: `publish`, `get_status`,
`update_metadata`, `delete`, `get_analytics`. Everything platform-specific —
resumable uploads, chunk sizes, status vocabularies, privacy field names —
stays inside its adapter, so the pipeline never grows a `if platform ==
"tiktok"` branch (§52).

Three rules this module enforces on behalf of the engine:

* **Never publish during a test run.** `dry_run` short-circuits before any
  network call (§108). The engine sets it from the run, not from node config.
* **Never publish twice.** The adapter is handed an `idempotency_key` and an
  already-known `external_id`; if the latter is present it returns it instead
  of uploading again (§55). The database's unique index is the second line of
  defence, not the first.
* **Never log a token.** Credentials are resolved per call from the secret
  store and never enter a log line, a response body or an asset's metadata
  (§63, §89).

Access tokens are refreshed from a refresh token held in the same Valkey secret
store the provider bindings use; the refreshed access token is cached in memory
only for its lifetime, so a restart re-derives it rather than persisting a
second copy of a credential.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.providers.base import (
    Provider,
    ProviderCategory,
    ProviderResult,
    classify_http_error,
)

logger = logging.getLogger("hermes.content_factory.providers.publishing")


class PublishingProvider(Provider):
    category = ProviderCategory.PUBLISHING
    platform = ""

    def __init__(self, provider_id: str, *, config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, config=config)
        self.timeout = float(self.config.get("timeout", 120.0))
        self._token_cache: dict[str, tuple[str, float]] = {}

    @property
    def is_free(self) -> bool:
        return True  # platforms do not bill for uploads

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        """`request` carries: `credentials_ref`, `video_url` or `video_path`,
        `title`, `description`, `privacy`, `scheduled_at`, `external_id`,
        `idempotency_key`, `dry_run`."""
        if request.get("dry_run"):
            logger.info("%s: dry run — nothing published", self.id)
            return ProviderResult(
                output={
                    "externalId": "",
                    "externalUrl": "",
                    "dryRun": True,
                    "wouldPublish": {
                        "platform": self.platform,
                        "title": request.get("title", ""),
                        "privacy": request.get("privacy", "private"),
                    },
                },
                provider=self.id,
            )

        existing = str(request.get("external_id") or "").strip()
        if existing:
            # A retry after a partial failure: the upload already succeeded, so
            # publishing again would produce a duplicate video (§55).
            logger.info("%s: publication %s already exists — not re-uploading", self.id, existing)
            return ProviderResult(
                output={"externalId": existing, "externalUrl": self.external_url(existing), "reused": True},
                provider=self.id,
            )

        return await self.publish(request)

    async def publish(self, request: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError

    def external_url(self, external_id: str) -> str:
        return external_id

    async def get_analytics(self, external_id: str, credentials_ref: str = "") -> dict[str, Any]:
        return {}

    def normalize_error(self, exc: Exception) -> ContentFactoryError:
        if isinstance(exc, ContentFactoryError):
            return exc
        error = classify_http_error(exc)
        if error.code in {ErrorCode.PROVIDER_ERROR, ErrorCode.PROVIDER_UNAVAILABLE}:
            # Publishing failures are their own category in the UI (§103) — an
            # upload that failed is not the same problem as a model that failed.
            return ContentFactoryError(
                ErrorCode.PUBLISH_FAILED, error.message, retryable=error.retryable, details=error.details
            )
        return error

    # ── Credentials ──────────────────────────────────────────────────────
    def _secret(self, credentials_ref: str) -> dict[str, Any]:
        """Reads the channel's stored OAuth material. Refresh tokens never leave
        this method (§50)."""
        if not credentials_ref:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH, f"{self.id}: channel is not connected", retryable=False
            )
        from backend.valkey_client import get_value

        raw = get_value(credentials_ref)
        if not raw:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH,
                f"{self.id}: stored credentials are missing or expired",
                retryable=False,
            )
        import json

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH, f"{self.id}: stored credentials are unreadable", retryable=False
            ) from None
        return parsed

    def _cached_token(self, credentials_ref: str) -> Optional[str]:
        cached = self._token_cache.get(credentials_ref)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        return None

    def _cache_token(self, credentials_ref: str, token: str, expires_in: float) -> None:
        self._token_cache[credentials_ref] = (token, time.time() + max(60.0, expires_in))

    def _client(self, credentials_ref: str, *, timeout: Optional[float] = None) -> httpx.AsyncClient:
        """Builds a per-account client so token, upload and analytics traffic
        all use the same assigned proxy. The URL is resolved from the secret
        store and is never logged or returned by the API."""
        secret = self._secret(credentials_ref)
        proxy = str(secret.get("proxy_url") or "").strip() or None
        return httpx.AsyncClient(timeout=timeout or self.timeout, proxy=proxy)


class YouTubeProvider(PublishingProvider):
    """§51. Resumable upload, then a videos.update for anything the initial
    snippet cannot carry."""

    platform = "YOUTUBE"

    def __init__(self, provider_id: str = "youtube", *, config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, config=config)

    def external_url(self, external_id: str) -> str:
        return f"https://www.youtube.com/watch?v={external_id}"

    async def _access_token(self, credentials_ref: str) -> str:
        cached = self._cached_token(credentials_ref)
        if cached:
            return cached
        secret = self._secret(credentials_ref)
        async with self._client(credentials_ref, timeout=30.0) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": secret.get("client_id") or os.environ.get("YOUTUBE_CLIENT_ID", ""),
                    "client_secret": secret.get("client_secret") or os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
                    "refresh_token": secret.get("refresh_token", ""),
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code >= 400:
                raise ContentFactoryError(
                    ErrorCode.PROVIDER_AUTH,
                    "YouTube refused to refresh the channel token — reconnect the channel",
                    retryable=False,
                )
            body = response.json()
        token = str(body.get("access_token") or "")
        self._cache_token(credentials_ref, token, float(body.get("expires_in") or 3600))
        return token

    async def publish(self, request: dict[str, Any]) -> ProviderResult:
        credentials_ref = str(request.get("credentials_ref") or "")
        token = await self._access_token(credentials_ref)
        payload = {
            "snippet": {
                "title": str(request.get("title", ""))[:100],
                "description": str(request.get("description", ""))[:5000],
                "tags": list(request.get("tags") or [])[:30],
                "categoryId": str(request.get("category_id") or "22"),
            },
            "status": {
                "privacyStatus": str(request.get("privacy") or "private"),
                "selfDeclaredMadeForKids": bool(request.get("made_for_kids", False)),
                # Platforms require synthetic media to be disclosed; the pipeline
                # generates every frame, so this is always true rather than a
                # per-run choice.
                "containsSyntheticMedia": True,
            },
        }
        if request.get("scheduled_at"):
            payload["status"]["publishAt"] = request["scheduled_at"]
            payload["status"]["privacyStatus"] = "private"

        try:
            async with self._client(credentials_ref) as client:
                init = await client.post(
                    "https://www.googleapis.com/upload/youtube/v3/videos",
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Upload-Content-Type": "video/*",
                    },
                    json=payload,
                )
                init.raise_for_status()
                upload_url = init.headers.get("location")
                if not upload_url:
                    raise ContentFactoryError(
                        ErrorCode.PUBLISH_FAILED, "YouTube did not return an upload URL"
                    )

                video_bytes = await _read_video(request, client)
                upload = await client.put(
                    upload_url,
                    headers={"Content-Type": "video/*", "Content-Length": str(len(video_bytes))},
                    content=video_bytes,
                )
                upload.raise_for_status()
                body = upload.json()
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        external_id = str(body.get("id") or "")
        if not external_id:
            raise ContentFactoryError(ErrorCode.PUBLISH_FAILED, "YouTube returned no video id")
        return ProviderResult(
            output={
                "externalId": external_id,
                "externalUrl": self.external_url(external_id),
                "publishedAt": (body.get("status") or {}).get("publishAt"),
                "privacyStatus": (body.get("status") or {}).get("privacyStatus"),
            },
            provider=self.id,
        )

    async def get_analytics(self, external_id: str, credentials_ref: str = "") -> dict[str, Any]:
        """Public statistics via the Data API — enough for §56 without the
        Analytics API's extra scopes."""
        token = await self._access_token(credentials_ref)
        async with self._client(credentials_ref, timeout=30.0) as client:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics", "id": external_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            items = response.json().get("items") or []
        stats = (items[0].get("statistics") if items else {}) or {}
        return {
            "views": int(stats.get("viewCount") or 0),
            "likes": int(stats.get("likeCount") or 0),
            "comments": int(stats.get("commentCount") or 0),
            "shares": 0,
        }


class TikTokProvider(PublishingProvider):
    """§52. Content Posting API: init a publish, hand TikTok a URL to pull, then
    poll until it reports the post is live."""

    platform = "TIKTOK"

    def __init__(self, provider_id: str = "tiktok", *, config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, config=config)

    def external_url(self, external_id: str) -> str:
        return f"https://www.tiktok.com/video/{external_id}"

    async def _access_token(self, credentials_ref: str) -> str:
        cached = self._cached_token(credentials_ref)
        if cached:
            return cached
        secret = self._secret(credentials_ref)
        async with self._client(credentials_ref, timeout=30.0) as client:
            response = await client.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key": secret.get("client_key") or os.environ.get("TIKTOK_CLIENT_KEY", ""),
                    "client_secret": secret.get("client_secret") or os.environ.get("TIKTOK_CLIENT_SECRET", ""),
                    "grant_type": "refresh_token",
                    "refresh_token": secret.get("refresh_token", ""),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise ContentFactoryError(
                    ErrorCode.PROVIDER_AUTH,
                    "TikTok refused to refresh the channel token — reconnect the channel",
                    retryable=False,
                )
            body = response.json()
        token = str(body.get("access_token") or "")
        self._cache_token(credentials_ref, token, float(body.get("expires_in") or 3600))
        return token

    async def publish(self, request: dict[str, Any]) -> ProviderResult:
        credentials_ref = str(request.get("credentials_ref") or "")
        token = await self._access_token(credentials_ref)
        video_url = str(request.get("video_url") or "")
        if not video_url:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "TikTok pulls the file from a URL — a pre-signed asset URL is required",
                retryable=False,
            )
        payload = {
            "post_info": {
                "title": str(request.get("title", ""))[:2200],
                "privacy_level": str(request.get("privacy") or "SELF_ONLY"),
                "disable_comment": bool(request.get("disable_comment", False)),
                # Same disclosure rule as YouTube: the content is generated.
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": True,
            },
            "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
        }
        try:
            async with self._client(credentials_ref) as client:
                response = await client.post(
                    "https://open.tiktokapis.com/v2/post/publish/video/init/",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        data = body.get("data") or {}
        publish_id = str(data.get("publish_id") or "")
        if not publish_id:
            error = (body.get("error") or {}).get("message") or "TikTok returned no publish id"
            raise ContentFactoryError(ErrorCode.PUBLISH_FAILED, str(error))
        # TikTok's publish is asynchronous on their side: the id is real, the
        # post is not live yet. The publication row stays PUBLISHING until
        # get_status confirms it, rather than claiming success early.
        return ProviderResult(
            output={"externalId": publish_id, "externalUrl": "", "pending": True},
            provider=self.id,
        )

    async def get_status(self, job_id: str) -> Any:  # noqa: D401 - see base contract
        raise NotImplementedError("TikTok status requires the channel's credentials_ref; use fetch_status()")

    async def fetch_status(self, publish_id: str, credentials_ref: str) -> dict[str, Any]:
        token = await self._access_token(credentials_ref)
        async with self._client(credentials_ref, timeout=30.0) as client:
            response = await client.post(
                "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"publish_id": publish_id},
            )
            response.raise_for_status()
            data = (response.json().get("data") or {})
        return {
            "status": str(data.get("status") or ""),
            "externalId": str((data.get("publicaly_available_post_id") or [""])[0] or ""),
            "failReason": data.get("fail_reason") or "",
        }

    async def get_analytics(self, external_id: str, credentials_ref: str = "") -> dict[str, Any]:
        """Creator-authorized video metrics from TikTok's Display API."""
        token = await self._access_token(credentials_ref)
        async with self._client(credentials_ref, timeout=30.0) as client:
            response = await client.post(
                "https://open.tiktokapis.com/v2/video/query/",
                params={"fields": "id,view_count,like_count,comment_count,share_count"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"filters": {"video_ids": [external_id]}},
            )
            response.raise_for_status()
            videos = ((response.json().get("data") or {}).get("videos") or [])
        stats = videos[0] if videos else {}
        return {
            "views": int(stats.get("view_count") or 0),
            "likes": int(stats.get("like_count") or 0),
            "comments": int(stats.get("comment_count") or 0),
            "shares": int(stats.get("share_count") or 0),
        }


async def _read_video(request: dict[str, Any], client: httpx.AsyncClient) -> bytes:
    """Bytes for the upload, from a local path or a (pre-signed) URL."""
    path = request.get("video_path")
    if path:
        from pathlib import Path

        return Path(str(path)).read_bytes()
    url = str(request.get("video_url") or "")
    if not url:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "No video to publish", retryable=False
        )
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.content


def build_publishing_providers() -> list[Provider]:
    """Both adapters are registered unconditionally: they hold no keys of their
    own (credentials live on the channel), so registration costs nothing and a
    channel that is not connected fails at publish time with PROVIDER_AUTH —
    which is what the Channels page shows."""
    return [YouTubeProvider(), TikTokProvider()]
