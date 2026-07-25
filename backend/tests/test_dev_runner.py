import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import control_plane, dev_runner_api, tools


TOKEN = "test-runner-token"


@pytest.fixture()
def runner_client(tmp_path, monkeypatch):
    repo = tmp_path / "dev-repo"
    repo.mkdir()
    monkeypatch.setattr(dev_runner_api, "REPO_ROOT", repo.resolve())
    monkeypatch.setenv("DEV_RUNNER_TOKEN", TOKEN)
    client = TestClient(dev_runner_api.app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


# ── Risk classification ───────────────────────────────────────────────────────

def test_risk_classes_for_all_six_dev_tools():
    assert control_plane.classify_tool_risk("dev_read_file") == "R1"
    assert control_plane.classify_tool_risk("dev_list_dir") == "R1"
    assert control_plane.classify_tool_risk("dev_write_file") == "R2"
    assert control_plane.classify_tool_risk("dev_patch") == "R2"
    assert control_plane.classify_tool_risk("dev_exec") == "R2"
    assert control_plane.classify_tool_risk("dev_run_tests") == "R2"
    # The deprecated raw-shell escape hatch stays R4.
    assert control_plane.classify_tool_risk("execute_command") == "R4"


# ── Path canonicalization inside the runner ───────────────────────────────────

def test_write_with_path_traversal_is_rejected(runner_client):
    response = runner_client.post(
        "/fs/write", json={"path": "../../etc/passwd", "content": "owned"}
    )
    assert response.status_code == 403
    assert "escapes" in response.json()["detail"]


def test_read_and_list_with_path_traversal_are_rejected(runner_client):
    assert runner_client.post("/fs/read", json={"path": "../secret.txt"}).status_code == 403
    assert runner_client.post("/fs/list", json={"path": "../../"}).status_code == 403


def test_write_then_read_roundtrip(runner_client):
    write = runner_client.post("/fs/write", json={"path": "pkg/app.py", "content": "print('hi')\n"})
    assert write.status_code == 200
    read = runner_client.post("/fs/read", json={"path": "pkg/app.py"})
    assert read.status_code == 200
    assert read.json()["content"] == "print('hi')\n"
    listing = runner_client.post("/fs/list", json={"path": "pkg"})
    assert [e["name"] for e in listing.json()["entries"]] == ["app.py"]


# ── Exec behaviour ────────────────────────────────────────────────────────────

def test_exec_output_is_truncated_to_64kb(runner_client):
    response = runner_client.post(
        "/exec", json={"argv": ["python3", "-c", "print('x' * 200000)"], "timeout_s": 60}
    )
    body = response.json()
    assert body["exit_code"] == 0
    assert len(body["stdout"].encode()) <= dev_runner_api.MAX_OUTPUT_BYTES + 100
    assert "[output truncated]" in body["stdout"]


def test_exec_timeout_returns_error(runner_client):
    response = runner_client.post(
        "/exec", json={"argv": ["python3", "-c", "import time; time.sleep(5)"], "timeout_s": 1}
    )
    body = response.json()
    assert body["timed_out"] is True
    assert body["exit_code"] == -1
    assert "Timed out" in body["stderr"]


def test_exec_never_uses_shell_semantics(runner_client):
    # A shell metacharacter must arrive as a literal argv token, not be expanded.
    response = runner_client.post(
        "/exec", json={"argv": ["echo", "$(id)"], "timeout_s": 10}
    )
    assert response.json()["stdout"].strip() == "$(id)"


def test_requests_without_valid_token_are_rejected(runner_client, monkeypatch):
    bare = TestClient(dev_runner_api.app)
    assert bare.post("/fs/read", json={"path": "x"}).status_code == 401
    bare.headers["Authorization"] = "Bearer wrong"
    assert bare.post("/fs/read", json={"path": "x"}).status_code == 401
    monkeypatch.delenv("DEV_RUNNER_TOKEN")
    assert bare.post("/fs/read", json={"path": "x"}).status_code == 503


def test_auto_test_runner_with_no_suite_is_a_clean_noop(runner_client):
    response = runner_client.post("/test", json={"runner": "auto"})
    body = response.json()
    assert body["exit_code"] == 0
    assert body["runner"] == "none"


# ── Backend proxy tools (mocked httpx) ────────────────────────────────────────

def test_unreachable_runner_gives_actionable_error(monkeypatch):
    monkeypatch.setenv("DEV_RUNNER_TOKEN", TOKEN)
    with patch.object(tools.httpx, "post", side_effect=httpx.ConnectError("refused")):
        result = json.loads(tools.dev_read_file("app.py"))
    assert "unreachable" in result["error"]
    assert "dev-runner" in result["error"]


def test_proxy_surfaces_runner_rejection_detail(monkeypatch):
    monkeypatch.setenv("DEV_RUNNER_TOKEN", TOKEN)
    denied = httpx.Response(403, json={"detail": "Path escapes the dev repository"},
                            request=httpx.Request("POST", "http://dev-runner:8600/fs/write"))
    with patch.object(tools.httpx, "post", return_value=denied):
        result = json.loads(tools.dev_write_file("../../etc/passwd", "x"))
    assert "403" in result["error"]
    assert "escapes" in result["error"]


def test_proxy_requires_configured_token(monkeypatch):
    monkeypatch.delenv("DEV_RUNNER_TOKEN", raising=False)
    result = json.loads(tools.dev_read_file("app.py"))
    assert "DEV_RUNNER_TOKEN" in result["error"]


def test_dev_exec_validates_argv_and_clamps_timeout(monkeypatch):
    monkeypatch.setenv("DEV_RUNNER_TOKEN", TOKEN)
    assert "argv" in json.loads(tools.dev_exec([]))["error"]
    assert "argv" in json.loads(tools.dev_exec("ls"))["error"]

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False},
                              request=httpx.Request("POST", url))

    with patch.object(tools.httpx, "post", side_effect=fake_post):
        tools.dev_exec(["ls"], timeout_s=99999)
    assert captured["payload"]["timeout_s"] == 600


def test_dev_run_tests_validates_runner():
    assert "runner" in json.loads(tools.dev_run_tests("bash"))["error"]


def test_execute_tool_routes_dev_tools(monkeypatch):
    monkeypatch.setenv("DEV_RUNNER_TOKEN", TOKEN)
    ok = httpx.Response(200, json={"path": "a.py", "content": "data"},
                        request=httpx.Request("POST", "http://dev-runner:8600/fs/read"))
    with patch.object(tools.httpx, "post", return_value=ok) as mocked:
        result = json.loads(tools.execute_tool("dev_read_file", {"path": "a.py"}))
    assert result["content"] == "data"
    assert mocked.call_args[0][0].endswith("/fs/read")
