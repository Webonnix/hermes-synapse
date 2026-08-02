import json
from unittest.mock import patch

import httpx
import pytest

from backend import control_plane, tools


TOKEN = "test-browser-runner-token"


def test_disabled_by_default_short_circuits_without_a_network_call(monkeypatch):
    monkeypatch.delenv("BROWSER_AGENT_ENABLED", raising=False)
    monkeypatch.setenv("BROWSER_RUNNER_TOKEN", TOKEN)
    with patch.object(tools.httpx, "post") as mocked:
        result = json.loads(tools.browser_read("read example.com"))
    mocked.assert_not_called()
    assert "disabled" in result["error"]
    assert "BROWSER_AGENT_ENABLED" in result["error"]


def test_enabled_but_missing_token_gives_a_clear_error(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_ENABLED", "true")
    monkeypatch.delenv("BROWSER_RUNNER_TOKEN", raising=False)
    result = json.loads(tools.browser_read("read example.com"))
    assert "BROWSER_RUNNER_TOKEN" in result["error"]


def test_unreachable_runner_gives_actionable_error(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_ENABLED", "true")
    monkeypatch.setenv("BROWSER_RUNNER_TOKEN", TOKEN)
    with patch.object(tools.httpx, "post", side_effect=httpx.ConnectError("refused")):
        result = json.loads(tools.browser_read("read example.com"))
    assert "unreachable" in result["error"]
    assert "browser-runner" in result["error"]


def test_browser_read_sends_read_only_mode_with_no_domain_restriction(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_ENABLED", "true")
    monkeypatch.setenv("BROWSER_RUNNER_TOKEN", TOKEN)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return httpx.Response(200, json={"result": "The Example Domain", "success": True, "steps": 2, "cost_usd": 0.0},
                              request=httpx.Request("POST", url))

    with patch.object(tools.httpx, "post", side_effect=fake_post):
        result = json.loads(tools.browser_read("find the page title", start_url="https://example.com"))

    assert captured["url"].endswith("/run")
    assert captured["payload"]["mode"] == "read_only"
    assert captured["payload"]["start_url"] == "https://example.com"
    assert captured["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert result["result"] == "The Example Domain"


def test_browser_task_sends_interactive_mode_and_parses_allowed_domains(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_ENABLED", "true")
    monkeypatch.setenv("BROWSER_RUNNER_TOKEN", TOKEN)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return httpx.Response(200, json={"result": "done", "success": True, "steps": 5, "cost_usd": 0.01},
                              request=httpx.Request("POST", url))

    with patch.object(tools.httpx, "post", side_effect=fake_post):
        tools.browser_task("fill in the contact form", allowed_domains="example.com, other.com")

    assert captured["payload"]["mode"] == "interactive"
    assert captured["payload"]["allowed_domains"] == ["example.com", "other.com"]


def test_proxy_surfaces_runner_rejection_detail(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_ENABLED", "true")
    monkeypatch.setenv("BROWSER_RUNNER_TOKEN", TOKEN)
    busy = httpx.Response(429, json={"detail": "Another browser task is already running."},
                          request=httpx.Request("POST", "http://browser-runner:8800/run"))
    with patch.object(tools.httpx, "post", return_value=busy):
        result = json.loads(tools.browser_task("do something"))
    assert "429" in result["error"]
    assert "already running" in result["error"]


def test_execute_tool_routes_browser_tools(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_ENABLED", "true")
    monkeypatch.setenv("BROWSER_RUNNER_TOKEN", TOKEN)
    ok = httpx.Response(200, json={"result": "hello", "success": True, "steps": 1, "cost_usd": 0.0},
                        request=httpx.Request("POST", "http://browser-runner:8800/run"))
    with patch.object(tools.httpx, "post", return_value=ok) as mocked:
        result = json.loads(tools.execute_tool("browser_read", {"task": "say hello"}))
    assert result["result"] == "hello"
    assert mocked.call_args[0][0].endswith("/run")


def test_browser_tools_are_registered_in_the_schema():
    names = {t["function"]["name"] for t in tools.TOOLS_SCHEMA}
    assert "browser_read" in names
    assert "browser_task" in names
