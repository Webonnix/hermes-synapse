import ast
import json
from pathlib import Path

import pytest

from backend import tts as tts_module
from backend.tts import QWEN3_REFERENCE_TEXT, get_tts_status, synthesize_speech

QWEN3_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
# Captured before the autouse fixture stubs it out, for the one test that needs it.
_REAL_QWEN3_HEALTH = tts_module._qwen3_health
XTTS_READY = {"ready": True, "model": "xtts_v2", "speaker": "vexa-reference.wav"}


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(autouse=True)
def offline_sidecars(monkeypatch):
    """No test may reach a GPU service: both health probes default to offline."""
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: None)
    monkeypatch.setattr(tts_module, "_qwen3_health", lambda config: None)
    monkeypatch.setenv("VOICE_TTS_ENABLED", "true")
    for name in ("VOICE_TTS_QWEN3_VOICE", "VOICE_TTS_QWEN3_URL", "VOICE_TTS_QWEN3_REF_AUDIO", "VOICE_TTS_QWEN3_REF_TEXT"):
        monkeypatch.delenv(name, raising=False)


def _qwen3_serving(monkeypatch):
    monkeypatch.setattr(tts_module, "_qwen3_health", lambda config: {"models": [QWEN3_MODEL]})


def test_auto_never_picks_qwen3_even_when_it_is_serving(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "auto")
    _qwen3_serving(monkeypatch)
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: XTTS_READY)

    status = get_tts_status()

    assert status["active_provider"] == "xtts"
    assert "qwen3" not in [item["provider"] for item in status["providers"]]


def test_opting_in_to_qwen3_makes_it_the_active_provider(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "qwen3")
    _qwen3_serving(monkeypatch)
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: XTTS_READY)

    status = get_tts_status()

    assert status["active_provider"] == "qwen3"
    assert status["voice"] == "vexa-reference.wav"
    assert status["available"] is True


def test_qwen3_without_its_model_loaded_falls_back_to_xtts(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "qwen3")
    # Reachable, but vLLM lists no model yet (still loading weights).
    monkeypatch.setattr(tts_module, "_qwen3_health", lambda config: {"models": []})
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: XTTS_READY)

    assert get_tts_status()["active_provider"] == "xtts"


@pytest.mark.parametrize(
    ("text", "language"),
    [("Привет, как дела?", "Russian"), ("Hello there, how are you?", "English")],
)
def test_qwen3_clones_the_vexa_reference_in_the_detected_language(monkeypatch, tmp_path, text, language):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "qwen3")
    _qwen3_serving(monkeypatch)
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"RIFFqwen-wav")

    monkeypatch.setattr(tts_module.urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "answer.wav"

    result = synthesize_speech(text, str(output))

    assert sent["url"] == "http://qwen3-tts:8091/v1/audio/speech"
    payload = sent["payload"]
    assert payload["model"] == QWEN3_MODEL
    assert payload["input"] == text
    assert payload["task_type"] == "Base"
    assert payload["language"] == language
    assert payload["response_format"] == "wav"
    assert payload["ref_audio"] == "file:///xtts-speakers/vexa-reference.wav"
    assert payload["ref_text"] == QWEN3_REFERENCE_TEXT
    assert "voice" not in payload
    assert "speed" not in payload
    assert result == {"provider": "qwen3", "voice": "vexa-reference.wav", "size_bytes": len(b"RIFFqwen-wav")}
    assert output.read_bytes() == b"RIFFqwen-wav"


def test_qwen3_uses_an_uploaded_voice_instead_of_the_reference_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "qwen3")
    monkeypatch.setenv("VOICE_TTS_QWEN3_VOICE", "vexa")
    _qwen3_serving(monkeypatch)
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"RIFFqwen-wav")

    monkeypatch.setattr(tts_module.urllib.request, "urlopen", fake_urlopen)

    result = synthesize_speech("Привет.", str(tmp_path / "answer.wav"), rate=1.2)

    assert sent["payload"]["voice"] == "vexa"
    assert "ref_audio" not in sent["payload"]
    assert "ref_text" not in sent["payload"]
    assert sent["payload"]["speed"] == 1.2
    assert result["voice"] == "vexa"


def test_failing_qwen3_falls_back_to_xtts_instead_of_going_mute(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "qwen3")
    _qwen3_serving(monkeypatch)
    monkeypatch.setattr(tts_module, "_xtts_health", lambda config: XTTS_READY)
    called = []

    def fake_urlopen(request, timeout=None):
        called.append(request.full_url)
        if request.full_url.endswith("/v1/audio/speech"):
            raise OSError("connection refused")
        return _FakeResponse(b"RIFFxtts-wav")

    monkeypatch.setattr(tts_module.urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "answer.wav"

    result = synthesize_speech("Привет, как дела?", str(output))

    assert result["provider"] == "xtts"
    assert called == ["http://qwen3-tts:8091/v1/audio/speech", "http://xtts:8700/synthesize"]
    assert output.read_bytes() == b"RIFFxtts-wav"


def test_qwen3_health_reads_the_served_models(monkeypatch):
    monkeypatch.setattr(tts_module, "_qwen3_health", _REAL_QWEN3_HEALTH)
    monkeypatch.setitem(tts_module._qwen3_health_cache, "expires_at", 0.0)
    requested = []

    def fake_urlopen(url, timeout=None):
        requested.append(url)
        return _FakeResponse(json.dumps({"object": "list", "data": [{"id": QWEN3_MODEL}]}).encode())

    monkeypatch.setattr(tts_module.urllib.request, "urlopen", fake_urlopen)

    health = tts_module._qwen3_health(tts_module._tts_config())

    assert requested == ["http://qwen3-tts:8091/v1/models"]
    assert health == {"models": [QWEN3_MODEL]}


def test_qwen3_reference_text_matches_what_the_xtts_reference_clip_says():
    # The Base task clones in context: a transcript that drifts from the clip
    # (built by src/xtts/server.py) degrades the cloned voice silently.
    server = Path(__file__).resolve().parents[2] / "xtts" / "server.py"
    tree = ast.parse(server.read_text(encoding="utf-8"))
    reference = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "REFERENCE_TEXT" for t in node.targets)
    )

    assert ast.literal_eval(reference) == QWEN3_REFERENCE_TEXT
