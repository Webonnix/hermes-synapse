import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from backend import control_plane, database
from backend.tool_permissions import OWNER, SUBAGENT


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "control-plane.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    return db_path


def test_risk_policy_defaults_unknown_tools_to_r4(control_db):
    assert control_plane.classify_tool_risk("get_system_stats") == "R0"
    assert control_plane.classify_tool_risk("add_calendar_event") == "R3"
    assert control_plane.classify_tool_risk("execute_command") == "R4"
    assert control_plane.classify_tool_risk("untrusted_plugin_action") == "R4"


def test_browser_read_is_zero_approval_browser_task_requires_one(control_db):
    # browser_read excludes click/input/submit actions server-side, matches
    # web_search's R1 (no approval). browser_task is the full interactive
    # agent, matches add_calendar_event/create_subagent's R3 (one approval).
    assert control_plane.classify_tool_risk("browser_read") == "R1"
    assert control_plane.classify_tool_risk("browser_task") == "R3"

    read_task = control_plane.create_tool_task("browser_read", {"task": "read a page"}, chat_id="test")
    assert read_task["approvals_required"] == 0
    assert read_task["status"] == "queued"

    interactive_task = control_plane.create_tool_task("browser_task", {"task": "fill a form"}, chat_id="test")
    assert interactive_task["approvals_required"] == 1
    assert interactive_task["status"] == "awaiting_approval"


def test_r3_action_is_queued_until_approved(control_db):
    tools_module = ModuleType("backend.tools")
    execute = MagicMock()
    tools_module.execute_tool = execute
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        result = json.loads(control_plane.execute_governed_tool(
            "add_calendar_event", {"title": "Review", "date": "2026-07-17"}, "dashboard"
        ))
        assert result["status"] == "awaiting_approval"
        assert result["risk_class"] == "R3"
        execute.assert_not_called()

        task = control_plane.approve_task(result["task_id"])
        assert task["status"] == "approved"
        execute.return_value = json.dumps({"status": "success"})
        completed = json.loads(control_plane.execute_governed_tool(
            "add_calendar_event", task["tool_arguments"], "dashboard", approved_task_id=task["id"]
        ))
        assert completed["status"] == "success"
        assert control_plane.get_task(task["id"])["status"] == "done"


def test_r4_requires_two_explicit_confirmations(control_db):
    # execute_command is owner-only (backend/tool_permissions.py); the R4
    # double-confirmation is what gates it *for the owner*.
    pending = json.loads(control_plane.execute_governed_tool(
        "execute_command", {"command": "uname -a"}, "dashboard", principal=OWNER
    ))
    first = control_plane.approve_task(pending["task_id"])
    assert first["status"] == "awaiting_approval"
    assert first["approval_count"] == 1
    second = control_plane.approve_task(pending["task_id"])
    assert second["status"] == "approved"
    assert second["approval_count"] == 2


def test_kill_switch_blocks_new_tool_execution(control_db):
    control_plane.set_kill_switch(True, "incident test")
    result = json.loads(control_plane.execute_governed_tool(
        "get_system_stats", {}, "dashboard", principal=OWNER
    ))
    assert result["status"] == "killed"
    assert "incident test" in result["reason"]

    state = control_plane.set_kill_switch(False, "test complete")
    assert state["kill_switch"] is False


def test_evidence_ledger_hashes_results_and_redacts_secret_fields(control_db):
    task = control_plane.create_tool_task(
        "get_weather", {"location": "Minsk", "api_key": "must-not-leak"}, "dashboard"
    )
    assert task["tool_arguments"]["api_key"] == "[REDACTED]"
    control_plane.start_task(task["id"])
    control_plane.finish_task(task["id"], '{"temperature": 20}')
    event = control_plane.list_events(limit=1)[0]
    assert event["evidence_id"].startswith("EV-")
    assert len(event["output_hash"]) == 64


def test_capability_review_task_is_durable_but_not_executable(control_db):
    task = control_plane.create_review_task(
        "Review visual validator",
        {"package": "playwright", "token": "must-not-leak"},
        risk_class="R3",
        acceptance=["Exact version and checksum verified"],
    )

    assert task["status"] == "awaiting_approval"
    assert task["tool_name"] is None
    assert task["tool_arguments"]["token"] == "[REDACTED]"
    assert task["acceptance"] == ["Exact version and checksum verified"]
    assert control_plane.approve_task(task["id"])["status"] == "approved"


def test_a_subagent_principal_cannot_reach_the_host_even_via_the_control_plane(control_db):
    """Third of the three enforcement layers in tool_permissions.py: agent.py
    filters the schema and re-checks before dispatch, and this refuses even a
    caller that skipped both."""
    tools_module = ModuleType("backend.tools")
    execute = MagicMock()
    tools_module.execute_tool = execute
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        for tool in ("execute_command", "dev_exec", "git_push", "get_system_stats"):
            result = json.loads(control_plane.execute_governed_tool(tool, {}, "bot", principal=SUBAGENT))
            assert result["status"] == "forbidden"
        execute.assert_not_called()
        # No Control Plane task is opened either — a blocked call leaves no
        # approval for the owner to accidentally grant later.
        assert control_plane.list_tasks(limit=50) == []


def test_an_approved_task_cannot_be_replayed_by_a_lesser_principal(control_db):
    tools_module = ModuleType("backend.tools")
    execute = MagicMock(return_value=json.dumps({"status": "success"}))
    tools_module.execute_tool = execute
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        pending = json.loads(control_plane.execute_governed_tool(
            "execute_command", {"command": "uname -a"}, "dashboard", principal=OWNER
        ))
        control_plane.approve_task(pending["task_id"])
        task = control_plane.approve_task(pending["task_id"])
        assert task["status"] == "approved"

        replayed = json.loads(control_plane.execute_governed_tool(
            "execute_command", task["tool_arguments"], "bot",
            approved_task_id=task["id"], principal=SUBAGENT,
        ))
        assert replayed["status"] == "forbidden"
        execute.assert_not_called()


def test_the_owners_shell_still_refuses_host_wrecking_commands(control_db):
    tools_module = ModuleType("backend.tools")
    execute = MagicMock()
    tools_module.execute_tool = execute
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        result = json.loads(control_plane.execute_governed_tool(
            "execute_command", {"command": "rm -rf /"}, "dashboard", principal=OWNER
        ))
        assert result["status"] == "forbidden"
        assert "Refused" in result["error"]
        execute.assert_not_called()
