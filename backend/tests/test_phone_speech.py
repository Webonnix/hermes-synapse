"""Speech adapters for calls: event mapping, streaming synthesis, fallbacks, config.

Vendor transports are mocked at the HTTP layer (respx) or driven with the messages the
vendors document, so the real parsing and resampling code runs."""

import asyncio
import base64
import json
import math
import struct

import httpx
import pytest
import respx

from backend import database, phone_audio, phone_speech


@pytest.fixture()
def speech_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "speech.db"))
    phone_speech.reset_schema_cache()
    secrets = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: secrets.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: secrets.get(k))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda k: secrets.pop(k, None))
    return secrets


def _drain(stream):
    events = []
    while not stream.queue.empty():
        events.append(stream.queue.get_nowait())
    return [(e.kind, e.text) for e in events]


def _tone(ms, amplitude=8000):
    n = 8000 * ms // 1000
    return b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(n))


# ── STT event mapping ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepgram_segments_become_one_final_turn():
    stt = phone_speech.DeepgramSTT("key")
    alt = lambda t: {"channel": {"alternatives": [{"transcript": t}]}}  # noqa: E731
    stt.handle_message({"type": "SpeechStarted"})
    stt.handle_message({"type": "Results", "is_final": False, **alt("здравствуйте")})
    stt.handle_message({"type": "Results", "is_final": True, "speech_final": False, **alt("Здравствуйте,")})
    stt.handle_message({"type": "Results", "is_final": False, **alt("вы работаете")})
    stt.handle_message({"type": "Results", "is_final": True, "speech_final": True, **alt("вы работаете в субботу?")})
    assert _drain(stt) == [
        ("speech_started", ""), ("partial", "здравствуйте"), ("partial", "Здравствуйте, вы работаете"),
        ("final", "Здравствуйте, вы работаете в субботу?"),
    ]


@pytest.mark.asyncio
async def test_deepgram_utterance_end_flushes_a_turn_without_speech_final():
    stt = phone_speech.DeepgramSTT("key")
    stt.handle_message({"type": "Results", "is_final": True, "speech_final": False,
                        "channel": {"alternatives": [{"transcript": "алло"}]}})
    stt.handle_message({"type": "UtteranceEnd"})
    stt.handle_message({"type": "UtteranceEnd"})
    assert _drain(stt) == [("final", "алло")]
    assert "encoding=linear16" in stt.url() and "sample_rate=8000" in stt.url() and "language=multi" in stt.url()


