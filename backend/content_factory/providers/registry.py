"""Provider registry — resolution, fallback chains and concurrency limits.

Three jobs:

1. **Naming.** A pipeline node stores provider *ids*, never objects, so a
   version snapshot (§11) stays valid across restarts and redeploys.
2. **Fallback (§21).** `chain_for()` returns the primary provider followed by
   its declared fallbacks, already filtered to ones that are actually usable
   right now (credentials present, not disabled). The engine walks that list;
   which rung answered is recorded on the node run, so a trace shows Kling →
   rate limit → Runway → success rather than just "video generated".
3. **Concurrency (§92).** One semaphore per provider id. Limits are per
   *provider*, not per pipeline, because that is how the vendor's quota works —
   five parallel runs must not send fifteen simultaneous Kling jobs.

Model selection is deliberately *not* baked into code (§100): a provider's
default model comes from configuration, and a node may override it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable, Optional

from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.providers.base import Provider, ProviderCategory

logger = logging.getLogger("hermes.content_factory.registry")

# §92 defaults. Overridable per provider with CF_CONCURRENCY_<ID>.
_DEFAULT_CONCURRENCY = {
    ProviderCategory.LLM: 8,
    ProviderCategory.IMAGE: 4,
    ProviderCategory.VIDEO: 3,
    ProviderCategory.VOICE: 6,
    ProviderCategory.MUSIC: 2,
    ProviderCategory.PUBLISHING: 2,
    ProviderCategory.ANALYTICS: 4,
    ProviderCategory.MODERATION: 4,
    ProviderCategory.SEARCH: 4,
    ProviderCategory.STORAGE: 8,
}


def mock_providers_enabled() -> bool:
    """Mock providers (§128) are the default in tests and whenever the flag is
    set; production must opt in explicitly, so a misconfigured deployment fails
    loudly instead of silently publishing placeholder video."""
    return os.environ.get("CONTENT_FACTORY_MOCK_PROVIDERS", "").strip().lower() in {"1", "true", "yes"}


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._defaults: dict[str, str] = {}  # category -> provider id

    # ── Registration ─────────────────────────────────────────────────────
    def register(self, provider: Provider, *, default_for: Optional[str] = None) -> Provider:
        self._providers[provider.id] = provider
        if default_for:
            self._defaults.setdefault(default_for, provider.id)
        elif provider.category and provider.category not in self._defaults:
            self._defaults[provider.category] = provider.id
        return provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)
        self._semaphores.pop(provider_id, None)
        for category, current in list(self._defaults.items()):
            if current == provider_id:
                self._defaults.pop(category)

    def clear(self) -> None:
        self._providers.clear()
        self._semaphores.clear()
        self._defaults.clear()

    # ── Lookup ───────────────────────────────────────────────────────────
    def get(self, provider_id: str) -> Optional[Provider]:
        return self._providers.get(provider_id)

    def require(self, provider_id: str) -> Provider:
        provider = self.get(provider_id)
        if provider is None:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"Provider '{provider_id}' is not configured",
                retryable=False,
                details={"providerId": provider_id},
            )
        return provider

    def for_category(self, category: str) -> list[Provider]:
        return [p for p in self._providers.values() if p.category == category]

    def default_for(self, category: str) -> Optional[Provider]:
        provider_id = self._defaults.get(category)
        return self._providers.get(provider_id) if provider_id else None

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def chain_for(
        self,
        category: str,
        primary: str = "",
        fallbacks: Iterable[str] = (),
    ) -> list[Provider]:
        """Primary then fallbacks, deduped, unknown ids dropped with a warning.

        An unknown id is a warning rather than an error because a fallback list
        outliving one of its providers must not take the pipeline down — that
        is the opposite of what a fallback is for. An empty result *is* an
        error, raised by the caller.
        """
        ordered: list[Provider] = []
        seen: set[str] = set()
        for provider_id in [primary, *fallbacks]:
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            provider = self.get(provider_id)
            if provider is None:
                logger.warning("Provider '%s' referenced but not configured — skipped", provider_id)
                continue
            if provider.category != category:
                logger.warning(
                    "Provider '%s' is %s, not %s — skipped", provider_id, provider.category, category
                )
                continue
            ordered.append(provider)
        if not ordered:
            fallback_default = self.default_for(category)
            if fallback_default is not None:
                ordered.append(fallback_default)
        return ordered

    # ── Concurrency (§92) ────────────────────────────────────────────────
    def limit_for(self, provider: Provider) -> int:
        env_key = f"CF_CONCURRENCY_{provider.id.upper().replace('-', '_')}"
        raw = os.environ.get(env_key)
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                logger.warning("Ignoring non-numeric %s=%r", env_key, raw)
        configured = provider.config.get("concurrency")
        if configured:
            return max(1, int(configured))
        return _DEFAULT_CONCURRENCY.get(provider.category, 4)

    def semaphore(self, provider: Provider) -> asyncio.Semaphore:
        existing = self._semaphores.get(provider.id)
        if existing is None:
            existing = asyncio.Semaphore(self.limit_for(provider))
            self._semaphores[provider.id] = existing
        return existing


_registry: Optional[ProviderRegistry] = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def set_registry_for_tests(registry: Optional[ProviderRegistry]) -> None:
    global _registry
    _registry = registry


def build_registry() -> ProviderRegistry:
    """Assembles providers from the environment and from `provider_bindings`.

    Every adapter is optional: a missing key or an uninstalled SDK removes that
    provider from the registry and is logged, but never prevents the module —
    or the app — from starting. A pipeline that references it fails at the node
    with PROVIDER_UNAVAILABLE, which is a diagnosable state; an import-time
    crash in a shared backend is not.
    """
    registry = ProviderRegistry()

    if mock_providers_enabled():
        from backend.content_factory.providers import mocks

        registry.register(mocks.MockLLMProvider(), default_for=ProviderCategory.LLM)
        registry.register(mocks.MockImageProvider(), default_for=ProviderCategory.IMAGE)
        registry.register(mocks.MockVoiceProvider(), default_for=ProviderCategory.VOICE)
        registry.register(mocks.MockVideoProvider(), default_for=ProviderCategory.VIDEO)
        registry.register(mocks.MockPublishingProvider(), default_for=ProviderCategory.PUBLISHING)
        logger.warning("Content Factory: MOCK providers active — nothing will reach a real API")
        return registry

    for loader in (_load_llm, _load_image, _load_voice, _load_video, _load_publishing):
        try:
            loader(registry)
        except Exception:
            logger.exception("Content Factory: %s failed; those providers stay unavailable", loader.__name__)

    _apply_configured_defaults(registry)
    return registry


def _apply_configured_defaults(registry: ProviderRegistry) -> None:
    """The per-category default chosen on the Integrations screen.

    Registration order decides the default otherwise, which is an accident of
    how the loaders are written — fine as a starting point, wrong as the answer
    to "which model does an image node use". A default naming a provider that
    is no longer registered is ignored rather than honoured: the alternative is
    a category whose default cannot answer.
    """
    try:
        from backend.content_factory.integrations import category_defaults
    except Exception:
        return
    for category, provider_id in category_defaults().items():
        provider = registry.get(provider_id)
        if provider is not None and provider.category == category:
            registry._defaults[category] = provider_id
        elif provider_id:
            logger.warning(
                "Default %s provider '%s' is not configured — falling back to registration order",
                category, provider_id,
            )


def _load_llm(registry: ProviderRegistry) -> None:
    from backend.content_factory.providers.llm import build_llm_providers

    for provider in build_llm_providers():
        registry.register(provider)
    # The local model is always the default: free, always present, and the
    # thing a budget-exhausted agent degrades to (`budget_fallback_to_local`).
    local = next((p for p in registry.for_category(ProviderCategory.LLM) if p.is_free), None)
    if local:
        registry._defaults[ProviderCategory.LLM] = local.id


def _load_image(registry: ProviderRegistry) -> None:
    from backend.content_factory.providers.image import build_image_providers

    for provider in build_image_providers():
        registry.register(provider)


def _load_voice(registry: ProviderRegistry) -> None:
    from backend.content_factory.providers.voice import build_voice_providers

    for provider in build_voice_providers():
        registry.register(provider)


def _load_video(registry: ProviderRegistry) -> None:
    from backend.content_factory.providers.video import build_video_providers

    for provider in build_video_providers():
        registry.register(provider)


def _load_publishing(registry: ProviderRegistry) -> None:
    from backend.content_factory.providers.publishing import build_publishing_providers

    for provider in build_publishing_providers():
        registry.register(provider)


def describe_providers() -> list[dict[str, Any]]:
    """Rows for the Providers settings page (§62). No secrets, ever (§89)."""
    registry = get_registry()
    return [
        {
            "id": provider.id,
            "category": provider.category,
            "model": provider.model,
            "free": provider.is_free,
            "asyncJob": provider.is_async_job,
            "concurrency": registry.limit_for(provider),
            "isDefault": registry.default_for(provider.category) is provider,
        }
        for provider in sorted(registry.all(), key=lambda p: (p.category, p.id))
    ]
