"""Multilingual TTS sidecar (Coqui XTTS-v2) for Vexa's voice.

Piper — the previous voice engine — ships one .onnx model per language, so
Russian and English replies came out in two audibly different voices. XTTS-v2 is
a single multilingual model driven by a *speaker reference*, which means one
timbre reads both languages.

The reference itself is built from the Piper English voice the setup already
uses (`XTTS_REFERENCE_PIPER_MODEL`): on first boot the service synthesizes a
fixed English paragraph with Piper and hands that wav to XTTS as the speaker to
clone. The result is the same voice Vexa already had in English, now also
speaking Russian. Without a Piper model configured it falls back to one of
XTTS's built-in studio speakers (`XTTS_SPEAKER`).
"""

import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("vexa.xtts")

MODEL_NAME = os.getenv("XTTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2").strip()
DEVICE = os.getenv("XTTS_DEVICE", "cuda").strip() or "cuda"
SPEAKER = os.getenv("XTTS_SPEAKER", "Daisy Studious").strip()
SPEAKER_WAV = os.getenv("XTTS_SPEAKER_WAV", "/speakers/vexa-reference.wav").strip()
REFERENCE_PIPER_MODEL = os.getenv("XTTS_REFERENCE_PIPER_MODEL", "").strip()
REFERENCE_PIPER_BINARY = os.getenv("XTTS_REFERENCE_PIPER_BINARY", "piper").strip() or "piper"
MAX_CHARS = max(200, int(os.getenv("XTTS_MAX_CHARS", "4000")))
DEFAULT_LANGUAGE = os.getenv("XTTS_DEFAULT_LANGUAGE", "en").strip() or "en"

# Roughly 20 seconds of varied phonemes. XTTS clones from ~6s upwards, and more
# material with different sentence melodies gives a noticeably steadier clone
# than a single short line.
REFERENCE_TEXT = (
    "Hello, I am Vexa, your autonomous voice control centre. "
    "All systems are online and every agent is standing by for your command. "
    "I can read your messages, run your tasks, and answer questions at any hour. "
    "Would you like me to walk you through what changed today? "
    "Just say the word, and I will take care of the rest."
)

# XTTS-v2's own language codes. Anything else is rejected rather than silently
# read with the wrong phonemizer.
SUPPORTED_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru",
    "nl", "cs", "ar", "zh-cn", "hu", "ko", "ja", "hi",
}

app = FastAPI(title="Vexa XTTS", docs_url=None, redoc_url=None)

_model: Any = None
_model_error: str = ""
_speaker_wav_path: str = ""
_load_lock = threading.Lock()
# One GPU model, one inference at a time — XTTS is not re-entrant.
_infer_lock = threading.Lock()


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1)
    language: str = Field(default=DEFAULT_LANGUAGE, max_length=8)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


def _build_reference_wav() -> str:
    """Render the Piper English voice to a wav XTTS can clone from.

    Returns the path, or "" when no Piper model is configured/usable — the
    caller then falls back to a built-in studio speaker.
    """
    if not SPEAKER_WAV:
        return ""
    target = Path(SPEAKER_WAV)
    if target.is_file() and target.stat().st_size > 0:
        logger.info("Using existing speaker reference %s", target)
        return str(target)
    if not REFERENCE_PIPER_MODEL:
        return ""

    piper_model = Path(REFERENCE_PIPER_MODEL).expanduser()
    if not piper_model.is_file():
        logger.warning("Reference Piper model %s is missing — falling back to a built-in speaker.", piper_model)
        return ""

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [REFERENCE_PIPER_BINARY, "-m", str(piper_model.resolve()), "-f", str(target)],
            input=REFERENCE_TEXT,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not build the speaker reference with Piper: %s", exc)
        return ""

    if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "no audio produced").strip()[:300]
        logger.warning("Piper failed to build the speaker reference: %s", detail)
        return ""

    logger.info("Built speaker reference %s from %s", target, piper_model.name)
    return str(target)


def _speaker_name() -> str:
    """What the dashboard shows next to the provider — the reference clip's name
    when cloning, otherwise the built-in studio speaker."""
    return Path(_speaker_wav_path).stem if _speaker_wav_path else SPEAKER


def _load_model() -> None:
    global _model, _model_error, _speaker_wav_path
    with _load_lock:
        if _model is not None:
            return
        try:
            # Imported lazily: pulling in torch at module import would stall the
            # HTTP server (and its health check) for the whole model load.
            from TTS.api import TTS

            _speaker_wav_path = _build_reference_wav()
            logger.info("Loading %s on %s…", MODEL_NAME, DEVICE)
            model = TTS(MODEL_NAME)
            try:
                model.to(DEVICE)
            except Exception as exc:  # noqa: BLE001 — a missing GPU must not be fatal
                logger.warning("Could not move the model to %s (%s) — staying on CPU.", DEVICE, exc)
                model.to("cpu")
            _model = model
            _model_error = ""
            logger.info(
                "XTTS ready. speaker=%s",
                _speaker_wav_path or SPEAKER,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced through /health instead
            _model_error = f"{type(exc).__name__}: {exc}"
            logger.error("XTTS failed to load: %s", _model_error)


@app.on_event("startup")
async def _startup() -> None:
    threading.Thread(target=_load_model, name="xtts-load", daemon=True).start()


@app.get("/health")
async def health() -> Dict[str, Any]:
    ready = _model is not None
    return {
        "ready": ready,
        "model": MODEL_NAME,
        "device": DEVICE,
        "speaker": _speaker_name(),
        "cloned_from_piper": bool(_speaker_wav_path),
        "languages": sorted(SUPPORTED_LANGUAGES),
        "error": _model_error,
    }


@app.get("/speakers")
async def speakers() -> Dict[str, Any]:
    """Built-in studio speakers, for picking a different XTTS_SPEAKER."""
    if _model is None:
        raise HTTPException(status_code=503, detail=_model_error or "Model is still loading.")
    return {"speakers": list(getattr(_model, "speakers", []) or [])}


def _synthesize(text: str, language: str, speed: float, output_path: str) -> None:
    kwargs: Dict[str, Any] = {"text": text, "language": language, "file_path": output_path}
    if _speaker_wav_path:
        kwargs["speaker_wav"] = _speaker_wav_path
    else:
        kwargs["speaker"] = SPEAKER
    with _infer_lock:
        try:
            _model.tts_to_file(speed=speed, **kwargs)
        except TypeError:
            # Older coqui builds don't take `speed` on this path — rate control is
            # a nicety, a missing one shouldn't cost the whole reply.
            _model.tts_to_file(**kwargs)


@app.post("/synthesize")
async def synthesize(payload: SynthesisRequest) -> Response:
    if _model is None:
        raise HTTPException(status_code=503, detail=_model_error or "Model is still loading.")

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Speech text is empty.")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"Speech text is longer than {MAX_CHARS} characters.")

    language = payload.language.strip().lower() or DEFAULT_LANGUAGE
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail=f"Language '{language}' is not supported by {MODEL_NAME}.")

    temp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(prefix="xtts_", suffix=".wav", delete=False) as tmp:
            temp_path = tmp.name
        import anyio

        await anyio.to_thread.run_sync(_synthesize, text, language, payload.speed, temp_path)
        audio = Path(temp_path).read_bytes()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — reported to the backend as 503
        logger.exception("Synthesis failed")
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    if not audio:
        raise HTTPException(status_code=503, detail="Synthesis produced no audio.")
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "X-Vexa-XTTS-Language": language,
            "X-Vexa-XTTS-Speaker": _speaker_name(),
            "Cache-Control": "no-store",
        },
    )
