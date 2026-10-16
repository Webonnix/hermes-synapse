"""Phone lines: validation, write-only secrets, Asterisk trunks, provider signatures and
the Twilio/Telnyx REST clients (respx-mocked)."""

import base64
import json
import os
import stat
import time

import httpx
import pytest
import respx

from backend import database, phone_lines


@pytest.fixture()
def lines_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "phone.db"))
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    monkeypatch.delenv("PHONE_ASTERISK_STATE_DIR", raising=False)
    phone_lines.reset_schema_cache()
    database.init_db()
    database.save_subagent(id="agent-1", name="Секретарь", system_prompt="", model="local")
    secrets = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: secrets.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: secrets.get(k))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda k: secrets.pop(k, None))
    return secrets


def _zadarma(**extra):
    payload = {"name": "Офис", "provider": "zadarma", "number": "+972 50 123 4567", "subagent_id": "agent-1",
               "sip_login": "123456-100", "sip_password": "p;ss", "owner_number": "+972501111111"}
    payload.update(extra)
    return phone_lines.create_line(payload)


# ── lines ───────────────────────────────────────────────────────────────────

def test_a_line_is_normalised_and_its_secret_never_comes_back(lines_db):
    line = _zadarma()
    assert line["number"] == "+972501234567" and line["config"]["sip_server"] == "sip.zadarma.com"
    assert line["secrets_set"] == {"sip_password": True}
    assert "p;ss" not in json.dumps(phone_lines.list_lines())
    assert phone_lines.line_secrets(line["id"]) == {"sip_password": "p;ss"}


def test_updating_without_a_secret_keeps_the_stored_one(lines_db):
    line = _zadarma()
    phone_lines.update_line(line["id"], {"name": "Офис 2", "sip_password": ""})
    assert phone_lines.line_secrets(line["id"])["sip_password"] == "p;ss"
    assert phone_lines.get_line(line["id"])["name"] == "Офис 2"


@pytest.mark.parametrize("payload, message", [
    ({"provider": "skype"}, "provider"),
    ({"number": "12"}, "Номер"),
    ({"subagent_id": "vexa"}, "Vexa"),
    ({"subagent_id": "ghost"}, "не найден"),
    ({"sip_login": "bad login"}, "SIP-логин"),
    ({"sip_server": "https://sip.zadarma.com"}, "SIP-сервер"),
    ({"max_call_minutes": 500}, "длительность"),
])
def test_invalid_lines_are_refused(lines_db, payload, message):
    with pytest.raises(ValueError, match=message):
        _zadarma(**payload)


def test_the_provider_of_a_line_cannot_change(lines_db):
    line = _zadarma()
    with pytest.raises(ValueError):
        phone_lines.update_line(line["id"], {"provider": "twilio"})


def test_telnyx_public_key_must_be_an_ed25519_key(lines_db):
    with pytest.raises(ValueError, match="Public Key"):
        phone_lines.create_line({"name": "T", "provider": "telnyx", "api_key": "KEY1", "connection_id": "1", "public_key": "abc"})


def test_settings_accept_only_an_https_origin(lines_db):
    assert phone_lines.update_settings({"public_base_url": "https://hermes.example.com/"})["public_base_url"] == "https://hermes.example.com"
    for bad in ("http://hermes.example.com", "https://hermes.example.com/path", "ftp://x"):
        with pytest.raises(ValueError):
            phone_lines.update_settings({"public_base_url": bad})


# ── trunks ──────────────────────────────────────────────────────────────────

def test_trunk_config_registers_escapes_and_is_written_private(lines_db):
    line = _zadarma()
    content = phone_lines.render_trunks()
    name = phone_lines.trunk_name(line["id"])
    assert f"[{name}]\ntype=registration" in content
    assert "client_uri=sip:123456-100@sip.zadarma.com" in content
    assert r"password=p\;ss" in content
    assert "rtp_symmetric=yes" in content and "line=yes" in content
    assert f"type=identify\nendpoint={name}\nmatch=sip.zadarma.com" in content
    path = phone_lines.write_trunks()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert phone_lines.line_id_from_channel(f"PJSIP/{name}-0000001a") == line["id"]


def test_two_lines_on_one_server_are_told_apart_by_registration_not_by_address(lines_db):
    _zadarma()
    _zadarma(name="Склад", number="+972502222222", sip_login="123456-101")
    assert "type=identify" not in phone_lines.render_trunks()


def test_disabled_and_incomplete_lines_are_left_out(lines_db):
    line = _zadarma()
    phone_lines.update_line(line["id"], {"enabled": False})
    phone_lines.create_line({"name": "Без пароля", "provider": "zadarma", "sip_login": "999"})
    assert "type=registration" not in phone_lines.render_trunks()


def test_newline_injection_into_the_trunk_file_is_refused(lines_db):
    with pytest.raises(ValueError):
        _zadarma(sip_password="x\n[evil]\ntype=endpoint")


# ── signatures & tokens ─────────────────────────────────────────────────────