@pytest.mark.asyncio
async def test_openai_realtime_events_map_to_turns():
    stt = phone_speech.OpenAIRealtimeSTT("key", language="auto")
    assert "language" not in stt.session_update()["session"]["audio"]["input"]["transcription"]
    assert phone_speech.OpenAIRealtimeSTT("k", language="ru").session_update()["session"]["audio"]["input"]["transcription"]["language"] == "ru"
    stt.handle_message({"type": "input_audio_buffer.speech_started"})
    stt.handle_message({"type": "conversation.item.input_audio_transcription.delta", "delta": "Hello"})
    stt.handle_message({"type": "conversation.item.input_audio_transcription.delta", "delta": " there"})
    stt.handle_message({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hello there."})
    assert _drain(stt) == [("speech_started", ""), ("partial", "Hello"), ("partial", "Hello there"), ("final", "Hello there.")]


class _FakeBatch(phone_speech.VADBatchSTT):
    name = "fake"

    async def recognize(self, pcm):
        return f"{phone_audio.split_pcm_duration_ms(pcm)}ms"


@pytest.mark.asyncio
async def test_vad_batch_recogniser_announces_speech_then_the_whole_utterance():
    stt = _FakeBatch()
    audio = b"\x00" * 4000 + _tone(700) + b"\x00" * 16000
    for frame in phone_audio.frames(audio):
        await stt.send(frame)
    await asyncio.sleep(0.01)
    events = _drain(stt)
    assert events[0] == ("speech_started", "")
    assert events[1][0] == "final" and int(events[1][1][:-2]) >= 700


class _Scripted(phone_speech.STTStream):
    def __init__(self, fail_start=False, name="primary"):
        super().__init__()
        self.fail_start = fail_start
        self.name = name
        self.sent = 0

    async def start(self):
        if self.fail_start:
            raise phone_speech.SpeechError("down")

    async def send(self, frame):
        self.sent += 1


@pytest.mark.asyncio
async def test_fallback_stt_switches_when_the_primary_cannot_start():
    fallback = _Scripted(name="local")
    stream = phone_speech.FallbackSTT(_Scripted(fail_start=True), lambda: fallback)
    await stream.start()
    await stream.send(b"\x00" * 320)
    fallback.emit("final", "привет")
    await asyncio.sleep(0.01)
    assert stream.active is fallback and fallback.sent == 1
    assert [(e.kind, e.text) for e in [stream.queue.get_nowait(), stream.queue.get_nowait()]] == [("degraded", "local"), ("final", "привет")]
    await stream.close()


@pytest.mark.asyncio
async def test_fallback_stt_switches_mid_call_on_a_stream_error():
    primary, fallback = _Scripted(), _Scripted(name="local")
    stream = phone_speech.FallbackSTT(primary, lambda: fallback)
    await stream.start()
    primary.emit("final", "первое")
    primary.emit("error", "boom")
    await asyncio.sleep(0.01)
    fallback.emit("final", "второе")
    await asyncio.sleep(0.01)
    assert [e.text for e in [stream.queue.get_nowait() for _ in range(3)]] == ["первое", "local", "второе"]
    await stream.close()


# ── TTS ─────────────────────────────────────────────────────────────────────

class _Engine(phone_speech.TTSEngine):
    def __init__(self, chunks=(), fail_after=None, name="e"):
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.name = name

    async def synthesize(self, text):
        for index, chunk in enumerate(self.chunks):
            if self.fail_after is not None and index == self.fail_after:
                raise phone_speech.SpeechError("x")
            yield chunk
        if self.fail_after is not None and self.fail_after >= len(self.chunks):
            raise phone_speech.SpeechError("x")


async def _collect(engine, text="текст"):
    return [chunk async for chunk in engine.synthesize(text)]


@pytest.mark.asyncio
async def test_fallback_tts_takes_over_a_phrase_that_produced_nothing():
    engine = phone_speech.FallbackTTS(_Engine(fail_after=0), _Engine([b"B"]))
    assert await _collect(engine) == [b"B"] and engine.failures == 1


@pytest.mark.asyncio
async def test_fallback_tts_does_not_restart_a_phrase_already_being_spoken():
    engine = phone_speech.FallbackTTS(_Engine([b"A", b"A"], fail_after=1), _Engine([b"B"]))
    assert await _collect(engine) == [b"A"]


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_elevenlabs_streams_16k_pcm_resampled_to_8k_with_the_language_hint(respx_mock):
    pcm16k = b"\x10\x00" * 16000  # one second
    route = respx_mock.post(url__regex=r"https://api\.elevenlabs\.io/v1/text-to-speech/voice-1/stream.*").mock(
        return_value=httpx.Response(200, content=pcm16k[:3] + pcm16k[3:])
    )
    engine = phone_speech.ElevenLabsTTS("secret", "voice-1")
    audio = b"".join(await _collect(engine, "Привет"))
    assert abs(len(audio) - 16000) < 200
    request = route.calls.last.request
    assert request.headers["xi-api-key"] == "secret"
    assert "output_format=pcm_16000" in str(request.url)
    assert json.loads(request.content)["language_code"] == "ru"


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_openai_tts_24k_pcm_comes_back_at_8k(respx_mock):
    respx_mock.post("https://api.openai.com/v1/audio/speech").mock(return_value=httpx.Response(200, content=b"\x01\x00" * 24000))
    audio = b"".join(await _collect(phone_speech.OpenAITTS("k"), "Hello"))
    assert abs(len(audio) - 16000) < 200


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_yandex_tts_parses_a_stream_of_concatenated_json_chunks(respx_mock):
    parts = [base64.b64encode(b"\x01\x00" * 800).decode(), base64.b64encode(b"\x02\x00" * 400).decode()]
    body = "".join(json.dumps({"result": {"audioChunk": {"data": p}}}) for p in parts)
    route = respx_mock.post(phone_speech.YandexTTS.URL).mock(return_value=httpx.Response(200, content=body.encode()))
    audio = b"".join(await _collect(phone_speech.YandexTTS("k", folder_id="b1g"), "Добрый день"))
    assert audio == b"\x01\x00" * 800 + b"\x02\x00" * 400
    sent = json.loads(route.calls.last.request.content)
    assert sent["hints"] == [{"voice": "alena"}] and route.calls.last.request.headers["authorization"] == "Api-Key k"


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_a_vendor_error_is_a_speech_error_without_the_body(respx_mock):
    respx_mock.post("https://api.openai.com/v1/audio/speech").mock(return_value=httpx.Response(401, text="invalid key sk-xxx"))
    with pytest.raises(phone_speech.SpeechError) as exc:
        await _collect(phone_speech.OpenAITTS("k"))
    assert "sk-xxx" not in str(exc.value)


# ── config & builders ───────────────────────────────────────────────────────

def test_recommended_defaults_and_validation(speech_db):
    assert phone_speech.get_config()["stt_provider"] == "deepgram"
    assert [p["id"] for p in phone_speech.CATALOG["stt"] if p["recommended"]] == ["deepgram"]
    assert [p["id"] for p in phone_speech.CATALOG["tts"] if p["recommended"]] == ["elevenlabs"]
    saved = phone_speech.update_config({"stt_provider": "yandex", "stt_model": "bogus", "tts_provider": "openai", "tts_voice": "nope"})
    assert saved["stt_model"] == "general" and saved["stt_language"] == "ru-RU" and saved["tts_voice"] == "coral"
    assert phone_speech.get_config()["tts_provider"] == "openai"
    with pytest.raises(ValueError):
        phone_speech.update_config({"stt_provider": "whisper-cloud"})


def test_keys_are_write_only_and_only_for_providers_that_need_one(speech_db):
    phone_speech.set_secret("deepgram", "dg-secret")
    status = phone_speech.catalog_status()
    deepgram = next(p for p in status["stt"] if p["id"] == "deepgram")
    assert deepgram["has_secret"] is True and "dg-secret" not in json.dumps(status)
    with pytest.raises(ValueError):
        phone_speech.set_secret("local", "x")


def test_builders_fall_back_to_local_engines_without_a_key(speech_db):
    assert isinstance(phone_speech.build_stt(), phone_speech.LocalWhisperSTT)
    assert isinstance(phone_speech.build_tts(), phone_speech.LocalXTTS)
    phone_speech.set_secret("deepgram", "k")
    phone_speech.set_secret("elevenlabs", "k")
    stt = phone_speech.build_stt()
    tts = phone_speech.build_tts()
    assert isinstance(stt, phone_speech.FallbackSTT) and isinstance(stt.primary, phone_speech.DeepgramSTT)
    assert isinstance(tts, phone_speech.FallbackTTS) and isinstance(tts.primary, phone_speech.ElevenLabsTTS)
    phone_speech.update_config({"fallback_local": False})
    assert phone_speech.build_tts().fallback is None
