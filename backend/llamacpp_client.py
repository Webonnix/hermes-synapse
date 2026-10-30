"""Asynchronous client for the local llama.cpp server (``llama-server``).

Since 2026-09-13 the local model is served by upstream llama.cpp instead of
Ollama (docs/inference-engine-evaluation-2026-09-13.md): tensor-split across
both GPUs plus the model's MTP draft head, one model resident for the life of
the process. The rest of Hermes consumes the provider-neutral contract in
``llm_client``; this module owns everything specific to llama-server:

* the OpenAI-compatible SSE stream, re-emitted as the same
  ``{content, thinking, tool_calls, done}`` chunks the chat UI already renders,
* reasoning switches, which llama-server takes as jinja ``chat_template_kwargs``
  rather than a top-level ``think`` flag,
* images, which the generic OpenAI path drops but vision turns need,
* server timings (``prompt_ms`` / ``predicted_ms``), so generation stats stay
  decode-time based rather than wall-clock based.

There is no model lifecycle here on purpose: llama-server loads exactly one
model at start-up and keeps it resident, so there is nothing to pull, unload
or keep warm.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from backend.llm_gate import PRIORITY_INTERACTIVE, llamacpp_gate

LOCAL_PROVIDER = "llamacpp"

#: ``subagents.model_provider`` value meaning "no pinned provider": start on the
#: local model and escalate through the global router chain. Rows written before
#: the llama.cpp migration carried "ollama"; database.init_db rewrites them, and
#: reads stay tolerant of the old spelling so a stale row can never be mistaken
#: for an external provider binding id.
LOCAL_PROVIDER_ID = "local"
_LEGACY_LOCAL_PROVIDER_IDS = frozenset({"ollama"})

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "qwen3.8-27b"

# Hostnames that are this deployment's llama-server, used when a caller passes
# an endpoint without naming the provider (per-turn overrides, env fallbacks).
_LOCAL_HOST_MARKERS = ("hermes-llama",)

# Qwen3.8's template accepts only these; "high" (valid for Qwen3.x hybrids)
# is the one people type, so it maps onto the nearest real level.
_EFFORT_ALIASES = {"high": "xhigh", "max": "xhigh", "minimal": "low"}
_VALID_EFFORTS = frozenset({"low", "medium", "xhigh"})


class LlamaCppError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, code: str = "llamacpp_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass
class LlamaCppChatResult:
    model: str
    content: str = ""
    thinking: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    done_reason: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    # Prompt tokens served from the slot's prompt cache (llama-server cache_n).
    cached_tokens: Optional[int] = None
    # Milliseconds spent decoding and ingesting the prompt, straight from the
    # server's timings: completion_tokens / eval_ms is the rate the model
    # actually sustained, independent of queueing and network time.
    eval_ms: Optional[float] = None
    prompt_ms: Optional[float] = None


# ── Configuration helpers ─────────────────────────────────────────────────────

def normalize_base_url(value: Optional[str] = None) -> str:
    """Server root without a trailing slash or ``/v1`` — callers add the path."""
    base = (value or os.getenv("LLAMACPP_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def normalize_provider(value: Any) -> str:
    """Canonical provider name. The pre-migration "ollama" setting means "the
    local model", which is llama.cpp now, not an unknown provider."""
    provider = str(value or "").strip().lower()
    return LOCAL_PROVIDER if provider in _LEGACY_LOCAL_PROVIDER_IDS else provider


def is_llamacpp_provider(api_base: str = "", provider: str = "") -> bool:
    configured = normalize_provider(provider)
    if configured:
        return configured == LOCAL_PROVIDER
    base = (api_base or "").strip().lower()
    if base:
        host = (urlparse(base).hostname or "").lower()
        env_host = (urlparse(normalize_base_url()).hostname or "").lower()
        return any(marker in host for marker in _LOCAL_HOST_MARKERS) or (bool(env_host) and host == env_host)
    return normalize_provider(os.getenv("LLM_PROVIDER", "")) == LOCAL_PROVIDER


def is_local_provider_id(provider_id: Any) -> bool:
    """Whether a ``model_provider``-style id means the local model rather than a
    provider_bindings row. Empty counts as local — that has always been the
    default for an agent with nothing pinned."""
    value = str(provider_id or "").strip().lower()
    return not value or value == LOCAL_PROVIDER_ID or value in _LEGACY_LOCAL_PROVIDER_IDS


