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


# ── Verification gate before push (Phase 3) ───────────────────────────────────

class _GovernedWithTests:
    """Governed-tool mock where dev_run_tests results are scripted."""

    def __init__(self, test_results):
        self.test_results = list(test_results)
        self.calls = []

    def __call__(self, tool_name, arguments, chat_id="default", **kwargs):
        self.calls.append(tool_name)
        if tool_name == "dev_run_tests":
            result = self.test_results.pop(0) if self.test_results else {"exit_code": 0}
            return json.dumps(result)
        return json.dumps({"status": "ok", "tool": tool_name})


_FAILING = {"exit_code": 1, "stdout": "", "stderr": "2 failed, 1 passed", "timed_out": False}
_PASSING = {"exit_code": 0, "stdout": "3 passed", "stderr": "", "timed_out": False}


@pytest.mark.asyncio
async def test_push_is_allowed_only_after_tests_pass(runs_db, scripted_llm, template_plan, monkeypatch):
    governed = _GovernedWithTests([_PASSING])
    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [
        _llm_tool("git_push", {}),
        _llm_text("DONE: pushed"),
    ]
    run = dev_runs.create_run("push my change")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    # Verification ran BEFORE the push reached the governed executor.
    assert governed.calls == ["dev_run_tests", "git_push"]
    verify_steps = [s for s in dev_runs.get_steps(run["id"]) if s["phase"] == "verify"]
    assert verify_steps and verify_steps[0]["status"] == "done"


@pytest.mark.asyncio
async def test_push_after_failed_tests_is_blocked_and_feeds_context(runs_db, scripted_llm, template_plan, monkeypatch):
    governed = _GovernedWithTests([_FAILING])
    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [
        _llm_tool("git_push", {}),
        _llm_text("DONE: will fix tests first"),
    ]
    run = dev_runs.create_run("push my change")
    result = await dev_runs.process_run(run["id"])
    assert "git_push" not in governed.calls  # push blocked
    steps = dev_runs.get_steps(run["id"])
    failed_verify = [s for s in steps if s["phase"] == "verify" and s["status"] == "failed"]
    assert failed_verify and "2 failed" in failed_verify[0]["summary"]
    assert result["status"] == "done"  # run itself continues (model chose to stop)


@pytest.mark.asyncio
async def test_three_failures_escalate_to_r3_then_approve_unblocks_push(
    runs_db, scripted_llm, template_plan, monkeypatch
):
    governed = _GovernedWithTests([_FAILING, _FAILING, _FAILING])
    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [_llm_tool("git_push", {})] * 3
    run = dev_runs.create_run("push my change")

    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "awaiting_approval"
    assert "override task" in result["status_reason"]
    assert governed.calls.count("dev_run_tests") == 3
    assert "git_push" not in governed.calls

    # A durable R3 review task exists in the Control Plane.
    pending = [t for t in control_plane.list_tasks(status="awaiting_approval")
               if t["risk_class"] == "R3" and run["id"] in (t.get("requester") or "")]
    assert len(pending) == 1
    override_task = pending[0]
    assert "owner override" in override_task["goal"]

    # Owner approves the override → resumed run pushes without re-running tests.
    control_plane.approve_task(override_task["id"])
    dev_runs.resume_run(run["id"])
    scripted_llm["script"] = [_llm_tool("git_push", {}), _llm_text("DONE: pushed with override")]
    final = await dev_runs.process_run(run["id"])
    assert final["status"] == "done"
    assert governed.calls[-1] == "git_push"
    override_steps = [s for s in dev_runs.get_steps(run["id"])
                      if s["phase"] == "verify" and "override" in s["summary"].lower()]
    assert override_steps


@pytest.mark.asyncio
async def test_verification_verdicts_are_remembered_in_project_memory(
    runs_db, scripted_llm, template_plan, monkeypatch
):
    governed = _GovernedWithTests([_PASSING])
    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [_llm_tool("git_push", {}), _llm_text("DONE: pushed")]
    run = dev_runs.create_run("push my change")
    await dev_runs.process_run(run["id"])
    entries = autonomy.recent_project_entries(limit=10)
    verdicts = [e for e in entries if e["kind"] == "verification" and run["id"] in e["title"]]
    assert verdicts and "passed" in verdicts[0]["title"]


# ── Run events and Telegram commands (Phase 4) ────────────────────────────────

