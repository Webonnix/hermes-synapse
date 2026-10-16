"""Shared fixtures for telephony tests: one temp database for everything the call path
touches, an in-memory Valkey, an agent, and fake speech/agent/WebSocket edges."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from backend import (
    bot_access, channel_replies, client_activity, client_tasks, clients, conversation_state, database, inbox,
    inbox_routing, inbox_rules, phone_audio, phone_lines, phone_service, phone_speech, voice_providers,
)


class FakeSTT(phone_speech.STTStream):
    name = "fake-stt"

    def __init__(self, final_after_frames: int = 0, text: str = "Здравствуйте, вы работаете сегодня?"):
        super().__init__()
        self.final_after_frames = final_after_frames
        self.text = text
        self.frames = 0

    async def send(self, frame):
        self.frames += 1
        if self.final_after_frames and self.frames == self.final_after_frames:
            self.emit("final", self.text)


class FakeTTS(phone_speech.TTSEngine):
    name = "fake-tts"

    def __init__(self):
        self.phrases = []

    async def synthesize(self, text):
        self.phrases.append(text)
        yield b"\x10\x00" * 800


class FakeWebSocket:
    def __init__(self, messages=()):
        self.incoming: "asyncio.Queue[str]" = asyncio.Queue()
        for message in messages:
            self.incoming.put_nowait(json.dumps(message))
        self.sent = []
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_code = code

    async def receive_text(self):
        return await self.incoming.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def push(self, message):
        self.incoming.put_nowait(json.dumps(message))


def ulaw_media(ms: int = 20) -> dict:
    pcm = b"\x00\x08" * (8 * ms)
    return {"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(phone_audio.pcm_to_ulaw(pcm)).decode()}}


@pytest.fixture()
def phone_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phone.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (bot_access, conversation_state, channel_replies, clients, client_tasks, client_activity):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    for module in (clients, client_tasks, client_activity):
        if hasattr(module, "reset_schema_cache"):
            module.reset_schema_cache()
    for module in (inbox, inbox_rules, inbox_routing, voice_providers, phone_lines, phone_speech):
        module.reset_schema_cache()
    monkeypatch.setattr(inbox, "_last_notified", {})
    monkeypatch.setattr(inbox, "_event_loop", None)
    monkeypatch.setattr(phone_service, "ACTIVE", {})
    monkeypatch.setattr(phone_service, "_CONTROLS", {})
    database.init_db()
    secrets = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: secrets.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: secrets.get(k))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda k: secrets.pop(k, None))
    database.save_subagent(id="agent-1", name="Секретарь", system_prompt="", model="local")
    phone_lines.update_settings({"public_base_url": "https://hermes.example.com"})

    tts = FakeTTS()
    stt_holder = {"stt": FakeSTT(final_after_frames=10)}
    monkeypatch.setattr(phone_speech, "build_stt", lambda config=None: stt_holder["stt"])
    monkeypatch.setattr(phone_speech, "build_tts", lambda config=None: tts)

    agent_calls = []

    async def fake_agent(session, text, on_token):
        agent_calls.append(text)
        await on_token("Да, сегодня мы работаем до шести. ")
        return "Да, сегодня мы работаем до шести."

    from backend import phone_session

    monkeypatch.setattr(phone_session, "default_agent_turn", fake_agent)
    return {"secrets": secrets, "tts": tts, "stt": stt_holder, "agent_calls": agent_calls, "tmp": tmp_path}
