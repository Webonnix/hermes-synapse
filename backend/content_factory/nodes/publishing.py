"""Publishing and analytics nodes (§13 PUBLISHING / ANALYTICS).

One node type covers every platform: the channel decides which adapter runs
(§52 — no platform-specific logic in the core pipeline). What the node itself
owns is the part that must be identical on every platform:

* a claim on the publication record *before* anything is uploaded (§55);
* dry-run enforcement taken from the run, not from the node's config (§108);
* recording the external id the moment the platform returns it, so a crash
  immediately afterwards cannot lead to a second upload.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.content_factory import content as content_store
from backend.content_factory import db
from backend.content_factory.domain import ContentFactoryError, ErrorCode, NodeResult, PipelineNode
from backend.content_factory.nodes import util
from backend.content_factory.nodes.base import NodeContext, NodeHandler, register
from backend.content_factory.providers.base import ProviderCategory
from backend.content_factory.providers.registry import get_registry
from backend.content_factory.schema import WORKSPACE_DEFAULT

logger = logging.getLogger("hermes.content_factory.nodes.publishing")

_PROVIDER_BY_PLATFORM = {"YOUTUBE": "youtube", "TIKTOK": "tiktok"}


class PublishNode(NodeHandler):
    type = "publish"
    provider_category = ProviderCategory.PUBLISHING

    def validate(self, node: PipelineNode) -> list[dict[str, str]]:
        if not (node.config.get("channelId") or node.input_mapping.get("channelId")):
            return [
                {
                    "code": "NO_CHANNEL",
                    "message": "publish node has no channel selected",
                    "severity": "error",
                }
            ]
        return []

    async def execute(self, context: NodeContext) -> NodeResult:
        channel_id = str(context.require("channelId"))
        channel = content_store.get_channel(channel_id)
        if not channel:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, f"No such channel: {channel_id}", retryable=False
            )
        from backend.content_factory import settings as cf_settings

        network_settings = cf_settings.get_settings()["network"]
        if network_settings["requireProxyForPublishing"] and not int(channel.get("proxy_enabled") or 0):
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "Publishing proxy is required by Content Factory settings for this account",
                retryable=False,
            )

        content_id = str(context.config("contentId") or "")
        asset_id = str(context.config("assetId") or "")
        title = str(context.config("title", ""))
        description = str(context.config("description", ""))

        claim = content_store.claim_publication(
            content_id=content_id or f"run:{context.run.get('id')}",
            channel_id=channel_id,
            publication_version=int(context.config("publicationVersion", 1)),
            title=title,
            description=description,
            scheduled_at=context.config("scheduledAt"),
            dry_run=context.dry_run,
            metadata={"nodeId": context.node.id},
            workspace_id=str(context.run.get("workspace_id") or WORKSPACE_DEFAULT),
        )
        publication = claim["publication"]

        if not claim["should_publish"]:
            # Either already live, or another worker holds the claim. Both mean
            # "do not upload"; the node completes successfully because the
            # desired end state — this video published once — already holds.
            return NodeResult(
                output={
                    "publicationId": publication["id"],
                    "externalId": publication.get("external_id", ""),
                    "externalUrl": publication.get("external_url", ""),
                    "skipped": True,
                    "reason": "already_published_or_in_flight",
                },
                provider="internal",
                logs=[f"publish skipped: publication {publication['id']} already claimed"],
            )

        provider_id = _PROVIDER_BY_PLATFORM.get(str(channel["platform"]).upper(), "")
        provider = get_registry().get(provider_id) if provider_id else None
        if provider is None:
            provider = get_registry().default_for(ProviderCategory.PUBLISHING)
        if provider is None:
            content_store.mark_failed(
                publication["id"], code=ErrorCode.PROVIDER_UNAVAILABLE, message="no publishing provider"
            )
            raise ContentFactoryError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"No publishing provider for {channel['platform']}",
                retryable=False,
            )

        request = {
            "credentials_ref": channel.get("credentials_ref", ""),
            "title": title,
            "description": description,
            "tags": context.config("tags") or [],
            "privacy": context.config("privacy", "private"),
            "made_for_kids": bool(context.config("madeForKids", False)),
            "category_id": str(context.config("categoryId", "22")),
            "scheduled_at": context.config("scheduledAt"),
            "dry_run": context.dry_run,
            "external_id": publication.get("external_id") or "",
            "idempotency_key": f"{publication['content_id']}:{channel_id}:{publication['publication_version']}",
        }
        video_url = _asset_url(asset_id)
        if video_url:
            request["video_url"] = video_url

        await context.report(30, f"publishing to {channel['platform']}")
        try:
            import asyncio

            result = await asyncio.wait_for(
                util.run_provider(provider, request),
                timeout=float(network_settings["connectionTimeoutSeconds"]),
            )
        except TimeoutError as error:
            content_store.mark_failed(
                publication["id"], code=ErrorCode.NODE_TIMEOUT, message="publishing connection timed out"
            )
            raise ContentFactoryError(
                ErrorCode.NODE_TIMEOUT, "Publishing connection timed out", retryable=True
            ) from error
        except ContentFactoryError as error:
            content_store.mark_failed(publication["id"], code=error.code, message=error.message)
            raise

        external_id = str(result.output.get("externalId") or "")
        if context.dry_run:
            db.execute(
                "UPDATE cf_publications SET status = 'DRAFT', updated_at = ? WHERE id = ?",
                (db.now_iso(), publication["id"]),
            )
            return NodeResult(
                output={"publicationId": publication["id"], "dryRun": True, **result.output},
                provider=provider.id,
                logs=["dry run — nothing was published"],
            )

        if not external_id:
            content_store.mark_failed(
                publication["id"], code=ErrorCode.PUBLISH_FAILED, message="platform returned no id"
            )
            raise ContentFactoryError(ErrorCode.PUBLISH_FAILED, "Platform returned no video id")

        updated = content_store.mark_published(
            publication["id"],
            external_id=external_id,
            external_url=str(result.output.get("externalUrl") or provider.external_url(external_id)),
        )
        if content_id:
            content_store.update_content_item(content_id, status="PUBLISHED")

        return NodeResult(
            output={
                "publicationId": updated["id"],
                "externalId": external_id,
                "externalUrl": updated.get("external_url", ""),
                "platform": channel["platform"],
            },
            provider=provider.id,
            logs=[f"published to {channel['platform']} as {external_id}"],
        )


class AnalyticsNode(NodeHandler):
    """§56–57. Writes a snapshot rather than overwriting a counter, so growth
    over time stays reconstructable."""

    type = "analytics"
    provider_category = ProviderCategory.PUBLISHING

    async def execute(self, context: NodeContext) -> NodeResult:
        publication_id = str(context.require("publicationId"))
        publication = content_store.get_publication(publication_id)
        if not publication or not publication.get("external_id"):
            return NodeResult(output={"skipped": True, "reason": "not_published"}, provider="internal")

        channel = content_store.get_channel(str(publication["channel_id"]))
        provider = get_registry().get(_PROVIDER_BY_PLATFORM.get(str(publication["platform"]).upper(), ""))
        if provider is None or channel is None:
            return NodeResult(output={"skipped": True, "reason": "no_provider"}, provider="internal")

        metrics = await provider.get_analytics(
            str(publication["external_id"]), channel.get("credentials_ref", "")
        )
        _write_snapshot(publication_id, str(context.run.get("workspace_id") or WORKSPACE_DEFAULT), metrics)
        return NodeResult(output={"metrics": metrics}, provider=provider.id)


def _write_snapshot(publication_id: str, workspace_id: str, metrics: dict[str, Any]) -> None:
    import uuid

    views = int(metrics.get("views") or 0)
    engagements = sum(int(metrics.get(key) or 0) for key in ("likes", "comments", "shares"))
    db.execute(
        """
        INSERT INTO cf_analytics_snapshots
            (id, workspace_id, publication_id, timestamp, views, likes, comments, shares,
             watch_time, avg_view_duration, followers_delta, engagement_rate, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"snap-{uuid.uuid4().hex[:12]}", workspace_id, publication_id, db.now_iso(),
            views, int(metrics.get("likes") or 0), int(metrics.get("comments") or 0),
            int(metrics.get("shares") or 0), float(metrics.get("watch_time") or 0),
            float(metrics.get("avg_view_duration") or 0), int(metrics.get("followers_delta") or 0),
            round(engagements / views, 6) if views else 0.0,
            db.dumps(metrics), db.now_iso(),
        ),
    )


def _asset_url(asset_id: str) -> str:
    if not asset_id:
        return ""
    asset = util.get_asset(asset_id)
    if not asset:
        return ""
    # Long enough for a platform to pull a large file, short enough that a
    # leaked link is not a permanent one.
    return util.asset_url(asset, expires_in=6 * 3600)


for handler in (PublishNode(), AnalyticsNode()):
    register(handler)