class _EventCollector:
    def __init__(self, monkeypatch):
        from backend import websocket_manager
        self.ws_events = []
        self.telegram_messages = []

        async def fake_broadcast(payload):
            if payload.get("type") == "dev_run_event":
                self.ws_events.append(payload)

        monkeypatch.setattr(websocket_manager.manager, "broadcast", fake_broadcast)

        import backend.bot as bot

        collector = self

        class _FakeBot:
            async def send_message(self, chat_id, text, **kwargs):
                collector.telegram_messages.append((chat_id, text))

        class _FakeApp:
            bot = _FakeBot()

        monkeypatch.setattr(bot, "telegram_app", _FakeApp())
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


@pytest.fixture()
def event_collector(monkeypatch):
    return _EventCollector(monkeypatch)


@pytest.mark.asyncio
async def test_awaiting_approval_event_reaches_websocket_and_mock_bot(
    runs_db, scripted_llm, template_plan, event_collector, monkeypatch
):
    def fake_governed(tool_name, arguments, chat_id="default", **kwargs):
        return json.dumps({"status": "awaiting_approval", "task_id": "T-777", "risk_class": "R2"})

    monkeypatch.setattr(dev_runs, "execute_governed_tool", fake_governed)
    scripted_llm["script"] = [_llm_tool("dev_write_file", {"path": "a.py", "content": "1"})]
    run = dev_runs.create_run("goal")
    await dev_runs.process_run(run["id"])

    approval_events = [e for e in event_collector.ws_events if e["event"] == "awaiting_approval"]
    assert approval_events and approval_events[0]["run_id"] == run["id"]
    assert approval_events[0]["status"] == "awaiting_approval"
    assert any("T-777" in text for _, text in event_collector.telegram_messages)
    assert all(int(chat) == 42 for chat, _ in event_collector.telegram_messages)


@pytest.mark.asyncio
async def test_events_carry_only_capped_summaries(runs_db, scripted_llm, template_plan, event_collector, governed_ok):
    scripted_llm["script"] = [_llm_text("DONE: " + "x" * 1000)]
    run = dev_runs.create_run("goal")
    await dev_runs.process_run(run["id"])
    assert event_collector.ws_events  # started + done
    for event in event_collector.ws_events:
        assert len(event["summary"]) <= 200
        assert set(event) == {"type", "run_id", "status", "event", "summary"}
    done_events = [e for e in event_collector.ws_events if e["event"] == "done"]
    assert done_events and done_events[0]["run_id"] == run["id"]


@pytest.mark.asyncio
async def test_budget_80_event_fires_once_on_crossing(runs_db, scripted_llm, template_plan, event_collector, governed_ok):
    scripted_llm["script"] = [_llm_tool("dev_exec", {"argv": ["ls"]}) for _ in range(10)]
    run = dev_runs.create_run("goal", iter_budget=5)
    await dev_runs.process_run(run["id"])
    budget_events = [e for e in event_collector.ws_events if e["event"] == "budget_80"]
    assert len(budget_events) == 1
    assert "80%" in budget_events[0]["summary"]


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text):
        self.message = _FakeMessage(text)
        self.effective_chat = None
        self.effective_user = None


@pytest.mark.asyncio
async def test_telegram_pause_and_cancel_text_commands(runs_db):
    from backend import bot

    run = dev_runs.create_run("goal")
    dev_runs.update_run(run["id"], status="running")

    update = _FakeUpdate(f"пауза {run['id']}")
    handled = await bot._try_dev_run_command(update, update.message.text)
    assert handled is True
    assert dev_runs.get_run(run["id"])["status"] == "paused"
    assert any("paused" in reply for reply in update.message.replies)

    update2 = _FakeUpdate(f"отмена {run['id']}")
    assert await bot._try_dev_run_command(update2, update2.message.text) is True
    assert dev_runs.get_run(run["id"])["status"] == "cancelled"

    unknown = _FakeUpdate("пауза run-aaaaaaaaaaaa")
    assert await bot._try_dev_run_command(unknown, unknown.message.text) is True
    assert any("не найден" in reply for reply in unknown.message.replies)

    ordinary = _FakeUpdate("привет, как дела?")
    assert await bot._try_dev_run_command(ordinary, ordinary.message.text) is False
    assert ordinary.message.replies == []


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
