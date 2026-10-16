"""Phone audio plumbing: codecs, framing, VAD, phrasing, numbers."""

import math
import struct

from backend import phone_audio


def _tone(ms: int, amplitude: int = 8000, freq: int = 440) -> bytes:
    samples = phone_audio.SAMPLE_RATE * ms // 1000
    return b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * n / 8000))) for n in range(samples))


def _silence(ms: int) -> bytes:
    return b"\x00" * (phone_audio.SAMPLE_RATE * phone_audio.SAMPLE_WIDTH * ms // 1000)


def test_ulaw_round_trip_keeps_the_signal():
    pcm = _tone(100)
    back = phone_audio.ulaw_to_pcm(phone_audio.pcm_to_ulaw(pcm))
    assert len(back) == len(pcm)
    assert abs(phone_audio.rms(back) - phone_audio.rms(pcm)) < phone_audio.rms(pcm) * 0.05


def test_frame_buffer_reslices_and_keeps_the_remainder():
    buffer = phone_audio.FrameBuffer()
    assert buffer.push(b"\x01" * 500) == [b"\x01" * 320]
    assert buffer.push(b"\x01" * 140) == [b"\x01" * 320]
    assert buffer.flush() is None


def test_stateful_resampling_changes_length_by_the_rate_ratio():
    resampler = phone_audio.Resampler(8000, 24000)
    out = b"".join(resampler.convert(frame) for frame in phone_audio.frames(_tone(1000)))
    assert abs(len(out) - 48000) < 200


def test_wav_of_another_rate_comes_back_as_8k_pcm():
    wav = phone_audio.pcm_to_wav(phone_audio.resample(_tone(500), 8000, 24000), rate=24000)
    pcm = phone_audio.wav_to_pcm8k(wav)
    assert abs(len(pcm) - 8000) < 100


def test_vad_ignores_line_noise_and_detects_a_real_utterance():
    vad = phone_audio.EnergyVAD()
    events = []
    audio = _silence(500) + _tone(60, amplitude=3000) + _silence(500) + _tone(800) + _silence(900)
    for frame in phone_audio.frames(audio):
        event = vad.process(frame)
        if event:
            events.append(event)
    assert [e.kind for e in events] == ["speech_start", "speech_end"]
    assert phone_audio.split_pcm_duration_ms(events[1].audio) >= 800


def test_phrases_are_cut_at_sentence_ends_and_the_first_one_early():
    chunker = phone_audio.PhraseChunker()
    out = []
    for token in ["Здравствуйте", "! ", "Да, мы ", "работаем ", "в субботу", " с десяти", " утра. ", "Чем ещё помочь"]:
        out.extend(chunker.push(token))
    out.append(chunker.flush())
    assert out == ["Здравствуйте!", "Да, мы работаем в субботу с десяти утра.", "Чем ещё помочь"]


def test_long_phrase_without_punctuation_is_still_released():
    chunker = phone_audio.PhraseChunker(max_chars=80)
    phrases = chunker.push("слово " * 30)
    assert phrases and all(len(p) <= 80 for p in phrases)


def test_speakable_strips_markdown_links_and_emoji():
    assert phone_audio.speakable("**Цена**: [сайт](https://x.ru) 😀 от `100`") == "Цена : сайт от 100"


def test_e164_normalisation():
    assert phone_audio.normalize_e164("+7 (900) 123-45-67") == "+79001234567"
    assert phone_audio.normalize_e164("sip:+972501234567@sip.zadarma.com") == "+972501234567"
    assert phone_audio.normalize_e164("0048221234567") == "+48221234567"
    assert phone_audio.normalize_e164("anonymous") == ""
