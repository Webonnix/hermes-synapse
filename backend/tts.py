import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


class VoiceSynthesisError(RuntimeError):
    """Raised when a configured local TTS provider cannot synthesize speech."""


# Exactly what src/xtts/server.py had Piper read to build vexa-reference.wav —
# Qwen3-TTS's Base task clones in context, so it needs the transcript of the
# reference clip alongside the audio itself.
QWEN3_REFERENCE_TEXT = (
    "Hello, I am Vexa, your autonomous voice control centre. "
    "All systems are online and every agent is standing by for your command. "
    "I can read your messages, run your tasks, and answer questions at any hour. "
    "Would you like me to walk you through what changed today? "
    "Just say the word, and I will take care of the rest."
)
_QWEN3_LANGUAGES = {"ru": "Russian", "en": "English"}


def _tts_config() -> Dict[str, Any]:
    provider = (os.getenv("VOICE_TTS_PROVIDER", "auto").strip() or "auto").lower()
    return {
        "enabled": os.getenv("VOICE_TTS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        "provider": provider,
        # XTTS-v2 sidecar (src/xtts): one multilingual model, so Russian and
        # English replies share a single voice instead of Piper's two
        # per-language ones. Preferred over Piper whenever it answers /health.
        "xtts_url": (os.getenv("VOICE_TTS_XTTS_URL", "http://xtts:8700").strip() or "http://xtts:8700").rstrip("/"),
        "xtts_timeout_seconds": max(5, int(os.getenv("VOICE_TTS_XTTS_TIMEOUT_SECONDS", "120"))),
        # Qwen3-TTS on vLLM-Omni (compose service qwen3-tts), selected with
        # VOICE_TTS_PROVIDER=qwen3; "auto" never picks it. By default it clones the
        # same reference XTTS used, read from XTTS's speaker volume;
        # VOICE_TTS_QWEN3_VOICE switches to a voice uploaded once via
        # POST /v1/audio/voices instead.
        "qwen3_url": (os.getenv("VOICE_TTS_QWEN3_URL", "http://qwen3-tts:8091").strip() or "http://qwen3-tts:8091").rstrip("/"),
        "qwen3_model": os.getenv("VOICE_TTS_QWEN3_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base").strip()
        or "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "qwen3_voice": os.getenv("VOICE_TTS_QWEN3_VOICE", "").strip(),
        "qwen3_ref_audio": os.getenv("VOICE_TTS_QWEN3_REF_AUDIO", "file:///xtts-speakers/vexa-reference.wav").strip()
        or "file:///xtts-speakers/vexa-reference.wav",
        "qwen3_ref_text": os.getenv("VOICE_TTS_QWEN3_REF_TEXT", "").strip() or QWEN3_REFERENCE_TEXT,
        "qwen3_timeout_seconds": max(5, int(os.getenv("VOICE_TTS_QWEN3_TIMEOUT_SECONDS", "60"))),
        "piper_binary": os.getenv("VOICE_TTS_PIPER_BINARY", "piper").strip() or "piper",
        "piper_model": os.getenv("VOICE_TTS_PIPER_MODEL", "").strip(),
        # Piper models are single-language (a ru_RU model mangles English the same
        # way RHVoice's Russian voice does) — an optional English model lets
        # synthesize_speech pick per reply, mirroring voice/voice_en below.
        "piper_model_en": os.getenv("VOICE_TTS_PIPER_MODEL_EN", "").strip(),
        "rhvoice_binary": os.getenv("VOICE_TTS_RHVOICE_BINARY", "RHVoice-test").strip() or "RHVoice-test",
        "voice": os.getenv("VOICE_TTS_VOICE", "").strip(),
        # RHVoice's Russian voices mangle English text (and vice versa) — a second,
        # English-language voice lets synthesize_speech auto-pick per reply instead of
        # always using the Russian one. rhvoice-english must be installed (Dockerfile).
        "voice_en": os.getenv("VOICE_TTS_VOICE_EN", "slt").strip(),
        "timeout_seconds": max(2, int(os.getenv("VOICE_TTS_TIMEOUT_SECONDS", "30"))),
        "max_chars": max(200, int(os.getenv("VOICE_TTS_MAX_CHARS", "4000"))),
    }


def _is_latin_dominant(text: str) -> bool:
    cyrillic = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return latin > cyrillic


def _detect_voice_for_text(text: str, config: Dict[str, Any]) -> str:
    """Pick the Russian or English default voice based on the dominant script in
    `text`, so an English reply isn't read with the Russian phonetic engine (which
    RHVoice will otherwise attempt, producing a mangled/transliterated result)."""
    if _is_latin_dominant(text) and config.get("voice_en"):
        return config["voice_en"]
    return config["voice"]


def _select_piper_model(text: str, config: Dict[str, Any]) -> str:
    """Same idea as `_detect_voice_for_text`, but for Piper's per-language .onnx
    models — a Russian Piper voice reads English text phonetically, mangled the
    same way a mismatched RHVoice voice would."""
    if _is_latin_dominant(text) and config.get("piper_model_en"):
        return config["piper_model_en"]
    return config["piper_model"]


def _detect_language_for_text(text: str) -> str:
    """XTTS language code for `text` — the model is multilingual, but it still
    needs to be told which phonemizer to read the sentence with."""
    return "en" if _is_latin_dominant(text) else "ru"


def _binary_available(binary: str) -> bool:
    return bool(shutil.which(binary))


# The XTTS sidecar loads ~2 GB of weights onto the GPU, so /health is cheap but
# not free, and it is probed on every status call *and* every synthesis. A short
# TTL keeps a burst of replies from turning into a burst of probes, while still
# noticing a restarted sidecar within seconds.
_XTTS_HEALTH_TTL_SECONDS = 5.0
_xtts_health_cache: Dict[str, Any] = {"expires_at": 0.0, "url": "", "health": None}


def _xtts_health(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = config["xtts_url"]
    now = time.monotonic()
    if _xtts_health_cache["url"] == url and _xtts_health_cache["expires_at"] > now:
        return _xtts_health_cache["health"]

    health: Optional[Dict[str, Any]]
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        health = payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        health = None

    _xtts_health_cache.update({"expires_at": now + _XTTS_HEALTH_TTL_SECONDS, "url": url, "health": health})
    return health


_qwen3_health_cache: Dict[str, Any] = {"expires_at": 0.0, "url": "", "health": None}


def _qwen3_health(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """GET /v1/models on the vLLM-Omni server: None when unreachable, else the
    served model ids. Cached like `_xtts_health` for the same reason."""
    url = config["qwen3_url"]
    now = time.monotonic()
    if _qwen3_health_cache["url"] == url and _qwen3_health_cache["expires_at"] > now:
        return _qwen3_health_cache["health"]

    health: Optional[Dict[str, Any]]
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        models = [str(item.get("id", "")) for item in data or [] if isinstance(item, dict)]
        health = {"models": models}
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError, AttributeError):
        health = None

    _qwen3_health_cache.update({"expires_at": now + _XTTS_HEALTH_TTL_SECONDS, "url": url, "health": health})
    return health


def _qwen3_voice_label(config: Dict[str, Any]) -> str:
    return config["qwen3_voice"] or config["qwen3_ref_audio"].rsplit("/", 1)[-1]


def _provider_status(provider: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if provider == "qwen3":
        health = _qwen3_health(config)
        return {
            "provider": "qwen3",
            "binary": config["qwen3_url"],
            "binary_available": health is not None,
            "model": config["qwen3_model"],
            # vLLM only lists a model once it is loaded and serving.
            "model_available": bool(health and config["qwen3_model"] in health["models"]),
            "voice": _qwen3_voice_label(config),
        }
    if provider == "xtts":
        health = _xtts_health(config)
        ready = bool(health and health.get("ready"))
        return {
            "provider": "xtts",
            "binary": config["xtts_url"],
            "binary_available": health is not None,
            "model": str(health.get("model", "")) if health else "",
            "model_available": ready,
            "voice": str(health.get("speaker") or "") if health else "",
        }
    if provider == "piper":
        model = Path(config["piper_model"]).expanduser() if config["piper_model"] else None
        return {
            "provider": "piper",
            "binary": config["piper_binary"],
            "binary_available": _binary_available(config["piper_binary"]),
            "model": str(model) if model else "",
            "model_available": bool(model and model.is_file()),
            "voice": config["voice"] or "model-default",
        }
    if provider == "rhvoice":
        return {
            "provider": "rhvoice",
            "binary": config["rhvoice_binary"],
            "binary_available": _binary_available(config["rhvoice_binary"]),
            "model": "",
            "model_available": True,
            "voice": config["voice"] or "default",
        }
    return {
        "provider": provider,
        "binary": "",
        "binary_available": False,
        "model": "",
        "model_available": False,
        "voice": config["voice"] or "default",
    }


_AUTO_PROVIDERS = ["xtts", "piper", "rhvoice"]


def _candidate_providers(config: Dict[str, Any]) -> List[str]:
    # "auto" prefers the multilingual sidecar: a single voice across languages is
    # the point, and Piper/RHVoice stay behind it as offline fallbacks.
    if config["provider"] == "auto":
        return list(_AUTO_PROVIDERS)
    # Qwen3-TTS is still young here: keep the whole "auto" chain behind it so a
    # stopped or crashed qwen3-tts container never leaves Vexa mute.
    if config["provider"] == "qwen3":
        return ["qwen3", *_AUTO_PROVIDERS]
    return [config["provider"]]


def get_tts_status() -> Dict[str, Any]:
    config = _tts_config()
    candidates = _candidate_providers(config)
    statuses = [_provider_status(provider, config) for provider in candidates]
    active = next(
        (
            item
            for item in statuses
            if item["binary_available"] and item["model_available"]
        ),
        None,
    )
    return {
        "enabled": config["enabled"],
        "configured_provider": config["provider"],
        "available": bool(config["enabled"] and active),
        "active_provider": active["provider"] if active else None,
        "voice": active["voice"] if active else config["voice"] or None,
        "providers": statuses,
        "browser_fallback": True,
        "downloads_models": False,
    }


def _ready_providers(config: Dict[str, Any]) -> List[str]:
    return [
        item["provider"]
        for item in (_provider_status(provider, config) for provider in _candidate_providers(config))
        if item["binary_available"] and item["model_available"]
    ]


def _synthesize_with_xtts(text: str, output: str, config: Dict[str, Any], rate: float) -> str:
    """POST to the XTTS sidecar and write the wav it returns. Returns the speaker
    name it used, for the caller's result payload."""
    body = json.dumps(
        {"text": text, "language": _detect_language_for_text(text), "speed": rate}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config['xtts_url']}/synthesize",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config["xtts_timeout_seconds"]) as response:
            audio = response.read()
            speaker = response.headers.get("X-Vexa-XTTS-Speaker", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:500] or str(exc)
        raise VoiceSynthesisError(f"xtts synthesis failed: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise VoiceSynthesisError(f"xtts synthesis failed: {exc}") from exc

    if not audio:
        raise VoiceSynthesisError("xtts synthesis failed: no audio produced")
    Path(output).write_bytes(audio)
    return speaker


def _synthesize_with_qwen3(text: str, output: str, config: Dict[str, Any], rate: float) -> str:
    """POST to vLLM-Omni's OpenAI-style /v1/audio/speech (Base task = voice
    cloning) and write the wav it returns. Returns the voice label it used."""
    body: Dict[str, Any] = {
        "model": config["qwen3_model"],
        "input": text,
        "task_type": "Base",
        "language": _QWEN3_LANGUAGES[_detect_language_for_text(text)],
        "response_format": "wav",
    }
    if config["qwen3_voice"]:
        body["voice"] = config["qwen3_voice"]
    else:
        body["ref_audio"] = config["qwen3_ref_audio"]
        body["ref_text"] = config["qwen3_ref_text"]
    if rate != 1.0:
        body["speed"] = rate
    request = urllib.request.Request(
        f"{config['qwen3_url']}/v1/audio/speech",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config["qwen3_timeout_seconds"]) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:500] or str(exc)
        raise VoiceSynthesisError(f"qwen3 synthesis failed: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise VoiceSynthesisError(f"qwen3 synthesis failed: {exc}") from exc

    if not audio:
        raise VoiceSynthesisError("qwen3 synthesis failed: no audio produced")
    Path(output).write_bytes(audio)
    return _qwen3_voice_label(config)


def synthesize_speech(text: str, output_path: str, voice: Optional[str] = None, rate: float = 1.0) -> Dict[str, Any]:
    config = _tts_config()
    clean_text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32).strip()
    if not clean_text:
        raise VoiceSynthesisError("Speech text is empty.")
    if len(clean_text) > config["max_chars"]:
        raise VoiceSynthesisError(f"Speech text is longer than {config['max_chars']} characters.")
    if not config["enabled"]:
        raise VoiceSynthesisError("Local speech synthesis is disabled by VOICE_TTS_ENABLED.")

    providers = _ready_providers(config)
    if not providers:
        raise VoiceSynthesisError("No configured local TTS provider is ready.")

    output = str(Path(output_path).resolve())
    last_error: Optional[VoiceSynthesisError] = None
    for index, provider in enumerate(providers):
        try:
            return _synthesize_with(provider, clean_text, output, config, voice, rate)
        except VoiceSynthesisError as exc:
            last_error = exc
            # A GPU sidecar that hiccups mid-conversation shouldn't leave Vexa
            # mute — fall through to the next ready provider (Piper/RHVoice) and
            # only surface the failure if nothing at all can speak.
            if index == len(providers) - 1:
                raise
    raise last_error or VoiceSynthesisError("No configured local TTS provider is ready.")


def _synthesize_with(
    provider: str,
    clean_text: str,
    output: str,
    config: Dict[str, Any],
    voice: Optional[str],
    rate: float,
) -> Dict[str, Any]:
    # An explicit `voice` argument (e.g. picked in Settings) always wins; otherwise
    # auto-pick the Russian or English default based on the text itself.
    selected_voice = (voice or _detect_voice_for_text(clean_text, config)).strip()
    if provider == "xtts":
        # XTTS clones one speaker for every language, so there is no per-language
        # voice to pick here — only the language the sentence is read in.
        speaker = _synthesize_with_xtts(clean_text, output, config, rate)
        return {
            "provider": "xtts",
            "voice": speaker or "xtts",
            "size_bytes": Path(output).stat().st_size,
        }
    if provider == "qwen3":
        # Same as XTTS: one cloned voice for both languages.
        return {
            "provider": "qwen3",
            "voice": _synthesize_with_qwen3(clean_text, output, config, rate),
            "size_bytes": Path(output).stat().st_size,
        }
    if provider == "piper":
        piper_model = _select_piper_model(clean_text, config)
        command = [
            config["piper_binary"],
            "-m",
            str(Path(piper_model).expanduser().resolve()),
            "-f",
            output,
        ]
        if rate != 1.0:
            command.extend(["--length-scale", f"{1 / rate:.3f}"])
    else:
        command = [config["rhvoice_binary"], "-o", output]
        if selected_voice:
            command.extend(["-p", selected_voice])
        if rate != 1.0:
            command.extend(["-r", f"{rate:.3f}"])

    try:
        completed = subprocess.run(
            command,
            input=clean_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=config["timeout_seconds"],
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VoiceSynthesisError(f"{provider} synthesis failed: {exc}") from exc

    if completed.returncode != 0 or not Path(output).is_file() or Path(output).stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "no audio produced").strip()[:500]
        raise VoiceSynthesisError(f"{provider} synthesis failed: {detail}")

    return {
        "provider": provider,
        "voice": selected_voice or "default",
        "size_bytes": Path(output).stat().st_size,
    }
