import asyncio
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
    assert run["iter_budget"] == 0  # 0 = unlimited; only an explicit cap pauses a run
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
async def test_default_run_has_no_iteration_cap(runs_db, scripted_llm, template_plan, governed_ok):
    # Distinct arguments each time: this asserts the absence of an iteration
    # cap, not the duplicate-action detector (which has its own tests below).
    scripted_llm["script"] = [_llm_tool("dev_exec", {"argv": ["ls", f"dir{i}"]}) for i in range(10)] + [
        _llm_text("DONE: finished after more than the old 200-iteration default would allow")
    ]
    run = dev_runs.create_run("goal")  # no iter_budget given → 0 (unlimited)
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert result["iter_used"] == 11
    assert result["iter_budget"] == 0


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
    assert seqs == sorted(set(seqs)) == [1, 2, 3, 4, 5]  # no duplicates, no re-planning
    # The two writes were never tested, so 'DONE:' triggers the completion
    # verification gate before the run is allowed to close.
    assert [s["phase"] for s in steps] == ["plan", "act", "act", "verify", "observe"]
    assert [t for t, _ in governed_ok] == ["dev_write_file", "dev_write_file", "dev_run_tests"]


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


# ── Observation replay: the executor must see real tool output ────────────────

@pytest.mark.asyncio
async def test_full_tool_output_reaches_the_next_prompt(runs_db, scripted_llm, template_plan, monkeypatch):
    """A read has to deliver the file, not a 400-char preview of it."""
    file_body = "def handler():\n" + "    # meaningful line\n" * 300

    def fake_governed(tool_name, arguments, chat_id="default", **kwargs):
        return json.dumps({"path": "app.py", "content": file_body})

    monkeypatch.setattr(dev_runs, "execute_governed_tool", fake_governed)
    scripted_llm["script"] = [_llm_tool("dev_read_file", {"path": "app.py"})]
    run = dev_runs.create_run("understand app.py")
    await dev_runs.process_run(run["id"], max_iterations=1)

    step = [s for s in dev_runs.get_steps(run["id"]) if s["tool"] == "dev_read_file"][0]
    assert len(step["result"]) > 4000
    prompt = dev_runs._build_messages(dev_runs.get_run(run["id"]))[1]["content"]
    assert "# meaningful line" in prompt
    assert prompt.count("# meaningful line") > 200  # the whole body, not a preview
    assert "DATA" in prompt  # untrusted-output framing is present


@pytest.mark.asyncio
async def test_observation_block_is_budget_capped(runs_db, monkeypatch):
    monkeypatch.setattr(dev_runs, "OBSERVATION_BUDGET_CHARS", 500)
    monkeypatch.setattr(dev_runs, "OBSERVATION_STEPS", 3)
    run = dev_runs.create_run("goal", start=False)
    for index in range(6):
        dev_runs.add_step(run["id"], "act", "dev_read_file", f"read {index}",
                          result="x" * 400 + str(index))
    block = dev_runs._observation_block(dev_runs.get_steps(run["id"]))
    assert len(block) < 1500
    assert "step 6" in block and "step 1" not in block  # newest kept, oldest dropped


# ── Loop protection ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_identical_action_is_refused_with_corrective_feedback(
    runs_db, scripted_llm, template_plan, governed_ok
):
    scripted_llm["script"] = [_llm_tool("dev_read_file", {"path": "a.py"})] * 4 + [
        _llm_text("DONE: gave up repeating myself")
    ]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    # DUPLICATE_ACTION_LIMIT identical executions are allowed; the next is refused.
    assert [t for t, _ in governed_ok] == ["dev_read_file"] * dev_runs.DUPLICATE_ACTION_LIMIT
    blocked = [s for s in dev_runs.get_steps(run["id"]) if s["status"] == "blocked"]
    assert len(blocked) == 1
    assert "Change the approach" in blocked[0]["summary"]


@pytest.mark.asyncio
async def test_relentless_duplicate_action_fails_the_run(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [_llm_tool("dev_read_file", {"path": "a.py"})] * 12
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "failed"
    assert "Loop detected" in result["status_reason"]
    # Never executed again after the limit, and the run stops well short of 12.
    assert len([t for t, _ in governed_ok]) == dev_runs.DUPLICATE_ACTION_LIMIT
    assert result["iter_used"] <= dev_runs.DUPLICATE_ACTION_HARD_LIMIT + 1


@pytest.mark.asyncio
async def test_consecutive_failures_stop_the_run(runs_db, scripted_llm, template_plan, monkeypatch):
    def always_fails(tool_name, arguments, chat_id="default", **kwargs):
        return json.dumps({"error": "dev-runner is unreachable"})

    monkeypatch.setattr(dev_runs, "execute_governed_tool", always_fails)
    scripted_llm["script"] = [_llm_tool("dev_read_file", {"path": f"{i}.py"}) for i in range(20)]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "failed"
    assert "consecutive failing steps" in result["status_reason"]
    assert result["iter_used"] <= dev_runs.MAX_CONSECUTIVE_FAILURES + 1


@pytest.mark.asyncio
async def test_hard_iteration_ceiling_pauses_even_without_a_budget(
    runs_db, scripted_llm, template_plan, governed_ok, monkeypatch
):
    monkeypatch.setattr(dev_runs, "HARD_ITERATION_CAP", 4)
    scripted_llm["script"] = [_llm_tool("dev_read_file", {"path": f"{i}.py"}) for i in range(20)]
    run = dev_runs.create_run("goal")  # iter_budget = 0 (unlimited)
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "paused"
    assert "Hard iteration ceiling" in result["status_reason"]
    assert result["iter_used"] == 4


@pytest.mark.asyncio
async def test_all_tool_calls_of_one_response_are_executed(runs_db, scripted_llm, template_plan, governed_ok):
    multi = NormalizedLLMResponse(
        status="tool_call", provider="mock", model="mock-model",
        tool_calls=[
            {"function": {"name": "dev_read_file", "arguments": json.dumps({"path": "a.py"})}},
            {"function": {"name": "dev_read_file", "arguments": json.dumps({"path": "b.py"})}},
        ],
        usage=LLMUsage(),
    )
    scripted_llm["script"] = [multi, _llm_text("DONE: read both")]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert [args["path"] for _, args in governed_ok] == ["a.py", "b.py"]


# ── Completion verification ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_is_rejected_when_the_changes_do_not_pass_tests(
    runs_db, scripted_llm, template_plan, monkeypatch
):
    governed = _GovernedWithTests([_FAILING, _PASSING])
    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [
        _llm_tool("dev_write_file", {"path": "a.py", "content": "boom"}),
        _llm_text("DONE: shipped it"),          # rejected — tests fail
        _llm_tool("dev_patch", {"path": "a.py", "unified_diff": "--- fix"}),
        _llm_text("DONE: fixed and verified"),  # accepted — tests pass
    ]
    run = dev_runs.create_run("write a module")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert governed.calls == ["dev_write_file", "dev_run_tests", "dev_patch", "dev_run_tests"]
    rejections = [s for s in dev_runs.get_steps(run["id"])
                  if "Completion rejected" in s["summary"]]
    assert len(rejections) == 1


