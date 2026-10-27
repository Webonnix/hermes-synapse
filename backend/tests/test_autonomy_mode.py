import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from backend import autonomy_mode, control_plane, database
from backend.tool_permissions import OWNER, SUBAGENT


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "autonomy-mode.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    return db_path


def _fake_tools(result='{"ok": true}'):
    tools_module = ModuleType("backend.tools")
    tools_module.execute_tool = MagicMock(return_value=result)
    return tools_module


def test_default_mode_is_supervised_and_unknown_values_fall_back(control_db):
    assert autonomy_mode.get_mode() == "supervised"
    database.set_setting(autonomy_mode.SETTING_KEY, "yolo")
    assert autonomy_mode.get_mode() == "supervised"
    with pytest.raises(ValueError):
        autonomy_mode.set_mode("yolo")


@pytest.mark.parametrize(
    ("tool", "risk", "supervised", "auto", "full"),
    [
        ("web_search", "R1", 0, 0, 0),
        ("add_calendar_event", "R3", 1, 0, 0),
        ("create_subagent", "R3", 1, 0, 0),
        ("phone_call", "R3", 1, 1, 0),
        ("browser_task", "R3", 1, 1, 0),
        ("execute_command", "R4", 2, 2, 0),
        ("delete_todoist_task", "R4", 2, 2, 0),
        ("untrusted_plugin_action", "R4", 2, 2, 0),
        ("ctrader_place_order", "R4", 2, 2, 2),
        ("bcm_transfer", "R4", 2, 2, 2),
    ],
)
def test_approvals_per_mode(control_db, tool, risk, supervised, auto, full):
    assert autonomy_mode.approvals_required(tool, risk, "supervised") == supervised
    assert autonomy_mode.approvals_required(tool, risk, "auto") == auto
    assert autonomy_mode.approvals_required(tool, risk, "full") == full


def test_auto_mode_runs_r3_without_approval_and_leaves_evidence(control_db):
    autonomy_mode.set_mode("auto")
    tools_module = _fake_tools()
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        result = json.loads(control_plane.execute_governed_tool(
            "add_calendar_event", {"title": "Review", "date": "2026-07-17"}, "dashboard", principal=OWNER,
        ))
    assert result == {"ok": True}
    tools_module.execute_tool.assert_called_once()
    task = control_plane.list_tasks()[0]
    assert task["status"] == "done"
    assert task["autonomy_level"] == "L4"
    assert any(event["event_type"] == "auto_approved" for event in control_plane.list_events())


def test_auto_mode_still_queues_sensitive_actions(control_db):
    autonomy_mode.set_mode("auto")
    tools_module = _fake_tools()
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        result = json.loads(control_plane.execute_governed_tool(
            "execute_command", {"command": "ls"}, "dashboard", principal=OWNER,
        ))
    assert result["status"] == "awaiting_approval"
    assert result["confirmations"] == "0/2"
    tools_module.execute_tool.assert_not_called()


def test_full_mode_keeps_principal_policy_and_shell_deny_list(control_db):
    autonomy_mode.set_mode("full")
    tools_module = _fake_tools()
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        denied = json.loads(control_plane.execute_governed_tool(
            "execute_command", {"command": "ls"}, "agent", principal=SUBAGENT,
        ))
        refused = json.loads(control_plane.execute_governed_tool(
            "execute_command", {"command": "sudo reboot"}, "dashboard", principal=OWNER,
        ))
    assert denied["status"] == "forbidden"
    assert refused["status"] == "forbidden"
    tools_module.execute_tool.assert_not_called()


def test_full_mode_respects_the_kill_switch(control_db):
    autonomy_mode.set_mode("full")
    control_plane.set_kill_switch(True, "test stop")
    result = json.loads(control_plane.execute_governed_tool(
        "add_calendar_event", {"title": "x"}, "dashboard", principal=OWNER,
    ))
    assert result["status"] == "killed"


def test_loosening_the_mode_releases_an_identical_pending_request(control_db):
    args = {"title": "Review", "date": "2026-07-17"}
    pending = control_plane.create_tool_task("add_calendar_event", args, "dashboard")
    assert pending["status"] == "awaiting_approval"
    autonomy_mode.set_mode("auto")
    again = control_plane.create_tool_task("add_calendar_event", args, "dashboard")
    assert again["id"] == pending["id"]
    assert again["approvals_required"] == 0
    assert again["approval_required"] is False


def test_prompt_directive_only_outside_supervised(control_db):
    assert autonomy_mode.prompt_directive("supervised") == ""
    assert "автоном" in autonomy_mode.prompt_directive("auto")
    assert "полную автономию" in autonomy_mode.prompt_directive("full")


def test_summary_reports_the_mode(control_db):
    autonomy_mode.set_mode("full")
    assert control_plane.get_summary()["policy"]["autonomy_mode"] == "full"
