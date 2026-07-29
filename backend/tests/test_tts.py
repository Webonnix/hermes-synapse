import json
import subprocess
from pathlib import Path

import pytest


class _FakeResponse:
    """Minimal stand-in for the object urlopen hands back as a context manager."""

    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

from backend import tts as tts_module
from backend.tts import (
    VoiceSynthesisError,
    _detect_language_for_text,
    _select_piper_model,
    get_tts_status,
    synthesize_speech,
)


@pytest.fixture(autouse=True)
def offline_xtts(monkeypatch):
    """Default every test to an unreachable XTTS sidecar — no test should depend
    on a GPU service, and the health probe must never hit the network. Tests that
    need a live sidecar re-patch `_xtts_health` themselves."""
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: None)


def test_tts_is_opt_in_and_never_downloads_models(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_ENABLED", "false")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "auto")

    status = get_tts_status()

    assert status["enabled"] is False
    assert status["available"] is False
    assert status["browser_fallback"] is True
    assert status["downloads_models"] is False


def test_disabled_tts_rejects_synthesis_without_creating_audio(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TTS_ENABLED", "false")
    output = tmp_path / "answer.wav"

    with pytest.raises(VoiceSynthesisError, match="VOICE_TTS_ENABLED"):
        synthesize_speech("Привет. Я Vexa.", str(output))

    assert not Path(output).exists()


def test_select_piper_model_picks_english_model_for_latin_text():
    config = {"piper_model": "/models/ru.onnx", "piper_model_en": "/models/en.onnx"}

    assert _select_piper_model("Hello there, how are you?", config) == "/models/en.onnx"
    assert _select_piper_model("Привет, как дела?", config) == "/models/ru.onnx"


def test_select_piper_model_falls_back_without_english_model():
    config = {"piper_model": "/models/ru.onnx", "piper_model_en": ""}

    assert _select_piper_model("Hello there, how are you?", config) == "/models/ru.onnx"


def test_tts_status_api_reports_safe_fallback(monkeypatch):
    from backend.auth import active_sessions
    from backend.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VOICE_TTS_ENABLED", "false")
    active_sessions.add("tts-test-token")
    client = TestClient(app, headers={"Authorization": "Bearer tts-test-token"})

    response = client.get("/api/voice/tts/status")

    assert response.status_code == 200
    assert response.json()["browser_fallback"] is True


def test_tts_api_returns_service_unavailable_when_not_configured(monkeypatch):
    from backend.auth import active_sessions
    from backend.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("VOICE_TTS_ENABLED", "false")
    active_sessions.add("tts-test-token")
    client = TestClient(app, headers={"Authorization": "Bearer tts-test-token"})

    response = client.post("/api/voice/synthesize", json={"text": "Проверка Vexa."})

    assert response.status_code == 503
    assert "VOICE_TTS_ENABLED" in response.json()["detail"]


def test_detect_language_picks_the_script_the_reply_is_written_in():
    assert _detect_language_for_text("Hello there, how are you?") == "en"
    assert _detect_language_for_text("Привет, как дела?") == "ru"


def test_auto_provider_prefers_xtts_so_both_languages_share_one_voice(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_ENABLED", "true")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "auto")
    monkeypatch.setattr(
        tts_module,
        "_xtts_health",
        lambda config: {"ready": True, "model": "xtts_v2", "speaker": "vexa-reference.wav"},
    )

    status = get_tts_status()

    assert status["active_provider"] == "xtts"
    assert status["voice"] == "vexa-reference.wav"
    assert status["available"] is True


def test_loading_xtts_is_not_used_until_its_weights_are_ready(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_ENABLED", "true")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "xtts")
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: {"ready": False, "model": "xtts_v2"})

    status = get_tts_status()

    assert status["available"] is False
    assert status["active_provider"] is None


def test_xtts_synthesis_sends_the_detected_language_and_writes_the_wav(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TTS_ENABLED", "true")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "xtts")
    monkeypatch.setattr(
        tts_module,
        "_xtts_health",
        lambda config: {"ready": True, "model": "xtts_v2", "speaker": "vexa-reference.wav"},
    )
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"RIFFfake-wav", {"X-Vexa-XTTS-Speaker": "vexa-reference.wav"})

    monkeypatch.setattr(tts_module.urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "answer.wav"

    result = synthesize_speech("Привет, как дела?", str(output))

    assert sent["payload"]["language"] == "ru"
    assert sent["url"].endswith("/synthesize")
    assert result["provider"] == "xtts"
    assert result["voice"] == "vexa-reference.wav"
    assert output.read_bytes() == b"RIFFfake-wav"


def test_failing_xtts_falls_back_to_piper_instead_of_going_mute(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TTS_ENABLED", "true")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "auto")
    monkeypatch.setattr(
        tts_module,
        "_xtts_health",
        lambda config: {"ready": True, "model": "xtts_v2", "speaker": "vexa-reference.wav"},
    )

    def failing_urlopen(request, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(tts_module.urllib.request, "urlopen", failing_urlopen)

    # A Piper install that is present and ready, so it can take over.
    piper_model = tmp_path / "ru.onnx"
    piper_model.write_bytes(b"model")
    monkeypatch.setenv("VOICE_TTS_PIPER_MODEL", str(piper_model))
    monkeypatch.setattr(tts_module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    output = tmp_path / "answer.wav"

    def fake_run(command, **kwargs):
        Path(output).write_bytes(b"RIFFpiper-wav")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(tts_module.subprocess, "run", fake_run)

    result = synthesize_speech("Привет, как дела?", str(output))

    assert result["provider"] == "piper"
    assert output.read_bytes() == b"RIFFpiper-wav"