@pytest.mark.asyncio
async def test_done_needs_no_extra_test_run_when_the_model_already_ran_them(
    runs_db, scripted_llm, template_plan, monkeypatch
):
    governed = _GovernedWithTests([_PASSING])
    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [
        _llm_tool("dev_write_file", {"path": "a.py", "content": "ok"}),
        _llm_tool("dev_run_tests", {"runner": "auto"}),
        _llm_text("DONE: written and tested"),
    ]
    run = dev_runs.create_run("write a module")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert governed.calls == ["dev_write_file", "dev_run_tests"]  # not run twice


@pytest.mark.asyncio
async def test_done_without_changes_closes_immediately(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [
        _llm_tool("dev_read_file", {"path": "a.py"}),
        _llm_text("DONE: nothing needed changing"),
    ]
    run = dev_runs.create_run("investigate")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert [t for t, _ in governed_ok] == ["dev_read_file"]


# ── Cost accounting and transient-failure recovery ────────────────────────────

@pytest.mark.asyncio
async def test_cost_is_derived_from_tokens_when_the_provider_omits_it(
    runs_db, scripted_llm, template_plan, governed_ok, monkeypatch
):
    """usage.cost is never populated by llm_client, so the budget has to be fed
    from token counts — otherwise cost_budget can never trip."""
    monkeypatch.setattr(dev_runs, "_llm_config", lambda: {
        "api_base": "https://api.openai.com/v1", "api_key": "k",
        "model": "gpt-4o", "provider_options": {"provider": "openai"},
    })
    priced = NormalizedLLMResponse(
        status="tool_call", provider="openai", model="gpt-4o",
        tool_calls=[{"function": {"name": "dev_read_file", "arguments": '{"path": "a.py"}'}}],
        usage=LLMUsage(input_tokens=200_000, output_tokens=100_000),
    )
    scripted_llm["script"] = [priced] * 5
    run = dev_runs.create_run("goal", cost_budget=2.0)
    result = await dev_runs.process_run(run["id"])
    assert result["cost_used"] > 0
    assert result["status"] == "paused"
    assert "Cost budget" in result["status_reason"]


@pytest.mark.asyncio
async def test_transient_provider_error_is_retried_before_pausing(
    runs_db, template_plan, governed_ok, monkeypatch
):
    monkeypatch.setattr(dev_runs, "LLM_RETRY_BASE_DELAY", 0.0)
    attempts = {"n": 0}

    async def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return NormalizedLLMResponse(status="provider_error", provider="mock", model="m",
                                         error_message="502 upstream", usage=LLMUsage())
        return _llm_text("DONE: recovered without a human")

    monkeypatch.setattr(dev_runs, "call_llm_normalized", flaky)
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_persistent_provider_error_still_pauses(runs_db, template_plan, governed_ok, monkeypatch):
    monkeypatch.setattr(dev_runs, "LLM_RETRY_BASE_DELAY", 0.0)
    calls = {"n": 0}

    async def always_down(**kwargs):
        calls["n"] += 1
        return NormalizedLLMResponse(status="provider_error", provider="mock", model="m",
                                     error_message="502 upstream", usage=LLMUsage())

    monkeypatch.setattr(dev_runs, "call_llm_normalized", always_down)
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "paused"
    assert calls["n"] == dev_runs.LLM_RETRY_ATTEMPTS


# ── Approval resumes the run by itself ────────────────────────────────────────

@pytest.mark.asyncio
async def test_approving_a_dev_run_task_resumes_the_run(runs_db, scripted_llm, template_plan, monkeypatch):
    from backend import approval_dispatch

    queued = {"done": False}

    def governed(tool_name, arguments, chat_id="default", **kwargs):
        if tool_name == "dev_write_file" and not queued["done"]:
            queued["done"] = True
            return json.dumps({"status": "awaiting_approval", "task_id": "T-1", "risk_class": "R3"})
        return json.dumps({"status": "ok", "tool": tool_name})

    monkeypatch.setattr(dev_runs, "execute_governed_tool", governed)
    scripted_llm["script"] = [_llm_tool("dev_write_file", {"path": "a.py", "content": "1"})]
    run = dev_runs.create_run("goal")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "awaiting_approval"

    # The owner approves the queued Control Plane task; the dispatcher runs the
    # tool for real (patched here) and must hand the run back to the worker.
    monkeypatch.setattr(control_plane, "execute_governed_tool",
                        lambda *args, **kwargs: json.dumps({"status": "ok", "written": True}))
    task = {"id": "T-1", "status": "approved", "requester": f"dev-run:{run['id']}",
            "tool_name": "dev_write_file", "tool_arguments": {"path": "a.py", "content": "1"}}
    resumed = await approval_dispatch.execute_if_ready(task)
    assert resumed is not None
    run_after = dev_runs.get_run(run["id"])
    assert run_after["status"] == "running"
    executed_step = [s for s in dev_runs.get_steps(run["id"]) if "after owner approval" in s["summary"]]
    assert executed_step and executed_step[0]["result"]


def test_approval_hook_ignores_non_dev_run_requesters(runs_db):
    assert dev_runs.on_control_task_approved(
        {"id": "T-9", "status": "approved", "requester": "chat:default", "tool_name": "x"}
    ) is None


# ── Metrics ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_report_autonomous_completion(runs_db, scripted_llm, template_plan, governed_ok):
    scripted_llm["script"] = [_llm_text("DONE: trivial goal")]
    autonomous = dev_runs.create_run("easy")
    await dev_runs.process_run(autonomous["id"])

    scripted_llm["script"] = [_llm_text("BLOCKED: missing credentials")]
    blocked = dev_runs.create_run("hard")
    await dev_runs.process_run(blocked["id"])

    report = dev_runs.metrics()
    assert report["terminal_runs"] == 2
    assert report["task_success_rate"] == 0.5
    assert report["autonomous_completion_rate"] == 0.5
    assert report["by_status"] == {"done": 1, "failed": 1}
    assert report["avg_steps"] > 0


# ── Crash recovery ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_crashed_iteration_is_recovered_automatically(runs_db, template_plan, governed_ok, monkeypatch):
    monkeypatch.setattr(dev_runs, "AUTO_RECOVER_DELAY_SECONDS", 0.0)
    crashes = {"n": 0}

    async def sometimes_crashes(**kwargs):
        crashes["n"] += 1
        if crashes["n"] == 1:
            raise RuntimeError("sandbox vanished mid-iteration")
        return _llm_text("DONE: finished after recovering")

    monkeypatch.setattr(dev_runs, "call_llm_normalized", sometimes_crashes)
    run = dev_runs.create_run("goal")
    crashed = await dev_runs.process_run(run["id"])
    assert crashed["status"] == "paused"
    assert crashed["status_reason"].startswith(dev_runs.AUTO_RECOVER_PREFIX)

    assert dev_runs.recover_crashed_runs() == [run["id"]]
    assert dev_runs.get_run(run["id"])["status"] == "running"
    finished = await dev_runs.process_run(run["id"])
    assert finished["status"] == "done"
    assert [s["phase"] for s in dev_runs.get_steps(run["id"])].count("recover") == 1


@pytest.mark.asyncio
async def test_repeated_crashes_stop_asking_for_recovery(runs_db, template_plan, governed_ok, monkeypatch):
    monkeypatch.setattr(dev_runs, "AUTO_RECOVER_DELAY_SECONDS", 0.0)

    async def always_crashes(**kwargs):
        raise RuntimeError("still broken")

    monkeypatch.setattr(dev_runs, "call_llm_normalized", always_crashes)
    run = dev_runs.create_run("goal")
    for _ in range(dev_runs.MAX_AUTO_RECOVERIES + 2):
        await dev_runs.process_run(run["id"])
        dev_runs.recover_crashed_runs()
    final = dev_runs.get_run(run["id"])
    assert final["status"] == "paused"
    assert "owner input needed" in final["status_reason"]
    assert dev_runs._recovery_attempts(run["id"]) == dev_runs.MAX_AUTO_RECOVERIES


def test_budget_pauses_are_not_auto_recovered(runs_db):
    run = dev_runs.create_run("goal")
    dev_runs.update_run(run["id"], status="paused", status_reason="Iteration budget exhausted (5/5)")
    assert dev_runs.recover_crashed_runs() == []
    assert dev_runs.get_run(run["id"])["status"] == "paused"


def test_run_detail_payload_omits_the_full_tool_output(runs_db):
    """The dashboard polls this endpoint; the observation blobs stay server-side."""
    run = dev_runs.create_run("goal", start=False)
    dev_runs.add_step(run["id"], "act", "dev_read_file", "read app.py", result="x" * 15000)
    detail = dev_runs.get_run(run["id"], with_steps=True)
    assert detail["steps"][0]["summary"] == "read app.py"
    assert "result" not in detail["steps"][0]
    assert dev_runs.get_steps(run["id"])[0]["result"] == "x" * 15000  # still there internally


@pytest.mark.asyncio
async def test_repeating_tests_between_edits_is_not_treated_as_a_loop(
    runs_db, scripted_llm, template_plan, governed_ok
):
    """write → test → write → test is a healthy rhythm, not a duplicate loop."""
    scripted_llm["script"] = [
        _llm_tool("dev_write_file", {"path": "a.py", "content": "1"}),
        _llm_tool("dev_run_tests", {"runner": "auto"}),
        _llm_tool("dev_write_file", {"path": "b.py", "content": "2"}),
        _llm_tool("dev_run_tests", {"runner": "auto"}),
        _llm_tool("dev_write_file", {"path": "c.py", "content": "3"}),
        _llm_tool("dev_run_tests", {"runner": "auto"}),
        _llm_text("DONE: three modules, each verified"),
    ]
    run = dev_runs.create_run("build three modules")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "done"
    assert [t for t, _ in governed_ok].count("dev_run_tests") == 3  # none refused
    assert not [s for s in dev_runs.get_steps(run["id"]) if s["status"] == "blocked"]


# ── Upgrade path ─────────────────────────────────────────────────────────────

def test_migration_adds_columns_to_an_existing_populated_table(tmp_path, monkeypatch):
    """Production upgrades an existing DB in place: pre-migration rows must
    survive and the loop must degrade gracefully on their empty observations."""
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE dev_runs (
            id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
            plan_id TEXT, trace_id TEXT, iter_used INTEGER NOT NULL DEFAULT 0,
            iter_budget INTEGER NOT NULL DEFAULT 0, cost_used REAL NOT NULL DEFAULT 0,
            cost_budget REAL, wall_deadline TEXT, checkpoint_step TEXT,
            status_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE dev_run_steps (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
            phase TEXT NOT NULL, tool TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'done',
            created_at TEXT NOT NULL, UNIQUE (run_id, seq)
        );
        INSERT INTO dev_runs VALUES
            ('run-old', 'legacy goal', 'running', NULL, 'trace-old', 4, 0, 0, NULL, NULL,
             '2', '', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00');
        INSERT INTO dev_run_steps VALUES
            ('step-old-1', 'run-old', 1, 'plan', '', 'legacy plan', 'done', '2026-08-01T00:00:00+00:00'),
            ('step-old-2', 'run-old', 2, 'act', 'dev_read_file', 'legacy read', 'done', '2026-08-01T00:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    monkeypatch.setattr(dev_runs, "DB_PATH", db_path)
    monkeypatch.setattr(autonomy, "DB_PATH", db_path)
    database.init_db()

    steps = dev_runs.get_steps("run-old")
    assert [s["summary"] for s in steps] == ["legacy plan", "legacy read"]
    assert steps[1]["result"] == "" and steps[1]["fingerprint"] == ""
    # Old rows carry no observations and no fingerprints — the new logic simply
    # sees nothing rather than misbehaving.
    assert dev_runs._observation_block(steps) == ""
    assert dev_runs._duplicate_count(steps, "") == 0
    assert "legacy read" in dev_runs._build_messages(dev_runs.get_run("run-old"))[1]["content"]
    # New steps on the same run store the new fields normally.
    dev_runs.add_step("run-old", "act", "dev_read_file", "fresh read", result="body", fingerprint="fp1")
    assert dev_runs.get_steps("run-old")[-1]["result"] == "body"
    assert dev_runs.metrics()["runs_considered"] == 1


# ── Site lineage: continuations, revisions, rollback ─────────────────────────
# A card that builds a site is worthless if the next card cannot refine it, so
# these cover the whole chain: the DB link, the executor context that stops a
# continuation rebuilding from scratch, the working tree it inherits, and the
# stable URL that survives every revision (and every deletion).

@pytest.fixture()
def previews(tmp_path, monkeypatch):
    """Isolated previews root, so alias/snapshot tests never touch real ones."""
    from backend import dev_sandbox

    root = tmp_path / "dev-previews"
    root.mkdir()
    monkeypatch.setattr(dev_sandbox, "PREVIEWS_ROOT", root)
    monkeypatch.setattr(dev_sandbox, "RUNS_ROOT", tmp_path / "dev-repo-runs")
    return root


def _publish_snapshot(previews_root, run_id: str, body: str = "site") -> None:
    (previews_root / run_id).mkdir(parents=True, exist_ok=True)
    (previews_root / run_id / "index.html").write_text(body)


def test_continuation_joins_its_parents_chain(runs_db):
    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("make the header sticky", parent_run_id=root["id"])
    third = dev_runs.create_run("add a pricing block", parent_run_id=second["id"])

    assert root["root_run_id"] == root["id"] and root["revision"] == 1
    assert second["parent_run_id"] == root["id"]
    assert second["root_run_id"] == root["id"] and second["revision"] == 2
    # Revision counts the chain, not the parent — a third card continuing the
    # second is v3 even though its parent is v2.
    assert third["root_run_id"] == root["id"] and third["revision"] == 3


def test_continuation_inherits_owner_and_budgets(runs_db):
    root = dev_runs.create_run("build a landing page", assignee_agent_id="agent-a",
                               iter_budget=25, cost_budget=3.5)
    inherited = dev_runs.create_run("refine it", parent_run_id=root["id"])
    assert inherited["assignee_agent_id"] == "agent-a"
    assert inherited["iter_budget"] == 25 and inherited["cost_budget"] == 3.5
    # …unless the caller deliberately sets its own.
    overridden = dev_runs.create_run("refine it harder", parent_run_id=root["id"],
                                     assignee_agent_id="agent-b", iter_budget=5)
    assert overridden["assignee_agent_id"] == "agent-b" and overridden["iter_budget"] == 5


def test_continuation_of_a_missing_parent_is_refused(runs_db):
    with pytest.raises(KeyError):
        dev_runs.create_run("refine nothing", parent_run_id="run-doesnotexist")


def test_continuation_context_tells_the_executor_the_code_already_exists(runs_db):
    root = dev_runs.create_run("build a landing page")
    dev_runs.update_run(root["id"], status="done", status_reason="published",
                        demo_url="/demo/site-" + root["id"] + "/")
    dev_runs.add_step(root["id"], "act", "dev_publish_demo", "published 12 files")
    child = dev_runs.create_run("make the header sticky", parent_run_id=root["id"])

    prompt = dev_runs._build_messages(dev_runs.get_run(child["id"]))[1]["content"]
    assert "CONTINUATION" in prompt
    assert "ALREADY CONTAINS" in prompt
    assert "build a landing page" in prompt          # what the previous revision was for
    assert "published 12 files" in prompt            # how it ended
    assert "/demo/site-" in prompt                   # and where it must republish
    # The planner sees the same context, or it would plan a greenfield build.
    assert "CONTINUATION" in dev_runs._planning_goal(dev_runs.get_run(child["id"]))


def test_a_root_run_carries_no_continuation_context(runs_db):
    run = dev_runs.create_run("build a landing page")
    assert dev_runs._continuation_context(dev_runs.get_run(run["id"])) == ""
    assert "CONTINUATION" not in dev_runs._build_messages(dev_runs.get_run(run["id"]))[1]["content"]


def test_lineage_marks_the_revision_the_stable_url_serves(runs_db, previews):
    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    _publish_snapshot(previews, root["id"], "v1")
    _publish_snapshot(previews, second["id"], "v2")
    dev_runs.promote_revision(second["id"])

    chain = dev_runs.lineage(root["id"])
    assert [item["revision"] for item in chain] == [1, 2]
    assert [item["is_live"] for item in chain] == [False, True]
    assert all(item["has_snapshot"] for item in chain)
    # Asking from any card in the chain returns the same chain.
    assert [item["id"] for item in dev_runs.lineage(second["id"])] == [root["id"], second["id"]]


def test_promote_revision_rolls_the_site_back_without_losing_the_newer_build(runs_db, previews):
    from backend import dev_sandbox

    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    _publish_snapshot(previews, root["id"], "v1")
    _publish_snapshot(previews, second["id"], "v2")

    dev_runs.promote_revision(second["id"])
    alias = dev_sandbox.site_alias_path(root["id"])
    assert (alias / "index.html").read_text() == "v2"

    rolled_back = dev_runs.promote_revision(root["id"])
    assert (alias / "index.html").read_text() == "v1"
    assert rolled_back["demo_url"] == f"/demo/site-{root['id']}/"   # one URL for the chain
    assert rolled_back["demo_snapshot_url"] == f"/demo/{root['id']}/"
    # Rollback is a pointer move: the newer build is still there to return to.
    assert (previews / second["id"] / "index.html").read_text() == "v2"


def test_promoting_an_unpublished_revision_is_refused(runs_db, previews):
    root = dev_runs.create_run("build a landing page")
    with pytest.raises(FileNotFoundError):
        dev_runs.promote_revision(root["id"])


def test_deleting_the_live_revision_falls_back_to_a_surviving_one(runs_db, previews):
    from backend import dev_sandbox

    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    _publish_snapshot(previews, root["id"], "v1")
    _publish_snapshot(previews, second["id"], "v2")
    dev_runs.promote_revision(second["id"])

    assert dev_runs.delete_run(second["id"]) is True
    alias = dev_sandbox.site_alias_path(root["id"])
    assert (alias / "index.html").read_text() == "v1"  # URL still serves something real

    # …and when the last revision goes, the URL is retired rather than dangling.
    dev_runs.delete_run(root["id"])
    assert not alias.is_symlink()


def test_deleting_a_chain_that_never_published_creates_no_site(runs_db, previews):
    from backend import dev_sandbox

    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    _publish_snapshot(previews, root["id"], "v1")  # snapshot on disk, never promoted

    dev_runs.delete_run(second["id"])
    assert not dev_sandbox.site_alias_path(root["id"]).exists()


def test_deleting_a_middle_revision_keeps_the_chain_connected(runs_db, previews):
    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    third = dev_runs.create_run("refine it again", parent_run_id=second["id"])

    dev_runs.delete_run(second["id"])
    # The orphan is re-parented onto its grandparent instead of pointing at a
    # row that no longer exists (which would silently break continuations).
    assert dev_runs.get_run(third["id"])["parent_run_id"] == root["id"]
    assert [item["id"] for item in dev_runs.lineage(third["id"])] == [root["id"], third["id"]]


@pytest.mark.asyncio
async def test_the_sandbox_is_cloned_from_the_parent_run(runs_db, monkeypatch):
    """The whole point of a continuation: it opens the previous revision's
    working tree, not an empty repo."""
    from backend import dev_sandbox, tools

    root = dev_runs.create_run("build a landing page")
    child = dev_runs.create_run("refine it", parent_run_id=root["id"], start=False)
    seen = {}

    async def fake_ensure(run_id, parent_run_id=None):
        seen["run_id"], seen["parent_run_id"] = run_id, parent_run_id
        return "http://sandbox"

    monkeypatch.setattr(dev_sandbox, "ensure_sandbox", fake_ensure)
    monkeypatch.setattr(dev_sandbox, "release_sandbox", lambda run_id: asyncio.sleep(0))
    monkeypatch.setattr(dev_runs, "process_run", lambda run_id: asyncio.sleep(0))

    await dev_runs._run_with_sandbox(child["id"])
    assert seen == {"run_id": child["id"], "parent_run_id": root["id"]}


def test_migration_backfills_lineage_for_pre_lineage_rows(runs_db):
    """Rows created before lineage existed must still answer "which chain?" —
    every code path relies on root_run_id being set."""
    import sqlite3

    conn = sqlite3.connect(runs_db)
    conn.execute(
        "INSERT INTO dev_runs (id, goal, status, created_at, updated_at, root_run_id) "
        "VALUES ('run-legacy0001', 'old goal', 'done', '2026-08-01T00:00:00+00:00', "
        "'2026-08-01T00:00:00+00:00', NULL)"
    )
    conn.commit()
    conn.close()

    database.init_db()
    legacy = dev_runs.get_run("run-legacy0001")
    assert legacy["root_run_id"] == "run-legacy0001"
    assert legacy["revision"] == 1
    assert [item["id"] for item in dev_runs.lineage("run-legacy0001")] == ["run-legacy0001"]


def test_a_continuation_waits_for_the_revision_it_continues(runs_db):
    """Cloning a working tree that is still being written to would race the
    parent's own sandbox, so the queue holds the card instead of refusing it."""
    import sqlite3

    root = dev_runs.create_run("build a landing page")          # planned
    child = dev_runs.create_run("refine it", parent_run_id=root["id"])

    def selectable():
        """The worker's own pick-up query (dev_runs.worker_loop)."""
        conn = sqlite3.connect(runs_db)
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in dev_runs.ASSIGNED_BUSY_STATUSES)
        rows = conn.execute(
            f"""SELECT r.id FROM dev_runs r
                WHERE r.status IN ('planned', 'running')
                  AND (r.status <> 'planned' OR NOT EXISTS (
                        SELECT 1 FROM dev_runs p
                        WHERE p.id = r.parent_run_id
                          AND p.status IN ({placeholders})))
                ORDER BY r.created_at""",
            dev_runs.ASSIGNED_BUSY_STATUSES,
        ).fetchall()
        conn.close()
        return [row["id"] for row in rows]

    assert selectable() == [root["id"]]
    assert dev_runs.waits_for_parent(dev_runs.get_run(child["id"])) is True

    dev_runs.update_run(root["id"], status="done")
    assert selectable() == [child["id"]]
    assert dev_runs.waits_for_parent(dev_runs.get_run(child["id"])) is False


# ── Click-to-comment feedback ─────────────────────────────────────────────────
# The overlay on a published demo lets the owner comment on a live element
# instead of writing a fresh brief from memory; these cover the backend half:
# collecting comments across a whole product chain and folding them into one
# continuation rather than one card per remark.

def test_feedback_is_recorded_against_the_run_it_was_left_on(runs_db):
    run = dev_runs.create_run("build a landing page")
    item = dev_runs.add_feedback(run["id"], comment="make this button bigger",
                                 page_path="/pricing.html", selector="#cta",
                                 element_text="Buy now", viewport="mobile")
    assert item["run_id"] == run["id"]
    assert item["root_run_id"] == run["id"]
    assert item["status"] == "open"
    assert item["comment"] == "make this button bigger"


def test_feedback_requires_nonempty_comment(runs_db):
    run = dev_runs.create_run("build a landing page")
    with pytest.raises(ValueError):
        dev_runs.add_feedback(run["id"], comment="   ")


def test_feedback_on_a_missing_run_is_refused(runs_db):
    with pytest.raises(KeyError):
        dev_runs.add_feedback("run-doesnotexist", comment="hi")


def test_listing_feedback_spans_the_whole_chain(runs_db):
    """An owner may still be commenting on an older build after a newer
    revision shipped — the comment must not be lost off in a side table only
    the old card can see."""
    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    dev_runs.add_feedback(root["id"], comment="old comment")
    dev_runs.add_feedback(second["id"], comment="new comment")

    from_root = [f["comment"] for f in dev_runs.list_feedback(root["id"])]
    from_child = [f["comment"] for f in dev_runs.list_feedback(second["id"])]
    assert from_root == from_child == ["old comment", "new comment"]


def test_dismissed_feedback_is_excluded_by_default(runs_db):
    run = dev_runs.create_run("build a landing page")
    item = dev_runs.add_feedback(run["id"], comment="skip me")
    dev_runs.dismiss_feedback(item["id"])
    assert dev_runs.list_feedback(run["id"]) == []
    assert [f["status"] for f in dev_runs.list_feedback(run["id"], status="dismissed")] == ["dismissed"]
    assert dev_runs.list_feedback(run["id"], status=None)[0]["status"] == "dismissed"


def test_dismissing_twice_is_refused(runs_db):
    run = dev_runs.create_run("build a landing page")
    item = dev_runs.add_feedback(run["id"], comment="skip me")
    dev_runs.dismiss_feedback(item["id"])
    with pytest.raises(KeyError):
        dev_runs.dismiss_feedback(item["id"])


def test_consuming_feedback_folds_every_open_comment_into_one_card(runs_db):
    run = dev_runs.create_run("build a landing page")
    dev_runs.add_feedback(run["id"], comment="make the header sticky",
                          page_path="/", selector="header", element_text="Nav")
    dev_runs.add_feedback(run["id"], comment="fix the footer color",
                          page_path="/", selector="footer")
    dev_runs.add_feedback(run["id"], comment="typo on pricing page",
                          page_path="/pricing.html")

    child = dev_runs.consume_feedback(run["id"])
    assert child["parent_run_id"] == run["id"]  # only revision, so it's the live one
    assert "make the header sticky" in child["goal"]
    assert "fix the footer color" in child["goal"]
    assert "typo on pricing page" in child["goal"]
    assert "build a landing page" in child["goal"]  # product context carried along

    # All three are now applied, attributed to the new card, and gone from
    # the open queue — not one card per remark.
    applied = dev_runs.list_feedback(run["id"], status="applied")
    assert len(applied) == 3
    assert all(item["consumed_by_run_id"] == child["id"] for item in applied)
    assert dev_runs.list_feedback(run["id"]) == []


def test_consuming_feedback_continues_the_live_revision_not_the_commented_one(runs_db, monkeypatch):
    """A comment left on an old build should refine what is live NOW, not
    resurrect a stale checkout."""
    from backend import dev_sandbox

    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    dev_runs.add_feedback(root["id"], comment="old-build remark")

    monkeypatch.setattr(dev_sandbox, "current_site_revision", lambda root_id: second["id"])
    child = dev_runs.consume_feedback(root["id"])
    assert child["parent_run_id"] == second["id"]


def test_consuming_feedback_falls_back_to_root_when_nothing_is_live(runs_db, monkeypatch):
    from backend import dev_sandbox

    root = dev_runs.create_run("build a landing page")
    dev_runs.add_feedback(root["id"], comment="never published yet")
    monkeypatch.setattr(dev_sandbox, "current_site_revision", lambda root_id: None)
    child = dev_runs.consume_feedback(root["id"])
    assert child["parent_run_id"] == root["id"]


def test_consuming_with_no_open_feedback_is_refused(runs_db):
    run = dev_runs.create_run("build a landing page")
    with pytest.raises(ValueError):
        dev_runs.consume_feedback(run["id"])


def test_deleting_the_only_revision_clears_its_unreachable_feedback(runs_db):
    """Once nothing survives to consume it into, dangling feedback would
    otherwise sit invisible forever."""
    run = dev_runs.create_run("build a landing page")
    dev_runs.add_feedback(run["id"], comment="orphan me")
    dev_runs.delete_run(run["id"])

    import sqlite3
    conn = sqlite3.connect(runs_db)
    count = conn.execute("SELECT COUNT(*) FROM dev_run_feedback WHERE root_run_id = ?",
                         (run["id"],)).fetchone()[0]
    conn.close()
    assert count == 0


def test_deleting_one_revision_keeps_the_chains_feedback(runs_db):
    root = dev_runs.create_run("build a landing page")
    second = dev_runs.create_run("refine it", parent_run_id=root["id"])
    dev_runs.add_feedback(root["id"], comment="still valid")
    dev_runs.delete_run(second["id"])
    assert [f["comment"] for f in dev_runs.list_feedback(root["id"])] == ["still valid"]


# ── Failure detection blind spot: a "successful" call that reports failure ───
# Discovered live in production 2026-08-18: dev_exec calls that time out come
# back as HTTP 200 with {"timed_out": true} in the body, no top-level "error"
# key. A run kept retrying npm install with a growing timeout for over an
# hour, 20+ times in a row, because every one of those steps was recorded
# "done" — the circuit breakers that exist specifically to catch this pattern
# never saw a single failure.

def test_tool_result_recognizes_a_timed_out_call_as_a_failure():
    assert dev_runs._tool_result_indicates_failure(
        "dev_exec", {"exit_code": -1, "stdout": "", "stderr": "Timed out after 600s", "timed_out": True}
    ) is True


def test_tool_result_recognizes_a_nonzero_exit_code_as_a_failure():
    assert dev_runs._tool_result_indicates_failure(
        "dev_exec", {"exit_code": 1, "stdout": "", "stderr": "npm ERR!"}
    ) is True
    assert dev_runs._tool_result_indicates_failure(
        "dev_run_tests", {"exit_code": 1, "runner": "pytest"}
    ) is True


def test_tool_result_treats_a_clean_exit_as_success():
    assert dev_runs._tool_result_indicates_failure(
        "dev_exec", {"exit_code": 0, "stdout": "v20.19.2\n", "stderr": ""}
    ) is False


def test_tool_result_ignores_exit_code_on_tools_where_it_is_not_meaningful():
    """Only dev_exec/dev_run_tests actually run a shell command with a real
    exit code; other tools must not be second-guessed by this heuristic."""
    assert dev_runs._tool_result_indicates_failure(
        "dev_publish_demo", {"exit_code": "not-a-real-field", "demo_url": "/demo/x/"}
    ) is False


@pytest.mark.asyncio
async def test_a_timed_out_dev_exec_trips_the_consecutive_failure_gate(runs_db, template_plan, governed_ok, monkeypatch):
    """The whole point of the fix: a run that keeps retrying a doomed shell
    command now actually stops instead of burning its budget silently."""
    scripted = {"n": 0}

    async def fake_call(**kwargs):
        scripted["n"] += 1
        if scripted["n"] > 20:
            return _llm_text("DONE: gave up")
        return _llm_tool("dev_exec", {"argv": ["npm", "install"], "timeout_s": 60 + scripted["n"]})

    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)
    monkeypatch.setattr(dev_runs, "execute_governed_tool",
                        lambda *a, **kw: json.dumps({"exit_code": -1, "stderr": "Timed out after 60s",
                                                     "timed_out": True}))
    run = dev_runs.create_run("build something that needs npm install")
    result = await dev_runs.process_run(run["id"])
    assert result["status"] == "failed"
    assert scripted["n"] < 20  # stopped well before the script ran out


def test_dev_exec_fingerprint_ignores_the_retried_timeout():
    """A retry that only raises timeout_s is still the same action — that is
    what lets duplicate-detection (and therefore the failure gate above) see
    it as a repeat instead of 20 unrelated 'new' calls."""
    a = dev_runs._fingerprint("dev_exec", {"argv": ["npm", "install"], "timeout_s": 120})
    b = dev_runs._fingerprint("dev_exec", {"argv": ["npm", "install"], "timeout_s": 600})
    assert a == b


def test_dev_exec_fingerprint_still_distinguishes_different_commands():
    a = dev_runs._fingerprint("dev_exec", {"argv": ["npm", "install"], "timeout_s": 120})
    b = dev_runs._fingerprint("dev_exec", {"argv": ["npm", "test"], "timeout_s": 120})
    assert a != b


def test_other_tools_fingerprint_on_every_argument_as_before():
    """The timeout_s carve-out is dev_exec-specific — a tool that happens to
    take a same-named argument must not silently lose it from the hash."""
    a = dev_runs._fingerprint("dev_run_tests", {"runner": "auto", "timeout_s": 30})
    b = dev_runs._fingerprint("dev_run_tests", {"runner": "auto", "timeout_s": 90})
    assert a != b


# ── Long runs: staying alive for hours without losing the thread ─────────────
# A run that works for hours cannot keep appending to its prompt, and cannot
# just forget either. Observed live: at iteration 60 of a real run the model
# spent its whole budget on reasoning and returned no visible answer at all
# ("LLM failure: empty"), parking the card until a human pressed Resume.

@pytest.mark.asyncio
async def test_history_is_left_alone_on_a_short_run(runs_db, monkeypatch):
    """Compaction must cost nothing until a run is actually long."""
    called = []
    async def fake_call(**kwargs):
        called.append(1)
        return _llm_text("digest")
    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)

    run = dev_runs.create_run("short task")
    for i in range(5):
        dev_runs.add_step(run["id"], "act", "dev_read_file", f"read {i}")
    result = await dev_runs._compact_history(dev_runs.get_run(run["id"]))
    assert called == []
    assert result["progress_digest"] == ""


@pytest.mark.asyncio
async def test_a_long_run_folds_old_steps_into_a_digest(runs_db, monkeypatch):
    async def fake_call(**kwargs):
        return _llm_text("Built the landing page; index.html and src/App.tsx exist and build cleanly.")
    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)

    run = dev_runs.create_run("long task")
    for i in range(dev_runs.COMPACT_AFTER_STEPS + 10):
        dev_runs.add_step(run["id"], "act", "dev_read_file", f"read file {i}")

    compacted = await dev_runs._compact_history(dev_runs.get_run(run["id"]))
    assert "landing page" in compacted["progress_digest"]
    assert compacted["digest_through_seq"] > 0


