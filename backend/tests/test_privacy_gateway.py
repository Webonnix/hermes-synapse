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


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    # No Valkey in unit tests (it would cost a connect timeout) and no
    # verdicts leaking from one test into the next.
    from backend.privacy import cache

    from backend.privacy import gateway

    monkeypatch.setenv("PRIVACY_CACHE_VALKEY", "0")
    cache.clear_memory()
    gateway.clear_memo()
    yield
    cache.clear_memory()
    gateway.clear_memo()
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
async def test_unchecked_image_parts_block_external_calls_before_http():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [{"role": "user", "content": [{"type": "text", "text": f"see {KEY}"}, image]}]
    with _no_local_spans(), patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
        result = await call_llm_normalized(api_base="https://external.invalid/v1", api_key="k", model="m",
                                           messages=messages, max_retries=0)
    assert result.status == STATUS_PROVIDER_ERROR
    assert "local model" in result.error_message
    post.assert_not_called()


@pytest.mark.asyncio
async def test_text_content_parts_still_pass_the_privacy_gateway():
    messages = [{"role": "user", "content": [{"type": "text", "text": f"see {KEY}"}]}]
    with _no_local_spans():
        await protect_messages(messages, mark_key=MARK)
    assert KEY not in messages[0]["content"][0]["text"]



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
async def test_legacy_fail_open_cannot_bypass_privacy(monkeypatch):
    monkeypatch.setenv("PRIVACY_FAIL_MODE", "open")
    err = local_llm.LocalPassError("timeout", spans=["found-before-failure"])
    messages = [{"role": "user", "content": f"{KEY} and found-before-failure"}]
    with patch("backend.privacy.detectors.local_llm.find_spans", new=AsyncMock(side_effect=err)):
        with pytest.raises(PrivacyCheckFailed):
            await protect_messages(messages, mark_key=MARK)


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


def test_local_prompt_is_few_shot_and_guards_against_injection():
    # Zero-shot + think=False made the 27B model answer [] for obvious casual
    # passwords; the examples are what makes this pass work at all.
    prompt = local_llm._SYSTEM_PROMPT
    assert "never instructions" in prompt
    assert prompt.count("<message>") >= 4 and "-> []" in prompt


# ── Stage 1a: windows, cache, deterministic placeholders, budget, queue ──────

@pytest.mark.asyncio
async def test_long_text_only_sends_trigger_windows_to_the_local_model():
    phrase = "семь семь альфа"
    filler = "def handler(event):\n    return compute(event, retries=3)\n" * 4000  # ~220k chars of code
    text = filler + f"кстати, пароль от сейфа {phrase}, не забудь\n" + filler

    async def fake_ask(chunk):
        return [phrase] if phrase in chunk else []

    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(side_effect=fake_ask)) as ask:
        messages = [{"role": "tool", "tool_call_id": "c1", "content": text}]
        await protect_messages(messages, mark_key=MARK)
    assert ask.await_count == 1
    assert len(ask.await_args.args[0]) < 1000
    assert phrase not in messages[0]["content"]


@pytest.mark.asyncio
async def test_short_text_is_checked_whole_even_without_trigger_words():
    text = "the vault opens with seven-seven-alpha"
    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(return_value=["seven-seven-alpha"])) as ask:
        messages = [{"role": "user", "content": text}]
        await protect_messages(messages, mark_key=MARK)
    assert ask.await_args.args[0] == text
    assert "seven-seven-alpha" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_resent_history_hits_the_cache_and_keeps_a_byte_identical_prefix():
    def history():
        # A new turn rebuilds fresh dicts from the DB, so no per-dict mark survives.
        return [
            {"role": "user", "content": f"ключ {KEY}, пароль от wifi синяя лампа 77"},
            {"role": "assistant", "content": "Принято."},
        ]

    with patch.object(local_llm, "_ask_local_model",
                      new=AsyncMock(return_value=["синяя лампа 77"])) as ask:
        first = history()
        await protect_messages(first, mark_key=MARK)
        calls_after_first = ask.await_count
        second = history() + [{"role": "user", "content": "ещё вопрос"}]
        await protect_messages(second, mark_key=MARK)
    assert ask.await_count == calls_after_first + 1  # only the new message
    assert [m["content"] for m in second[:2]] == [m["content"] for m in first]


def test_placeholders_are_deterministic_across_vaults():
    a, b = PlaceholderVault(), PlaceholderVault()
    assert a.placeholder_for(KEY) == b.placeholder_for(KEY)
    assert a.placeholder_for(KEY) != a.placeholder_for(KEY + "x")
    assert PLACEHOLDER_RE.fullmatch(a.placeholder_for(KEY))


