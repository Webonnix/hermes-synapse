"""External voice providers (backend/voice_providers.py): validation, credential
handling, the HTTP guard rails and the webhook — with the transport mocked by respx so
the real adapter code runs."""

import hashlib
import hmac
import json
import logging
import socket

import httpx
import pytest
import respx
from httpx import Response

from backend import bot_access_gate, inbox, voice_providers
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture

SECRET = "sk-live-" + "q" * 32


def _provider(kind="openai_compatible", base="https://voice.example.com/v1", **extra):
    payload = {"name": "Voice", "kind": kind, "base_url": base, "api_key": SECRET, "max_retries": 0, "timeout_seconds": 3}
    payload.update(extra)
    return voice_providers.create_provider(payload)


# ── validation ──────────────────────────────────────────────────────────────

def test_a_provider_cannot_claim_a_capability_its_kind_does_not_have(inbox_env):
    with pytest.raises(ValueError):
        _provider(kind="voice_agent_http", capabilities=["tts"])
    provider = _provider(kind="voice_agent_http")
    with pytest.raises(ValueError):
        voice_providers.create_target({"provider_id": provider["id"], "name": "x", "capability_mode": "tts", "voice_id": "v"})


def test_display_names_accept_ordinary_punctuation_but_not_control_characters(inbox_env):
    provider = _provider(name="OpenAI (прод) — «TTS»")
    target = voice_providers.create_target({
        "provider_id": provider["id"], "name": "Голос «Nova»", "capability_mode": "tts", "voice_id": "nova",
    })
    assert target["name"] == "Голос «Nova»"
    with pytest.raises(ValueError):
        _provider(name="bad\nname")
    with pytest.raises(ValueError):
        _provider(name="x" * 65)


def test_a_tts_target_needs_a_voice(inbox_env):
    provider = _provider()
    with pytest.raises(ValueError):
        voice_providers.create_target({"provider_id": provider["id"], "name": "x", "capability_mode": "tts"})


@pytest.mark.parametrize("config", [{"api_key": "x"}, {"Authorization": "Bearer x"}, {"webhook_token": "x"}])
def test_a_secret_smuggled_into_config_is_refused(inbox_env, config):
    with pytest.raises(ValueError, match="секрет"):
        _provider(config=config)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest", "http://voice.example.com", "ftp://voice.example.com",
    "https://user:pw@voice.example.com", "https://voice.example.com/v1?key=1",
])
def test_unsafe_base_urls_are_refused(inbox_env, url):
    with pytest.raises(ValueError):
        _provider(base=url)


@pytest.mark.parametrize("url", ["https://api.elevenlabs.io", "http://10.0.0.5:8700", "http://omniroute:20128/v1"])
def test_https_and_private_self_hosted_urls_are_accepted(inbox_env, url):
    assert _provider(base=url)["base_url"] == url


# ── credentials ─────────────────────────────────────────────────────────────

def test_the_api_never_returns_the_secret_only_that_one_exists(inbox_env):
    provider = _provider()
    listed = voice_providers.list_providers()
    assert provider["has_secret"] is True
    assert SECRET not in json.dumps(listed)
    assert inbox_env.secrets[f"voice_provider_secret:{provider['id']}"] == SECRET


def test_clearing_and_rotating_the_secret(inbox_env):
    provider = _provider()
    assert voice_providers.update_provider(provider["id"], {"clear_api_key": True})["has_secret"] is False
    assert voice_providers.update_provider(provider["id"], {"api_key": "new-key"})["has_secret"] is True
    rotated = voice_providers.generate_webhook_secret(provider["id"])
    assert len(rotated) >= 32 and voice_providers.get_provider(provider["id"])["has_webhook_secret"]


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_the_secret_goes_in_the_header_and_never_into_the_logs(inbox_env, respx_mock, caplog):
    provider = _provider()
    route = respx_mock.get("https://voice.example.com/v1/models").mock(return_value=Response(200, json={"data": []}))
    caplog.set_level(logging.DEBUG)

    health = await voice_providers.test_provider(provider["id"])

    assert health["ok"] is True
    assert route.calls.last.request.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in caplog.text
    assert voice_providers.get_provider(provider["id"])["last_health"]["ok"] is True


# ── transport guard rails ───────────────────────────────────────────────────

@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_connection_test_that_times_out_is_a_result_not_an_exception(inbox_env, respx_mock):
    provider = _provider()
    respx_mock.get("https://voice.example.com/v1/models").mock(side_effect=httpx.ConnectTimeout("no route"))

    health = await voice_providers.test_provider(provider["id"])

    assert health["ok"] is False and "таймаут" in health["error"]


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_redirects_are_never_followed(inbox_env, respx_mock):
    provider = _provider()
    respx_mock.get("https://voice.example.com/v1/models").mock(
        return_value=Response(302, headers={"location": "http://169.254.169.254/"})
    )
    health = await voice_providers.test_provider(provider["id"])
    assert health["ok"] is False and "редирект" in health["error"]


