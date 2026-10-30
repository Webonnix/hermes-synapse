"""/agent-llm/v1 — agent desktops' door to the local model (llm_proxy.py).

llama.cpp is an httpx MockTransport; requests go through the real router and
the real llm_gate.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from backend import llm_gate
from backend.agent_runtimes import desktop, llm_proxy

MODEL = "qwen-local"
_real_killed = llm_proxy._killed


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("AGENT_DESKTOPS_ENABLED", "true")
    monkeypatch.setenv("AGENT_DESKTOP_TOKEN_SECRET", "s3cret")
    monkeypatch.setenv("AGENT_LLM_MODEL", MODEL)
    monkeypatch.setenv("AGENT_LLM_MAX_TOKENS", "4096")
    monkeypatch.setenv("LLAMACPP_BASE_URL", "http://llama:8080")
    monkeypatch.setenv("LLAMACPP_API_KEY", "upstream-key")
    monkeypatch.setenv("AGENT_LLM_EXTERNAL_ENABLED", "false")
    monkeypatch.setattr(llm_proxy, "usage_by_desktop", {})
    monkeypatch.setattr(llm_proxy, "_slots", {})
    monkeypatch.setattr(llm_proxy, "_catalog", {"at": 0.0, "ids": None})
    monkeypatch.setattr(llm_proxy, "_killed", lambda: False)


class Upstream:
    """Fake llama.cpp: records requests, answers JSON or an SSE stream."""

    def __init__(self, monkeypatch, *, status=200, hold: asyncio.Event | None = None):
        self.requests = []
        self.status = status
        self.hold = hold
        monkeypatch.setattr(llm_proxy, "_http_client",
                            lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handle)))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.hold is not None:
            await self.hold.wait()
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"message": "loading model"}})
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "hel"}}]},
                {"choices": [{"delta": {"content": "lo"}}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}},
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 1}})


def _client():
    app = FastAPI()
    app.include_router(llm_proxy.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend")


def _auth(key="alice"):
    return {"Authorization": f"Bearer {desktop.llm_token(key)}"}


def _body(**extra):
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.parametrize("header", [
    {},
    {"Authorization": "Bearer alice.0000"},
    {"Authorization": "Bearer ../etc.abc"},
])
async def test_requests_without_a_valid_desktop_token_are_refused(env, monkeypatch, header):
    upstream = Upstream(monkeypatch)
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=header)
    assert response.status_code == 401
    assert not upstream.requests


async def test_a_token_is_bound_to_its_desktop(env):
    token = desktop.llm_token("alice")
    assert desktop.verify_llm_token(token) == "alice"
    forged = "bob." + token.split(".", 1)[1]
    assert desktop.verify_llm_token(forged) is None
    assert token.split(".", 1)[1] != desktop.desktop_token("alice")


async def test_disabled_desktops_get_no_model(env, monkeypatch):
    monkeypatch.setenv("AGENT_DESKTOPS_ENABLED", "false")
    Upstream(monkeypatch)
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth())
    assert response.status_code == 503


async def test_only_the_local_model_is_served(env, monkeypatch):
    upstream = Upstream(monkeypatch)
    async with _client() as client:
        models = (await client.get("/agent-llm/v1/models", headers=_auth())).json()
        response = await client.post("/agent-llm/v1/chat/completions",
                                     json={**_body(), "model": "gpt-5"}, headers=_auth())
    assert [m["id"] for m in models["data"]] == [MODEL]
    assert response.status_code == 404 and response.json()["error"]["code"] == "model_not_found"
    assert not upstream.requests


@pytest.mark.parametrize("extra,expected", [
    ({"max_tokens": 1_000_000}, 4096),
    ({}, 4096),
    ({"max_completion_tokens": 100}, 100),
    ({"max_tokens": "lots"}, 4096),
])
async def test_max_tokens_is_capped(env, monkeypatch, extra, expected):
    upstream = Upstream(monkeypatch)
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions", json=_body(**extra), headers=_auth())
    assert response.status_code == 200
    sent = upstream.requests[0][1]
    assert sent["max_tokens"] == expected and "max_completion_tokens" not in sent


async def test_plain_request_reaches_llama_with_its_key_and_counts_usage(env, monkeypatch):
    upstream = Upstream(monkeypatch)
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth())
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    request, _ = upstream.requests[0]
    assert str(request.url) == "http://llama:8080/v1/chat/completions"
    # The desktop's own token never reaches llama; the backend's key does.
    assert request.headers["authorization"] == "Bearer upstream-key"
    assert llm_proxy.usage_by_desktop["alice"] == {"requests": 1, "prompt_tokens": 7, "completion_tokens": 1}
    assert llm_gate.llamacpp_gate().active == 0


async def test_stream_is_relayed_whole_and_frees_the_queue(env, monkeypatch):
    upstream = Upstream(monkeypatch)
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions", json=_body(stream=True), headers=_auth())
    lines = [line for line in response.text.split("\n") if line.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    text = "".join(json.loads(l[5:])["choices"][0]["delta"]["content"]
                   for l in lines[:-1] if json.loads(l[5:])["choices"])
    assert text == "hello"
    assert upstream.requests[0][1]["stream_options"] == {"include_usage": True}
    assert llm_proxy.usage_by_desktop["alice"]["completion_tokens"] == 2
    assert llm_gate.llamacpp_gate().active == 0
    assert not llm_proxy._slots["alice"].locked()


async def test_agents_queue_behind_chat_in_the_shared_gate(env, monkeypatch):
    Upstream(monkeypatch)
    seen = []
    gate = llm_gate.llamacpp_gate()
    real_acquire = gate.acquire

    async def spy(priority, timeout=None):
        seen.append(priority)
        await real_acquire(priority, timeout)

    monkeypatch.setattr(gate, "acquire", spy)
    async with _client() as client:
        await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth())
    assert seen == [llm_gate.PRIORITY_BACKGROUND]


async def test_one_request_in_flight_per_desktop_and_a_busy_model_answers_503(env, monkeypatch):
    hold = asyncio.Event()
    Upstream(monkeypatch, hold=hold)
    monkeypatch.setenv("AGENT_LLM_QUEUE_TIMEOUT", "0.2")
    async with _client() as client:
        first = asyncio.create_task(client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth()))
        await asyncio.sleep(0.05)
        same = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth())
        other = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth("bob"))
        hold.set()
        assert (await first).status_code == 200
    assert same.status_code == 429
    # bob waited for the single slot alice held, and gave up at the queue timeout.
    assert other.status_code == 503 and other.json()["error"]["code"] == "model_busy"
    assert not llm_proxy._slots["bob"].locked()


async def test_upstream_error_passes_through_and_releases(env, monkeypatch):
    Upstream(monkeypatch, status=503)
    async with _client() as client:
        plain = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth())
        streamed = await client.post("/agent-llm/v1/chat/completions", json=_body(stream=True), headers=_auth())
    assert plain.status_code == 503 and streamed.status_code == 503
    assert llm_gate.llamacpp_gate().active == 0
    assert not llm_proxy._slots["alice"].locked()


# ── kill switch and OmniRoute ───────────────────────────────────────────────

async def test_kill_switch_refuses_every_model(env, monkeypatch, tmp_path):
    from backend import control_plane, database

    db_path = str(tmp_path / "cp.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    # The env fixture stubs the kill-switch read; this test needs the real one.
    monkeypatch.setattr(llm_proxy, "_killed", _real_killed)
    upstream = Upstream(monkeypatch)
    control_plane.set_kill_switch(True, "test")
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions", json=_body(), headers=_auth())
    assert response.status_code == 503 and response.json()["error"]["code"] == "killed"
    assert not upstream.requests


class Router:
    """Fake OmniRoute (catalogue + completions) beside the fake llama."""

    def __init__(self, monkeypatch, ids=("ds/deepseek-flash", "openai/gpt-x", "gemini/pro")):
        self.ids = list(ids)
        self.requests = []
        self.catalog_calls = 0
        monkeypatch.setenv("ROUTER_API_BASE", "http://omniroute:20128")
        monkeypatch.setenv("ROUTER_API_KEY", "router-secret")
        monkeypatch.setattr(llm_proxy, "_http_client",
                            lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handle)))

    async def handle(self, request):
        if request.url.path == "/v1/models":
            self.catalog_calls += 1
            return httpx.Response(200, json={"data": [{"id": i} for i in self.ids]})
        body = json.loads(request.content)
        self.requests.append((request, body))
        return httpx.Response(200, json={"choices": [{"message": {"content": "from router"}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})


@pytest.fixture()
def usage_db(tmp_path, monkeypatch):
    from backend import database, router_usage

    db_path = str(tmp_path / "usage.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(router_usage, "DB_PATH", db_path)
    return db_path


async def test_omniroute_models_are_hidden_until_the_owner_enables_them(env, monkeypatch, usage_db):
    router = Router(monkeypatch)
    async with _client() as client:
        models = (await client.get("/agent-llm/v1/models", headers=_auth())).json()
        call = await client.post("/agent-llm/v1/chat/completions",
                                 json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
    assert [m["id"] for m in models["data"]] == [MODEL]
    assert call.status_code == 404 and not router.requests and router.catalog_calls == 0


async def test_enabled_omniroute_models_are_listed_routed_and_logged(env, monkeypatch, usage_db):
    from backend import router_usage

    monkeypatch.setenv("AGENT_LLM_EXTERNAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_LLM_EXTERNAL_MODELS", "ds/*,gemini/*")
    router = Router(monkeypatch)
    async with _client() as client:
        models = (await client.get("/agent-llm/v1/models", headers=_auth())).json()
        ok = await client.post("/agent-llm/v1/chat/completions",
                               json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
        blocked = await client.post("/agent-llm/v1/chat/completions",
                                    json={**_body(), "model": "omniroute/openai/gpt-x"}, headers=_auth())
    ids = [m["id"] for m in models["data"]]
    assert ids == [MODEL, "omniroute/ds/deepseek-flash", "omniroute/gemini/pro"]
    assert ok.json()["choices"][0]["message"]["content"] == "from router"
    assert blocked.status_code == 404
    request, body = router.requests[0]
    # The router gets its own model id and key; the desktop token stays here.
    assert body["model"] == "ds/deepseek-flash" and request.headers["authorization"] == "Bearer router-secret"
    assert str(request.url) == "http://omniroute:20128/v1/chat/completions"
    # The GPU queue is for the local model only.
    assert llm_gate.llamacpp_gate().active == 0
    with router_usage._connect() as connection:
        rows = connection.execute("SELECT tier_label, kind, input_tokens, output_tokens FROM router_usage_log").fetchall()
    assert [tuple(r) for r in rows] == [("agent-desktop:alice", "agent_desktop", 1000, 500)]
    assert router.catalog_calls == 1  # cached between the listing and the calls


async def test_daily_request_budget_stops_omniroute_but_not_the_local_model(env, monkeypatch, usage_db):
    monkeypatch.setenv("AGENT_LLM_EXTERNAL_ENABLED", "true")
    monkeypatch.setenv("AGENT_LLM_EXTERNAL_DAILY_REQUESTS", "1")
    router = Router(monkeypatch)
    async with _client() as client:
        first = await client.post("/agent-llm/v1/chat/completions",
                                  json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
        second = await client.post("/agent-llm/v1/chat/completions",
                                   json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
        other_desktop = await client.post("/agent-llm/v1/chat/completions",
                                          json={**_body(), "model": "omniroute/ds/deepseek-flash"},
                                          headers=_auth("bob"))
    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["error"]["code"] == "budget_exceeded"
    assert other_desktop.status_code == 200  # the budget is per desktop
    assert len(router.requests) == 2


async def test_unreachable_router_refuses_instead_of_guessing(env, monkeypatch, usage_db):
    monkeypatch.setenv("AGENT_LLM_EXTERNAL_ENABLED", "true")

    async def down(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(llm_proxy, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    async with _client() as client:
        response = await client.post("/agent-llm/v1/chat/completions",
                                     json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
    assert response.status_code == 503


async def test_router_key_comes_from_the_dashboard_store_first(env, monkeypatch, usage_db):
    from backend import database

    monkeypatch.setenv("AGENT_LLM_EXTERNAL_ENABLED", "true")
    router = Router(monkeypatch)
    monkeypatch.setenv("ROUTER_API_KEY", "")
    monkeypatch.setattr(database, "get_api_key", lambda name: "stored-key" if name == "ROUTER_API_KEY" else None)
    async with _client() as client:
        await client.post("/agent-llm/v1/chat/completions",
                          json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
    assert router.requests[0][0].headers["authorization"] == "Bearer stored-key"


async def test_without_a_router_key_only_the_local_model_is_offered(env, monkeypatch, usage_db):
    from backend import database

    monkeypatch.setenv("AGENT_LLM_EXTERNAL_ENABLED", "true")
    router = Router(monkeypatch)
    monkeypatch.setenv("ROUTER_API_KEY", "")
    monkeypatch.setattr(database, "get_api_key", lambda name: None)
    async with _client() as client:
        models = (await client.get("/agent-llm/v1/models", headers=_auth())).json()
        call = await client.post("/agent-llm/v1/chat/completions",
                                 json={**_body(), "model": "omniroute/ds/deepseek-flash"}, headers=_auth())
    assert [m["id"] for m in models["data"]] == [MODEL]
    assert call.status_code == 503 and router.catalog_calls == 0
