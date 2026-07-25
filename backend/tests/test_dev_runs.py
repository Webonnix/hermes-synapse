import json

import pytest

from backend import autonomy, control_plane, database, dev_runs
from backend import llm_client
from backend.llm_client import LLMUsage, NormalizedLLMResponse


@pytest.fixture()
def runs_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dev-runs.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    monkeypatch.setattr(dev_runs, "DB_PATH", db_path)
    monkeypatch.setattr(autonomy, "DB_PATH", db_path)
    database.init_db()
    return db_path


def _llm_text(content):
    return NormalizedLLMResponse(status="success", provider="mock", model="mock-model",
                                 content=content, usage=LLMUsage(cost=0.0))


def _llm_tool(name, arguments, cost=0.0):
    return NormalizedLLMResponse(
        status="tool_call", provider="mock", model="mock-model",
        tool_calls=[{"function": {"name": name, "arguments": json.dumps(arguments)}}],
        usage=LLMUsage(cost=cost),
    )


@pytest.fixture()
def scripted_llm(monkeypatch):
    """Feeds a scripted sequence of NormalizedLLMResponse objects to the worker."""
    state = {"script": [], "calls": 0}

    async def fake_call(**kwargs):
        state["calls"] += 1
        if not state["script"]:
            return _llm_text("DONE: nothing left in the script")
        return state["script"].pop(0)

    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)
    return state


@pytest.fixture()
def template_plan(monkeypatch):
    """Skips LLM planning: dev-runs get the deterministic template plan."""
    async def fake_plan(goal, root=None):
        return autonomy.build_plan(goal, root=root)

    monkeypatch.setattr(autonomy, "build_plan_llm", fake_plan)


@pytest.fixture()
def governed_ok(monkeypatch):
    executed = []

    def fake_governed(tool_name, arguments, chat_id="default", **kwargs):
        executed.append((tool_name, arguments))
        return json.dumps({"status": "ok", "tool": tool_name})

    monkeypatch.setattr(dev_runs, "execute_governed_tool", fake_governed)
    return executed


# ── CRUD and lifecycle ────────────────────────────────────────────────────────

def test_create_run_defaults(runs_db):
    run = dev_runs.create_run("Add a hello endpoint")
    assert run["status"] == "planned"
    assert run["iter_budget"] == 200
    assert run["iter_used"] == 0
    assert run["trace_id"].startswith("trace-")
    assert dev_runs.get_run(run["id"], with_steps=True)["steps"] == []


def test_pause_resume_cancel_transitions(runs_db):
    run = dev_runs.create_run("goal")
    with pytest.raises(ValueError):
        dev_runs.resume_run(run["id"])  # cannot resume a planned run
    paused = dev_runs.pause_run(run["id"], "manual")
    assert paused["status"] == "paused"
    resumed = dev_runs.resume_run(run["id"])
    assert resumed["status"] == "running"
    cancelled = dev_runs.cancel_run(run["id"])
    assert cancelled["status"] == "cancelled"
    # Cancelling again is a no-op, not an error.
    assert dev_runs.cancel_run(run["id"])["status"] == "cancelled"


def test_update_run_rejects_unknown_fields(runs_db):
    run = dev_runs.create_run("goal")
    with pytest.raises(ValueError):
        dev_runs.update_run(run["id"], goal="rewrite history")
    with pytest.raises(ValueError):
        dev_runs.update_run(run["id"], status="not-a-status")


# ── Worker loop on mock LLM ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_three_step_cycle_reaches_done(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [
        _llm_tool("dev_write_file", {"path": "app.py", "content": "print('hi')"}),
        _llm_tool("dev_run_tests", {"runner": "auto"}),
        _llm_text("DONE: endpoint implemented and tests pass"),
    ]
    run = dev_runs.create_run("Add a hello endpoint")
    finished = await dev_runs.process_run(run["id"])
    assert finished["status"] == "done"
    assert finished["iter_used"] == 3
    steps = dev_runs.get_steps(run["id"])
    assert [s["phase"] for s in steps] == ["plan", "act", "act", "observe"]
    assert [t for t, _ in governed_ok] == ["dev_write_file", "dev_run_tests"]
    assert finished["plan_id"] and autonomy.get_plan(finished["plan_id"]) is not None


