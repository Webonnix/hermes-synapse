"""Durable, resumable autonomous dev-runs.

A dev-run is a long-lived plan→act→observe loop that develops code in the
sandboxed dev-repo (via the governed dev_* / git_* tools) outside the chat's
tool-iteration limit. State lives in the additive `dev_runs` / `dev_run_steps`
tables, so a backend restart resumes unfinished runs from their checkpoint
without duplicating steps.

Every tool call goes through control_plane.execute_governed_tool — the sandbox
does not bypass risk classes, approvals, budgets or the kill-switch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.database import DB_PATH
from backend.llm_client import call_llm_normalized
from backend.control_plane import execute_governed_tool, get_control_state

logger = logging.getLogger("hermes.dev_runs")

RUN_STATUSES = (
    "planned", "running", "paused", "awaiting_approval",
    "verifying", "done", "failed", "cancelled",
)
ACTIVE_STATUSES = ("planned", "running", "verifying")

# Tools the executor loop may use. All are R1/R2 sandbox- or dev-repo-scoped.
DEV_RUN_TOOLS = (
    "dev_read_file", "dev_write_file", "dev_patch", "dev_list_dir",
    "dev_exec", "dev_run_tests", "git_status", "git_diff", "git_commit", "git_push",
)

DEFAULT_ITER_BUDGET = 200
POLL_SECONDS = float(os.getenv("DEV_RUNS_POLL_SECONDS", "5"))
HISTORY_STEPS_IN_CONTEXT = 40

EXECUTOR_SYSTEM_PROMPT = (
    "You are the Hermes autonomous development executor working inside a sandboxed "
    "dev repository. Achieve the stated goal by calling the provided tools, one "
    "action at a time. Read before you write; keep changes minimal and coherent; "
    "run tests with dev_run_tests before committing. Never invent file contents — "
    "inspect them. When the goal is fully achieved and verified, reply with plain "
    "text starting with 'DONE:' followed by a one-paragraph summary. If the goal "
    "is impossible, reply with 'BLOCKED:' and the reason."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_run(
    goal: str,
    *,
    iter_budget: int = DEFAULT_ITER_BUDGET,
    cost_budget: Optional[float] = None,
    wall_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Dev-run goal must not be empty")
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    trace_id = f"trace-{uuid.uuid4().hex[:12]}"
    deadline = (
        (datetime.now(timezone.utc) + timedelta(minutes=wall_minutes)).isoformat(timespec="seconds")
        if wall_minutes else None
    )
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO dev_runs
               (id, goal, status, trace_id, iter_used, iter_budget, cost_used,
                cost_budget, wall_deadline, created_at, updated_at)
               VALUES (?, ?, 'planned', ?, 0, ?, 0, ?, ?, ?, ?)""",
            (run_id, goal[:8000], trace_id, max(1, int(iter_budget)), cost_budget, deadline, now, now),
        )
    return get_run(run_id)  # type: ignore[return-value]


