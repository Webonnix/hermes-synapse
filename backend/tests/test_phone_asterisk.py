"""Asterisk driver: the RTP media leg over a real UDP loopback, and the ARI call flow with
the HTTP side faked. (The real Asterisk path was verified separately against the Ubuntu
26.04 Asterisk 22.5 package.)"""

import asyncio
import socket

import pytest

from backend import inbox_rules, phone_asterisk, phone_audio, phone_lines, phone_rtp, phone_service
from backend.tests.phone_helpers import phone_env  # noqa: F401


class _FakeEndpoint:
    def __init__(self):
        self.sent = []

    def send(self, datagram):
        self.sent.append(phone_rtp.parse(datagram))


@pytest.mark.asyncio
async def test_the_sender_paces_silence_then_speech_and_a_clear_stops_it_within_a_frame():
    endpoint = _FakeEndpoint()
    out = phone_asterisk.RtpAudioOut(endpoint)
    pacer = asyncio.create_task(out.run())
    await asyncio.sleep(0.05)
    await out.play(b"\x00\x10" * 160 * 10)  # 200 ms of speech
    assert out.pending_ms() == 200
    await asyncio.sleep(0.07)
    await out.clear()
    assert out.pending_ms() == 0
    await asyncio.sleep(0.05)
    out.close()
    pacer.cancel()
    speech = phone_audio.pcm_to_ulaw(b"\x00\x10" * 160)
    payloads = [p.payload for p in endpoint.sent]
    assert payloads[0] == phone_audio.pcm_to_ulaw(phone_audio.SILENCE_FRAME)
    assert 2 <= payloads.count(speech) <= 6
    assert all(p.payload_type == phone_rtp.PT_PCMU for p in endpoint.sent)
    sequences = [p.sequence for p in endpoint.sent]
    assert all((b - a) % 65536 == 1 for a, b in zip(sequences, sequences[1:]))


class FakeARI:
    def __init__(self, rtp_target=None):
        self.requests = []
        self.rtp_target = rtp_target

    async def __call__(self, method, path, params=None, json_body=None, timeout=10.0):
        self.requests.append((method, path, dict(params or {})))
        if path == "/bridges":
            return {"id": "bridge-1"}
        if path == "/channels/externalMedia" and self.rtp_target:
            return {"id": params["channelId"], "channelvars": {
                "UNICASTRTP_LOCAL_ADDRESS": self.rtp_target[0], "UNICASTRTP_LOCAL_PORT": str(self.rtp_target[1])}}
        return {}


def _controller(ari):
    controller = phone_asterisk.AsteriskController()
    controller.ari = ari
    controller.rtp_bind_host = "127.0.0.1"
    controller.media_ip_override = "127.0.0.1"
    controller.ports = phone_asterisk.PortPool(41000, 41020)
    return controller


def _zadarma_line(**extra):
    payload = {"name": "Офис", "provider": "zadarma", "number": "+972501234567", "subagent_id": "agent-1",
               "sip_login": "100", "sip_password": "pw", "owner_number": "+972509999999"}
    payload.update(extra)
    return phone_lines.create_line(payload)


def _inbound_event(line, caller="+15551112222", channel_id="chan-1"):
    return {"type": "StasisStart", "args": ["inbound", "972501234567", ""],
            "channel": {"id": channel_id, "name": f"PJSIP/hermes_{line['id']}-00000001", "caller": {"number": caller}}}


