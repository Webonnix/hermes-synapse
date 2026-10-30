"""Decision history, suggested rules and the narrow waiver an owner rule grants."""
import pytest

from backend import autonomy_mode, control_plane, database

DESKTOP_RUN = {"agent": "vexa", "runtime": "opencode", "title": "npm test", "tool_call_id": "c1"}


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "control-plane.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    return db_path


def _approve(tool, arguments, n=1):
    for i in range(n):
        task = control_plane.create_tool_task(tool, {**arguments, "tool_call_id": f"c{i}"}, "chat")
        control_plane.approve_task(task["id"])


def test_features_keep_who_and_where_not_free_text():
    features = control_plane.decision_features(
        {"to": "a@b.ru", "url": "https://Example.com/x?y=1", "path": "/repo/src/a.py", "message": "secret text"})
    assert features == {"to": "a@b.ru", "domain": "example.com", "path_dir": "/repo/src"}


def test_decisions_record_features_and_context(control_db):
    _approve("agent_desktop_execute", DESKTOP_RUN)
    [decision] = control_plane.decision_history()
    assert decision["decision"] == "approved" and decision["tool"] == "agent_desktop_execute"
    assert decision["features"]["agent"] == "vexa" and decision["context_sources"] == ["unknown"]


def test_repeated_approvals_become_a_suggestion_not_a_rule(control_db):
    _approve("agent_desktop_execute", DESKTOP_RUN, n=control_plane.SUGGEST_AFTER_APPROVALS)
    suggestions = control_plane.policy_suggestions()
    assert {"tool_name": "agent_desktop_execute", "feature_key": "agent", "feature_value": "vexa"}.items() \
        <= suggestions[0].items()
    assert control_plane.list_approval_rules() == []


def test_one_rejection_withholds_the_suggestion(control_db):
    _approve("agent_desktop_execute", DESKTOP_RUN, n=control_plane.SUGGEST_AFTER_APPROVALS)
    task = control_plane.create_tool_task("agent_desktop_execute", {**DESKTOP_RUN, "tool_call_id": "x"}, "chat")
    control_plane.reject_task(task["id"])
    assert not [s for s in control_plane.policy_suggestions() if s["feature_key"] == "agent"]


def test_a_rule_waives_only_matching_r2_in_auto(control_db, monkeypatch):
    control_plane.create_approval_rule("agent_desktop_execute", "agent", "vexa")
    assert autonomy_mode.approvals_required("agent_desktop_execute", "R2", "auto", DESKTOP_RUN) == 0
    assert autonomy_mode.approvals_required("agent_desktop_execute", "R2", "supervised", DESKTOP_RUN) == 1
    assert autonomy_mode.approvals_required("agent_desktop_execute", "R2", "auto", {**DESKTOP_RUN, "agent": "x"}) == 1
    monkeypatch.setattr(autonomy_mode, "get_mode", lambda: "auto")
    task = control_plane.create_tool_task("agent_desktop_execute", DESKTOP_RUN, "chat")
    assert task["status"] == "queued"
    events = [e for e in control_plane.list_events() if e["task_id"] == task["id"]]
    assert any(e["event_type"] == "auto_approved" and e["metadata"].get("approval_rule") for e in events)


@pytest.mark.parametrize("tool", ["agent_desktop_delete", "execute_command", "ctrader_place_order"])
def test_rules_never_cover_r3_r4_or_money(control_db, tool):
    with pytest.raises(ValueError):
        control_plane.create_approval_rule(tool, "agent", "vexa")


def test_rules_need_an_exact_identifying_value(control_db):
    for key, value in (("amount", "10"), ("agent", "  ")):
        with pytest.raises(ValueError):
            control_plane.create_approval_rule("agent_desktop_execute", key, value)
