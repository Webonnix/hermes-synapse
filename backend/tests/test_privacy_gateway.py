"""Tests for backend/privacy: what the gateway covers beyond plain string
content, placeholder consistency, chunked local checks and the fail mode.

The local model is patched out everywhere: find_spans for gateway-level
tests, _ask_local_model where chunking itself is under test.
"""

import json
import json as _json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.llm_client import STATUS_PROVIDER_ERROR, call_llm_normalized
from backend.privacy import config
from backend.privacy.detectors import local_llm
from backend.privacy.gateway import (
    PrivacyCheckFailed,
    protect_messages,
    restore_arguments,
    restore_text,
)
from backend.privacy.vault import PLACEHOLDER_RE, PlaceholderVault

MARK = "_test_mark"
KEY = "sk-" + "k" * 24


def _no_local_spans(spans=None):
    return patch("backend.privacy.detectors.local_llm.find_spans",
                 new=AsyncMock(return_value=list(spans or [])))


@pytest.mark.asyncio
async def test_same_secret_in_two_messages_gets_one_placeholder():
    messages = [
        {"role": "user", "content": f"key {KEY}"},
        {"role": "tool", "content": f"echo: {KEY}", "tool_call_id": "c1"},
    ]
    with _no_local_spans():
        vault = await protect_messages(messages, mark_key=MARK)
    first = PLACEHOLDER_RE.findall(messages[0]["content"])
    second = PLACEHOLDER_RE.findall(messages[1]["content"])
    assert first == second and len(first) == 1
    assert len(vault) == 1


@pytest.mark.asyncio
async def test_secret_from_an_earlier_call_reuses_its_placeholder():
    messages = [{"role": "user", "content": f"key {KEY}"}]
    with _no_local_spans():
        await protect_messages(messages, mark_key=MARK)
        old = PLACEHOLDER_RE.findall(messages[0]["content"])[0]
        messages.append({"role": "user", "content": f"again {KEY}"})
        vault = await protect_messages(messages, mark_key=MARK)
    assert messages[1]["content"] == f"again {old}"
    assert vault.value_for(old) == KEY


@pytest.mark.asyncio
async def test_multimodal_text_parts_are_redacted_and_images_kept():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [{"role": "user", "content": [{"type": "text", "text": f"see {KEY}"}, image]}]
    with _no_local_spans():
        await protect_messages(messages, mark_key=MARK)
    parts = messages[0]["content"]
    assert KEY not in parts[0]["text"] and PLACEHOLDER_RE.search(parts[0]["text"])
    assert parts[1] == image


@pytest.mark.asyncio
async def test_tool_call_arguments_are_redacted_per_value_and_stay_valid_json():
    args = json.dumps({"command": "login", "note": "password: hunter2secret", "token": KEY})
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": args}},
    ]}]
    with _no_local_spans():
        await protect_messages(messages, mark_key=MARK)
    sent = messages[0]["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(sent)  # the raw-string approach would have eaten the quote
    assert parsed["command"] == "login"
    assert "hunter2secret" not in sent and KEY not in sent


def test_restore_arguments_keeps_json_valid_for_values_with_quotes():
    vault = PlaceholderVault()
    tricky = 'pa"ss\\word'
    placeholder = vault.placeholder_for(tricky)
    restored = restore_arguments(json.dumps({"password": placeholder}), vault)
    assert json.loads(restored) == {"password": tricky}


def test_restore_text_leaves_unknown_placeholders_alone():
    vault = PlaceholderVault()
    known = vault.placeholder_for("real")
    assert restore_text(f"{known} and [SECRET_ffffff]", vault) == "real and [SECRET_ffffff]"


@pytest.mark.asyncio
async def test_local_span_is_added_but_never_rewrites_a_placeholder():
    messages = [{"role": "user", "content": f"wifi is cat ginger 2019, key {KEY}"}]
    # A hostile/odd answer: a real span, a span that is part of a placeholder,
    # and one that does not occur at all.
    with _no_local_spans(["cat ginger 2019", "SECRET_", "not in text"]):
        vault = await protect_messages(messages, mark_key=MARK)
    content = messages[0]["content"]
    assert "cat ginger 2019" not in content and KEY not in content
    assert len(PLACEHOLDER_RE.findall(content)) == 2
    assert sorted(vault.as_dict().values()) == sorted(["cat ginger 2019", KEY])


def test_split_chunks_covers_the_whole_text_with_overlap():
    text = "".join(chr(ord("a") + i % 26) for i in range(10000))
    chunks = local_llm.split_chunks(text, 3000, 200)
    assert len(chunks) > 3
    assert all(len(c) <= 3000 for c in chunks)
    assert chunks[-1].endswith(text[-200:])
    rebuilt = chunks[0] + "".join(c[200:] for c in chunks[1:])
    assert rebuilt == text


@pytest.mark.asyncio
async def test_long_text_is_checked_past_the_old_4000_char_cut(monkeypatch):
    monkeypatch.setenv("PRIVACY_LLM_CHUNK_CHARS", "1000")
    secret_phrase = "the vault code is seven-seven-alpha"
    text = "x " * 3000 + secret_phrase + " y" * 100

    async def fake_ask(chunk):
        return [secret_phrase] if secret_phrase in chunk else []

    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(side_effect=fake_ask)) as ask:
        messages = [{"role": "user", "content": text}]
        await protect_messages(messages, mark_key=MARK)
    assert ask.await_count > 4
    assert secret_phrase not in messages[0]["content"]


