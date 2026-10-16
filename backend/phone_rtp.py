"""Minimal RTP (RFC 3550) for the Asterisk media leg: G.711 μ-law, 8 kHz, 20 ms.

Why RTP and not AudioSocket
───────────────────────────
The first media leg used ARI ``externalMedia`` with AudioSocket over TCP. Asterisk 22.5.2
(the Ubuntu 26.04 package) reads the AudioSocket header with a *single non-blocking*
``read()`` and hangs the channel up on ``EAGAIN`` — any moment the socket happens to be
empty when Asterisk looks is fatal. Upstream later added a poll-and-retry, but this is
the version installed. It reproduced reliably in the live test (the call died in its
first second while the session did ordinary work), whatever pacing was tried.

RTP over UDP has no such failure mode: a late or missing packet is a gap, not a
hang-up, and Asterisk's own RTP stack deals with jitter. That is what every media leg
here uses now.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass
from typing import Optional

RTP_VERSION = 2
PT_PCMU = 0
PCMU_SILENCE = b"\xff" * 160
SAMPLES_PER_FRAME = 160


@dataclass
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: bool
    payload: bytes


def parse(data: bytes) -> Optional[RtpPacket]:
    """Parses one RTP datagram; None for anything that is not RTP v2 (RTCP, junk)."""
    if len(data) < 12:
        return None
    first, second, sequence, timestamp, ssrc = struct.unpack("!BBHII", data[:12])
    if first >> 6 != RTP_VERSION:
        return None
    payload_type = second & 0x7F
    if 72 <= payload_type <= 76:  # RTCP multiplexed on the same port
        return None
    offset = 12 + 4 * (first & 0x0F)
    if first & 0x10:  # header extension
        if len(data) < offset + 4:
            return None
        ext_words = struct.unpack("!H", data[offset + 2:offset + 4])[0]
        offset += 4 + 4 * ext_words
    end = len(data)
    if first & 0x20 and end > offset:  # padding
        end -= data[-1]
    if offset > end:
        return None
    return RtpPacket(payload_type, sequence, timestamp, ssrc, bool(second & 0x80), data[offset:end])


class Packetizer:
    def __init__(self, payload_type: int = PT_PCMU, ssrc: Optional[int] = None):
        self.payload_type = payload_type
        self.ssrc = ssrc if ssrc is not None else random.getrandbits(32)
        self.sequence = random.getrandbits(16)
        self.timestamp = random.getrandbits(32)
        self._first = True

    def packet(self, payload: bytes, samples: int = SAMPLES_PER_FRAME) -> bytes:
        marker = 0x80 if self._first else 0
        self._first = False
        header = struct.pack("!BBHII", 0x80, marker | self.payload_type, self.sequence, self.timestamp, self.ssrc)
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp = (self.timestamp + samples) & 0xFFFFFFFF
        return header + payload
