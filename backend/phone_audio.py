"""Audio plumbing for phone calls: codecs, resampling, voice activity, phrasing.

Every transport (Asterisk RTP, Twilio and Telnyx media streams) is normalised
to one internal format before anything else sees it: **16-bit signed little-endian
PCM, 8 kHz, mono, 20 ms frames (320 bytes)**. That is what a phone line carries
anyway, so nothing is gained by upsampling in the middle; a speech service that wants
another rate gets a conversion at its own edge, not in the call loop.

``audioop`` is used for μ-law and resampling. It is deprecated in Python 3.13 but the
backend image pins 3.11; ``audioop-lts`` is a drop-in when that changes.
"""

from __future__ import annotations

import io
import re
import wave
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

try:  # pragma: no cover - import shim
    import audioop  # type: ignore[import-not-found]
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore[no-redef]

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000  # 320
SILENCE_FRAME = b"\x00" * FRAME_BYTES


def ulaw_to_pcm(data: bytes) -> bytes:
    return audioop.ulaw2lin(data, SAMPLE_WIDTH)


def pcm_to_ulaw(data: bytes) -> bytes:
    return audioop.lin2ulaw(data, SAMPLE_WIDTH)


def alaw_to_pcm(data: bytes) -> bytes:
    return audioop.alaw2lin(data, SAMPLE_WIDTH)


class Resampler:
    """Stateful rate conversion. The state carries the filter across calls, so a
    stream converted chunk by chunk has no click at every chunk boundary."""

    def __init__(self, from_rate: int, to_rate: int):
        self.from_rate = from_rate
        self.to_rate = to_rate
        self._state = None

    def convert(self, pcm: bytes) -> bytes:
        if self.from_rate == self.to_rate or not pcm:
            return pcm
        if len(pcm) % SAMPLE_WIDTH:
            pcm = pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH]
        out, self._state = audioop.ratecv(pcm, SAMPLE_WIDTH, 1, self.from_rate, self.to_rate, self._state)
        return out


def resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    return Resampler(from_rate, to_rate).convert(pcm)


def rms(pcm: bytes) -> int:
    if not pcm:
        return 0
    if len(pcm) % SAMPLE_WIDTH:
        pcm = pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH]
    return audioop.rms(pcm, SAMPLE_WIDTH)


def frames(pcm: bytes, frame_bytes: int = FRAME_BYTES, pad: bool = True) -> Iterator[bytes]:
    for start in range(0, len(pcm), frame_bytes):
        chunk = pcm[start:start + frame_bytes]
        if len(chunk) < frame_bytes:
            if not pad:
                return
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        yield chunk


class FrameBuffer:
    """Re-slices arbitrarily sized audio into exact frames; keeps the remainder."""

    def __init__(self, frame_bytes: int = FRAME_BYTES):
        self.frame_bytes = frame_bytes
        self._pending = b""

    def push(self, data: bytes) -> List[bytes]:
        self._pending += data
        out = []
        while len(self._pending) >= self.frame_bytes:
            out.append(self._pending[: self.frame_bytes])
            self._pending = self._pending[self.frame_bytes:]
        return out

    def flush(self) -> Optional[bytes]:
        if not self._pending:
            return None
        chunk = self._pending + b"\x00" * (self.frame_bytes - len(self._pending))
        self._pending = b""
        return chunk


def wav_to_pcm8k(wav_bytes: bytes) -> bytes:
    """Mono 16-bit PCM at 8 kHz from a WAV of any rate/channels/width."""
    with wave.open(io.BytesIO(wav_bytes)) as reader:
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        rate = reader.getframerate()
        pcm = reader.readframes(reader.getnframes())
    if width != SAMPLE_WIDTH:
        pcm = audioop.lin2lin(pcm, width, SAMPLE_WIDTH)
    if channels == 2:
        pcm = audioop.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5)
    elif channels != 1:
        raise ValueError(f"unsupported channel count {channels}")
    return resample(pcm, rate, SAMPLE_RATE)


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


# ── voice activity ──────────────────────────────────────────────────────────
@dataclass
class VADEvent:
    kind: str  # "speech_start" | "speech_end"
    audio: bytes = b""


