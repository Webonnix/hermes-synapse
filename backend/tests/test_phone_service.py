"""Call orchestration: inbound decisions, Twilio/Telnyx webhooks and media streams,
outbound calls — the real service code against faked provider edges."""

import asyncio
import base64
import json
import time

import httpx
import pytest
import respx

from backend import conversation_state, inbox, inbox_rules, phone_lines, phone_service
from backend.tests.phone_helpers import FakeWebSocket, phone_env, ulaw_media  # noqa: F401

ACCOUNT = "AC" + "b" * 32


def _twilio_line(**extra):
    payload = {"name": "TW", "provider": "twilio", "account_sid": ACCOUNT, "auth_token": "tok", "subagent_id": "agent-1",
               "number": "+15550000001", "owner_number": "+15550009999"}
    payload.update(extra)
    return phone_lines.create_line(payload)


def _sign(url, params, token="tok"):
    import hashlib
    import hmac

    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _voice(line, **params):
    params = {"CallSid": "CA1", "From": "+15551112222", "To": line["number"], "Direction": "inbound", **params}
    url = phone_lines.provider_urls(line)["twilio_voice"]
    return phone_service.twilio_voice_webhook(line["id"], url, params, _sign(url, params))


# ── decisions ───────────────────────────────────────────────────────────────

def test_an_ordinary_caller_goes_to_the_ai(phone_env):
    assert phone_service.inbound_decision(_twilio_line(), "+15551112222")["action"] == "ai"


def test_rules_takeover_capacity_and_disabled_lines_decide_before_the_ai(phone_env, monkeypatch):
    line = _twilio_line()
    inbox_rules.create_rule("+15551112222", "block", binding_id=line["id"])
    assert phone_service.inbound_decision(line, "+15551112222")["action"] == "block"

    monkeypatch.setattr(inbox_rules, "is_within_business_hours", lambda binding_id, moment=None: True)
    inbox_rules.create_rule("+1555333", "pass", "prefix", binding_id=line["id"])
    assert phone_service.inbound_decision(line, "+15553334444")["action"] == "transfer"

    conversation_state.set_handling_mode(line["id"], "phone", "+15557776666", "owner_takeover")
    assert phone_service.inbound_decision(line, "+15557776666")["reason"] == "taken_over"

    phone_lines.update_settings({"max_concurrent_calls": 1})
    phone_lines.create_call(line=line, direction="inbound", remote_number="+1555", status="active")
    assert phone_service.inbound_decision(line, "+15550101010")["action"] == "busy"

    disabled = phone_lines.update_line(line["id"], {"inbound_enabled": False})
    assert phone_service.inbound_decision(disabled, "+15550101010")["action"] == "reject"


# ── Twilio ──────────────────────────────────────────────────────────────────

def test_twilio_voice_webhook_refuses_a_forged_request(phone_env):
    line = _twilio_line()
    url = phone_lines.provider_urls(line)["twilio_voice"]
    assert phone_service.twilio_voice_webhook(line["id"], url, {"CallSid": "CA1"}, "forged")[0] == 403
    assert phone_lines.list_calls() == []


def test_twilio_voice_webhook_opens_a_signed_media_stream(phone_env):
    line = _twilio_line()
    status, twiml = _voice(line)
    [call] = phone_lines.list_calls()
    token = phone_lines.media_token(call["id"])
    assert status == 200 and call["provider_call_id"] == "CA1" and call["direction"] == "inbound"
    assert f"wss://hermes.example.com/api/phone/twilio/media/{line['id']}/{call['id']}/{token}" in twiml


def test_a_blocked_twilio_caller_is_rejected_and_archived(phone_env):
    line = _twilio_line()
    inbox_rules.create_rule("+15551112222", "block")
    status, twiml = _voice(line)
    [call] = phone_lines.list_calls()
    assert "<Reject" in twiml and call["status"] == "rejected"
    conversation = inbox.find_conversation(line["id"], "phone", "+15551112222")
    assert "не принят" in inbox.list_messages(conversation["id"])["items"][-1]["text"]