def test_twilio_signature_matches_the_reference_vector():
    # The vector twilio-python's own RequestValidator tests use.
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675309", "Digits": "1234",
              "From": "+14158675309", "To": "+18005551212"}
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert phone_lines.validate_twilio_signature("12345", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not phone_lines.validate_twilio_signature("12345", url, {**params, "Digits": "0000"}, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not phone_lines.validate_twilio_signature("", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")


def test_telnyx_ed25519_signature_is_verified_and_stale_ones_refused():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    body = b'{"data":{"event_type":"call.initiated"}}'
    ts = str(int(time.time()))
    signature = base64.b64encode(key.sign(f"{ts}|".encode() + body)).decode()
    assert phone_lines.verify_telnyx_signature(public, ts, body, signature)
    assert not phone_lines.verify_telnyx_signature(public, ts, body + b" ", signature)
    old = str(int(time.time()) - 3600)
    old_sig = base64.b64encode(key.sign(f"{old}|".encode() + body)).decode()
    assert not phone_lines.verify_telnyx_signature(public, old, body, old_sig)


def test_media_tokens_bind_to_one_call(lines_db):
    token = phone_lines.media_token("call-a")
    assert phone_lines.check_media_token("call-a", token)
    assert not phone_lines.check_media_token("call-b", token)
    assert not phone_lines.check_media_token("call-a", "")


def test_twiml_escapes_what_it_embeds():
    assert "&amp;" in phone_lines.twiml_stream("wss://h/x?a=1&b=2", 'c"1')
    assert "&quot;" in phone_lines.twiml_stream("wss://h/x", 'c"1')


# ── provider clients ────────────────────────────────────────────────────────

ACCOUNT = "AC" + "a" * 32


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_twilio_number_search_includes_the_monthly_price(lines_db, respx_mock):
    line = phone_lines.create_line({"name": "TW", "provider": "twilio", "account_sid": ACCOUNT, "auth_token": "tok",
                                    "subagent_id": "agent-1"})
    respx_mock.get(f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/AvailablePhoneNumbers/US/Local.json").mock(
        return_value=httpx.Response(200, json={"available_phone_numbers": [
            {"phone_number": "+15551230000", "friendly_name": "(555) 123-0000", "locality": "Austin", "capabilities": {"voice": True}},
        ]}))
    respx_mock.get("https://pricing.twilio.com/v1/PhoneNumbers/Countries/US").mock(return_value=httpx.Response(200, json={
        "price_unit": "USD", "phone_number_prices": [{"number_type": "local", "current_price": "1.15"}]}))
    numbers = await phone_lines.search_numbers(line, "us", "Local", "123")
    assert numbers[0]["phone_number"] == "+15551230000" and numbers[0]["monthly_cost"] == "1.15"


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_buying_a_twilio_number_points_it_at_this_line(lines_db, respx_mock):
    phone_lines.update_settings({"public_base_url": "https://hermes.example.com"})
    line = phone_lines.create_line({"name": "TW", "provider": "twilio", "account_sid": ACCOUNT, "auth_token": "tok",
                                    "subagent_id": "agent-1"})
    route = respx_mock.post(f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/IncomingPhoneNumbers.json").mock(
        return_value=httpx.Response(201, json={"sid": "PN1", "phone_number": "+15551230000"}))
    await phone_lines.buy_number(line, "+1 555 123 0000")
    sent = dict(pair.split("=", 1) for pair in route.calls.last.request.content.decode().split("&"))
    assert sent["VoiceUrl"] == httpx.QueryParams({"u": f"https://hermes.example.com/api/phone/twilio/voice/{line['id']}"}).__str__()[2:]
    updated = phone_lines.get_line(line["id"])
    assert updated["number"] == "+15551230000" and updated["config"]["number_sid"] == "PN1"


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_provider_error_messages_are_surfaced(lines_db, respx_mock):
    respx_mock.get(f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}.json").mock(
        return_value=httpx.Response(401, json={"message": "Authenticate"}))
    with pytest.raises(phone_lines.PhoneProviderError, match="Authenticate"):
        await phone_lines.TwilioClient(ACCOUNT, "bad").validate()


@respx.mock(assert_all_called=True)
@pytest.mark.asyncio
async def test_telnyx_dial_carries_bidirectional_stream_parameters(respx_mock):
    route = respx_mock.post("https://api.telnyx.com/v2/calls").mock(
        return_value=httpx.Response(200, json={"data": {"call_control_id": "v3:abc"}}))
    ccid = await phone_lines.TelnyxClient("KEY").create_call(
        connection_id="conn", to="+15550001", from_="+15550002", webhook_url="https://h/api/phone/telnyx/events/l",
        media_url="wss://h/api/phone/telnyx/media/l/c/t", client_state="call-1")
    body = json.loads(route.calls.last.request.content)
    assert ccid == "v3:abc"
    assert body["stream_bidirectional_mode"] == "rtp" and body["stream_bidirectional_codec"] == "PCMU"
    assert base64.b64decode(body["client_state"]).decode() == "call-1"
    assert route.calls.last.request.headers["authorization"] == "Bearer KEY"


@pytest.mark.asyncio
async def test_zadarma_numbers_are_not_bought_from_here(lines_db):
    line = _zadarma()
    with pytest.raises(ValueError, match="кабинете"):
        await phone_lines.search_numbers(line, "IL")


def test_calls_are_recorded_and_stale_ones_reconciled(lines_db):
    line = _zadarma()
    call = phone_lines.create_call(line=line, direction="inbound", remote_number="+15550001", provider_call_id="ch1")
    assert phone_lines.count_active_calls() == 1
    assert phone_lines.find_call_by_provider("zadarma", "ch1")["id"] == call["id"]
    phone_lines.update_call(call["id"], degraded=["stt:local"], turns=3)
    assert phone_lines.get_call(call["id"])["degraded"] == ["stt:local"]
    assert phone_lines.reconcile_stale_calls() == 1
    assert phone_lines.get_call(call["id"])["end_reason"] == "backend_restart"