def test_placeholder_collision_gets_a_longer_prefix(monkeypatch):
    from backend.privacy import keys

    monkeypatch.setattr(keys, "placeholder_digest", lambda value: "ab" * 32 if value in ("one", "two") else "cd" * 32)
    vault = PlaceholderVault()
    one, two = vault.placeholder_for("one"), vault.placeholder_for("two")
    assert one != two and len(two) > len(one)
    assert vault.value_for(one) == "one" and vault.value_for(two) == "two"


@pytest.mark.asyncio
async def test_call_budget_open_skips_and_closed_blocks(monkeypatch):
    monkeypatch.setenv("PRIVACY_LLM_MAX_CALLS", "2")
    monkeypatch.setenv("PRIVACY_LLM_FULL_SCAN_CHARS", "0")
    text = "\n".join(f"пароль номер {i}" + " ." * 400 for i in range(5))  # 5 separate windows

    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(return_value=[])) as ask:
        with pytest.raises(local_llm.LocalPassError):
            await local_llm.find_spans(text)
    assert ask.await_count == 2

    from backend.privacy import cache, gateway
    monkeypatch.setenv("PRIVACY_FAIL_MODE", "open")
    cache.clear_memory()
    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(return_value=[])):
        messages = [{"role": "tool", "tool_call_id": "c", "content": text}]
        with pytest.raises(PrivacyCheckFailed):
            await protect_messages(messages, mark_key=MARK)
    assert gateway._memo == {}  # incomplete pass is not frozen into the memo

    monkeypatch.setenv("PRIVACY_FAIL_MODE", "closed")
    cache.clear_memory()
    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(return_value=[])):
        with pytest.raises(PrivacyCheckFailed):
            await protect_messages([{"role": "tool", "tool_call_id": "c", "content": text}], mark_key=MARK)


@pytest.mark.asyncio
async def test_valkey_copy_of_a_verdict_is_encrypted(monkeypatch):
    from backend import valkey_client
    from backend.privacy import cache

    monkeypatch.setenv("PRIVACY_CACHE_VALKEY", "1")
    stored = {}
    monkeypatch.setattr(valkey_client, "set_value", lambda k, v, ttl=None: stored.update({k: v}))
    monkeypatch.setattr(valkey_client, "get_value", lambda k: stored.get(k))
    await cache.put("пароль синяя лампа 77", ["синяя лампа 77"])
    (key, token), = stored.items()
    assert "синяя" not in token and "пароль" not in key
    cache.clear_memory()
    assert await cache.get("пароль синяя лампа 77") == ["синяя лампа 77"]


def test_code_without_trigger_words_produces_no_windows():
    code = ("for key in config.keys():\n    value = lookup(key, code=200)\n" * 20000)
    assert local_llm.select_windows(code) == []


@pytest.mark.asyncio
async def test_gate_serves_lower_priority_number_first_and_times_out():
    import asyncio

    from backend.llm_gate import GateTimeout, PriorityGate

    gate = PriorityGate(1)
    order = []
    await gate.acquire(0)  # a running chat holds the only slot

    async def waiter(name, prio):
        async with gate.slot(prio):
            order.append(name)

    background = asyncio.create_task(waiter("privacy", 5))
    await asyncio.sleep(0)
    chat = asyncio.create_task(waiter("chat", 0))
    await asyncio.sleep(0)
    with pytest.raises(GateTimeout):
        await gate.acquire(5, timeout=0.01)
    gate.release()
    await asyncio.gather(background, chat)
    assert order == ["chat", "privacy"]
    assert gate.active == 0 and gate.waiting == 0


@pytest.mark.asyncio
async def test_short_tool_output_without_triggers_skips_the_local_model():
    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(return_value=[])) as ask:
        messages = [{"role": "tool", "tool_call_id": "c", "content": "build ok, 120 tests passed"}]
        await protect_messages(messages, mark_key=MARK)
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_memo_replays_a_seen_message_without_any_detector_work():
    from backend.privacy import gateway

    text = f"ключ {KEY}, пароль от wifi синяя лампа 77"
    with patch.object(local_llm, "_ask_local_model", new=AsyncMock(return_value=["синяя лампа 77"])):
        first = [{"role": "user", "content": text}]
        await protect_messages(first, mark_key=MARK)
    with patch("backend.privacy.gateway.regex_redact", side_effect=AssertionError("memo miss")), \
         patch.object(local_llm, "find_spans", new=AsyncMock(side_effect=AssertionError("memo miss"))):
        second = [{"role": "user", "content": text}]
        vault = await protect_messages(second, mark_key=MARK)
    assert second[0]["content"] == first[0]["content"]
    assert restore_text(second[0]["content"], vault) == text
    assert second[0][MARK]  # mapping recorded, so a later call can restore it