def get_run(run_id: str, with_steps: bool = False) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM dev_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        if with_steps:
            run["steps"] = [dict(item) for item in conn.execute(
                "SELECT * FROM dev_run_steps WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()]
    return run


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dev_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


_UPDATABLE_FIELDS = {
    "status", "plan_id", "iter_used", "cost_used", "checkpoint_step", "status_reason",
}


def update_run(run_id: str, **fields: Any) -> Dict[str, Any]:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Cannot update dev_run fields: {sorted(unknown)}")
    if "status" in fields and fields["status"] not in RUN_STATUSES:
        raise ValueError(f"Unknown dev-run status: {fields['status']}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values())
    with _connect() as conn:
        cursor = conn.execute(
            f"UPDATE dev_runs SET {assignments}, updated_at = ? WHERE id = ?",
            (*values, _now(), run_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(run_id)
    run = get_run(run_id)
    assert run is not None
    return run


def add_step(run_id: str, phase: str, tool: str, summary: str, status: str = "done") -> Dict[str, Any]:
    """Appends the next step (seq is allocated atomically) and moves the checkpoint."""
    step_id = f"step-{uuid.uuid4().hex[:12]}"
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM dev_run_steps WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO dev_run_steps (id, run_id, seq, phase, tool, summary, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (step_id, run_id, next_seq, phase[:40], (tool or "")[:80], (summary or "")[:1000], status, now),
        )
        conn.execute(
            "UPDATE dev_runs SET checkpoint_step = ?, updated_at = ? WHERE id = ?",
            (str(next_seq), now, run_id),
        )
    return {"id": step_id, "run_id": run_id, "seq": next_seq, "phase": phase,
            "tool": tool, "summary": summary, "status": status, "created_at": now}


def get_steps(run_id: str) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dev_run_steps WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


# ── Lifecycle commands ───────────────────────────────────────────────────────

def pause_run(run_id: str, reason: str = "Paused by owner") -> Dict[str, Any]:
    return update_run(run_id, status="paused", status_reason=reason[:500])


def resume_run(run_id: str) -> Dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] not in ("paused", "awaiting_approval"):
        raise ValueError(f"Dev-run cannot resume from status {run['status']}")
    return update_run(run_id, status="running", status_reason="")


def cancel_run(run_id: str, reason: str = "Cancelled by owner") -> Dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] in ("done", "failed", "cancelled"):
        return run
    return update_run(run_id, status="cancelled", status_reason=reason[:500])


# ── Planning ─────────────────────────────────────────────────────────────────

async def _plan(run: Dict[str, Any]) -> Dict[str, Any]:
    from backend.autonomy import build_plan_llm

    plan = await build_plan_llm(run["goal"])
    run = update_run(run["id"], plan_id=plan["id"], status="running")
    titles = "; ".join(step.get("title", step.get("id", "?")) for step in plan["steps"])
    add_step(run["id"], "plan", "", f"Plan {plan['id']} ({plan['tier']}): {titles}"[:1000])
    await _emit_event(run["id"], "running", f"Plan ready: {titles}", "started")
    return run


def _plan_context(plan_id: Optional[str]) -> str:
    if not plan_id:
        return ""
    try:
        from backend.autonomy import get_plan
        plan = get_plan(plan_id)
    except Exception:
        return ""
    if not plan:
        return ""
    lines = ["Accepted plan:"]
    for step in plan["steps"]:
        acceptance = "; ".join(step.get("acceptance", []))
        lines.append(f"- [{step.get('id')}] {step.get('title', '')}"
                     + (f" — acceptance: {acceptance}" if acceptance else ""))
    return "\n".join(lines)


# ── Run events (WebSocket + Telegram owner notifications) ────────────────────

RUN_EVENTS = ("started", "phase_done", "awaiting_approval", "done", "failed", "budget_80")


async def _notify_owner_telegram(payload: Dict[str, Any]) -> None:
    """Sends the event to the owner chat via the main bot (same owner-chat
    pattern as Control Plane approval notifications)."""
    import backend.bot as bot
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
    if not chat_id or not getattr(bot, "telegram_app", None) or not bot.telegram_app.bot:
        return
    icon = {"awaiting_approval": "⏳", "done": "✅", "failed": "❌", "budget_80": "⚠️"}.get(payload["event"], "🛠️")
    text = (f"{icon} Dev-run {payload['run_id']}: {payload['event']} (status: {payload['status']})\n"
            f"{payload['summary']}")
    await bot.telegram_app.bot.send_message(chat_id=int(chat_id), text=text)


async def _emit_event(run_id: str, status: str, summary: str, event: str) -> None:
    """Broadcasts a dev-run event to all dashboard clients and the owner's
    Telegram. Payload carries only a capped summary — never prompts or args."""
    payload = {
        "type": "dev_run_event",
        "run_id": run_id,
        "status": status,
        "event": event,
        "summary": (summary or "")[:200],
    }
    try:
        from backend.websocket_manager import manager
        await manager.broadcast(payload)
    except Exception as exc:
        logger.debug("Dev-run event broadcast failed: %s", exc)
    try:
        await _notify_owner_telegram(payload)
    except Exception as exc:
        logger.warning("Dev-run Telegram notification failed: %s", exc)


def _crossed_80_percent(used_before: float, used_after: float, budget: Optional[float]) -> bool:
    if not budget or budget <= 0:
        return False
    threshold = 0.8 * budget
    return used_before < threshold <= used_after


# ── Verification gate before push ────────────────────────────────────────────

MAX_VERIFY_ATTEMPTS = 3
_OVERRIDE_MARK = "OVERRIDE-TASK:"


def _verify_attempts(run_id: str) -> int:
    return sum(1 for step in get_steps(run_id)
               if step["phase"] == "verify" and step["status"] == "failed")


def _override_task_id(run_id: str) -> Optional[str]:
    for step in reversed(get_steps(run_id)):
        if step["phase"] == "verify" and _OVERRIDE_MARK in step["summary"]:
            return step["summary"].split(_OVERRIDE_MARK, 1)[1].strip().split()[0]
    return None


def _remember_verification(run: Dict[str, Any], verdict: str, detail: str) -> None:
    """Future runs learn from past verification outcomes via project memory."""
    try:
        from backend.autonomy import remember_project_entry
        remember_project_entry(
            "verification",
            f"Dev-run {run['id']}: {verdict}",
            f"Goal: {run['goal'][:300]}\nVerdict: {verdict}\n{detail[:800]}",
            source="dev_runs",
        )
    except Exception as exc:
        logger.warning("Could not persist verification memory for %s: %s", run["id"], exc)


async def _gate_push(run: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    """Mandatory dev_run_tests before any git_push. Returns (allowed, run).

    A failed verification blocks the push and feeds the test output back into
    the run context (as a failed verify step). After MAX_VERIFY_ATTEMPTS
    failures the run escalates to awaiting_approval with an R3 override task;
    once the owner approves that task, the next push attempt is allowed through.
    """
    override_id = _override_task_id(run["id"])
    if override_id:
        from backend.control_plane import get_task
        task = get_task(override_id)
        if task and task["status"] == "approved":
            add_step(run["id"], "verify", "dev_run_tests",
                     f"Owner override approved ({override_id}); push permitted despite failing tests.")
            _remember_verification(run, "owner-override",
                                   f"Push allowed by approved Control Plane task {override_id}. "
                                   "Residual risk: tests were still failing at override time.")
            return True, run

    result_raw = await asyncio.to_thread(
        execute_governed_tool, "dev_run_tests", {"runner": "auto"}, f"dev-run:{run['id']}"
    )
    try:
        result = json.loads(result_raw)
    except (TypeError, ValueError):
        result = {"error": str(result_raw)[:300]}
    failed = bool(result.get("error")) or result.get("exit_code") not in (0, None) or result.get("timed_out")
    if not failed:
        add_step(run["id"], "verify", "dev_run_tests", "Verification passed; push permitted.")
        _remember_verification(run, "passed", f"Test runner: {result.get('runner', 'auto')}.")
        await _emit_event(run["id"], run["status"], "Verification passed; pushing.", "phase_done")
        return True, run

    detail = (result.get("error") or result.get("stderr") or result.get("stdout") or "unknown failure")
    attempts = _verify_attempts(run["id"]) + 1
    add_step(run["id"], "verify", "dev_run_tests",
             f"Verification failed (attempt {attempts}/{MAX_VERIFY_ATTEMPTS}): {detail}"[:1000],
             "failed")
    if attempts >= MAX_VERIFY_ATTEMPTS:
        from backend.control_plane import create_review_task
        review = create_review_task(
            goal=f"Dev-run {run['id']}: tests still failing after {attempts} fix attempts — owner override required for push",
            arguments={"run_id": run["id"], "goal": run["goal"][:300], "last_failure": str(detail)[:500]},
            risk_class="R3",
            acceptance=["Owner reviewed the failing tests and explicitly accepts pushing anyway"],
            rollback="Reject this task and let the run keep fixing tests, or cancel the run.",
            requester=f"dev-run:{run['id']}",
        )
        add_step(run["id"], "verify", "dev_run_tests",
                 f"Escalated to Control Plane. {_OVERRIDE_MARK} {review['id']}", "escalated")
        _remember_verification(run, "escalated",
                              f"3 verification attempts failed; override task {review['id']} created. "
                              f"Last failure: {str(detail)[:300]}")
        run = update_run(run["id"], status="awaiting_approval",
                         status_reason=f"Verification failed {attempts}x; owner override task {review['id']}")
        await _emit_event(run["id"], "awaiting_approval",
                          f"Tests failing after {attempts} attempts; override task {review['id']} awaits owner.",
                          "awaiting_approval")
        return False, run
    return False, get_run(run["id"])  # type: ignore[return-value]


# ── Executor loop ────────────────────────────────────────────────────────────

def _tool_schemas() -> List[Dict[str, Any]]:
    from backend.tools import TOOLS_SCHEMA
    return [schema for schema in TOOLS_SCHEMA
            if schema.get("function", {}).get("name") in DEV_RUN_TOOLS]


def _build_messages(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps = get_steps(run["id"])[-HISTORY_STEPS_IN_CONTEXT:]
    history_lines = [
        f"{step['seq']}. [{step['phase']}{'/' + step['tool'] if step['tool'] else ''} "
        f"{step['status']}] {step['summary']}"
        for step in steps
    ]
    history = "\n".join(history_lines) or "(no steps executed yet)"
    user = (
        f"Goal:\n{run['goal']}\n\n{_plan_context(run.get('plan_id'))}\n\n"
        f"Executed steps so far:\n{history}\n\n"
        f"Iterations used: {run['iter_used']}/{run['iter_budget']}.\n"
        "Decide the single next action and call the corresponding tool, or finish "
        "with 'DONE:'/'BLOCKED:' as instructed."
    )
    return [
        {"role": "system", "content": EXECUTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _check_gates(run: Dict[str, Any]) -> Optional[str]:
    """Returns a pause reason when a gate trips, else None."""
    state = get_control_state()
    if state.get("kill_switch"):
        return f"Kill switch engaged: {state.get('reason') or 'owner request'}"
    if run["iter_used"] >= run["iter_budget"]:
        return f"Iteration budget exhausted ({run['iter_used']}/{run['iter_budget']})"
    if run["cost_budget"] is not None and run["cost_used"] >= run["cost_budget"]:
        return f"Cost budget exhausted (${run['cost_used']:.4f}/${run['cost_budget']:.4f})"
    if run["wall_deadline"]:
        try:
            deadline = datetime.fromisoformat(run["wall_deadline"])
            if datetime.now(timezone.utc) >= deadline:
                return f"Wall-clock deadline passed ({run['wall_deadline']})"
        except ValueError:
            pass
    return None


def _llm_config() -> Dict[str, Any]:
    from backend.agent import agent_instance
    return {
        "api_base": agent_instance.api_base,
        "api_key": agent_instance.api_key,
        "model": agent_instance.model,
        "provider_options": {
            "provider": agent_instance.provider,
            "num_ctx": agent_instance.ollama_num_ctx,
            "keep_alive": agent_instance.ollama_keep_alive,
            "think": agent_instance.ollama_think,
        },
    }


async def _iterate(run: Dict[str, Any]) -> Dict[str, Any]:
    """One plan→act→observe iteration. Returns the refreshed run row."""
    reason = _check_gates(run)
    if reason:
        logger.info("Dev-run %s paused: %s", run["id"], reason)
        return update_run(run["id"], status="paused", status_reason=reason)

    config = _llm_config()
    response = await call_llm_normalized(
        api_base=config["api_base"],
        api_key=config["api_key"],
        model=config["model"],
        messages=_build_messages(run),
        tools=_tool_schemas(),
        temperature=0.2,
        provider_options=config["provider_options"],
    )
    cost_delta = float(response.usage.cost or 0.0)
    iter_before, cost_before = run["iter_used"], run["cost_used"]
    run = update_run(
        run["id"],
        iter_used=iter_before + 1,
        cost_used=cost_before + cost_delta,
    )
    if _crossed_80_percent(iter_before, run["iter_used"], run["iter_budget"]) or \
            _crossed_80_percent(cost_before, run["cost_used"], run["cost_budget"]):
        await _emit_event(run["id"], run["status"],
                          f"Budget 80% reached: iterations {run['iter_used']}/{run['iter_budget']}, "
                          f"cost ${run['cost_used']:.4f}", "budget_80")

    if response.has_tool_calls:
        call = response.tool_calls[0]
        function = call.get("function", {}) if isinstance(call, dict) else {}
        tool_name = function.get("name", "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                arguments = {}
        except (TypeError, ValueError):
            arguments = {}
        if tool_name not in DEV_RUN_TOOLS:
            add_step(run["id"], "act", tool_name, "Rejected: tool not allowed in dev-runs", "failed")
            return get_run(run["id"])  # type: ignore[return-value]
        if tool_name == "git_push":
            allowed, run = await _gate_push(run)
            if not allowed:
                return run
        result_raw = await asyncio.to_thread(
            execute_governed_tool, tool_name, arguments, f"dev-run:{run['id']}"
        )
        try:
            result = json.loads(result_raw)
        except (TypeError, ValueError):
            result = {"raw": str(result_raw)[:300]}
        if isinstance(result, dict) and result.get("status") == "awaiting_approval":
            add_step(run["id"], "act", tool_name,
                     f"Queued in Control Plane as {result.get('task_id')} ({result.get('risk_class')})",
                     "awaiting_approval")
            run = update_run(run["id"], status="awaiting_approval",
                             status_reason=f"Control Plane approval required: {result.get('task_id')}")
            await _emit_event(run["id"], "awaiting_approval",
                              f"Tool {tool_name} requires owner approval ({result.get('task_id')})",
                              "awaiting_approval")
            return run
        error = result.get("error") if isinstance(result, dict) else None
        summary = str(error) if error else json.dumps(result, ensure_ascii=False)[:400]
        add_step(run["id"], "act", tool_name, summary[:400], "failed" if error else "done")
        return get_run(run["id"])  # type: ignore[return-value]

    text = (response.content or "").strip()
    if text.startswith("DONE:"):
        add_step(run["id"], "observe", "", text[:1000])
        run = update_run(run["id"], status="done", status_reason="")
        await _emit_event(run["id"], "done", text, "done")
        return run
    if text.startswith("BLOCKED:"):
        add_step(run["id"], "observe", "", text[:1000], "failed")
        run = update_run(run["id"], status="failed", status_reason=text[:500])
        await _emit_event(run["id"], "failed", text, "failed")
        return run
    if not response.is_success:
        add_step(run["id"], "observe", "",
                 f"LLM error: {response.status} {response.error_message or ''}"[:400], "failed")
        return update_run(run["id"], status="paused",
                          status_reason=f"LLM failure: {response.status}")
    add_step(run["id"], "observe", "", (text or "(empty model reply)")[:400])
    return get_run(run["id"])  # type: ignore[return-value]


async def process_run(run_id: str, max_iterations: Optional[int] = None) -> Dict[str, Any]:
    """Drives one run until it leaves the active statuses (or max_iterations)."""
    run = get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] == "planned":
        try:
            run = await _plan(run)
        except Exception as exc:
            logger.exception("Dev-run %s planning failed", run_id)
            return update_run(run_id, status="failed", status_reason=f"Planning failed: {exc}"[:500])
    iterations = 0
    while run["status"] == "running":
        if max_iterations is not None and iterations >= max_iterations:
            break
        try:
            run = await _iterate(run)
        except Exception as exc:
            logger.exception("Dev-run %s iteration crashed", run_id)
            return update_run(run_id, status="paused", status_reason=f"Iteration error: {exc}"[:500])
        iterations += 1
    return run


async def worker_loop() -> None:
    """Background poller started from the app lifespan. Picks up planned/running
    runs (including ones interrupted by a restart) and drives them serially."""
    logger.info("Dev-runs worker started (poll every %.1fs).", POLL_SECONDS)
    while True:
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT id FROM dev_runs WHERE status IN ('planned', 'running') "
                    "ORDER BY created_at LIMIT 1"
                ).fetchone()
            if row:
                await process_run(row["id"])
        except asyncio.CancelledError:
            logger.info("Dev-runs worker stopped.")
            raise
        except Exception:
            logger.exception("Dev-runs worker iteration failed")
        await asyncio.sleep(POLL_SECONDS)
