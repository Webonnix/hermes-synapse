import struct

from backend import phone_rtp


def test_packetizer_marks_the_first_packet_and_advances_sequence_and_timestamp():
    packetizer = phone_rtp.Packetizer(ssrc=7)
    first = phone_rtp.parse(packetizer.packet(b"\x01" * 160))
    second = phone_rtp.parse(packetizer.packet(b"\x02" * 160))
    assert first.marker and not second.marker
    assert (second.sequence - first.sequence) % 65536 == 1
    assert (second.timestamp - first.timestamp) % 2 ** 32 == 160
    assert first.ssrc == second.ssrc == 7 and second.payload == b"\x02" * 160


def test_parse_handles_csrc_extension_and_padding():
    csrc = struct.pack("!I", 42)
    extension = struct.pack("!HH", 0xBEEF, 1) + b"\x00" * 4
    payload = b"\xaa" * 10
    padding = b"\x00\x00\x03"
    first = 0x80 | 0x20 | 0x10 | 0x01  # padding, extension, one CSRC
    packet = struct.pack("!BBHII", first, 0, 1, 2, 3) + csrc + extension + payload + padding
    parsed = phone_rtp.parse(packet)
    assert parsed.payload == payload


def test_parse_rejects_non_rtp():
    assert phone_rtp.parse(b"\x00" * 20) is None                      # version 0
    assert phone_rtp.parse(b"\x80") is None                           # too short
    rtcp = struct.pack("!BBHII", 0x80, 200, 0, 0, 0)                  # RTCP sender report (PT 72 after mask)
    assert phone_rtp.parse(rtcp) is None