@pytest.mark.asyncio
async def test_kill_switch_stops_run_within_one_iteration(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [_llm_tool("dev_exec", {"argv": ["ls"]})] * 10
    run = dev_runs.create_run("goal")
    control_plane.set_kill_switch(True, "incident")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "paused"
    assert "Kill switch" in result["status_reason"]
    assert scripted_llm["calls"] == 0  # stopped before any model call
    control_plane.set_kill_switch(False, "over")


@pytest.mark.asyncio
async def test_iteration_budget_exhaustion_pauses(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [_llm_tool("dev_exec", {"argv": ["ls"]}) for _ in range(10)]
    run = dev_runs.create_run("goal", iter_budget=2)
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "paused"
    assert "Iteration budget" in result["status_reason"]
    assert result["iter_used"] == 2


@pytest.mark.asyncio
async def test_cost_budget_exhaustion_pauses(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [_llm_tool("dev_exec", {"argv": ["ls"]}, cost=1.0) for _ in range(10)]
    run = dev_runs.create_run("goal", cost_budget=1.5)
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "paused"
    assert "Cost budget" in result["status_reason"]
    assert result["cost_used"] >= 1.5


@pytest.mark.asyncio
async def test_restart_resumes_from_checkpoint_without_duplicate_steps(
    runs_db, scripted_llm, template_plan, governed_ok
):
    scripted_llm["script"] = [
        _llm_tool("dev_write_file", {"path": "a.py", "content": "1"}),
        _llm_tool("dev_write_file", {"path": "b.py", "content": "2"}),
    ]
    run = dev_runs.create_run("goal")
    # First worker performs planning + two act iterations, then "the process dies".
    interrupted = await dev_runs.process_run(run["id"], max_iterations=2)
    assert interrupted["status"] == "running"
    seqs_before = [s["seq"] for s in dev_runs.get_steps(run["id"])]
    assert seqs_before == [1, 2, 3]  # plan + 2 acts
    assert interrupted["checkpoint_step"] == "3"

    # A fresh worker picks the run up from the DB and finishes it.
    scripted_llm["script"] = [_llm_text("DONE: both files written")]
    finished = await dev_runs.process_run(run["id"])
    assert finished["status"] == "done"
    steps = dev_runs.get_steps(run["id"])
    seqs = [s["seq"] for s in steps]
    assert seqs == sorted(set(seqs)) == [1, 2, 3, 4]  # no duplicates, no re-planning
    assert [s["phase"] for s in steps] == ["plan", "act", "act", "observe"]
    assert len([t for t, _ in governed_ok]) == 2  # tools were not re-executed


@pytest.mark.asyncio
async def test_tool_awaiting_approval_moves_run_to_awaiting_approval(runs_db, scripted_llm, template_plan, monkeypatch):
    def fake_governed(tool_name, arguments, chat_id="default", **kwargs):
        return json.dumps({"status": "awaiting_approval", "task_id": "T-123", "risk_class": "R2"})

    monkeypatch.setattr(dev_runs, "execute_governed_tool", fake_governed)
    scripted_llm["script"] = [_llm_tool("dev_write_file", {"path": "a.py", "content": "1"})]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "awaiting_approval"
    assert "T-123" in result["status_reason"]
    # Owner approval unblocks the run via resume.
    assert dev_runs.resume_run(run["id"])["status"] == "running"


@pytest.mark.asyncio
async def test_tool_outside_allowlist_is_rejected(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [
        _llm_tool("execute_command", {"command": "rm -rf /"}),
        _llm_text("DONE: gave up on shell"),
    ]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    rejected = [s for s in dev_runs.get_steps(run["id"]) if s["status"] == "failed"]
    assert rejected and "not allowed" in rejected[0]["summary"]
    assert governed_ok == []  # the governed executor was never reached


@pytest.mark.asyncio
async def test_blocked_reply_fails_run(runs_db, scripted_llm, template_plan):
    scripted_llm["script"] = [_llm_text("BLOCKED: repository does not contain the described module")]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "failed"
    assert "BLOCKED" in result["status_reason"]


# ── LLM planner with schema validation and retry ──────────────────────────────

_VALID_PLAN_JSON = json.dumps({
    "tier": "verify",
    "capabilities": ["backend_validation"],
    "steps": [
        {"id": "discover", "title": "Map scope", "instructions": "Find the endpoint module.",
         "expected_output": ["file list"], "acceptance": ["Files verified to exist"]},
        {"id": "implement", "title": "Add endpoint", "instructions": "Write the handler.",
         "expected_output": ["diff"], "acceptance": ["Handler registered"]},
        {"id": "verify", "title": "Test", "instructions": "Run pytest.",
         "expected_output": ["test log"], "acceptance": ["All tests pass"]},
    ],
})


@pytest.mark.asyncio
async def test_build_plan_llm_retries_then_persists_valid_plan(runs_db, monkeypatch):
    responses = [_llm_text("{not json at all"), _llm_text(_VALID_PLAN_JSON)]

    async def fake_call(**kwargs):
        return responses.pop(0)

    monkeypatch.setattr(llm_client, "call_llm_normalized", fake_call)
    plan = await autonomy.build_plan_llm("Add a hello endpoint to the API")
    assert [s["id"] for s in plan["steps"]] == ["discover", "implement", "verify"]
    assert [s["agent"] for s in plan["steps"]] == ["scout", "implementer", "verifier"]
    assert plan["steps"][2]["max_attempts"] == 3
    assert "backend_validation" in plan["capabilities"]
    assert "repository_search" in plan["capabilities"]  # baseline always present
    stored = autonomy.get_plan(plan["id"])
    assert stored and stored["steps"][0]["acceptance"] == ["Files verified to exist"]


@pytest.mark.asyncio
async def test_build_plan_llm_falls_back_to_template_after_retries(runs_db, monkeypatch):
    async def always_invalid(**kwargs):
        return _llm_text("no json here")

    monkeypatch.setattr(llm_client, "call_llm_normalized", always_invalid)
    plan = await autonomy.build_plan_llm("Any goal")
    # Deterministic template fallback keeps the same step contract.
    assert [s["id"] for s in plan["steps"]][:3] == ["discover", "implement", "verify"]
    assert autonomy.get_plan(plan["id"]) is not None