@pytest.mark.asyncio
async def test_fail_open_keeps_regex_and_partial_spans(monkeypatch):
    monkeypatch.setenv("PRIVACY_FAIL_MODE", "open")
    err = local_llm.LocalPassError("timeout", spans=["found-before-failure"])
    messages = [{"role": "user", "content": f"{KEY} and found-before-failure"}]
    with patch("backend.privacy.detectors.local_llm.find_spans", new=AsyncMock(side_effect=err)):
        await protect_messages(messages, mark_key=MARK)
    content = messages[0]["content"]
    assert KEY not in content and "found-before-failure" not in content


@pytest.mark.asyncio
async def test_fail_closed_raises(monkeypatch):
    monkeypatch.setenv("PRIVACY_FAIL_MODE", "closed")
    assert config.fail_mode() == config.FAIL_CLOSED
    err = local_llm.LocalPassError("connection refused")
    with patch("backend.privacy.detectors.local_llm.find_spans", new=AsyncMock(side_effect=err)):
        with pytest.raises(PrivacyCheckFailed):
            await protect_messages([{"role": "user", "content": "hello"}], mark_key=MARK)


@pytest.mark.asyncio
async def test_fail_closed_call_never_reaches_the_provider(monkeypatch):
    monkeypatch.setenv("PRIVACY_FAIL_MODE", "closed")
    err = local_llm.LocalPassError("connection refused")
    post = AsyncMock()
    with patch("httpx.AsyncClient.post", new=post), \
         patch("backend.privacy.detectors.local_llm.find_spans", new=AsyncMock(side_effect=err)):
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": "hello"}], max_retries=0,
        )
    post.assert_not_called()
    assert r.status == STATUS_PROVIDER_ERROR
    assert "privacy" in (r.error_message or "").lower()


@pytest.mark.asyncio
async def test_call_restores_secret_inside_tool_call_arguments():
    sent = {}

    async def fake_post(url, json=None, headers=None):
        sent["content"] = json["messages"][0]["content"]
        placeholder = PLACEHOLDER_RE.findall(sent["content"])[0]
        body = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "use_key", "arguments": _json.dumps({"key": placeholder})}},
        ]}, "finish_reason": "tool_calls"}]}
        resp = httpx.Response(200, json=body, request=httpx.Request("POST", url))
        return resp

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), _no_local_spans():
        r = await call_llm_normalized(
            api_base="https://openrouter.ai/api/v1", api_key="k", model="m",
            messages=[{"role": "user", "content": f"use {KEY}"}], max_retries=0,
        )
    assert KEY not in sent["content"]
    assert json.loads(r.tool_calls[0]["function"]["arguments"]) == {"key": KEY}
