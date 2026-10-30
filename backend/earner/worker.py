"""The Earner's single sequential worker.

One loop, one job at a time: a queued research run, then a queued build, then
a queued execution. Each job is claimed by compare-and-set on its status, so a
second process (or a restart racing the old one) cannot run it twice. Before a
stage does anything it re-checks the content hash its approval was bound to.

There are no retries: a failed stage stays failed with its reason, and a stage
caught mid-way by a restart becomes ``interrupted`` (store.reconcile_interrupted)
— the operator decides whether it is worth a new approval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

import time

from backend.earner import approvals, budget, llm, repo, sandbox, scout, stages, store, submit, wallet

logger = logging.getLogger("hermes.earner.worker")

QUEUED_TTL = timedelta(hours=6)


def _notify(message: str) -> None:
    """Into the dashboard's own activity feed — never an external channel."""
    try:
        from backend.activity_logger import log_activity

        log_activity("earner", "Autonomous Earner", message)
    except Exception:
        logger.debug("activity log unavailable", exc_info=True)


def _approval_hash(connection, approval_id: str) -> Optional[Dict[str, Any]]:
    return store.row_dict(connection.execute(
        "SELECT * FROM earner_approvals WHERE id = ?", (approval_id,)).fetchone())


# ── claiming ─────────────────────────────────────────────────────────────────

