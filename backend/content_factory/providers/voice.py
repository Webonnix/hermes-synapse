"""Voice providers (§27).

Two tiers, and the free one is not a toy: VEXA already ships an XTTS-v2 sidecar
on the GPU plus Piper/RHVoice fallbacks (`tts.py`), which is enough for a
production Russian or English voiceover. ElevenLabs is registered alongside it
for the cases where the extra quality is worth per-character billing.

The local provider is wrapped rather than reimplemented, so a change to voice
selection, language detection or sidecar health in `tts.py` applies here too —
Vexa's speaking voice and the factory's narration stay the same voice.

Word-level timings (§34) are requested from ElevenLabs where available; the
subtitle node degrades to proportional timing when a provider cannot supply
them, rather than the pipeline losing subtitles altogether.
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path
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

logger = logging.getLogger("hermes.content_factory.providers.voice")

LOCAL_VOICE_ID = "local-voice"


class LocalVoiceProvider(Provider):
    """XTTS / Piper / RHVoice through `tts.synthesize_speech`."""

    category = ProviderCategory.VOICE

    def __init__(self, provider_id: str = LOCAL_VOICE_ID, *, config: Optional[dict[str, Any]] = None):
        super().__init__(provider_id, model="xtts-v2", config=config)

    @property
    def is_free(self) -> bool:
        return True

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        import asyncio

        from backend import tts

        text = str(request.get("text", "")).strip()
        if not text:
            raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "text is required", retryable=False)

        def _run() -> tuple[bytes, dict[str, Any]]:
            with tempfile.TemporaryDirectory(prefix="cf-voice-") as directory:
                output = str(Path(directory) / "voice.wav")
                # tts.py writes a file because that is what the chat voice path
                # needs; Content Factory wants bytes it can hand to object
                # storage, so the temp directory exists only for this call and
                # is removed before it returns (§120).
                meta = tts.synthesize_speech(
                    text, output, voice=request.get("voice"), rate=float(request.get("rate", 1.0))
                )
                return Path(output).read_bytes(), meta

        try:
            audio, meta = await asyncio.to_thread(_run)
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        duration = float(meta.get("duration_seconds") or 0.0) or _estimate_duration(text)
        usage = Usage(seconds_generated=duration)
        return ProviderResult(
            output={"duration": duration, "voice": meta.get("voice", ""), "provider": meta.get("provider", "")},
            assets=[
                GeneratedAsset(
                    type="VOICE",
                    mime_type="audio/wav",
                    data=audio,
                    filename="voice.wav",
                    duration=duration,
                    metadata={k: v for k, v in meta.items() if k != "path"},
                )
            ],
            usage=usage,
            cost=0.0,
            provider=self.id,
            model=str(meta.get("provider") or self.model),
        )

    def normalize_error(self, exc: Exception) -> ContentFactoryError:
        from backend.tts import VoiceSynthesisError

        if isinstance(exc, ContentFactoryError):
            return exc
        if isinstance(exc, VoiceSynthesisError):
            # A sidecar that is down is worth retrying; text that is empty or
            # too long is not, and `tts.py` distinguishes them only by message.
            retryable = "ready" in str(exc).lower() or "failed" in str(exc).lower()
            return ContentFactoryError(
                ErrorCode.PROVIDER_UNAVAILABLE if retryable else ErrorCode.VALIDATION_FAILED,
                str(exc),
                retryable=retryable,
            )
        return classify_http_error(exc)

    async def health(self) -> dict[str, Any]:
        from backend import tts

        return {"provider": self.id, **tts.get_tts_status()}


class ElevenLabsProvider(Provider):
    category = ProviderCategory.VOICE
    # $0.30 per 1000 characters at the Creator tier; overridden by config for
    # whatever plan the deployment is actually on.
    usd_per_1k_chars = 0.30

    def __init__(
        self,
        provider_id: str = "elevenlabs",
        *,
        api_key: str = "",
        model: str = "eleven_multilingual_v2",
        config: Optional[dict[str, Any]] = None,
    ):
        super().__init__(provider_id, model=model, config=config)
        self._api_key = api_key
        self.usd_per_1k_chars = float(self.config.get("usdPer1kChars", self.usd_per_1k_chars))
        self.timeout = float(self.config.get("timeout", 120.0))
        self._last_characters = 0

    async def execute(self, request: dict[str, Any]) -> ProviderResult:
        text = str(request.get("text", "")).strip()
        if not text:
            raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "text is required", retryable=False)
        voice_id = str(request.get("voice") or self.config.get("voiceId") or os.environ.get("ELEVENLABS_VOICE_ID", ""))
        if not voice_id:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "ElevenLabs needs a voice id", retryable=False
            )

        # `with-timestamps` returns base64 audio plus per-character alignment,
        # which is what makes word-accurate subtitles (§34) possible without a
        # second forced-alignment pass.
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        payload = {
            "text": text,
            "model_id": request.get("model") or self.model,
            "voice_settings": request.get("voice_settings") or self.config.get("voiceSettings") or {},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url, headers={"xi-api-key": self._api_key, "accept": "application/json"}, json=payload
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise self.normalize_error(exc) from exc

        audio = base64.b64decode(body.get("audio_base64") or "")
        if not audio:
            raise ContentFactoryError(ErrorCode.PROVIDER_ERROR, "ElevenLabs returned no audio")
        alignment = body.get("alignment") or body.get("normalized_alignment") or {}
        ends = alignment.get("character_end_times_seconds") or []
        duration = float(ends[-1]) if ends else _estimate_duration(text)

        self._last_characters = len(text)
        usage = Usage(seconds_generated=duration)
        return ProviderResult(
            output={"duration": duration, "alignment": alignment},
            assets=[
                GeneratedAsset(
                    type="VOICE",
                    mime_type="audio/mpeg",
                    data=audio,
                    filename="voice.mp3",
                    duration=duration,
                    metadata={"alignment": alignment, "voiceId": voice_id},
                )
            ],
            usage=usage,
            cost=self.calculate_cost(usage),
            provider=self.id,
            model=payload["model_id"],
        )

    def calculate_cost(self, usage: Usage) -> float:
        # Billed per character, which `Usage` does not carry — the character
        # count from the call that produced this usage is used instead of
        # inferring it from duration, which would be a guess.
        return round(self._last_characters / 1000.0 * self.usd_per_1k_chars, 6)

    def normalize_error(self, exc: Exception) -> ContentFactoryError:
        if isinstance(exc, ContentFactoryError):
            return exc
        return classify_http_error(exc)

    async def health(self) -> dict[str, Any]:
        return {"provider": self.id, "status": "connected" if self._api_key else "error"}


def _estimate_duration(text: str) -> float:
    """~15 characters per second of speech — only a fallback for providers that
    do not report a real duration; the renderer prefers the probed duration of
    the actual audio file."""
    return round(max(len(text), 1) / 15.0, 2)


def build_voice_providers() -> list[Provider]:
    """The local xtts voice is always present and free; ElevenLabs joins it when
    a key is configured — on the Integrations screen or in the environment."""
    from backend.content_factory.integrations import resolve_media_provider

    providers: list[Provider] = [LocalVoiceProvider()]
    resolved = resolve_media_provider("elevenlabs")
    if resolved is not None:
        providers.append(
            ElevenLabsProvider(
                api_key=resolved.api_key,
                model=resolved.model or "eleven_multilingual_v2",
                config=resolved.config,
            )
        )
    return providers
