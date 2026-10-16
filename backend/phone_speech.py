"""Speech services for phone calls: streaming recognition and synthesis.

The call loop (backend/phone_session.py) sees two small interfaces and nothing else:

* :class:`STTStream` — feed 20 ms frames of 8 kHz PCM, read :class:`STTEvent`s:
  ``speech_started`` (the caller began talking — barge-in), ``partial`` (interim text),
  ``final`` (the caller finished a turn; this text is what the agent answers).
* :class:`TTSEngine` — ``synthesize(text)`` yields 8 kHz PCM chunks *as they are
  produced*, so the first words play while the rest of the phrase is still rendering.

Which vendor sits behind them is a setting chosen in the dashboard. The catalogue
marks the recommended pair; the local engines (faster-whisper, XTTS) are always there
as the fallback, so a dead cloud account degrades a call instead of muting it.

Measured on this deployment (2026-09-15) for the local pair, which is why they are
the fallback and not the default: faster-whisper ``small`` on CPU needs ~2.1 s for a
6 s utterance, XTTS on GPU ~2.7 s for a 6 s phrase (no streaming) — against a phone
budget of well under a second from the end of the caller's speech to the first audio.

Credentials live in Valkey only (``phone_speech_secret:<provider>``); SQLite holds the
non-secret choices. Nothing here logs audio, transcripts or keys.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlencode

from backend import phone_audio

logger = logging.getLogger("hermes.phone_speech")

CONNECT_TIMEOUT = 8.0
TTS_READ_TIMEOUT = 20.0

CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "stt": [
        {
            "id": "deepgram", "label": "Deepgram Nova-3", "recommended": True, "needs_key": True, "streaming": True,
            "models": ["nova-3", "nova-2"], "default_model": "nova-3",
            "languages": ["multi", "ru", "en"], "default_language": "multi",
            "note": "Самая низкая задержка; русский и английский в одном разговоре (language=multi), "
                    "встроенное определение конца фразы. ~$0.0077/мин.",
        },
        {
            "id": "openai", "label": "OpenAI Realtime (gpt-4o-transcribe)", "recommended": False, "needs_key": True,
            "streaming": True, "models": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
            "default_model": "gpt-4o-mini-transcribe", "languages": ["auto", "ru", "en"], "default_language": "auto",
            "note": "Хорошая точность, один ключ с OpenAI TTS; задержка немного выше Deepgram.",
        },
        {
            "id": "yandex", "label": "Yandex SpeechKit", "recommended": False, "needs_key": True, "streaming": False,
            "models": ["general"], "default_model": "general", "languages": ["ru-RU", "en-US"],
            "default_language": "ru-RU", "needs_folder": True,
            "note": "Лучшее качество именно для русского. Фраза распознаётся целиком после паузы — "
                    "ответ начинается примерно на 0.5–1 с позже.",
        },
        {
            "id": "local", "label": "Локально (faster-whisper)", "recommended": False, "needs_key": False,
            "streaming": False, "models": ["small"], "default_model": "small", "languages": ["auto", "ru", "en"],
            "default_language": "auto",
            "note": "Бесплатно, аудио не покидает сервер; ~2 с на фразу — годится как резерв, не как основной.",
        },
    ],
    "tts": [
        {
            "id": "elevenlabs", "label": "ElevenLabs Flash v2.5", "recommended": True, "needs_key": True,
            "models": ["eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2"],
            "default_model": "eleven_flash_v2_5", "default_voice": "21m00Tcm4TlvDq8ikWAM",
            "note": "Первый звук за ~75–300 мс, естественные голоса, один голос говорит по-русски и по-английски.",
        },
        {
            "id": "openai", "label": "OpenAI TTS (gpt-4o-mini-tts)", "recommended": False, "needs_key": True,
            "models": ["gpt-4o-mini-tts", "tts-1"], "default_model": "gpt-4o-mini-tts", "default_voice": "coral",
            "voices": ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"],
            "note": "Хорошие голоса, можно задать манеру речи; задержка чуть выше ElevenLabs.",
        },
        {
            "id": "yandex", "label": "Yandex SpeechKit", "recommended": False, "needs_key": True, "needs_folder": True,
            "models": ["v3"], "default_model": "v3", "default_voice": "alena", "default_voice_en": "john",
            "voices": ["alena", "filipp", "ermil", "jane", "madirus", "omazh", "zahar", "marina", "john"],
            "note": "Очень естественный русский; английский — только голос john.",
        },
        {
            "id": "local", "label": "Локально (XTTS)", "recommended": False, "needs_key": False,
            "models": ["xtts_v2"], "default_model": "xtts_v2", "default_voice": "",
            "note": "Бесплатно, голос Vexa; ~2.7 с на фразу — только резерв.",
        },
    ],
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "stt_provider": "deepgram",
    "stt_model": "nova-3",
    "stt_language": "multi",
    "tts_provider": "elevenlabs",
    "tts_model": "eleven_flash_v2_5",
    "tts_voice": "21m00Tcm4TlvDq8ikWAM",
    "tts_voice_en": "",
    "yandex_folder_id": "",
    "fallback_local": True,
}

_schema_ready_for: Optional[str] = None
_schema_lock = threading.Lock()


class SpeechError(RuntimeError):
    """A speech service failed. The message is safe to show: no key, no body."""


# ── configuration ───────────────────────────────────────────────────────────
def _db_path() -> str:
    from backend import database

    return database.DB_PATH


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def reset_schema_cache() -> None:
    global _schema_ready_for
    _schema_ready_for = None


def _init_schema() -> None:
    global _schema_ready_for
    path = _db_path()
    if _schema_ready_for == path:
        return
    with _schema_lock:
        if _schema_ready_for == path:
            return
        with _connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS phone_speech_config (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "config TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        _schema_ready_for = path


def _provider(kind: str, provider_id: str) -> Optional[Dict[str, Any]]:
    return next((p for p in CATALOG[kind] if p["id"] == provider_id), None)


def get_config() -> Dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT config FROM phone_speech_config WHERE id = 1").fetchone()
    config = dict(DEFAULT_CONFIG)
    if row:
        try:
            stored = json.loads(row["config"])
            if isinstance(stored, dict):
                config.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
        except ValueError:
            logger.warning("phone_speech_config is unreadable — using defaults")
    return config


def validate_config(payload: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = dict(base or get_config())
    config.update({k: v for k, v in (payload or {}).items() if k in DEFAULT_CONFIG and v is not None})
    stt = _provider("stt", str(config["stt_provider"]))
    tts = _provider("tts", str(config["tts_provider"]))
    if not stt:
        raise ValueError("Неизвестный сервис распознавания")
    if not tts:
        raise ValueError("Неизвестный сервис синтеза")
    if config["stt_model"] not in stt["models"]:
        config["stt_model"] = stt["default_model"]
    if config["stt_language"] not in stt["languages"]:
        config["stt_language"] = stt["default_language"]
    if config["tts_model"] not in tts["models"]:
        config["tts_model"] = tts["default_model"]
    for key in ("tts_voice", "tts_voice_en", "yandex_folder_id"):
        value = str(config.get(key) or "").strip()
        if len(value) > 128 or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"{key}: некорректное значение")
        config[key] = value
    if "voices" in tts and config["tts_voice"] and config["tts_voice"] not in tts["voices"]:
        config["tts_voice"] = tts["default_voice"]
    config["fallback_local"] = bool(config.get("fallback_local", True))
    return config


def update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = validate_config(payload)
    _init_schema()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO phone_speech_config (id, config, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET config = excluded.config, updated_at = excluded.updated_at",
            (json.dumps(config), datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return config


def _secret_key(provider_id: str) -> str:
    return f"phone_speech_secret:{provider_id}"


def set_secret(provider_id: str, value: str) -> None:
    if provider_id not in {p["id"] for kind in CATALOG.values() for p in kind if p.get("needs_key")}:
        raise ValueError("Этому сервису ключ не нужен")
    secret = (value or "").strip()
    if not secret or len(secret) > 4096:
        raise ValueError("Пустой или слишком длинный ключ")
    from backend import valkey_client

    valkey_client.set_value(_secret_key(provider_id), secret)


def clear_secret(provider_id: str) -> None:
    from backend import valkey_client

    valkey_client.delete_value(_secret_key(provider_id))


def get_secret(provider_id: str) -> str:
    try:
        from backend import valkey_client

        return valkey_client.get_value(_secret_key(provider_id)) or ""
    except Exception:
        logger.warning("Secret store unavailable for speech provider %s", provider_id)
        return ""


def catalog_status() -> Dict[str, Any]:
    """The catalogue plus, per provider, whether it is usable right now."""
    out: Dict[str, Any] = {"config": get_config(), "stt": [], "tts": []}
    for kind in ("stt", "tts"):
        for provider in CATALOG[kind]:
            item = dict(provider)
            item["has_secret"] = bool(get_secret(provider["id"])) if provider.get("needs_key") else True
            out[kind].append(item)
    return out


# ── STT ─────────────────────────────────────────────────────────────────────
@dataclass
class STTEvent:
    kind: str  # speech_started | partial | final | error
    text: str = ""
    at: float = 0.0


class STTStream:
    """Base: an event queue plus lifecycle. Subclasses implement start/send/close."""

    name = "base"
    # Whether interim text arrives. Without it the session barges in on
    # speech_started alone; with it, it waits for a few real characters so a
    # cough does not cut the agent off.
    has_partials = True

    def __init__(self) -> None:
        self.queue: "asyncio.Queue[STTEvent]" = asyncio.Queue()
        self.closed = False

    def emit(self, kind: str, text: str = "") -> None:
        self.queue.put_nowait(STTEvent(kind, text, time.monotonic()))

    async def start(self) -> None:  # pragma: no cover - interface
        return None

    async def send(self, frame: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


class DeepgramSTT(STTStream):
    name = "deepgram"
    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, model: str = "nova-3", language: str = "multi"):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.language = language
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._keepalive: Optional[asyncio.Task] = None
        self._segments: List[str] = []

    def url(self) -> str:
        params = {
            "model": self.model, "language": self.language, "encoding": "linear16", "sample_rate": "8000",
            "channels": "1", "interim_results": "true", "endpointing": "350", "utterance_end_ms": "1000",
            "vad_events": "true", "smart_format": "true", "punctuate": "true",
        }
        return f"{self.URL}?{urlencode(params)}"

    async def start(self) -> None:
        import websockets

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.url(), additional_headers={"Authorization": f"Token {self.api_key}"},
                                   max_size=2 ** 20, ping_interval=20),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:
            raise SpeechError(f"Deepgram: не удалось подключиться ({type(exc).__name__})") from exc
        self._reader = asyncio.create_task(self._read())
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        with contextlib.suppress(Exception):
            while not self.closed:
                await asyncio.sleep(8)
                await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                self.handle_message(json.loads(raw))
        except Exception as exc:
            if not self.closed:
                logger.warning("Deepgram stream ended: %s", type(exc).__name__)
                self.emit("error", "Deepgram stream ended")

    def handle_message(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "SpeechStarted":
            self.emit("speech_started")
        elif kind == "Results":
            alternatives = ((message.get("channel") or {}).get("alternatives") or [{}])
            transcript = str(alternatives[0].get("transcript") or "").strip()
            if message.get("is_final"):
                if transcript:
                    self._segments.append(transcript)
                if message.get("speech_final") and self._segments:
                    self.emit("final", " ".join(self._segments))
                    self._segments = []
            elif transcript:
                self.emit("partial", " ".join(self._segments + [transcript]))
        elif kind == "UtteranceEnd":
            if self._segments:
                self.emit("final", " ".join(self._segments))
                self._segments = []
        elif kind == "Error" or message.get("err_code"):
            self.emit("error", "Deepgram error")

    async def send(self, frame: bytes) -> None:
        if self._ws is not None and not self.closed:
            await self._ws.send(frame)

    async def close(self) -> None:
        self.closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            with contextlib.suppress(Exception):
                await self._ws.close()
        for task in (self._reader, self._keepalive):
            if task:
                task.cancel()


class OpenAIRealtimeSTT(STTStream):
    name = "openai"
    URL = "wss://api.openai.com/v1/realtime?intent=transcription"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-transcribe", language: str = "auto"):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.language = language
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._resampler = phone_audio.Resampler(8000, 24000)
        self._partial = ""

    def session_update(self) -> Dict[str, Any]:
        transcription: Dict[str, Any] = {"model": self.model}
        if self.language and self.language != "auto":
            transcription["language"] = self.language
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": transcription,
                        "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                           "prefix_padding_ms": 300, "silence_duration_ms": 500},
                    }
                },
            },
        }

    async def start(self) -> None:
        import websockets

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.URL, additional_headers={"Authorization": f"Bearer {self.api_key}"},
                                   max_size=2 ** 20, ping_interval=20),
                timeout=CONNECT_TIMEOUT,
            )
            await self._ws.send(json.dumps(self.session_update()))
        except Exception as exc:
            raise SpeechError(f"OpenAI Realtime: не удалось подключиться ({type(exc).__name__})") from exc
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                self.handle_message(json.loads(raw))
        except Exception as exc:
            if not self.closed:
                logger.warning("OpenAI realtime stream ended: %s", type(exc).__name__)
                self.emit("error", "OpenAI stream ended")

    def handle_message(self, message: Dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        if kind == "input_audio_buffer.speech_started":
            self._partial = ""
            self.emit("speech_started")
        elif kind.endswith("input_audio_transcription.delta"):
            self._partial += str(message.get("delta") or "")
            if self._partial.strip():
                self.emit("partial", self._partial.strip())
        elif kind.endswith("input_audio_transcription.completed"):
            text = str(message.get("transcript") or "").strip()
            self._partial = ""
            if text:
                self.emit("final", text)
        elif kind == "error":
            error = message.get("error") or {}
            logger.warning("OpenAI realtime error: %s", str(error.get("code") or error.get("type") or "unknown")[:80])
            self.emit("error", "OpenAI error")

    async def send(self, frame: bytes) -> None:
        if self._ws is None or self.closed:
            return
        audio = self._resampler.convert(frame)
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode()}))

    async def close(self) -> None:
        self.closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._reader:
            self._reader.cancel()


class VADBatchSTT(STTStream):
    """For recognisers without streaming: our own VAD ends the turn, the whole
    utterance is recognised at once. Barge-in still works — ``speech_started`` fires
    from the VAD the moment the caller starts talking."""

    name = "vad-batch"
    has_partials = False

    def __init__(self) -> None:
        super().__init__()
        self.vad = phone_audio.EnergyVAD()
        self._tasks: List[asyncio.Task] = []

    async def recognize(self, pcm: bytes) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    async def send(self, frame: bytes) -> None:
        if self.closed:
            return
        event = self.vad.process(frame)
        if event is None:
            return
        if event.kind == "speech_start":
            self.emit("speech_started")
        elif event.kind == "speech_end" and phone_audio.split_pcm_duration_ms(event.audio) >= 300:
            self._tasks.append(asyncio.create_task(self._recognize_and_emit(event.audio)))

    async def _recognize_and_emit(self, pcm: bytes) -> None:
        try:
            text = (await self.recognize(pcm)).strip()
        except Exception as exc:
            logger.warning("%s recognition failed: %s", self.name, type(exc).__name__)
            self.emit("error", f"{self.name} recognition failed")
            return
        if text:
            self.emit("final", text)

    async def close(self) -> None:
        self.closed = True
        for task in self._tasks:
            task.cancel()


class YandexSTT(VADBatchSTT):
    name = "yandex"
    URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

    def __init__(self, api_key: str, folder_id: str = "", language: str = "ru-RU"):
        super().__init__()
        self.api_key = api_key
        self.folder_id = folder_id
        self.language = language

    async def recognize(self, pcm: bytes) -> str:
        import httpx

        params = {"lang": self.language, "format": "lpcm", "sampleRateHertz": "8000"}
        if self.folder_id:
            params["folderId"] = self.folder_id
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.post(self.URL, params=params, content=pcm,
                                         headers={"Authorization": f"Api-Key {self.api_key}"})
        if response.status_code != 200:
            raise SpeechError(f"Yandex STT HTTP {response.status_code}")
        return str((response.json() or {}).get("result") or "")


class LocalWhisperSTT(VADBatchSTT):
    name = "local"

    def __init__(self, language: str = "auto"):
        super().__init__()
        self.language = None if language in ("", "auto") else language

    async def recognize(self, pcm: bytes) -> str:
        return await asyncio.to_thread(self._recognize_sync, pcm)

    def _recognize_sync(self, pcm: bytes) -> str:
        import numpy as np

        from backend import voice

        model = voice._get_model()
        audio16 = phone_audio.resample(pcm, 8000, 16000)
        samples = np.frombuffer(audio16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = model.transcribe(samples, language=self.language, beam_size=1, vad_filter=False)
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


class FallbackSTT(STTStream):
    """Primary stream until it fails, then local recognition for the rest of the call."""

    name = "fallback"

    def __init__(self, primary: STTStream, fallback_factory):
        super().__init__()
        self.primary = primary
        self.fallback_factory = fallback_factory
        self.active: STTStream = primary
        self.degraded = False
        self._pump: Optional[asyncio.Task] = None

    @property
    def has_partials(self) -> bool:  # type: ignore[override]
        return self.active.has_partials

    async def start(self) -> None:
        try:
            await self.primary.start()
        except Exception as exc:
            logger.warning("Primary STT %s unavailable (%s) — local fallback", self.primary.name, exc)
            await self._switch()
        self._pump = asyncio.create_task(self._forward())

    async def _switch(self) -> None:
        if self.degraded or self.fallback_factory is None:
            return
        self.degraded = True
        with contextlib.suppress(Exception):
            await self.active.close()
        self.active = self.fallback_factory()
        await self.active.start()
        self.emit("degraded", self.active.name)

    async def _forward(self) -> None:
        while not self.closed:
            source = self.active
            event = await source.queue.get()
            if event.kind == "error" and source is self.primary and self.fallback_factory is not None:
                await self._switch()
                continue
            self.queue.put_nowait(event)

    async def send(self, frame: bytes) -> None:
        await self.active.send(frame)

    async def close(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            await self.active.close()
        if self._pump:
            self._pump.cancel()


# ── TTS ─────────────────────────────────────────────────────────────────────
def detect_language(text: str) -> str:
    cyrillic = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return "en" if latin > cyrillic else "ru"


class TTSEngine:
    name = "base"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:  # pragma: no cover - interface
        raise NotImplementedError
        yield b""


class _HttpStreamTTS(TTSEngine):
    source_rate = 8000

    async def _stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[bytes]:
        import httpx

        resampler = phone_audio.Resampler(self.source_rate, 8000)
        carry = b""
        timeout = httpx.Timeout(TTS_READ_TIMEOUT, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise SpeechError(f"{self.name} HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    data = carry + chunk
                    usable = len(data) - (len(data) % 2)
                    carry = data[usable:]
                    if usable:
                        yield resampler.convert(data[:usable])


class ElevenLabsTTS(_HttpStreamTTS):
    name = "elevenlabs"
    source_rate = 16000

    def __init__(self, api_key: str, voice_id: str, model: str = "eleven_flash_v2_5", voice_id_en: str = ""):
        self.api_key = api_key
        self.voice_id = voice_id or "21m00Tcm4TlvDq8ikWAM"
        self.voice_id_en = voice_id_en
        self.model = model

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        language = detect_language(text)
        voice = self.voice_id_en if (language == "en" and self.voice_id_en) else self.voice_id
        body: Dict[str, Any] = {"text": text, "model_id": self.model}
        if self.model in ("eleven_flash_v2_5", "eleven_turbo_v2_5"):
            body["language_code"] = language
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=pcm_16000"
        async for chunk in self._stream("POST", url, json=body, headers={"xi-api-key": self.api_key}):
            yield chunk


class OpenAITTS(_HttpStreamTTS):
    name = "openai"
    source_rate = 24000

    def __init__(self, api_key: str, voice: str = "coral", model: str = "gpt-4o-mini-tts"):
        self.api_key = api_key
        self.voice = voice or "coral"
        self.model = model

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        body: Dict[str, Any] = {"model": self.model, "voice": self.voice, "input": text, "response_format": "pcm"}
        if self.model == "gpt-4o-mini-tts":
            body["instructions"] = "Говори естественно и дружелюбно, как вежливый сотрудник по телефону."
        async for chunk in self._stream("POST", "https://api.openai.com/v1/audio/speech", json=body,
                                        headers={"Authorization": f"Bearer {self.api_key}"}):
            yield chunk


class YandexTTS(TTSEngine):
    name = "yandex"
    URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"

    def __init__(self, api_key: str, voice: str = "alena", voice_en: str = "john", folder_id: str = ""):
        self.api_key = api_key
        self.voice = voice or "alena"
        self.voice_en = voice_en or "john"
        self.folder_id = folder_id

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        import httpx

        voice = self.voice_en if detect_language(text) == "en" else self.voice
        body = {
            "text": text,
            "hints": [{"voice": voice}],
            "outputAudioSpec": {"rawAudio": {"audioEncoding": "LINEAR16_PCM", "sampleRateHertz": 8000}},
        }
        headers = {"Authorization": f"Api-Key {self.api_key}"}
        if self.folder_id:
            headers["x-folder-id"] = self.folder_id
        decoder = json.JSONDecoder()
        buffer = ""
        async with httpx.AsyncClient(timeout=httpx.Timeout(TTS_READ_TIMEOUT, connect=5.0)) as client:
            async with client.stream("POST", self.URL, json=body, headers=headers) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise SpeechError(f"Yandex TTS HTTP {response.status_code}")
                async for piece in response.aiter_text():
                    buffer += piece
                    while True:
                        stripped = buffer.lstrip()
                        if not stripped:
                            buffer = ""
                            break
                        try:
                            obj, end = decoder.raw_decode(stripped)
                        except ValueError:
                            buffer = stripped
                            break
                        buffer = stripped[end:]
                        data = (((obj or {}).get("result") or {}).get("audioChunk") or {}).get("data")
                        if data:
                            yield base64.b64decode(data)


class LocalXTTS(TTSEngine):
    name = "local"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        import httpx

        from backend import tts

        config = tts._tts_config()
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            response = await client.post(f"{config['xtts_url']}/synthesize",
                                         json={"text": text, "language": detect_language(text), "speed": 1.0})
        if response.status_code != 200 or not response.content:
            raise SpeechError(f"XTTS HTTP {response.status_code}")
        pcm = phone_audio.wav_to_pcm8k(response.content)
        for start in range(0, len(pcm), 3200):
            yield pcm[start:start + 3200]


class FallbackTTS(TTSEngine):
    """Primary engine; if it fails *before* producing any audio for a phrase, the
    fallback speaks that phrase instead. A failure mid-phrase ends the phrase — a
    second voice restarting the same sentence would sound worse than a cut."""

    name = "fallback"

    def __init__(self, primary: TTSEngine, fallback: Optional[TTSEngine]):
        self.primary = primary
        self.fallback = fallback
        self.failures = 0

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        produced = False
        try:
            async for chunk in self.primary.synthesize(text):
                produced = True
                yield chunk
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            logger.warning("TTS %s failed (%s)", self.primary.name, type(exc).__name__)
            if produced or self.fallback is None:
                return
        async for chunk in self.fallback.synthesize(text):
            yield chunk


# ── builders ────────────────────────────────────────────────────────────────
def build_stt(config: Optional[Dict[str, Any]] = None) -> STTStream:
    config = config or get_config()
    provider = config["stt_provider"]
    local_factory = (lambda: LocalWhisperSTT(_local_language(config))) if config.get("fallback_local", True) else None
    key = get_secret(provider) if provider != "local" else ""
    primary: Optional[STTStream] = None
    if provider == "deepgram" and key:
        primary = DeepgramSTT(key, config["stt_model"], config["stt_language"])
    elif provider == "openai" and key:
        primary = OpenAIRealtimeSTT(key, config["stt_model"], config["stt_language"])
    elif provider == "yandex" and key:
        primary = YandexSTT(key, config.get("yandex_folder_id") or "", config["stt_language"])
    elif provider == "local":
        return LocalWhisperSTT(_local_language(config))
    if primary is None:
        if local_factory is None:
            raise SpeechError("Сервис распознавания не настроен: нет ключа")
        logger.warning("STT provider %s has no key — using local recognition", provider)
        return LocalWhisperSTT(_local_language(config))
    return FallbackSTT(primary, local_factory)


def _local_language(config: Dict[str, Any]) -> str:
    language = str(config.get("stt_language") or "auto")
    if language.startswith("ru"):
        return "ru"
    if language.startswith("en"):
        return "en"
    return "auto"


def build_tts(config: Optional[Dict[str, Any]] = None) -> TTSEngine:
    config = config or get_config()
    provider = config["tts_provider"]
    fallback = LocalXTTS() if config.get("fallback_local", True) else None
    key = get_secret(provider) if provider != "local" else ""
    primary: Optional[TTSEngine] = None
    if provider == "elevenlabs" and key:
        primary = ElevenLabsTTS(key, config["tts_voice"], config["tts_model"], config.get("tts_voice_en") or "")
    elif provider == "openai" and key:
        primary = OpenAITTS(key, config["tts_voice"], config["tts_model"])
    elif provider == "yandex" and key:
        primary = YandexTTS(key, config["tts_voice"], config.get("tts_voice_en") or "", config.get("yandex_folder_id") or "")
    elif provider == "local":
        return LocalXTTS()
    if primary is None:
        if fallback is None:
            raise SpeechError("Сервис синтеза не настроен: нет ключа")
        logger.warning("TTS provider %s has no key — using local synthesis", provider)
        return fallback
    return FallbackTTS(primary, fallback)


async def test_tts(text: str = "Здравствуйте! Это проверка голоса.") -> Dict[str, Any]:
    """Renders one phrase with the configured engine and reports time to first audio."""
    engine = build_tts()
    started = time.monotonic()
    first = None
    total = 0
    try:
        async for chunk in engine.synthesize(text):
            if first is None:
                first = time.monotonic() - started
            total += len(chunk)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "engine": engine.name}
    return {
        "ok": total > 0, "engine": getattr(getattr(engine, "primary", engine), "name", engine.name),
        "first_audio_ms": int((first or 0) * 1000), "total_ms": int((time.monotonic() - started) * 1000),
        "audio_ms": phone_audio.split_pcm_duration_ms(b"\x00" * total),
        "degraded": bool(getattr(engine, "failures", 0)),
    }