@pytest.mark.asyncio
async def test_digested_steps_leave_the_verbatim_ledger(runs_db, monkeypatch):
    """The whole point: compaction has to BOUND the prompt. If digested steps
    kept appearing verbatim it would grow it instead."""
    async def fake_call(**kwargs):
        return _llm_text("earlier work summarized")
    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)

    run = dev_runs.create_run("long task")
    for i in range(dev_runs.COMPACT_AFTER_STEPS + 10):
        dev_runs.add_step(run["id"], "act", "dev_read_file", f"UNIQUE-MARKER-{i}")
    before = dev_runs._build_messages(dev_runs.get_run(run["id"]))[1]["content"]

    compacted = await dev_runs._compact_history(dev_runs.get_run(run["id"]))
    after = dev_runs._build_messages(compacted)[1]["content"]

    # A step inside the pre-compaction window but inside the compacted batch:
    # visible verbatim before, represented only by the digest after.
    marker = "UNIQUE-MARKER-34"
    assert marker in before
    assert marker not in after                 # folded away
    assert "earlier work summarized" in after  # but not forgotten
    assert len(after) < len(before)


@pytest.mark.asyncio
async def test_a_failed_compaction_never_kills_the_run(runs_db, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(dev_runs, "call_llm_normalized", boom)

    run = dev_runs.create_run("long task")
    for i in range(dev_runs.COMPACT_AFTER_STEPS + 10):
        dev_runs.add_step(run["id"], "act", "dev_read_file", f"read {i}")
    result = await dev_runs._compact_history(dev_runs.get_run(run["id"]))
    assert result["progress_digest"] == ""   # unchanged, run continues


@pytest.mark.asyncio
async def test_compaction_does_not_use_thinking(runs_db, monkeypatch):
    """Summarizing is not a reasoning task, and a runaway thinking chain here
    would stall the very run this exists to keep alive."""
    captured = {}
    async def fake_call(**kwargs):
        captured.update(kwargs)
        return _llm_text("digest")
    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)

    run = dev_runs.create_run("long task")
    for i in range(dev_runs.COMPACT_AFTER_STEPS + 10):
        dev_runs.add_step(run["id"], "act", "dev_exec", f"step {i}")
    await dev_runs._compact_history(dev_runs.get_run(run["id"]))
    assert captured["provider_options"]["think"] is False