@dataclass
class EnergyVAD:
    """Energy detector with an adaptive noise floor.

    Used for two things only: barge-in (the caller starts talking over the agent) and
    turn ending for speech services that have no endpointing of their own. It is
    deliberately conservative about *starting* speech — a phone line's hiss or a cough
    must not cut the agent off mid-sentence — and quick about noticing it again.
    """

    start_ms: int = 160
    end_ms: int = 700
    min_threshold: int = 350
    ratio: float = 3.2
    max_utterance_ms: int = 25000
    pre_roll_ms: int = 200

    _floor: float = 200.0
    _speaking: bool = False
    _above_ms: int = 0
    _below_ms: int = 0
    _utterance_ms: int = 0
    _buffer: bytearray = field(default_factory=bytearray)
    _pre_roll: List[bytes] = field(default_factory=list)

    @property
    def speaking(self) -> bool:
        return self._speaking

    def threshold(self) -> float:
        return max(self.min_threshold, self._floor * self.ratio)

    def reset(self) -> None:
        self._speaking = False
        self._above_ms = self._below_ms = self._utterance_ms = 0
        self._buffer = bytearray()
        self._pre_roll = []

    def process(self, frame: bytes) -> Optional[VADEvent]:
        level = rms(frame)
        loud = level >= self.threshold()
        if not loud and not self._speaking:
            # The floor only learns from what is not speech, so a long monologue
            # cannot raise the threshold until the speaker is no longer "speech".
            self._floor = self._floor * 0.97 + level * 0.03
        if not self._speaking:
            self._pre_roll.append(frame)
            max_pre = max(1, self.pre_roll_ms // FRAME_MS)
            if len(self._pre_roll) > max_pre:
                self._pre_roll.pop(0)
            self._above_ms = self._above_ms + FRAME_MS if loud else 0
            if self._above_ms >= self.start_ms:
                self._speaking = True
                self._below_ms = 0
                self._utterance_ms = self._above_ms
                self._buffer = bytearray(b"".join(self._pre_roll))
                self._pre_roll = []
                return VADEvent("speech_start")
            return None
        self._buffer.extend(frame)
        self._utterance_ms += FRAME_MS
        self._below_ms = 0 if loud else self._below_ms + FRAME_MS
        if self._below_ms >= self.end_ms or self._utterance_ms >= self.max_utterance_ms:
            audio = bytes(self._buffer)
            self.reset()
            return VADEvent("speech_end", audio)
        return None


# ── what gets spoken ────────────────────────────────────────────────────────
_MARKDOWN = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"[*_#>|~]+"), " "),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
]
_WHITESPACE = re.compile(r"\s+")


def speakable(text: str) -> str:
    """What a phone voice should say: no markdown, links, tables or emoji noise."""
    value = text or ""
    for pattern, replacement in _MARKDOWN:
        value = pattern.sub(replacement, value)
    # Emoji go before the whitespace collapse, or the gap they leave reads as a
    # double space that some engines render as a pause.
    value = "".join(
        ch if (ch.isprintable() and not (0x1F000 <= ord(ch) <= 0x1FAFF)) else " " for ch in value
    )
    return _WHITESPACE.sub(" ", value).strip()


_SENTENCE_END = re.compile(r"[.!?…]+[\"»)\]]?(?=\s|$)")
_SOFT_BREAK = re.compile(r"[,;:—–](?=\s)")


class PhraseChunker:
    """Collects streamed tokens into phrases worth sending to TTS.

    A token is too small to synthesise well; a whole paragraph reintroduces the
    silence the caller would hang up on. So: cut at the first sentence end, or at a
    comma once the phrase is long enough that waiting for the full stop would delay
    the first audio. The first phrase of a turn is allowed to be shorter than the
    rest, because the gap before it is the one the caller notices."""

    def __init__(self, first_min_chars: int = 24, min_chars: int = 60, max_chars: int = 220):
        self.first_min_chars = first_min_chars
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""
        self._emitted = 0

    def push(self, token: str) -> List[str]:
        self._buffer += token or ""
        out: List[str] = []
        while True:
            phrase = self._take()
            if not phrase:
                break
            out.append(phrase)
        return out

    def _take(self) -> Optional[str]:
        text = self._buffer
        if not text.strip():
            return None
        minimum = self.first_min_chars if self._emitted == 0 else self.min_chars
        match = _SENTENCE_END.search(text)
        cut = match.end() if match else None
        if cut is None and len(text) >= minimum:
            soft = None
            for candidate in _SOFT_BREAK.finditer(text):
                if candidate.end() >= minimum:
                    soft = candidate
                    break
            if soft:
                cut = soft.end()
        if cut is None and len(text) >= self.max_chars:
            space = text.rfind(" ", 0, self.max_chars)
            cut = space if space > minimum else self.max_chars
        if cut is None:
            return None
        phrase, self._buffer = text[:cut], text[cut:]
        phrase = speakable(phrase)
        if not phrase:
            return self._take()
        self._emitted += 1
        return phrase

    def flush(self) -> Optional[str]:
        phrase = speakable(self._buffer)
        self._buffer = ""
        if phrase:
            self._emitted += 1
            return phrase
        return None


def normalize_e164(value: str) -> str:
    """'+7 (900) 123-45-67' → '+79001234567'. Returns '' for anything that is not a
    plausible phone number, so a SIP URI or 'anonymous' never becomes a CRM key."""
    text = (value or "").strip()
    if text.lower().startswith(("sip:", "tel:")):
        text = text.split(":", 1)[1].split("@", 1)[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6 or len(digits) > 15:
        return ""
    if text.startswith("00"):
        digits = digits[2:]
    return "+" + digits


def split_pcm_duration_ms(pcm: bytes) -> int:
    return len(pcm) * 1000 // (SAMPLE_RATE * SAMPLE_WIDTH)


def db_like_level(pcm: bytes) -> Tuple[int, float]:
    level = rms(pcm)
    return level, round(level / 32768.0, 4)