def think_template_kwargs(think: Any) -> Dict[str, Any]:
    """Map Hermes' think setting onto the Qwen3.8 jinja template switches.

    Accepts the values the dashboard and env have always used: booleans, their
    string forms, and an effort level. Unknown strings are treated as "on" so a
    typo never silently disables reasoning the owner asked for.
    """
    if think is None:
        think = os.getenv("LOCAL_THINK", "false")
    if isinstance(think, bool):
        return {"enable_thinking": think}
    lowered = str(think).strip().lower()
    if lowered in {"", "false", "0", "no", "off", "none"}:
        return {"enable_thinking": False}
    if lowered in {"true", "1", "yes", "on"}:
        return {"enable_thinking": True}
    effort = _EFFORT_ALIASES.get(lowered, lowered)
    if effort in _VALID_EFFORTS:
        return {"enable_thinking": True, "reasoning_effort": effort}
    return {"enable_thinking": True}


# ── Message conversion ────────────────────────────────────────────────────────

def _image_url(image: str) -> str:
    image = str(image or "").strip()
    if image.startswith(("data:", "http://", "https://")):
        return image
    mime = "image/png"
    if image.startswith("/9j/"):
        mime = "image/jpeg"
    elif image.startswith("R0lG"):
        mime = "image/gif"
    elif image.startswith("UklG"):
        mime = "image/webp"
    return f"data:{mime};base64,{image}"


