"""Tests for keeping the LLM resident in VRAM.

The failure these guard against is invisible until someone sends a message:
`nvidia-smi` shows 24 GiB free and "No running processes found", and the next
chat turn silently pays a 30-60s reload. `keep_alive: -1` does not prevent it —
it only suppresses the expiry timer of the request that carried it, so an
Ollama restart, one request with a short keep_alive, or a click on the
dashboard's unload button all empty the cards for good.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend import model_keeper


def _ps(name: str, expires_in: str | None = "3000-01-01T00:00:00.000000000Z",
        context_length: int | None = None) -> dict:
    entry: dict = {"name": name, "model": name, "size_vram": 21_000_000_000}
    if expires_in is not None:
        entry["expires_at"] = expires_in
    if context_length is not None:
        entry["context_length"] = context_length
    return entry


def _in(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat().replace("+00:00", "Z")


# ── When the keeper is on at all ────────────────────────────────────────────

def test_keep_resident_follows_keep_alive_when_not_set_explicitly(monkeypatch):
    monkeypatch.delenv("OLLAMA_KEEP_RESIDENT", raising=False)
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "-1")
    assert model_keeper.keep_resident_enabled() is True

    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "5m")
    assert model_keeper.keep_resident_enabled() is False


def test_an_explicit_setting_wins_over_keep_alive(monkeypatch):
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "5m")
    monkeypatch.setenv("OLLAMA_KEEP_RESIDENT", "true")
    assert model_keeper.keep_resident_enabled() is True

    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "-1")
    monkeypatch.setenv("OLLAMA_KEEP_RESIDENT", "off")
    assert model_keeper.keep_resident_enabled() is False


# ── Reading Ollama's answer ─────────────────────────────────────────────────

def test_a_model_that_is_not_loaded_needs_a_pin():
    assert model_keeper.needs_pin([], "qwen-mtp:latest", 180) is True
    assert model_keeper.needs_pin([_ps("other:latest")], "qwen-mtp:latest", 180) is True


def test_a_pinned_model_is_left_alone():
    """The far-future expires_at Ollama reports for a -1 pin needs no special
    case — it simply isn't inside the horizon."""
    assert model_keeper.needs_pin([_ps("qwen-mtp:latest")], "qwen-mtp:latest", 180) is False


def test_the_implicit_latest_tag_still_matches():
    assert model_keeper.needs_pin([_ps("qwen-mtp:latest")], "qwen-mtp", 180) is False


def test_a_model_about_to_expire_is_repinned():
    """The five-minute expiry a stray request re-arms is caught here, minutes
    before the model would actually leave VRAM."""
    assert model_keeper.needs_pin([_ps("qwen-mtp:latest", _in(60))], "qwen-mtp:latest", 180) is True
    assert model_keeper.needs_pin([_ps("qwen-mtp:latest", _in(600))], "qwen-mtp:latest", 180) is False


def test_an_unreadable_expiry_is_treated_as_expiring():
    """A redundant load is one cheap request; a missed one costs the next
    message a full reload — so an answer we cannot parse errs toward pinning."""
    assert model_keeper.needs_pin([_ps("qwen-mtp:latest", None)], "qwen-mtp:latest", 180) is True
    assert model_keeper.needs_pin([_ps("qwen-mtp:latest", "not-a-date")], "qwen-mtp:latest", 180) is True


def test_nanosecond_precision_and_z_suffix_parse():
    """Ollama emits both; datetime.fromisoformat accepts neither on 3.10."""
    remaining = model_keeper._expires_in_seconds(
        {"expires_at": "2030-01-01T00:00:00.123456789Z"},
        now=datetime(2029, 12, 31, 23, 59, 0, tzinfo=timezone.utc),
    )
    assert remaining is not None and 59 < remaining < 61


# ── One pass ────────────────────────────────────────────────────────────────

class _FakeAgent:
    def __init__(self, provider="ollama", model="qwen-mtp:latest", num_ctx=65536):
        self.provider = provider
        self.model = model
        self.ollama_base_url = "http://hermes-ollama:11434"
        self.ollama_keep_alive = "-1"
        self.ollama_num_ctx = num_ctx


class _FakeClient:
    def __init__(self, running):
        self.running = running
        self.loaded: list = []

    async def list_running(self):
        return self.running

    async def load_model(self, model, keep_alive=-1, num_ctx=None):
        self.loaded.append((model, keep_alive, num_ctx))
        self.running = [_ps(model, context_length=num_ctx)]


def test_an_empty_gpu_gets_the_model_back():
    client = _FakeClient([])
    assert asyncio.run(model_keeper.pin_once(client=client, agent=_FakeAgent())) == "pinned"
    assert client.loaded == [("qwen-mtp:latest", -1, 65536)]


def test_a_resident_model_costs_one_ps_call_and_nothing_else():
    client = _FakeClient([_ps("qwen-mtp:latest", context_length=65536)])
    assert asyncio.run(model_keeper.pin_once(client=client, agent=_FakeAgent())) == "resident"
    assert client.loaded == []


def test_a_runner_loaded_at_the_wrong_context_is_replaced():
    """Ollama keys a runner by its options, so a model preloaded at the server
    default context is reloaded from scratch the moment the agent chats with a
    different num_ctx — the preload bought nothing and the VRAM went to a
    runner nobody uses."""
    client = _FakeClient([_ps("qwen-mtp:latest", context_length=262144)])
    assert asyncio.run(model_keeper.pin_once(client=client, agent=_FakeAgent(num_ctx=65536))) == "pinned"
    assert client.loaded == [("qwen-mtp:latest", -1, 65536)]


def test_a_runner_without_a_reported_context_is_left_alone():
    """Older Ollama builds omit context_length from /api/ps — unknown must not
    turn into a reload loop."""
    client = _FakeClient([_ps("qwen-mtp:latest")])
    assert asyncio.run(model_keeper.pin_once(client=client, agent=_FakeAgent(num_ctx=65536))) == "resident"
    assert client.loaded == []


def test_the_pin_follows_the_model_the_agent_actually_uses():
    """Switching models in the dashboard must move the pin, not fight it."""
    client = _FakeClient([_ps("old-model:latest")])
    asyncio.run(model_keeper.pin_once(client=client, agent=_FakeAgent(model="new-model:latest")))
    assert client.loaded == [("new-model:latest", -1, 65536)]


def test_nothing_is_pinned_while_the_agent_runs_on_another_provider():
    client = _FakeClient([])
    result = asyncio.run(model_keeper.pin_once(client=client, agent=_FakeAgent(provider="openrouter")))
    assert result == "skipped:provider"
    assert client.loaded == []