def claim_next() -> Optional[Dict[str, Any]]:
    with store.tx() as connection:
        run = connection.execute(
            "SELECT id, created_at FROM earner_runs WHERE status = ? ORDER BY created_at LIMIT 1",
            (store.RUN_QUEUED,)).fetchone()
        if run:
            if store.parse_iso(run["created_at"]) < store.now() - QUEUED_TTL:
                connection.execute("UPDATE earner_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                                   (store.RUN_FAILED, "Не начато вовремя — подтверждение устарело", store.iso(), run["id"]))
                return None
            cursor = connection.execute(
                "UPDATE earner_runs SET status = ?, started_at = ? WHERE id = ? AND status = ?",
                (store.RUN_RUNNING, store.iso(), run["id"], store.RUN_QUEUED))
            if cursor.rowcount == 1:
                return {"kind": "research", "id": run["id"]}
        for queued, running, kind in ((store.QUEUED_BUILD, store.BUILDING, "build"),
                                      (store.QUEUED_EXECUTION, store.EXECUTING, "execute")):
            row = connection.execute(
                "SELECT id, updated_at FROM earner_opportunities WHERE status = ? ORDER BY updated_at LIMIT 1",
                (queued,)).fetchone()
            if not row:
                continue
            if store.parse_iso(row["updated_at"]) < store.now() - QUEUED_TTL:
                store.set_opportunity(connection, row["id"], status=store.FAILED,
                                      error="Не начато вовремя — подтверждение устарело, запросите новое")
                return None
            if store.transition(connection, row["id"], queued, running, stage_started_at=store.iso()):
                return {"kind": kind, "id": row["id"]}
    return None


# ── research ─────────────────────────────────────────────────────────────────

async def run_research(run_id: str, fetch=None) -> None:
    try:
        await _run_research(run_id, fetch)
    except Exception as exc:
        logger.exception("Earner research run crashed")
        _finish_run(run_id, store.RUN_FAILED, error=f"Внутренняя ошибка: {type(exc).__name__}")


async def _run_research(run_id: str, fetch=None) -> None:
    with store.read() as connection:
        run = store.get_run(run_id, connection)
        approval = _approval_hash(connection, run["approval_id"])
    plan = run["plan"]
    if (approval is None or approval["status"] != approvals.CONSUMED
            or store.canonical_hash(plan) != run["plan_hash"]
            or (approval["action"] == "research" and approval["content_hash"] != run["plan_hash"])):
        _finish_run(run_id, store.RUN_FAILED, error="План исследования не совпадает с подтверждённым — отменено")
        return
    try:
        candidates, report, fetches = await asyncio.to_thread(scout.collect, plan, fetch or scout.egress.fetch)
        counts = await asyncio.to_thread(scout.store_candidates, run_id, candidates)
    except Exception as exc:
        logger.exception("Earner scout failed")
        _finish_run(run_id, store.RUN_FAILED, error=f"Scout: {type(exc).__name__}")
        return
    with store.tx() as connection:
        connection.execute(
            "UPDATE earner_runs SET sources_report = ?, candidates_new = ?, candidates_seen = ?, fetches = ? WHERE id = ?",
            (store.dumps(report), counts["new"], counts["seen"], fetches, run_id))
        for entry in report:
            if not entry["ok"]:
                store.log_event(connection, "run", run_id, "source_unavailable",
                                f"Источник {entry['source_id']} недоступен: {entry.get('error', '')}", entry)

    evaluated = proposed = 0
    error = ""
    settings = {"directions": plan.get("directions") or [], "constraints": plan.get("constraints") or ""}
    source_ids = [source["id"] for source in plan["sources"]]
    with store.read() as connection:
        pool = [store.row_dict(row) for row in connection.execute(
            f"SELECT id, title, labels, excerpt, is_demo, created_at FROM earner_opportunities WHERE status = ? "
            f"AND source_id IN ({','.join('?' * len(source_ids))}) ORDER BY created_at DESC LIMIT 200",
            (store.NEW, *source_ids)).fetchall()]
    # Best-matching first; a candidate with no profile match is left unevaluated
    # (status stays "new") instead of spending a model call to reject it.
    scored = sorted(((stages.relevance(row, settings["directions"]) + (100 if row["is_demo"] else 0), row)
                     for row in pool), key=lambda pair: (-pair[0], pair[1]["created_at"]), reverse=False)
    rows = [row for score, row in scored if score > 0][: int(plan["limits"]["max_evaluations_per_run"])]
    for row in rows:
        outcome = await evaluate_one(row["id"], settings)
        if outcome == "stop":
            error = "Оценка остановлена: лимит бюджета или недоступна модель (см. события)"
            break
        if outcome in ("proposed", "rejected"):
            evaluated += 1
        if outcome == "proposed":
            proposed += 1
    with store.tx() as connection:
        connection.execute("UPDATE earner_runs SET evaluated = ?, proposed = ? WHERE id = ?",
                           (evaluated, proposed, run_id))
    _finish_run(run_id, store.RUN_DONE, error=error)
    if proposed:
        _notify(f"Earner: {proposed} предложение(й) ждут вашего решения")


async def evaluate_one(opp_id: str, settings: Dict[str, Any]) -> str:
    with store.tx() as connection:
        if not store.transition(connection, opp_id, store.NEW, store.EVALUATING, stage_started_at=store.iso()):
            return "skipped"
        opp = store.get_opportunity(opp_id, connection)
    refusal = stages.precheck(opp)
    if refusal:
        _set_after(opp_id, store.EVALUATING, store.NOT_FIT, reject_reason=refusal,
                   event=("evaluated", f"Отклонено без вызова модели: {refusal}"))
        return "rejected"
    try:
        proposal, meta = await stages.evaluate(opp, settings,
                                               fallback_external=store.get_settings()["external_enabled"])
    except budget.BudgetExceeded as exc:
        _set_after(opp_id, store.EVALUATING, store.NEW, event=("budget_exceeded", str(exc)))
        return "stop"
    except llm.InferenceUnavailable as exc:
        _set_after(opp_id, store.EVALUATING, store.NEW, event=("inference_unavailable", str(exc)))
        return "stop"
    except (llm.ModelOutputError, stages.StageRejected) as exc:
        _set_after(opp_id, store.EVALUATING, store.FAILED, error=f"Evaluator: {exc}",
                   event=("stage_failed", f"Evaluator: {exc}"))
        return "failed"
    except Exception as exc:
        logger.exception("Earner evaluator crashed")
        _set_after(opp_id, store.EVALUATING, store.FAILED, error=f"Evaluator: внутренняя ошибка {type(exc).__name__}",
                   event=("stage_failed", f"Evaluator: {type(exc).__name__}"))
        return "failed"
    if proposal["decision"] != "propose":
        _set_after(opp_id, store.EVALUATING, store.NOT_FIT, evaluation=proposal,
                   reject_reason=proposal["reject_reason"] or "Не подходит",
                   event=("evaluated", f"Evaluator отклонил: {proposal['reject_reason'][:200]}"))
        return "rejected"
    proposal_hash = store.canonical_hash(proposal)
    _set_after(opp_id, store.EVALUATING, store.PROPOSED, evaluation={"model": meta["model"]}, proposal=proposal,
               proposal_hash=proposal_hash, event=("proposed", "Подготовлено предложение — нужно решение оператора"))
    try:
        approvals.request("generate", opp_id, "system:evaluator")
    except approvals.ApprovalError as exc:
        store.log_event(None, "opportunity", opp_id, "approval_not_created", exc.detail)
    return "proposed"


def _set_after(opp_id: str, expected: str, new_status: str, event=None, **fields: Any) -> bool:
    with store.tx() as connection:
        moved = store.transition(connection, opp_id, expected, new_status, **fields)
        if moved and event:
            store.log_event(connection, "opportunity", opp_id, event[0], event[1])
    return moved


def _finish_run(run_id: str, status: str, error: str = "") -> None:
    with store.tx() as connection:
        connection.execute("UPDATE earner_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                           (status, error[:500], store.iso(), run_id))
        store.log_event(connection, "run", run_id, status,
                        "Исследование завершено" if status == store.RUN_DONE else f"Исследование: {error}")


# ── build and review ─────────────────────────────────────────────────────────

def _grant_matches(opp: Dict[str, Any], field: str, content_key: str) -> bool:
    with store.read() as connection:
        approval = _approval_hash(connection, opp["grant_id"])
    if approval is None or approval["status"] != approvals.CONSUMED or approval["target_id"] != opp["id"]:
        return False
    return (approval["content"] or {}).get(content_key) == opp[field]


async def run_build(opp_id: str) -> None:
    opp = store.get_opportunity(opp_id)
    if not _grant_matches(opp, "proposal_hash", "proposal_hash") \
            or store.canonical_hash(opp["proposal"]) != opp["proposal_hash"]:
        _set_after(opp_id, store.BUILDING, store.FAILED,
                   error="Предложение изменилось после подтверждения — генерация отменена",
                   event=("grant_mismatch", "Содержимое не совпадает с подтверждённым"))
        return
    # What the external model may do comes from the approval the operator
    # confirmed — not from whatever the settings say now.
    with store.read() as connection:
        grant = _approval_hash(connection, opp["grant_id"])
    external = ((grant or {}).get("content") or {}).get("external") or {"enabled": False}
    attempts = int(external.get("max_attempts", 1)) if external.get("enabled") else 1
    fallback = bool(external.get("enabled"))
    repo_mode = stages.delivery_mode(opp["proposal"]) == stages.REPO_PATCH
    if repo_mode:
        prepared = await _prepare_repo(opp, fallback)
        if prepared is None:
            return

        def produce(provider, feedback):
            return stages.build_repo(opp, prepared, provider=provider, fallback_external=fallback, feedback=feedback)
    else:
        def produce(provider, feedback):
            return stages.build(opp, provider=provider, fallback_external=fallback, feedback=feedback)
    feedback = None
    review: Dict[str, Any] = {}
    attempt = 0
    in_review = False
    while attempt < attempts:
        attempt += 1
        provider = "external" if attempt > 1 or external.get("build_provider") == "external" else "local"
        if in_review and not _set_after(opp_id, store.REVIEWING, store.BUILDING, stage_started_at=store.iso(),
                                        event=("retry", "Reviewer отклонил — повторная генерация внешней моделью")):
            return
        in_review = False
        try:
            artifact, _ = await produce(provider, feedback)
        except stages.StageRejected as exc:
            if repo_mode and attempt < attempts:
                # A diff that does not apply is worth one more try with the
                # exact reason; a module that fails static checks is not.
                store.log_event(None, "opportunity", opp_id, "retry",
                                f"diff не принят ({exc}) — повторная генерация внешней моделью")
                feedback = {"diff_error": str(exc)}
                continue
            _set_after(opp_id, store.BUILDING, store.FAILED, error=f"Builder (попытка {attempt}): {exc}",
                       event=("stage_failed", f"Builder (попытка {attempt}): {exc}"))
            return
        except (budget.BudgetExceeded, llm.InferenceUnavailable, llm.ModelOutputError,
                llm.ExternalUnavailable) as exc:
            _set_after(opp_id, store.BUILDING, store.FAILED, error=f"Builder (попытка {attempt}): {exc}",
                       event=("stage_failed", f"Builder (попытка {attempt}): {exc}"))
            return
        except Exception as exc:
            logger.exception("Earner builder crashed")
            _set_after(opp_id, store.BUILDING, store.FAILED, error=f"Builder: внутренняя ошибка {type(exc).__name__}",
                       event=("stage_failed", f"Builder: {type(exc).__name__}"))
            return
        artifact["attempt"] = attempt
        artifact_hash = store.artifact_hash(artifact)
        built = (f"Builder ({artifact.get('provider')}, попытка {attempt}) подготовил diff: "
                 f"{artifact['static']['files_changed']} файл(ов), +{artifact['static']['added']}/-{artifact['static']['removed']}"
                 if repo_mode else
                 f"Builder ({artifact.get('provider')}, попытка {attempt}) создал "
                 f"{len(artifact['files'])} файл(ов), тестов: {artifact['static']['test_count']}")
        if not _set_after(opp_id, store.BUILDING, store.REVIEWING, artifact=artifact, artifact_hash=artifact_hash,
                          stage_started_at=store.iso(), event=("built", built)):
            return
        in_review = True
        opp = store.get_opportunity(opp_id)
        try:
            review, _ = await stages.review(opp, fallback_external=fallback)
        except (budget.BudgetExceeded, llm.InferenceUnavailable, llm.ModelOutputError, llm.ExternalUnavailable,
                stages.StageRejected) as exc:
            _set_after(opp_id, store.REVIEWING, store.FAILED, error=f"Reviewer: {exc}",
                       event=("stage_failed", f"Reviewer: {exc}"))
            return
        except Exception as exc:
            logger.exception("Earner reviewer crashed")
            _set_after(opp_id, store.REVIEWING, store.FAILED, error=f"Reviewer: внутренняя ошибка {type(exc).__name__}",
                       event=("stage_failed", f"Reviewer: {type(exc).__name__}"))
            return
        review["attempt"] = attempt
        if review["accepted"]:
            break
        if attempt == attempts:
            _set_after(opp_id, store.REVIEWING, store.REVIEW_REJECTED, review=review,
                       reject_reason=review["summary"] or "Reviewer отклонил артефакт",
                       event=("reviewed", f"Reviewer отклонил артефакт (попытка {attempt})"))
            return
        with store.tx() as connection:
            store.set_opportunity(connection, opp_id, review=review)
        feedback = {"criteria": review["criteria"], "quality_issues": review["quality_issues"],
                    "risks": review["risks"], "summary": review["summary"]}
    if not in_review:
        _set_after(opp_id, store.BUILDING, store.FAILED, error="Builder: diff не принят ни в одной попытке",
                   event=("stage_failed", "Builder: diff не принят"))
        return
    _set_after(opp_id, store.REVIEWING, store.AWAITING_EXECUTION, review=review,
               event=("reviewed", "Reviewer одобрил — решение об исполнении за оператором"))
    state = sandbox.repo_availability() if repo_mode else sandbox.availability()
    if state["available"]:
        try:
            approvals.request("execute", opp_id, "system:reviewer")
        except approvals.ApprovalError as exc:
            store.log_event(None, "opportunity", opp_id, "approval_not_created", exc.detail)
    else:
        store.log_event(None, "opportunity", opp_id, "execution_unavailable",
                        f"Исполнение недоступно: {state['reason']}")
    _notify("Earner: артефакт прошёл ревью и ждёт решения об исполнении")


async def _prepare_repo(opp: Dict[str, Any], fallback: bool) -> Optional[Dict[str, Any]]:
    """Snapshot, policy, test plan, locator and context — everything the
    repository Builder needs. Returns None after failing the stage."""
    opp_id = opp["id"]

    def fail(message: str, kind: str = "stage_failed") -> None:
        _set_after(opp_id, store.BUILDING, store.FAILED, error=message, event=(kind, message))

    parsed = repo.parse_issue_url(opp["url"])
    if not parsed:
        fail("Режим репозитория нужен только для GitHub issue")
        return None
    owner, name, _number = parsed
    try:
        with store.tx() as connection:
            budget.reserve(connection, "fetches", 3)
        snapshot, data = await asyncio.to_thread(repo.fetch_snapshot, owner, name)
        tree = await asyncio.to_thread(repo.load_tree, data)
    except budget.BudgetExceeded as exc:
        fail(f"Снимок репозитория: {exc}")
        return None
    except (repo.RepoError, scout.egress.EgressError) as exc:
        fail(f"Снимок репозитория: {exc}")
        return None
    store.log_event(None, "opportunity", opp_id, "snapshot",
                    f"Снимок {owner}/{name}@{snapshot['commit'][:10]}: {len(tree.files)} файлов, "
                    f"{snapshot['tarball_bytes'] // 1024} КБ архива", {"commit": snapshot["commit"]})
    if not repo.is_python_repo(tree, snapshot):
        fail(f"Репозиторий не на Python ({snapshot['language'] or 'язык не указан'}) — режим патча поддерживает только Python")
        return None
    policy = repo.ai_policy(tree)
    if policy["status"] == "forbidden":
        quote = policy["evidence"][0] if policy["evidence"] else {"path": "?", "text": ""}
        fail(f"Репозиторий не принимает вклад, созданный ИИ ({quote['path']}: «{quote['text'][:160]}») — "
             "генерация остановлена до вызова модели", "ai_policy_forbidden")
        return None
    plan = repo.test_plan(tree)
    picked: list = []
    try:
        picked, _ = await stages.locate(opp, tree, fallback_external=fallback)
    except (llm.ModelOutputError, stages.StageRejected) as exc:
        store.log_event(None, "opportunity", opp_id, "locator_skipped",
                        f"Locator не ответил корректно ({exc}) — файлы выбраны по тексту задачи")
    except (budget.BudgetExceeded, llm.InferenceUnavailable, llm.ExternalUnavailable) as exc:
        fail(f"Locator: {exc}")
        return None
    context = repo.select_context(tree, stages._issue_text(opp), picked)
    if not context["files"]:
        fail("Не нашлось файлов репозитория, связанных с задачей")
        return None
    return {"snapshot": snapshot, "tree": tree, "test_plan": plan, "ai_policy": policy, "context": context,
            "picked": picked}


# ── execution ────────────────────────────────────────────────────────────────

def _acquire_slot(holder: str) -> bool:
    with store.tx() as connection:
        cursor = connection.execute(
            "UPDATE earner_sandbox_slot SET holder = ?, acquired_at = ? WHERE id = 1 AND holder IS NULL",
            (holder, store.iso()))
        return cursor.rowcount == 1


def _release_slot(holder: str) -> None:
    with store.tx() as connection:
        connection.execute("UPDATE earner_sandbox_slot SET holder = NULL, acquired_at = NULL "
                           "WHERE id = 1 AND holder = ?", (holder,))


async def run_execution(opp_id: str) -> None:
    opp = store.get_opportunity(opp_id)
    artifact = opp["artifact"] or {}
    files = artifact.get("files") or []
    if not _grant_matches(opp, "artifact_hash", "artifact_hash") or store.artifact_hash(artifact) != opp["artifact_hash"]:
        _set_after(opp_id, store.EXECUTING, store.FAILED,
                   error="Артефакт изменился после подтверждения — исполнение отменено",
                   event=("grant_mismatch", "Содержимое не совпадает с подтверждённым"))
        return
    repo_mode = artifact.get("mode") == "repo"
    tarball = b""
    if repo_mode:
        try:
            tarball = await asyncio.to_thread(repo.load_blob, artifact["repo"]["tarball_sha256"])
            tree = await asyncio.to_thread(repo.load_tree, tarball)
        except repo.RepoError as exc:
            _set_after(opp_id, store.EXECUTING, store.FAILED, error=f"Снимок репозитория: {exc}",
                       event=("stage_failed", f"Снимок репозитория: {exc}"))
            return
        if not repo.overlay_matches(tree, artifact["diff"], files):
            _set_after(opp_id, store.EXECUTING, store.FAILED,
                       error="Файлы артефакта не совпадают с diff на снимке — исполнение отменено",
                       event=("grant_mismatch", "diff и файлы расходятся"))
            return
    if not _acquire_slot(opp_id):
        _set_after(opp_id, store.EXECUTING, store.FAILED, error="Sandbox занят другим запуском",
                   event=("stage_failed", "Sandbox занят"))
        return
    try:
        if repo_mode:
            meta = sandbox.repo_meta(artifact["test_plan"], files, artifact["repo"]["tarball_sha256"])
            result = await asyncio.to_thread(sandbox.run_repo, tarball, meta, opp_id)
        else:
            result = await asyncio.to_thread(sandbox.run, files, opp_id)
    except sandbox.SandboxUnavailable as exc:
        _set_after(opp_id, store.EXECUTING, store.FAILED, error=f"Исполнение недоступно: {exc}",
                   event=("execution_unavailable", str(exc)))
        return
    except Exception as exc:
        logger.exception("Earner sandbox run failed")
        _set_after(opp_id, store.EXECUTING, store.FAILED, error=f"Sandbox: {type(exc).__name__}",
                   event=("stage_failed", f"Sandbox: {type(exc).__name__}"))
        return
    finally:
        _release_slot(opp_id)
    # A real repository often fails some tests on its own base commit (an
    # optional dependency the image lacks). "no_regressions" — every failing
    # test also fails without the patch — counts as checked, and says so.
    passed = result["status"] in ("passed", "no_regressions")
    _set_after(opp_id, store.EXECUTING, store.CHECKS_PASSED if passed else store.CHECKS_FAILED, execution=result,
               event=("executed", f"Проверки в sandbox: {result['status']}"))
    _notify(f"Earner: проверки в sandbox завершены ({result['status']})")


# ── schedule ─────────────────────────────────────────────────────────────────

def maybe_schedule_run() -> Optional[str]:
    with store.tx() as connection:
        row = store.row_dict(connection.execute(
            "SELECT * FROM earner_schedule WHERE id = 1 AND active = 1").fetchone())
        if not row:
            return None
        now = store.now()
        if store.parse_iso(row["valid_until"]) <= now or row["runs_done"] >= row["max_runs"]:
            connection.execute("UPDATE earner_schedule SET active = 0, status_reason = ? WHERE id = 1",
                               ("Срок или число запусков исчерпаны",))
            store.log_event(connection, "schedule", "schedule", "schedule_finished", "Автопоиск завершён по сроку")
            return None
        if store.parse_iso(row["next_run_at"]) > now:
            return None
        current, _ = approvals.compute(connection, "research", "settings")
        if store.canonical_hash(current) != store.canonical_hash(row["plan"]):
            connection.execute("UPDATE earner_schedule SET active = 0, status_reason = ? WHERE id = 1",
                               ("Настройки изменились после разрешения",))
            return None
        next_run = store.iso(now + timedelta(hours=row["interval_hours"]))
        try:
            budget.reserve(connection, "tasks")
        except budget.BudgetExceeded as exc:
            connection.execute("UPDATE earner_schedule SET next_run_at = ? WHERE id = 1", (next_run,))
            store.log_event(connection, "schedule", "schedule", "budget_exceeded", f"Плановый запуск пропущен: {exc}")
            return None
        run_id = store.new_id("run")
        connection.execute(
            "INSERT INTO earner_runs (id, trigger, status, approval_id, plan, plan_hash, created_at) "
            "VALUES (?, 'schedule', ?, ?, ?, ?, ?)",
            (run_id, store.RUN_QUEUED, row["approval_id"], store.dumps(row["plan"]),
             store.canonical_hash(row["plan"]), store.iso()))
        connection.execute("UPDATE earner_schedule SET runs_done = runs_done + 1, next_run_at = ? WHERE id = 1",
                           (next_run,))
        store.log_event(connection, "run", run_id, "queued", "Плановое исследование поставлено в очередь")
        return run_id


# ── loop ─────────────────────────────────────────────────────────────────────

_autopilot_budget_logged = {"at": 0.0}


def autopilot_confirm() -> int:
    """Let the operator's autopilot grant consume pending generate/execute
    approvals (approvals.confirm_by_autopilot decides what it covers). An
    exhausted budget stops it for this tick; the approval stays pending."""
    confirmed = 0
    for row in approvals.list_approvals(approvals.PENDING, None, 50):
        if row["action"] not in approvals.AUTOPILOT_ACTIONS:
            continue
        try:
            if approvals.confirm_by_autopilot(row["id"]):
                confirmed += 1
        except budget.BudgetExceeded as exc:
            if time.monotonic() - _autopilot_budget_logged["at"] > 3600:
                _autopilot_budget_logged["at"] = time.monotonic()
                store.log_event(None, "schedule", "schedule", "autopilot_budget", f"Автопилот ждёт бюджета: {exc}")
            break
        except approvals.ApprovalError:
            continue
    return confirmed


_last_wallet_scan = {"at": 0.0}
WALLET_SCAN_SECONDS = 300


async def maybe_scan_wallet() -> None:
    """Read-only: new USDC/USDT transfers to the operator's public address."""
    if time.monotonic() - _last_wallet_scan["at"] < WALLET_SCAN_SECONDS:
        return
    _last_wallet_scan["at"] = time.monotonic()
    if not store.get_settings()["wallet"]["address"]:
        return
    results = await asyncio.to_thread(wallet.scan_all)
    if any(result.get("new") for result in results):
        _notify("Earner: на кошелёк пришло поступление USDC/USDT")


async def tick() -> Optional[Dict[str, Any]]:
    # Reading the wallet and sending a comment the operator already approved
    # are not new work, so they also run while new stages are paused.
    try:
        await maybe_scan_wallet()
    except Exception:
        logger.exception("Earner wallet scan failed")
    queued = await asyncio.to_thread(submit.claim_queued)
    if queued:
        await asyncio.to_thread(submit.send, queued)
        return {"kind": "submit", "id": queued}
    if not store.get_settings()["enabled"]:
        return None
    await asyncio.to_thread(approvals.expire_stale)
    await asyncio.to_thread(autopilot_confirm)
    try:
        await asyncio.to_thread(maybe_schedule_run)
    except approvals.ApprovalError as exc:
        logger.info("Scheduled research skipped: %s", exc.detail)
    job = await asyncio.to_thread(claim_next)
    if not job:
        return None
    if job["kind"] == "research":
        await run_research(job["id"])
    elif job["kind"] == "build":
        await run_build(job["id"])
    else:
        await run_execution(job["id"])
    return job


async def worker_loop(interval_seconds: float = 5.0) -> None:
    await asyncio.to_thread(store.init_schema)
    counts = await asyncio.to_thread(store.reconcile_interrupted)
    if counts["opportunities"] or counts["runs"]:
        logger.warning("Earner: marked %s stage(s) and %s run(s) interrupted", counts["opportunities"], counts["runs"])
    removed = await asyncio.to_thread(sandbox.cleanup_orphans)
    if removed:
        logger.warning("Earner: removed %d orphaned sandbox container(s)", removed)
    try:
        await asyncio.to_thread(repo.prune_blobs)
    except OSError:
        logger.warning("Earner: could not prune old repository snapshots", exc_info=True)
    while True:
        try:
            job = await tick()
        except Exception:
            logger.exception("Earner worker tick failed")
            job = None
        if not job:
            await asyncio.sleep(interval_seconds)
