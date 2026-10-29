"""CLI coding agents in agent desktops: ACP client, Control Plane gating, turns,
desktop container settings.

The agent is backend/tests/fakes/fake_acp_agent.py over an in-memory pipe, so
nothing here starts Docker or a real CLI.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from backend import autonomy_mode, control_plane, database
from backend.agent_runtimes import desktop, sessions
from backend.agent_runtimes.acp import AcpClosed, AcpConnection, AcpError
from backend.tests.fakes.fake_acp_agent import FakeAcpAgent


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "control-plane.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    return db_path


async def _noop_update(session_id, update):
    return None


async def _deny(params):
    return {"outcome": "cancelled"}


# ── ACP client ──────────────────────────────────────────────────────────────

async def test_acp_turn_streams_updates_and_refuses_unoffered_client_methods():
    agent = FakeAcpAgent(noise=True).start()
    updates = []

    async def on_update(session_id, update):
        updates.append((session_id, update["sessionUpdate"]))

    conn = AcpConnection(agent.transport, on_update=on_update, on_permission=_deny)
    conn.start()
    await conn.initialize()
    session_id = await conn.new_session("/home/agent/workspace")
    stop = await conn.prompt(session_id, "build it")
    await asyncio.sleep(0)

    assert stop == "end_turn"
    assert ("sess-1", "agent_message_chunk") in updates
    # Hermes advertises no fs/terminal, and answers an fs request with an error.
    init = next(m for m in agent.received if m.get("method") == "initialize")
    assert init["params"]["clientCapabilities"] == {"fs": {"readTextFile": False, "writeTextFile": False},
                                                    "terminal": False}
    assert agent.client_errors[0]["error"]["code"] == -32601
    await conn.close()


async def test_acp_rejects_another_protocol_version():
    agent = FakeAcpAgent(protocol_version=2).start()
    conn = AcpConnection(agent.transport, on_update=_noop_update, on_permission=_deny)
    conn.start()
    with pytest.raises(AcpError):
        await conn.initialize()
    await conn.close()


async def test_acp_agent_exit_fails_the_running_turn_instead_of_hanging():
    agent = FakeAcpAgent()
    agent.hold_prompt = asyncio.Event()
    agent.start()
    conn = AcpConnection(agent.transport, on_update=_noop_update, on_permission=_deny)
    conn.start()
    await conn.initialize()
    session_id = await conn.new_session("/w")
    turn = asyncio.create_task(conn.prompt(session_id, "x"))
    await asyncio.sleep(0.05)
    await agent.hang_up()
    with pytest.raises(AcpClosed):
        await asyncio.wait_for(turn, 2)
    assert conn.closed


async def test_acp_permission_handler_failure_answers_with_an_error_not_an_approval():
    agent = FakeAcpAgent(permission_kind="execute").start()

    async def broken(params):
        raise RuntimeError("boom")

    conn = AcpConnection(agent.transport, on_update=_noop_update, on_permission=broken)
    conn.start()
    await conn.initialize()
    await conn.prompt(await conn.new_session("/w"), "x")
    assert "error" in agent.permission_answers[0]
    assert "result" not in agent.permission_answers[0]
    await conn.close()


# ── option choice and update mapping ───────────────────────────────────────

def test_pick_option_never_answers_allow_always():
    options = [{"optionId": "always", "kind": "allow_always"}, {"optionId": "no", "kind": "reject_once"}]
    assert sessions.pick_option(options, allow=True) is None
    options.append({"optionId": "once", "kind": "allow_once"})
    assert sessions.pick_option(options, allow=True) == "once"
    assert sessions.pick_option(options, allow=False) == "no"


def test_map_update_shows_messages_tools_and_plans_only():
    assert sessions.map_update({"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "hi"}}) == {"type": "message", "text": "hi"}
    tool = sessions.map_update({"sessionUpdate": "tool_call_update", "toolCallId": "c", "status": "completed"})
    assert tool == {"type": "tool", "id": "c", "status": "completed"}
    assert sessions.map_update({"sessionUpdate": "available_commands_update", "availableCommands": []}) is None
    assert sessions.map_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "image"}}) is None


# ── turns through the Control Plane ─────────────────────────────────────────

@pytest.fixture()
def fake_desktop(monkeypatch):
    """Replaces the container and its WebSocket with a fake agent per test."""
    state = {"agent": None}

    async def ensure(key):
        return {"status": "ok"}

    async def open_transport(key, path):
        return state["agent"].transport

    monkeypatch.setattr(desktop, "ensure_desktop", ensure)
    monkeypatch.setattr(desktop.DesktopTransport, "open", staticmethod(open_transport))
    monkeypatch.setattr(sessions, "PERMISSION_POLL_S", 0.01)
    return state


async def _finish(manager, key, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        events = manager.events_since(key, 0)
        if any(e["type"] == "turn_end" for e in events):
            return events
        await asyncio.sleep(0.01)
    raise AssertionError(f"turn did not end: {manager.events_since(key, 0)}")


def _tasks(tool_name):
    return [t for t in control_plane.list_tasks(limit=50) if t["tool_name"] == tool_name]


async def test_turn_with_r2_permission_runs_without_approval_and_is_audited(control_db, fake_desktop):
    fake_desktop["agent"] = FakeAcpAgent(permission_kind="execute", reply="built").start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "build the site")
    events = await _finish(manager, "alice")

    assert fake_desktop["agent"].permission_answers[0]["result"]["outcome"] == {"outcome": "selected",
                                                                               "optionId": "once"}
    assert [e["text"] for e in events if e["type"] == "message"] == ["built"]
    assert events[-1]["stop_reason"] == "end_turn" and not events[-1]["error"]
    [task] = _tasks("agent_desktop_execute")
    assert task["risk_class"] == "R2" and task["status"] == "done"


async def test_supervised_delete_waits_for_the_owner_then_runs(control_db, fake_desktop):
    autonomy_mode.set_mode(autonomy_mode.SUPERVISED)
    fake_desktop["agent"] = FakeAcpAgent(permission_kind="delete").start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "clean up")

    for _ in range(300):
        pending = [t for t in _tasks("agent_desktop_delete") if t["status"] == "awaiting_approval"]
        if pending:
            break
        await asyncio.sleep(0.01)
    assert pending, "the delete should wait for an owner approval"
    assert not fake_desktop["agent"].permission_answers
    control_plane.approve_task(pending[0]["id"])

    await _finish(manager, "alice")
    assert fake_desktop["agent"].permission_answers[0]["result"]["outcome"]["optionId"] == "once"


async def test_rejected_permission_answers_the_agent_with_a_refusal(control_db, fake_desktop):
    autonomy_mode.set_mode(autonomy_mode.SUPERVISED)
    fake_desktop["agent"] = FakeAcpAgent(permission_kind="delete").start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "clean up")
    for _ in range(300):
        pending = [t for t in _tasks("agent_desktop_delete") if t["status"] == "awaiting_approval"]
        if pending:
            break
        await asyncio.sleep(0.01)
    control_plane.reject_task(pending[0]["id"])

    events = await _finish(manager, "alice")
    assert fake_desktop["agent"].permission_answers[0]["result"]["outcome"]["optionId"] == "no"
    assert [e["granted"] for e in events if e["type"] == "permission"] == [False]


async def test_kill_switch_refuses_even_an_r0_request(control_db, fake_desktop):
    control_plane.set_kill_switch(True, "test")
    fake_desktop["agent"] = FakeAcpAgent(permission_kind="read").start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "look around")
    await _finish(manager, "alice")
    assert fake_desktop["agent"].permission_answers[0]["result"]["outcome"]["optionId"] == "no"
    assert not _tasks("agent_desktop_read")


async def test_unknown_tool_kind_is_r4_even_in_auto_mode(control_db, fake_desktop):
    autonomy_mode.set_mode(autonomy_mode.AUTO)
    fake_desktop["agent"] = FakeAcpAgent(permission_kind="teleport").start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "x")
    for _ in range(300):
        tasks = _tasks(sessions.UNKNOWN_KIND_TOOL)
        if tasks:
            break
        await asyncio.sleep(0.01)
    assert tasks[0]["risk_class"] == "R4" and tasks[0]["approvals_required"] == 2
    await manager.cancel("alice", "grok")
    events = await _finish(manager, "alice")
    assert fake_desktop["agent"].permission_answers[0]["result"]["outcome"] == {"outcome": "cancelled"}
    assert events[-1]["stop_reason"] == "cancelled"


async def test_second_turn_while_busy_is_refused_and_unknown_runtime_rejected(control_db, fake_desktop):
    agent = FakeAcpAgent()
    agent.hold_prompt = asyncio.Event()
    fake_desktop["agent"] = agent.start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "one")
    with pytest.raises(sessions.SessionBusy):
        manager.start_turn("alice", "grok", "two")
    with pytest.raises(ValueError):
        manager.start_turn("alice", "bash", "x")
    agent.hold_prompt.set()
    await _finish(manager, "alice")


async def test_not_signed_in_agent_tells_the_owner_how_to_sign_in(control_db, fake_desktop):
    fake_desktop["agent"] = FakeAcpAgent(auth_required=True).start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "x")
    events = await _finish(manager, "alice")
    assert "grok login" in events[-1]["error"]
    assert events[-1]["error_code"] == "auth_required"


async def test_agent_crash_mid_turn_ends_the_turn_and_reconnects_next_time(control_db, fake_desktop):
    agent = FakeAcpAgent()
    agent.hold_prompt = asyncio.Event()
    fake_desktop["agent"] = agent.start()
    manager = sessions.AgentDesktopManager()
    manager.start_turn("alice", "grok", "x")
    await asyncio.sleep(0.05)
    await agent.hang_up()
    events = await _finish(manager, "alice")
    assert events[-1]["error"]

    fake_desktop["agent"] = FakeAcpAgent(reply="again").start()
    manager.start_turn("alice", "grok", "y")
    for _ in range(300):
        ends = [e for e in manager.events_since("alice", 0) if e["type"] == "turn_end"]
        if len(ends) == 2:
            break
        await asyncio.sleep(0.01)
    assert ends[-1]["stop_reason"] == "end_turn"


# ── desktop containers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["../etc", "Alice", "a" * 40, "", "-a", "a_b"])
def test_desktop_key_must_be_a_plain_slug(key):
    with pytest.raises(ValueError):
        desktop.container_name(key)


def test_desktop_tokens_need_a_secret_and_differ_per_desktop(monkeypatch):
    monkeypatch.delenv("AGENT_DESKTOP_TOKEN_SECRET", raising=False)
    with pytest.raises(desktop.DesktopUnavailable):
        desktop.desktop_token("alice")
    monkeypatch.setenv("AGENT_DESKTOP_TOKEN_SECRET", "s3cret")
    assert desktop.desktop_token("alice") != desktop.desktop_token("bob")


@pytest.mark.parametrize("runtime,allow,runtimes,ok", [
    ("runsc", "false", {"runc": {}, "runsc": {}}, True),
    ("runsc", "false", {"runc": {}}, False),
    ("runc", "false", {"runc": {}}, False),
    ("runc", "true", {"runc": {}}, True),
    ("", "true", {"runc": {}}, False),
])
def test_desktops_need_gvisor_unless_runc_is_opted_into(monkeypatch, runtime, allow, runtimes, ok):
    monkeypatch.setenv("AGENT_DESKTOP_RUNTIME", runtime)
    monkeypatch.setenv("AGENT_DESKTOP_ALLOW_RUNC", allow)
    client = MagicMock()
    client.info.return_value = {"Runtimes": runtimes}
    if ok:
        assert desktop.check_runtime(client) == runtime
    else:
        with pytest.raises(desktop.DesktopUnavailable):
            desktop.check_runtime(client)


def _docker(monkeypatch, tmp_path, *, existing=None, backend_networks=None):
    """A fake Docker client: the dev-runner on p_dev-net + p_dev-egress-net, this
    backend (looked up by hostname) and optionally an existing desktop."""
    monkeypatch.delenv("AGENT_DESKTOP_NETWORK_SOURCE", raising=False)
    monkeypatch.delenv("AGENT_LLM_BACKEND_IP", raising=False)
    monkeypatch.setenv("DEV_RUNNER_CONTAINER_NAME", "proj-dev-runner")
    monkeypatch.setenv("AGENT_DESKTOP_HOST_ROOT", "/srv/desktops")
    monkeypatch.setenv("AGENT_DESKTOP_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_DESKTOP_TOKEN_SECRET", "s3cret")
    monkeypatch.setenv("AGENT_DESKTOP_RUNTIME", "runsc")
    monkeypatch.setenv("LLM_MODEL", "qwen-local")
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(desktop.socket, "gethostname", lambda: "backend-ctr")
    if backend_networks is None:
        backend_networks = {"p_dev-net": {"IPAddress": "172.23.0.4"}, "p_web": {"IPAddress": "172.20.0.2"}}
    containers = {
        "proj-dev-runner": MagicMock(attrs={"NetworkSettings": {"Networks": {"p_dev-net": {}, "p_dev-egress-net": {}}}}),
        "backend-ctr": MagicMock(attrs={"NetworkSettings": {"Networks": backend_networks}}),
    }
    if existing is not None:
        containers["hermes-agent-desktop-alice"] = existing
    client = MagicMock()
    client.info.return_value = {"Runtimes": {"runsc": {}}}
    client.containers.list.return_value = []

    def get(name):
        if name in containers:
            return containers[name]
        raise LookupError(name)

    client.containers.get.side_effect = get
    monkeypatch.setattr(desktop, "_docker_client", lambda: client)
    return client


def test_new_desktop_is_locked_down_and_mounts_only_its_own_home(monkeypatch, tmp_path):
    client = _docker(monkeypatch, tmp_path)
    order = MagicMock()
    order.attach_mock(client.networks.get.return_value.connect, "connect")
    order.attach_mock(client.containers.create.return_value.start, "start")

    assert desktop._blocking_ensure("alice") == "hermes-agent-desktop-alice"
    # gVisor only sees networks attached before start.
    assert [c[0] for c in order.mock_calls] == ["connect", "start"]
    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["runtime"] == "runsc"
    assert kwargs["read_only"] is True and kwargs["cap_drop"] == ["ALL"]
    assert kwargs["volumes"] == {
        "/srv/desktops/alice/home": {"bind": "/home/agent", "mode": "rw"},
        "/srv/desktops/resolv.conf": {"bind": "/etc/resolv.conf", "mode": "ro"},
    }
    # gVisor ignores Docker's embedded DNS: public resolvers, written per deployment.
    assert (tmp_path / "resolv.conf").read_text().startswith("nameserver 1.1.1.1\n")
    assert kwargs["environment"]["AGENT_DESKTOP_TOKEN"] == desktop.desktop_token("alice")
    assert kwargs["network"] == "p_dev-net"
    client.networks.get.assert_called_once_with("p_dev-egress-net")
    # The backend's address on the network it shares with the desktop, not another one.
    assert kwargs["extra_hosts"] == {"hermes-backend": "172.23.0.4"}


def test_new_desktop_gets_the_local_model_provider_with_its_own_token(monkeypatch, tmp_path):
    client = _docker(monkeypatch, tmp_path)
    desktop._blocking_ensure("alice")
    env = client.containers.create.call_args.kwargs["environment"]
    assert env["OPENCODE_CONFIG"] == "/home/agent/.config/hermes/opencode.json"
    config = json.loads((tmp_path / "alice" / "home" / ".config" / "hermes" / "opencode.json").read_text())
    provider = config["provider"]["hermes"]
    assert provider["options"] == {"baseURL": "http://hermes-backend:8000/agent-llm/v1",
                                   "apiKey": desktop.llm_token("alice")}
    assert config["model"] == "hermes/qwen-local" and "qwen-local" in provider["models"]
    # The token is not in the container's environment, only in the agent's own file.
    assert desktop.llm_token("alice") not in json.dumps(env)


def test_without_a_shared_network_the_desktop_gets_no_local_model(monkeypatch, tmp_path):
    client = _docker(monkeypatch, tmp_path, backend_networks={"p_web": {"IPAddress": "172.20.0.2"}})
    desktop._blocking_ensure("alice")
    assert client.containers.create.call_args.kwargs["extra_hosts"] == {}
    config = json.loads((tmp_path / "alice" / "home" / ".config" / "hermes" / "opencode.json").read_text())
    assert "provider" not in config


def test_running_desktop_is_recreated_when_the_backend_address_changed(monkeypatch, tmp_path):
    stale = MagicMock(status="running", attrs={"HostConfig": {"ExtraHosts": ["hermes-backend:172.23.0.9"]}})
    client = _docker(monkeypatch, tmp_path, existing=stale)
    desktop._blocking_ensure("alice")
    stale.remove.assert_called_once_with(force=True)
    assert client.containers.create.call_args.kwargs["extra_hosts"] == {"hermes-backend": "172.23.0.4"}


def test_running_desktop_with_current_address_is_reused(monkeypatch, tmp_path):
    current = MagicMock(status="running", attrs={"HostConfig": {"ExtraHosts": ["hermes-backend:172.23.0.4"]}})
    client = _docker(monkeypatch, tmp_path, existing=current)
    desktop._blocking_ensure("alice")
    current.remove.assert_not_called()
    client.containers.create.assert_not_called()


def test_stopped_desktop_is_recreated_not_restarted(monkeypatch, tmp_path):
    stopped = MagicMock(status="exited", attrs={"HostConfig": {"ExtraHosts": []}})
    client = _docker(monkeypatch, tmp_path, existing=stopped)
    desktop._blocking_ensure("alice")
    stopped.start.assert_not_called()
    stopped.remove.assert_called_once_with(force=True)
    client.containers.create.assert_called_once()


def test_concurrency_cap_refuses_another_desktop(monkeypatch, tmp_path):
    client = _docker(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_DESKTOP_MAX_CONCURRENT", "1")
    other = MagicMock()
    other.name = "hermes-agent-desktop-bob"
    client.containers.list.return_value = [other]
    with pytest.raises(desktop.DesktopUnavailable):
        desktop._blocking_ensure("alice")
    client.containers.create.assert_not_called()


def test_audit_record_keeps_the_title_but_not_secrets(control_db):
    task = control_plane.create_tool_task(
        "agent_desktop_execute",
        {"agent": "alice", "title": "curl -H 'Authorization: Bearer abc.def'", "tool_call_id": "c1"},
        "agent-desktop:alice",
    )
    assert "abc.def" not in json.dumps(task["tool_arguments"])


# ── dashboard API ───────────────────────────────────────────────────────────

@pytest.fixture()
def api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend import auth
    from backend.agent_runtimes import api

    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(auth, "active_sessions", {"owner-token", "viewer-token"} | set())
    monkeypatch.setattr(auth, "session_roles", {"viewer-token": "manager"})
    return TestClient(app)


def test_api_refuses_a_role_without_the_desktop_permission(api_client):
    response = api_client.get("/api/agent-desktops", headers={"Authorization": "Bearer viewer-token"})
    assert response.status_code == 403


def test_api_reports_disabled_and_rejects_bad_keys(api_client, monkeypatch):
    monkeypatch.setenv("AGENT_DESKTOPS_ENABLED", "false")
    owner = {"Authorization": "Bearer owner-token"}
    listing = api_client.get("/api/agent-desktops", headers=owner).json()
    assert listing["enabled"] is False and {r["id"] for r in listing["runtimes"]} == {"grok", "opencode"}
    assert api_client.post("/api/agent-desktops/alice/start", headers=owner).status_code == 503
    assert api_client.post("/api/agent-desktops/Bad_Key/start", headers=owner).status_code == 422


@pytest.mark.parametrize("protocols", [[], ["hermes-vnc", "hermes-auth.forged"], ["hermes-vnc", "hermes-auth.viewer-token"]])
def test_vnc_needs_a_live_session_with_the_permission(api_client, monkeypatch, protocols):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("AGENT_DESKTOPS_ENABLED", "true")
    with pytest.raises(WebSocketDisconnect) as exc:
        with api_client.websocket_connect("/api/agent-desktops/alice/vnc", subprotocols=protocols) as ws:
            ws.receive_bytes()
    assert exc.value.code == 1008


def test_new_home_makes_opencode_ask_but_keeps_an_owner_edited_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DESKTOP_ROOT", str(tmp_path))
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
    desktop._prepare_home("alice")
    config = tmp_path / "alice" / "home" / ".config" / "opencode" / "opencode.json"
    assert json.loads(config.read_text())["permission"] == {"edit": "ask", "bash": "ask", "webfetch": "ask"}
    config.write_text('{"permission": {"bash": "allow"}}')
    desktop._prepare_home("alice")
    assert json.loads(config.read_text()) == {"permission": {"bash": "allow"}}
