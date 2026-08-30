"""LLM providers (§24).

Deliberately thin. HERMES already solved the hard parts of talking to a model —
`llm_client.call_llm_normalized` handles transport retry, Ollama vs
OpenAI-compatible payloads, streaming, refusal/empty/tool-only classification
and secret masking, and `NormalizedLLMResponse` is the single contract every
call path in the app already returns. Re-implementing any of that here would
give Content Factory a second, subtly different way to fail (which is exactly
what the architecture audit's RCA blames for empty answers).

So this adapter does three things `llm_client` does not:

1. presents the LLM as a `Provider`, so an LLM node and a video node look the
   same to the engine;
2. enforces structured output (§113) — request JSON, then validate against the
   node's schema and fail with a retryable code if it does not match;
3. resolves credentials from `provider_bindings` (governed, Valkey-stored)
   rather than reading environment keys directly.

Providers are built from what is actually configured: the local Ollama model is
always present and free, each approved external binding becomes its own
provider id, so an agent's `allowed_provider_ids` (§21 fallbacks) and a
pipeline's fallback list speak the same vocabulary.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from backend.content_factory import structured
from backend.content_factory.domain import ContentFactoryError, ErrorCode
from backend.content_factory.providers.base import (
    Provider,
    ProviderCategory,
    ProviderResult,
    Usage,
)

logger = logging.getLogger("hermes.content_factory.providers.llm")

LOCAL_PROVIDER_ID = "local-llm"


class LLMProvider(Provider):
    """One OpenAI-compatible endpoint (which includes Ollama and 9router).

    `binding_id` is the `provider_bindings` row this provider draws credentials
    from; None means the local model, which needs none.
    """

    category = ProviderCategory.LLM

    def __init__(
        self,
        provider_id: str,
        *,
        api_base: str,
        model: str,
        binding_id: Optional[str] = None,
        local: bool = False,
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(provider_id, model=model, config=config)
        self.api_base = api_base
        self.binding_id = binding_id
        self._local = local

    @property
    def is_free(self) -> bool:
        return self._local

    def _credentials(self) -> tuple[str, str]:
        """(api_base, api_key). Resolved per call, never cached: a revoked
        binding must stop working immediately, not at the next restart."""
        if self._local or not self.binding_id:
            return self.api_base, ""
        from backend.provider_governance import resolve_binding_credentials

        resolved = resolve_binding_credentials(self.binding_id)
        if not resolved:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH,
                f"Provider '{self.id}' has no usable credentials",
                retryable=False,
                details={"providerId": self.id},
            )
        return resolved

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        """`request` keys:

        * `messages` or `prompt` (+ optional `system`)
        * `output_schema` — when set, the response must be JSON matching it
        * `model`, `temperature`, `max_tokens` — per-node overrides (§100)
        """
        from backend.llm_client import call_llm_normalized

        api_base, api_key = self._credentials()
        model = str(request.get("model") or self.model)
        schema = request.get("output_schema") or {}
        messages = _build_messages(request, schema)

        extra_payload: dict[str, Any] = {}
        if schema and self.config.get("native_json", True):
            # OpenAI-compatible JSON mode. Endpoints that do not know the
            # parameter reject it, and llm_client's _refused_parameter path
            # retries once without it — so asking is safe and the prompt-level
            # instruction in _build_messages still carries the requirement.
            extra_payload["response_format"] = {"type": "json_object"}

        response = await call_llm_normalized(
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=float(request.get("temperature", self.config.get("temperature", 0.4))),
            max_tokens=request.get("max_tokens") or self.config.get("max_tokens"),
            timeout=request.get("timeout"),
            provider_options={"provider": "ollama" if self._local else ""},
            extra_payload=extra_payload or None,
        )

        if not response.is_success:
            raise _classify_normalized(response)

        text = response.content or ""
        parsed = structured.parse_and_validate(text, schema) if schema else None

        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
        )
        return ProviderResult(
            output={"text": text, "structured": parsed},
            usage=usage,
            cost=self.calculate_cost(usage, model=model),
            provider=self.id,
            model=response.model or model,
            raw_metadata=response.to_public_dict(),
        )

    def calculate_cost(self, usage: Usage, *, model: str = "") -> float:
        if self._local:
            return 0.0
        from backend.cost import calculate_cost

        return calculate_cost(
            model or self.model, usage.input_tokens, usage.output_tokens, self.binding_id
        )

    async def health(self) -> dict[str, Any]:
        """Credential resolution only — never a billable completion (§90)."""
        try:
            api_base, _ = self._credentials()
        except ContentFactoryError as exc:
            return {"provider": self.id, "status": "error", "code": exc.code}
        return {"provider": self.id, "status": "connected", "apiBase": api_base, "model": self.model}


def _build_messages(request: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    messages = request.get("messages")
    if messages:
        messages = [dict(message) for message in messages]
    else:
        messages = []
        system = request.get("system")
        if system:
            messages.append({"role": "system", "content": str(system)})
        messages.append({"role": "user", "content": str(request.get("prompt", ""))})

    if schema:
        instruction = structured.schema_instruction(schema)
        # Appended to the last user turn rather than added as a second system
        # message: models weight the tail of the prompt more reliably, and some
        # OpenAI-compatible endpoints only honour a single system message.
        for message in reversed(messages):
            if message.get("role") == "user":
                message["content"] = f"{message.get('content', '')}\n\n{instruction}"
                break
        else:
            messages.append({"role": "user", "content": instruction})
    return messages


def _classify_normalized(response: Any) -> ContentFactoryError:
    """`NormalizedLLMResponse.status` → §76 error code."""
    from backend import llm_client

    mapping = {
        llm_client.STATUS_TIMEOUT: ErrorCode.PROVIDER_TIMEOUT,
        llm_client.STATUS_PROVIDER_ERROR: ErrorCode.PROVIDER_ERROR,
        llm_client.STATUS_EMPTY: ErrorCode.INVALID_OUTPUT,
        llm_client.STATUS_PARSE_ERROR: ErrorCode.INVALID_OUTPUT,
        llm_client.STATUS_REFUSAL: ErrorCode.PROVIDER_ERROR,
    }
    code = mapping.get(getattr(response, "status", ""), ErrorCode.PROVIDER_ERROR)
    # A refusal is not worth retrying with the same prompt; everything else in
    # this map is transient by nature.
    retryable = None if response.status != llm_client.STATUS_REFUSAL else False
    return ContentFactoryError(
        code,
        response.error_message or f"LLM call failed ({response.status})",
        retryable=retryable,
        details={"status": response.status, "requestId": response.request_id},
    )


def build_llm_providers() -> list[Provider]:
    """Local model first, then every active `openai_compatible` binding."""
    providers: list[Provider] = []

    api_base = (
        os.environ.get("OLLAMA_BASE_URL")
        or os.environ.get("LLM_API_BASE")
        or "http://hermes-ollama:11434"
    ).rstrip("/")
    providers.append(
        LLMProvider(
            LOCAL_PROVIDER_ID,
            api_base=api_base,
            model=os.environ.get("LLM_MODEL", "qwen3:8b"),
            local=True,
        )
    )

    try:
        from backend.provider_governance import list_bindings
    except Exception:
        logger.warning("provider_governance unavailable — external LLM providers not registered")
        return providers

    for binding in list_bindings():
        if binding.get("status") != "active" or binding.get("provider_type") != "openai_compatible":
            continue
        providers.append(
            LLMProvider(
                binding["id"],
                api_base=binding["api_base"],
                model=binding.get("default_model") or os.environ.get("LLM_EXTERNAL_MODEL", ""),
                binding_id=binding["id"],
            )
        )
    return providers