@pytest.mark.asyncio
async def test_a_name_that_resolves_to_link_local_is_refused_at_call_time(inbox_env, monkeypatch):
    provider = _provider(base="https://metadata.example.com")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, 1, 6, "", ("169.254.169.254", 0))])

    health = await voice_providers.test_provider(provider["id"])

    assert health["ok"] is False and "link-local" in health["error"]


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_idempotent_reads_retry_but_writes_do_not(inbox_env, respx_mock):
    provider = _provider(max_retries=2)
    reads = respx_mock.get("https://voice.example.com/v1/models").mock(return_value=Response(503))
    writes = respx_mock.post("https://voice.example.com/v1/audio/speech").mock(return_value=Response(503))
    adapter = voice_providers.adapter_for(voice_providers.get_provider(provider["id"]))

    assert (await adapter.health()).ok is False
    result = await adapter.synthesize(text="привет", voice_id="alloy")

    assert reads.call_count == 3
    assert writes.call_count == 1
    assert result.ok is False and result.error == "HTTP 503"


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_a_malformed_voice_catalogue_degrades_to_what_can_be_read(inbox_env, respx_mock):
    provider = _provider(kind="elevenlabs", base="https://api.elevenlabs.io")
    route = respx_mock.get("https://api.elevenlabs.io/v1/voices")

    route.mock(return_value=Response(200, json={"voices": "not-a-list"}))
    assert await voice_providers.list_provider_voices(provider["id"]) == []

    route.mock(return_value=Response(200, text="<html>gateway</html>"))
    assert await voice_providers.list_provider_voices(provider["id"]) == []

    route.mock(return_value=Response(200, json={"voices": [
        {"voice_id": "abc", "name": "Rachel", "labels": {"language": "en"}}, {"name": "no id"}, "garbage",
    ]}))
    assert await voice_providers.list_provider_voices(provider["id"]) == [{"id": "abc", "name": "Rachel", "language": "en"}]


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_stt_errors_become_controlled_results(inbox_env, respx_mock):
    provider = _provider()
    respx_mock.post("https://voice.example.com/v1/audio/transcriptions").mock(return_value=Response(200, json={"oops": 1}))
    adapter = voice_providers.adapter_for(voice_providers.get_provider(provider["id"]))

    result = await adapter.transcribe(audio=b"RIFF....", mime="audio/wav")
    assert result.ok is False
    assert (await adapter.transcribe(audio=b"", mime="audio/wav")).ok is False


# ── webhook ─────────────────────────────────────────────────────────────────

def _signed(secret, payload):
    raw = json.dumps(payload).encode()
    return raw, {"X-Hermes-Signature": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}


def test_webhooks_need_a_valid_signature_and_unknown_providers_look_the_same(inbox_env):
    provider = _provider()
    secret = voice_providers.generate_webhook_secret(provider["id"])
    raw, headers = _signed(secret, {"event_id": "e1", "type": "status"})

    assert voice_providers.handle_webhook(provider["id"], raw, {"X-Hermes-Signature": "sha256=deadbeef"}) == (
        401, {"detail": "invalid signature"})
    assert voice_providers.handle_webhook("vp-unknown", raw, headers) == (401, {"detail": "invalid signature"})
    assert voice_providers.handle_webhook(provider["id"], raw, headers)[0] == 200


def test_a_webhook_is_processed_once_per_event_id(inbox_env):
    provider = _provider()
    secret = voice_providers.generate_webhook_secret(provider["id"])
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "голосовое")
    raw, headers = _signed(secret, {
        "event_id": "tr-1", "type": "transcript", "conversation_id": decision.conversation_id, "text": "Хочу вернуть заказ",
    })

    assert voice_providers.handle_webhook(provider["id"], raw, headers) == (200, {"status": "ok"})
    assert voice_providers.handle_webhook(provider["id"], raw, headers) == (200, {"status": "duplicate"})

    notes = [m["text"] for m in inbox.list_messages(decision.conversation_id)["items"] if m["speaker"] == "system"]
    assert len(notes) == 1 and "Хочу вернуть заказ" in notes[0]


def test_a_webhook_without_an_event_id_is_rejected(inbox_env):
    provider = _provider()
    secret = voice_providers.generate_webhook_secret(provider["id"])
    raw, headers = _signed(secret, {"type": "status"})
    assert voice_providers.handle_webhook(provider["id"], raw, headers)[0] == 400
