"""Shared helpers for node handlers: provider selection and asset persistence.

Provider *selection* lives here; provider *policy* (when to retry, when to move
down the fallback list) lives in the engine. A handler asks for "the provider
for this attempt" and gets one — it never decides on its own that a failure
warrants trying someone else, because that decision has to be made in one place
to be traceable (§21, §40).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Optional

from backend.content_factory import db, storage as storage_module
from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.nodes.base import NodeContext
from backend.content_factory.providers.base import GeneratedAsset, Provider, ProviderResult
from backend.content_factory.providers.registry import get_registry
from backend.content_factory.schema import WORKSPACE_DEFAULT

logger = logging.getLogger("hermes.content_factory.nodes.util")


def provider_chain(context: NodeContext, category: str) -> list[Provider]:
    """Ordered providers this node may use (§21).

    Order of precedence: the node's own `provider` + `fallbackPolicy`; then, for
    LLM nodes bound to an agent, that agent's permitted chain
    (`agents.resolve_agent_llm`, which honours tier and allowlist ceilings);
    then the registry's default for the category.
    """
    registry = get_registry()
    primary = str(context.config("provider", "") or "")
    fallbacks = list(context.node.fallback_policy or context.config("fallbacks", []) or [])

    if not primary and category == "LLM":
        agent_id = context.config("agentId") or context.config("agent_id")
        if agent_id:
            from backend.content_factory.agents import resolve_agent_llm

            allowed = resolve_agent_llm(str(agent_id))["provider_ids"]
            if allowed:
                primary, fallbacks = allowed[0], [*allowed[1:], *fallbacks]

    chain = registry.chain_for(category, primary, fallbacks)
    if not chain:
        raise ContentFactoryError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"{context.node.id}: no {category} provider is configured",
            retryable=False,
            details={"nodeId": context.node.id, "category": category},
        )
    return chain


def pick_provider(context: NodeContext, category: str) -> Provider:
    """The provider for this attempt. Index past the end means every rung has
    been tried — the engine treats that as final."""
    chain = provider_chain(context, category)
    if context.provider_index >= len(chain):
        raise ContentFactoryError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"{context.node.id}: every {category} provider in the fallback chain failed",
            retryable=False,
            details={"tried": [provider.id for provider in chain]},
        )
    return chain[context.provider_index]


async def run_provider(provider: Provider, request: dict[str, Any]) -> ProviderResult:
    """Executes under the provider's concurrency limit (§92)."""
    registry = get_registry()
    async with registry.semaphore(provider):
        return await provider.execute(request)


# ── Assets ───────────────────────────────────────────────────────────────────

async def persist_assets(
    context: NodeContext,
    result: ProviderResult,
    *,
    asset_type: str,
    node_run_id: str,
) -> list[dict[str, Any]]:
    """Moves everything a provider produced into object storage and records it
    in `cf_assets` (§45).

    Assets are immutable: a regenerated visual becomes a new row and a new key,
    so an old run's trace still resolves to the bytes it actually used.
    """
    import asyncio

    stored: list[dict[str, Any]] = []
    for index, asset in enumerate(result.assets):
        data = await _asset_bytes(asset)
        key = storage_module.build_key(
            str(context.run.get("project_id") or ""),
            str(context.run.get("id") or ""),
            asset.type or asset_type,
            _filename(asset, context, index),
        )
        store = storage_module.get_storage()
        obj = await asyncio.to_thread(store.put, key, data, mime_type=asset.mime_type)

        asset_id = f"asset-{uuid.uuid4().hex[:12]}"
        db.execute(
            """
            INSERT INTO cf_assets
                (id, workspace_id, project_id, pipeline_run_id, node_run_id, type, storage_key,
                 mime_type, size_bytes, duration, width, height, checksum, provider, model,
                 cost, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                context.run.get("workspace_id") or WORKSPACE_DEFAULT,
                context.run.get("project_id"),
                context.run.get("id"),
                node_run_id,
                asset.type or asset_type,
                obj.key,
                asset.mime_type,
                obj.size_bytes,
                asset.duration,
                asset.width,
                asset.height,
                obj.checksum,
                result.provider,
                result.model,
                # Cost belongs to the node run, not to each file; splitting one
                # charge across N assets would double-count in every report.
                0.0,
                db.dumps(asset.metadata),
                db.now_iso(),
            ),
        )
        stored.append(
            {
                "id": asset_id,
                "type": asset.type or asset_type,
                "storageKey": obj.key,
                "mimeType": asset.mime_type,
                "sizeBytes": obj.size_bytes,
                "duration": asset.duration,
                "width": asset.width,
                "height": asset.height,
            }
        )
    return stored


async def _asset_bytes(asset: GeneratedAsset) -> bytes:
    """Bytes for an asset, downloading from the provider's URL when needed.

    Vendor result URLs expire — often within hours — so the bytes are pulled
    into our own storage immediately rather than storing the link and
    discovering it is dead when the render worker asks for it.
    """
    if asset.data is not None:
        return asset.data
    if not asset.source_url:
        raise ContentFactoryError(
            ErrorCode.PROVIDER_ERROR, "Provider returned an asset with neither data nor URL"
        )
    import httpx

    try:
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            response = await client.get(asset.source_url)
            response.raise_for_status()
            return response.content
    except Exception as exc:
        from backend.content_factory.providers.base import classify_http_error

        raise classify_http_error(exc) from exc


def _filename(asset: GeneratedAsset, context: NodeContext, index: int) -> str:
    """Deterministic, collision-free and readable: node id, index, and a short
    content-independent suffix so two attempts of the same node do not
    overwrite each other."""
    base = asset.filename or f"{asset.type.lower()}.bin"
    stem, _, extension = base.rpartition(".")
    stem = stem or base
    suffix = hashlib.sha1(
        f"{context.node.id}:{context.attempt}:{index}:{uuid.uuid4().hex}".encode()
    ).hexdigest()[:8]
    return f"{context.node.id}-{index}-{suffix}.{extension}" if extension else f"{context.node.id}-{index}-{suffix}"


def asset_url(asset_row_or_key: Any, *, expires_in: int = 3600) -> str:
    """Pre-signed URL for an asset (§46)."""
    key = asset_row_or_key.get("storage_key") if isinstance(asset_row_or_key, dict) else asset_row_or_key
    if not key:
        return ""
    return storage_module.get_storage().presigned_get(str(key), expires_in=expires_in)


def get_asset(asset_id: str) -> Optional[dict[str, Any]]:
    return db.query_one("SELECT * FROM cf_assets WHERE id = ?", (asset_id,))


def record_usage(
    context: NodeContext,
    node_run_id: str,
    result: ProviderResult,
    *,
    operation: str,
) -> None:
    """One `cf_usage_records` row per external call (§59). Free/local providers
    are recorded too, at zero cost, so "what did this run actually do" is
    complete rather than only listing what was billed."""
    db.execute(
        """
        INSERT INTO cf_usage_records
            (id, workspace_id, pipeline_run_id, node_run_id, project_id, provider, model,
             operation, input_tokens, output_tokens, seconds_generated, images_generated,
             video_seconds, currency, cost, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?)
        """,
        (
            f"use-{uuid.uuid4().hex[:12]}",
            context.run.get("workspace_id") or WORKSPACE_DEFAULT,
            context.run.get("id"),
            node_run_id,
            context.run.get("project_id"),
            result.provider,
            result.model,
            operation,
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.seconds_generated,
            result.usage.images_generated,
            result.usage.video_seconds,
            round(float(result.cost or 0.0), 6),
            db.now_iso(),
        ),
    )