@pytest.mark.asyncio
async def test_a_twilio_media_stream_runs_a_whole_call(phone_env):
    line = _twilio_line()
    _voice(line)
    [call] = phone_lines.list_calls()
    ws = FakeWebSocket([{"event": "connected"}, {"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}}])
    task = asyncio.create_task(phone_service.handle_twilio_media(ws, line["id"], call["id"], phone_lines.media_token(call["id"])))
    for _ in range(12):
        ws.push(ulaw_media())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(phone_env["agent_calls"]) < 1:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    ws.push({"event": "stop"})
    await asyncio.wait_for(task, 5)

    assert ws.accepted
    assert phone_env["agent_calls"] == ["Здравствуйте, вы работаете сегодня?"]
    assert any(m["event"] == "media" and m["streamSid"] == "MZ1" for m in ws.sent)
    finished = phone_lines.get_call(call["id"])
    assert finished["status"] == "ended" and finished["turns"] == 2 and finished["first_audio_ms_avg"] is not None
    texts = [m["text"] for m in inbox.list_messages(finished["conversation_id"])["items"]]
    assert "Здравствуйте, вы работаете сегодня?" in texts and "Да, сегодня мы работаем до шести." in texts


@pytest.mark.asyncio
async def test_a_media_stream_with_a_wrong_token_is_closed_unaccepted(phone_env):
    line = _twilio_line()
    _voice(line)
    [call] = phone_lines.list_calls()
    ws = FakeWebSocket()
    await phone_service.handle_twilio_media(ws, line["id"], call["id"], "0" * 40)
    assert ws.closed_code == 1008 and not ws.accepted


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_an_outbound_twilio_call_is_placed_with_a_stream_twiml(phone_env, respx_mock):
    line = _twilio_line()
    route = respx_mock.post(f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Calls.json").mock(
        return_value=httpx.Response(201, json={"sid": "CA77"}))
    call = await phone_service.start_outbound(line["id"], "+1 555 444 3333", purpose="Подтвердить заказ", initiated_by="owner")
    assert call["provider_call_id"] == "CA77" and call["direction"] == "outbound" and call["purpose"] == "Подтвердить заказ"
    body = route.calls.last.request.content.decode()
    assert "Twiml=" in body and "To=%2B15554443333" in body
    assert call["conversation_id"]


@pytest.mark.asyncio
async def test_outbound_calls_to_blocked_numbers_or_the_line_itself_are_refused(phone_env):
    line = _twilio_line()
    inbox_rules.create_rule("+15554443333", "block")
    with pytest.raises(ValueError, match="заблокирован"):
        await phone_service.start_outbound(line["id"], "+15554443333")
    with pytest.raises(ValueError, match="самой линии"):
        await phone_service.start_outbound(line["id"], line["number"])


# ── Telnyx ──────────────────────────────────────────────────────────────────

def _telnyx_line():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    line = phone_lines.create_line({"name": "TX", "provider": "telnyx", "api_key": "KEY", "connection_id": "conn-1",
                                    "public_key": public, "subagent_id": "agent-1", "number": "+15550000002"})
    return line, key


def _telnyx_event(key, event_type, payload):
    raw = json.dumps({"data": {"event_type": event_type, "payload": payload}}).encode()
    ts = str(int(time.time()))
    signature = base64.b64encode(key.sign(f"{ts}|".encode() + raw)).decode()
    return raw, {"telnyx-timestamp": ts, "telnyx-signature-ed25519": signature}


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_telnyx_incoming_call_is_answered_with_streaming_once(phone_env, respx_mock):
    line, key = _telnyx_line()
    answer = respx_mock.post("https://api.telnyx.com/v2/calls/v3:cc1/actions/answer").mock(return_value=httpx.Response(200, json={}))
    raw, headers = _telnyx_event(key, "call.initiated", {"call_control_id": "v3:cc1", "direction": "incoming", "from": "+15551112222"})
    assert await phone_service.telnyx_event_webhook(line["id"], raw, headers) == 200
    assert await phone_service.telnyx_event_webhook(line["id"], raw, headers) == 200
    [call] = phone_lines.list_calls()
    body = json.loads(answer.calls.last.request.content)
    assert answer.call_count == 1
    assert body["stream_url"].endswith(f"/{call['id']}/{phone_lines.media_token(call['id'])}")


@pytest.mark.asyncio
async def test_telnyx_webhook_with_a_bad_signature_is_refused(phone_env):
    line, key = _telnyx_line()
    raw, headers = _telnyx_event(key, "call.initiated", {"call_control_id": "x", "direction": "incoming"})
    assert await phone_service.telnyx_event_webhook(line["id"], raw + b" ", headers) == 403


@pytest.mark.asyncio
async def test_telnyx_audio_is_batched_but_the_first_chunk_goes_out_at_once():
    ws = FakeWebSocket()
    out = phone_service.TelnyxAudioOut(ws, min_interval_s=0.2)
    await out.play(b"\x00\x01" * 800)
    assert len(ws.sent) == 1
    await out.play(b"\x00\x01" * 800)
    await out.play(b"\x00\x01" * 800)
    assert len(ws.sent) == 1
    await asyncio.sleep(0.3)
    assert len(ws.sent) == 2 and len(base64.b64decode(ws.sent[1]["media"]["payload"])) == 1600
    await out.clear()
    assert ws.sent[-1] == {"event": "clear"} and out.pending_ms() == 0