def _normalize_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for index, call in enumerate(tool_calls or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments", {})
        normalized.append({
            "id": call.get("id") or f"llamacpp_call_{index}",
            "type": "function",
            "function": {
                "name": function.get("name", ""),
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
            },
        })
    return normalized


def _openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from backend.agent import _extract_message_text  # lazy: agent imports this module

    result: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        text = _extract_message_text(message.get("content"))
        item: Dict[str, Any] = {"role": role}
        images = [img for img in (message.get("images") or []) if img]
        if images and role == "user":
            item["content"] = [
                *({"type": "image_url", "image_url": {"url": _image_url(img)}} for img in images),
                {"type": "text", "text": text},
            ]
        else:
            item["content"] = text
        if role == "assistant":
            tool_calls = _normalize_tool_calls(message.get("tool_calls"))
            if tool_calls:
                item["tool_calls"] = tool_calls
            if message.get("thinking"):
                item["reasoning_content"] = str(message["thinking"])
        elif role == "tool":
            item["tool_call_id"] = message.get("tool_call_id") or message.get("id") or "call_0"
            if message.get("name"):
                item["name"] = message["name"]
        result.append(item)
    return result


# Keys the caller's per-agent params must never override.
_PROTECTED_KEYS = frozenset({"model", "messages", "tools", "stream", "stream_options"})
# Per-agent params that are really template switches on llama-server.
_TEMPLATE_KEYS = ("enable_thinking", "reasoning_effort")


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"llama.cpp server returned HTTP {response.status_code}."
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if error:
        return str(error)
    return f"llama.cpp server returned HTTP {response.status_code}."


def _apply_timings(result: LlamaCppChatResult, chunk: Dict[str, Any]) -> None:
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        if usage.get("prompt_tokens") is not None:
            result.prompt_tokens = usage.get("prompt_tokens")
        if usage.get("completion_tokens") is not None:
            result.completion_tokens = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            result.cached_tokens = details.get("cached_tokens")
    timings = chunk.get("timings")
    if isinstance(timings, dict):
        if timings.get("predicted_ms") is not None:
            result.eval_ms = float(timings["predicted_ms"])
        if timings.get("prompt_ms") is not None:
            result.prompt_ms = float(timings["prompt_ms"])
        if result.completion_tokens is None and timings.get("predicted_n") is not None:
            result.completion_tokens = timings.get("predicted_n")
        if result.prompt_tokens is None and timings.get("prompt_n") is not None:
            result.prompt_tokens = int(timings["prompt_n"]) + int(timings.get("cache_n") or 0)
        if result.cached_tokens is None and timings.get("cache_n") is not None:
            result.cached_tokens = timings.get("cache_n")


class LlamaCppClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: float = 120.0,
        api_key: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.api_key = api_key if api_key is not None else os.getenv("LLAMACPP_API_KEY", "")
        self._client = client

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await client.request(method, f"{self.base_url}{path}", headers=self._headers(), **kwargs)
            if response.status_code >= 400:
                code = "loading" if response.status_code == 503 else "llamacpp_error"
                raise LlamaCppError(_error_message(response), status_code=response.status_code, code=code)
            return response
        except httpx.TimeoutException as exc:
            raise LlamaCppError("llama.cpp request timed out.", status_code=504, code="timeout") from exc
        except httpx.ConnectError as exc:
            raise LlamaCppError(
                f"Cannot connect to the llama.cpp server at {self.base_url}.",
                status_code=503,
                code="connection_error",
            ) from exc
        except httpx.RequestError as exc:
            raise LlamaCppError("llama.cpp request failed.", status_code=502, code="transport_error") from exc
        finally:
            if owns_client:
                await client.aclose()

    # ── Server state ─────────────────────────────────────────────────────────

    async def list_models(self) -> List[Dict[str, Any]]:
        response = await self._request("GET", "/v1/models")
        data = response.json()
        models = data.get("data") if isinstance(data, dict) else data
        return [item for item in (models or []) if isinstance(item, dict) and item.get("id")]

    async def model_ids(self) -> List[str]:
        return [str(item["id"]) for item in await self.list_models()]

    async def props(self) -> Dict[str, Any]:
        response = await self._request("GET", "/props")
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def status(self) -> Dict[str, Any]:
        """Everything the dashboard shows about the local server. Never raises:
        an unreachable or still-loading server is a status, not an exception."""
        status: Dict[str, Any] = {"available": False, "base_url": self.base_url}
        try:
            health = await self._request("GET", "/health")
            status["health"] = (health.json() or {}).get("status", "ok")
        except LlamaCppError as exc:
            status["health"] = "loading" if exc.code == "loading" else "unavailable"
            status["error"] = str(exc)
            status["code"] = exc.code
            return status
        status["available"] = True
        try:
            models = await self.list_models()
            status["models"] = [item["id"] for item in models]
            meta = models[0].get("meta") if models and isinstance(models[0].get("meta"), dict) else {}
            status["model"] = models[0]["id"] if models else None
            status["model_size"] = meta.get("size")
            status["model_params"] = meta.get("n_params")
            status["n_ctx_train"] = meta.get("n_ctx_train")
        except LlamaCppError as exc:
            status["error"] = str(exc)
        try:
            props = await self.props()
            settings = props.get("default_generation_settings") or {}
            status["n_ctx"] = settings.get("n_ctx") or props.get("n_ctx")
            status["total_slots"] = props.get("total_slots")
            status["model_path"] = props.get("model_path")
            status["build"] = props.get("build_info")
            modalities = props.get("modalities") or {}
            status["vision"] = bool(modalities.get("vision")) if isinstance(modalities, dict) else None
        except LlamaCppError:
            pass
        return status

    # ── Chat ─────────────────────────────────────────────────────────────────

    def _payload(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        think: Any,
        stream: bool,
        extra_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        template_kwargs = think_template_kwargs(think)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": _openai_messages(messages),
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream_options"] = {"include_usage": True}
        for key, value in (extra_payload or {}).items():
            if value is None or key in _PROTECTED_KEYS:
                continue
            if key in _TEMPLATE_KEYS:
                template_kwargs[key] = value
                continue
            payload[key] = value
        # Strata defaults to projection ON for a request that leaves the field out, so say it every time:
        # only a caller that asked for it (a web chat with the setting on) gets the uncensored model.
        payload["experimental_speed_projection"] = payload.get("experimental_speed_projection") is True
        if template_kwargs.get("reasoning_effort"):
            effort = _EFFORT_ALIASES.get(str(template_kwargs["reasoning_effort"]).lower(),
                                         str(template_kwargs["reasoning_effort"]).lower())
            if effort in _VALID_EFFORTS:
                template_kwargs["reasoning_effort"] = effort
            else:
                template_kwargs.pop("reasoning_effort")
        payload["chat_template_kwargs"] = template_kwargs
        return payload

    async def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        think: Any = None,
        stream_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        priority: int = PRIORITY_INTERACTIVE,
        queue_timeout: Optional[float] = None,
    ) -> LlamaCppChatResult:
        """One chat completion, queued through backend.llm_gate: lower
        `priority` goes first; `queue_timeout` (seconds) raises GateTimeout
        if no slot frees up in time. The timeout covers the wait only, not
        the generation."""
        async with llamacpp_gate().slot(priority, queue_timeout):
            return await self._chat_unqueued(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                tools=tools, think=think, stream_callback=stream_callback, extra_payload=extra_payload,
            )

    async def _chat_unqueued(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        think: Any,
        stream_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
        extra_payload: Optional[Dict[str, Any]],
    ) -> LlamaCppChatResult:
        payload = self._payload(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            tools=tools, think=think, stream=stream_callback is not None, extra_payload=extra_payload,
        )

        if stream_callback is None:
            response = await self._request("POST", "/v1/chat/completions", json=payload)
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                raise LlamaCppError(str(data["error"]))
            choice = ((data.get("choices") or [{}])[0]) if isinstance(data, dict) else {}
            message = choice.get("message") or {}
            result = LlamaCppChatResult(
                model=data.get("model") or model,
                content=str(message.get("content") or ""),
                thinking=str(message.get("reasoning_content") or message.get("reasoning") or ""),
                tool_calls=_normalize_tool_calls(message.get("tool_calls")),
                done_reason=choice.get("finish_reason"),
            )
            _apply_timings(result, data)
            return result

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0))
        result = LlamaCppChatResult(model=model)
        # Tool call fragments arrive keyed by index: the first carries id and
        # name, later ones only append to the arguments string.
        pending_calls: Dict[int, Dict[str, Any]] = {}
        try:
            async with client.stream(
                "POST", f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers(),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    code = "loading" if response.status_code == 503 else "llamacpp_error"
                    raise LlamaCppError(_error_message(response), status_code=response.status_code, code=code)
                saw_end = False
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("error:"):
                        raise LlamaCppError(line[len("error:"):].strip() or "llama.cpp stream error.")
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        saw_end = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise LlamaCppError("llama.cpp returned invalid stream data.") from exc
                    if chunk.get("error"):
                        error = chunk["error"]
                        raise LlamaCppError(str(error.get("message") if isinstance(error, dict) else error))
                    if chunk.get("model"):
                        result.model = chunk["model"]
                    _apply_timings(result, chunk)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = str(delta.get("content") or "")
                        thinking = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
                        for fragment in delta.get("tool_calls") or []:
                            index = int(fragment.get("index") or 0)
                            call = pending_calls.setdefault(
                                index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            if fragment.get("id"):
                                call["id"] = fragment["id"]
                            function = fragment.get("function") or {}
                            if function.get("name"):
                                call["function"]["name"] += function["name"]
                            if function.get("arguments"):
                                call["function"]["arguments"] += function["arguments"]
                        if content or thinking:
                            result.content += content
                            result.thinking += thinking
                            await stream_callback({
                                "content": content, "thinking": thinking, "tool_calls": [], "done": False,
                            })
                        if choice.get("finish_reason"):
                            result.done_reason = choice["finish_reason"]
                            saw_end = True
                if not saw_end:
                    raise LlamaCppError("llama.cpp stream ended before the final event.", code="interrupted_stream")
            result.tool_calls = _normalize_tool_calls(
                [pending_calls[index] for index in sorted(pending_calls)]
            )
            await stream_callback({"content": "", "thinking": "", "tool_calls": result.tool_calls, "done": True})
            return result
        except httpx.TimeoutException as exc:
            raise LlamaCppError("llama.cpp generation timed out.", status_code=504, code="timeout") from exc
        except httpx.ConnectError as exc:
            raise LlamaCppError(
                f"Cannot connect to the llama.cpp server at {self.base_url}.", status_code=503, code="connection_error",
            ) from exc
        except httpx.RequestError as exc:
            raise LlamaCppError("llama.cpp generation stream was interrupted.", status_code=502,
                                code="interrupted_stream") from exc
        finally:
            if owns_client:
                await client.aclose()