async def _settle(controller, predicate=None, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if predicate is None and not controller._tasks:
            return
        if predicate is not None and predicate():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_an_inbound_ai_call_is_answered_bridged_and_given_an_rtp_media_channel(phone_env):
    line = _zadarma_line()
    ari = FakeARI(rtp_target=("127.0.0.1", 45555))
    controller = _controller(ari)
    await controller._dispatch(_inbound_event(line))
    await _settle(controller, lambda: any(path == "/channels/externalMedia" for _, path, _ in ari.requests))
    await asyncio.sleep(0.05)

    [record] = phone_lines.list_calls()
    steps = [(m, p) for m, p, _ in ari.requests]
    assert steps[:2] == [("POST", "/channels/chan-1/answer"), ("POST", "/bridges")]
    media = next(params for method, path, params in ari.requests if path == "/channels/externalMedia")
    assert media["encapsulation"] == "rtp" and media["transport"] == "udp" and media["format"] == "ulaw"
    call = controller.calls[record["id"]]
    assert media["external_host"] == f"127.0.0.1:{call.rtp_port}"
    assert call.endpoint.remote == ("127.0.0.1", 45555)
    await controller._dispatch({"type": "StasisEnd", "channel": {"id": "chan-1"}})
    await _settle(controller, lambda: phone_lines.get_call(record["id"])["status"] == "ended")
    await controller.stop()


@pytest.mark.asyncio
async def test_a_blocked_caller_is_hung_up_without_answering(phone_env):
    line = _zadarma_line()
    inbox_rules.create_rule("+15551112222", "block")
    ari = FakeARI()
    controller = _controller(ari)
    await controller._dispatch(_inbound_event(line))
    await _settle(controller)
    assert ari.requests == [("DELETE", "/channels/chan-1", {"reason": "rejected"})]
    assert phone_lines.list_calls()[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_transfer_dials_the_owner_into_the_bridge_and_drops_the_ai(phone_env):
    line = _zadarma_line()
    ari = FakeARI()
    controller = _controller(ari)
    call = phone_asterisk.AsteriskCall(call_id="c1", line=line, direction="inbound", channel_id="chan-1",
                                       bridge_id="bridge-1", ext_id="hx-c1")
    controller._register(call)

    async def answer_owner():
        while not controller._channel_waiters:
            await asyncio.sleep(0.01)
        owner_channel = next(iter(controller._channel_waiters))
        await controller._dispatch({"type": "StasisStart", "args": ["transfer", "c1"], "channel": {"id": owner_channel}})

    asyncio.create_task(answer_owner())
    assert await controller.transfer(call, "+972509999999") is True
    originate = next(params for method, path, params in ari.requests if path == "/channels" and method == "POST")
    assert originate["endpoint"] == f"PJSIP/+972509999999@hermes_{line['id']}"
    assert ("DELETE", "/channels/hx-c1", {"reason": "normal"}) in ari.requests
    assert call.ext_id == "" and call.owner_channel_id


@pytest.mark.asyncio
async def test_an_unanswered_transfer_keeps_the_ai(phone_env):
    line = _zadarma_line()
    controller = _controller(FakeARI())
    call = phone_asterisk.AsteriskCall(call_id="c1", line=line, direction="inbound", channel_id="chan-1",
                                       bridge_id="bridge-1", ext_id="hx-c1")
    controller._register(call)

    async def reject_owner():
        while not controller._channel_waiters:
            await asyncio.sleep(0.01)
        owner_channel = next(iter(controller._channel_waiters))
        await controller._dispatch({"type": "ChannelDestroyed", "channel": {"id": owner_channel}, "cause": 17})

    asyncio.create_task(reject_owner())
    assert await controller.transfer(call, "+972509999999") is False
    assert call.ext_id == "hx-c1"


@pytest.mark.asyncio
async def test_caller_audio_over_rtp_reaches_the_agent_and_the_reply_comes_back(phone_env):
    line = _zadarma_line()
    asterisk_side = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    asterisk_side.bind(("127.0.0.1", 0))
    asterisk_side.setblocking(False)
    ari = FakeARI(rtp_target=asterisk_side.getsockname())
    controller = _controller(ari)
    await controller._dispatch(_inbound_event(line))
    [record] = [None]
    await _settle(controller, lambda: any(c.endpoint for c in controller.calls.values()))
    call = next(iter(controller.calls.values()))

    packetizer = phone_rtp.Packetizer()
    frame = phone_audio.pcm_to_ulaw(b"\x00\x08" * 160)
    for _ in range(12):
        asterisk_side.sendto(packetizer.packet(frame), ("127.0.0.1", call.rtp_port))
        await asyncio.sleep(0.005)
    await _settle(controller, lambda: bool(phone_env["agent_calls"]))
    await asyncio.sleep(0.2)

    received = 0
    loop = asyncio.get_running_loop()
    for _ in range(20):
        try:
            data = await asyncio.wait_for(loop.sock_recv(asterisk_side, 2048), timeout=0.1)
        except asyncio.TimeoutError:
            break
        if phone_rtp.parse(data):
            received += 1
    assert phone_env["agent_calls"] == ["Здравствуйте, вы работаете сегодня?"]
    assert received > 5

    await controller._dispatch({"type": "StasisEnd", "channel": {"id": "chan-1"}})
    record = phone_lines.list_calls()[0]
    await _settle(controller, lambda: phone_lines.get_call(record["id"])["status"] == "ended")
    asterisk_side.close()
    assert phone_lines.get_call(record["id"])["status"] == "ended"
    assert record["id"] not in phone_service.ACTIVE
    assert not controller.ports.in_use
    await controller.stop()