@pytest.mark.asyncio
async def test_an_empty_model_reply_compacts_and_retries_instead_of_pausing(runs_db, template_plan, governed_ok, monkeypatch):
    """The live failure this fixes: an empty reply used to park the card in
    `paused` until a human intervened, mid-way through hours of work."""
    calls = {"n": 0}
    async def fake_call(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return llm_client.NormalizedLLMResponse(
                status="empty", provider="mock", model="mock-model",
                usage=LLMUsage(cost=0.0), error_message="Model returned reasoning only")
        return _llm_text("compacted digest of earlier work")
    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)

    run = dev_runs.create_run("long task")
    for i in range(40):
        dev_runs.add_step(run["id"], "act", "dev_read_file", f"read {i}")

    result = await dev_runs._iterate(dev_runs.get_run(run["id"]))
    assert result["status"] != "paused"           # kept going on its own
    assert result["digest_through_seq"] > 0       # it compacted instead


@pytest.mark.asyncio
async def test_an_empty_reply_with_nothing_left_to_compact_asks_the_owner(runs_db, template_plan, governed_ok, monkeypatch):
    """Failing open forever would be worse than stopping — if the context is
    already minimal and the model still says nothing, that is a real blocker."""
    async def fake_call(**kwargs):
        return llm_client.NormalizedLLMResponse(
            status="empty", provider="mock", model="mock-model", usage=LLMUsage(cost=0.0))
    monkeypatch.setattr(dev_runs, "call_llm_normalized", fake_call)

    run = dev_runs.create_run("short task")
    dev_runs.add_step(run["id"], "act", "dev_read_file", "one step")
    result = await dev_runs._iterate(dev_runs.get_run(run["id"]))
    assert result["status"] == "paused"
    assert "could not be reduced" in result["status_reason"]


def test_a_long_brief_is_stored_whole(runs_db):
    """A serious card carries requirements, constraints, brand voice and
    acceptance criteria — the old 8k cap truncated real specs mid-sentence."""
    brief = "Build a platform.\n" + ("Requirement line with real detail.\n" * 1200)
    assert len(brief) > 8000
    run = dev_runs.create_run(brief)
    # create_run strips surrounding whitespace; nothing in the middle is lost.
    assert run["goal"] == brief.strip()
    assert run["goal"].endswith("Requirement line with real detail.")
