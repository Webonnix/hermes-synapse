"""agent_desktop/server.py — the bridge inside each agent desktop container.

The ACP side runs a tiny stdio echo program in place of a real CLI agent, and
the VNC side relays to a local TCP echo server in place of x11vnc.
"""

import importlib.util
import socket
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "desktop-token"
SERVER_PATH = Path(__file__).resolve().parents[2] / "agent_desktop" / "server.py"

ECHO_AGENT = """
import sys, os
print("pid", os.getpid(), file=sys.stderr, flush=True)
for line in sys.stdin:
    if line.strip() == "quit":
        print("bye from stderr", file=sys.stderr, flush=True)
        sys.exit(3)
    sys.stdout.write("echo:" + line)
    sys.stdout.flush()
"""


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("agent_desktop_server_under_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    script = tmp_path / "echo_agent.py"
    script.write_text(ECHO_AGENT)
    monkeypatch.setattr(module, "RUNTIMES", {"echo": [sys.executable, str(script)], "missing": ["no-such-cli-xyz"]})
    monkeypatch.setattr(module, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setenv("AGENT_DESKTOP_TOKEN", TOKEN)
    return module


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_reports_installed_runtimes(bridge):
    payload = TestClient(bridge.app).get("/health").json()
    assert payload["runtimes"] == {"echo": True, "missing": False}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_acp_requires_the_desktop_token(bridge, headers):
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(bridge.app).websocket_connect("/acp?runtime=echo", headers=headers) as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_no_token_configured_means_nobody_gets_in(bridge, monkeypatch):
    monkeypatch.setenv("AGENT_DESKTOP_TOKEN", "")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(bridge.app).websocket_connect("/acp?runtime=echo", headers={"Authorization": "Bearer "}) as ws:
            ws.receive_text()


@pytest.mark.parametrize("runtime", ["bash", "missing", ""])
def test_only_installed_table_runtimes_can_start(bridge, runtime):
    with pytest.raises(WebSocketDisconnect):
        with TestClient(bridge.app).websocket_connect(f"/acp?runtime={runtime}", headers=_auth()) as ws:
            ws.receive_text()


def test_acp_pipes_lines_both_ways_and_reports_the_exit(bridge):
    with TestClient(bridge.app).websocket_connect("/acp?runtime=echo", headers=_auth()) as ws:
        ws.send_text('{"jsonrpc":"2.0","id":1,"method":"initialize"}')
        assert ws.receive_text() == 'echo:{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        ws.send_text("quit")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1011
    assert "exit 3" in exc.value.reason and "bye from stderr" in exc.value.reason


def test_agent_does_not_receive_the_bridge_token(bridge, tmp_path, monkeypatch):
    script = tmp_path / "env_agent.py"
    script.write_text("import os,sys\nsys.stdin.readline()\n"
                      "print(os.environ.get('AGENT_DESKTOP_TOKEN','<none>'), flush=True)\n")
    monkeypatch.setitem(bridge.RUNTIMES, "env", [sys.executable, str(script)])
    with TestClient(bridge.app).websocket_connect("/acp?runtime=env", headers=_auth()) as ws:
        ws.send_text("go")
        assert ws.receive_text() == "<none>"


def _echo_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data[::-1])

    threading.Thread(target=serve, daemon=True).start()
    return server


def test_vnc_relays_bytes_to_the_loopback_server(bridge, monkeypatch):
    server = _echo_server()
    monkeypatch.setattr(bridge, "VNC_PORT", server.getsockname()[1])
    with TestClient(bridge.app).websocket_connect("/vnc", headers=_auth()) as ws:
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"\n800.300 BFR"
    server.close()


def test_vnc_requires_the_desktop_token(bridge):
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(bridge.app).websocket_connect("/vnc") as ws:
            ws.receive_bytes()
    assert exc.value.code == 1008
