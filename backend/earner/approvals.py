"""Operator approvals as a server-side state machine.

    pending ──confirm──► consumed        (the work is queued in the same tx)
       │ ├─decline───► declined
       │ ├─TTL───────► expired
       │ ├─new request► superseded
       └─content/settings changed ► invalidated

An approval is bound to its action, its target and the sha256 of the exact
content the operator was shown (``content``). ``confirm`` runs in one
BEGIN IMMEDIATE transaction that:

1. requires status ``pending`` (single use; a replay or a second concurrent
   confirm finds ``consumed`` and gets 409),
2. requires it to be unexpired,
3. requires the hash the operator's screen echoes back to match,
4. recomputes the content *now* from the live rows and requires the same hash
   (a proposal or artifact that changed since it was shown invalidates it),
5. reserves budget, flips the approval to ``consumed`` and queues the stage.

Any failure rolls all of it back. The worker re-checks the bound hash once more
when the stage starts (worker.py), so content changed between confirm and
pickup is refused as well.

Who may call ``confirm`` is decided in api.py: an operator session holding
``earner.approve``. Nothing in this module trusts a caller-supplied identity.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.earner import budget, store

ACTIONS = ("research", "generate", "execute", "schedule", "autopilot", "submit")
SETTINGS_BOUND = ("research", "schedule", "autopilot")
# What an autopilot grant may confirm on the operator's behalf. Research runs
# come from its own schedule; submit (outward text) is never on this list.
AUTOPILOT_ACTIONS = {"generate": "auto_generate", "execute": "auto_execute"}

PENDING, CONSUMED, DECLINED, EXPIRED, SUPERSEDED, INVALIDATED = (
    "pending", "consumed", "declined", "expired", "superseded", "invalidated")

NOT_INCLUDED = [
    "Регистрация на площадках, отклики и сообщения заказчикам",
    "Публикация кода, отправка результата, создание платных ресурсов",
    "Любые финансовые операции",
]


class ApprovalError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def ttl() -> timedelta:
    try:
        minutes = int(os.getenv("EARNER_APPROVAL_TTL_MINUTES", "60"))
    except ValueError:
        minutes = 60
    return timedelta(minutes=max(5, min(minutes, 24 * 60)))


# ── what an approval is about ────────────────────────────────────────────────

def _describe_source(source: Dict[str, Any]) -> str:
    if source["kind"] == "github_issues":
        return f"GitHub Issues (api.github.com), запрос «{source['query']}»"
    if source["kind"] == "rss":
        return f"RSS/Atom-лента {source['url']}"
    if source["kind"] == "freelancer_com":
        return f"Freelancer.com (публичный API), запрос «{source['query']}»"
    if source["kind"] == "freelancehunt":
        return f"Freelancehunt (публичный API), навыки {', '.join(str(i) for i in source['skill_ids'])}"
    if source["kind"] == "opire":
        return "Opire — награды за GitHub-issues (описание задачи читается через api.github.com, если он разрешён)"
    if source["kind"] == "superteam":
        return (f"Superteam Earn Agent API ({store.SUPERTEAM_HOST}): листинги {', '.join(source.get('types') or [])}, "
                "открытые для ИИ-агентов, ключ EARNER_SUPERTEAM_API_KEY")
    return "Демо-источник: встроенная учебная задача, без сети"


def _research_content(connection: sqlite3.Connection) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from backend.earner import llm

    settings = store.get_settings(connection)
    if not settings["enabled"]:
        raise ApprovalError(409, "Earner выключен оператором")
    sources = [source for source in settings["sources"] if source.get("enabled")]
    if not sources:
        raise ApprovalError(400, "Нет ни одного включённого источника")
    model = llm.describe()
    if not model["local"]:
        raise ApprovalError(409, model["reason"])
    limits = settings["limits"]
    run_limits = {key: limits[key] for key in
                  ("max_candidates_per_run", "max_evaluations_per_run", "max_fetches_per_run")}
    content = {
        "action": "research",
        "sources": sources,
        "allowed_hosts": settings["allowed_hosts"],
        "directions": settings["directions"],
        "constraints": settings["constraints"],
        "limits": run_limits,
        "llm": {"host": model["host"], "model": model["model"]},
    }
    network = [s for s in sources if s["kind"] != "demo"]
    summary = {
        "title": f"Исследование: {len(sources)} источн.",
        "will_do": [
            f"Scout запросит только перечисленные источники — не больше {run_limits['max_fetches_per_run']} HTTPS-запросов"
            + (" (сеть не нужна)" if not network else f" к хостам: {', '.join(settings['allowed_hosts'])}"),
            f"Сохранит до {run_limits['max_candidates_per_run']} кандидатов с URL, временем получения и текстом источника",
            f"Evaluator оценит до {run_limits['max_evaluations_per_run']} кандидатов локальной моделью "
            f"{model['model']} (по одному вызову на кандидата) и подготовит предложения",
        ],
        "data": [_describe_source(source) for source in sources]
                + ([f"Направления: {', '.join(settings['directions'])}"] if settings["directions"] else [])
                + ([f"Ограничения: {settings['constraints']}"] if settings["constraints"] else []),
        "limits": _budget_lines(),
        "not_included": ["Генерация кода (отдельное подтверждение)", "Исполнение кода (отдельное подтверждение)",
                         "Платные поисковые сервисы"] + NOT_INCLUDED,
    }
    return content, summary


def _schedule_content(connection: sqlite3.Connection, params: Dict[str, Any],
                      autopilot: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    research, summary = _research_content(connection)
    try:
        interval = int(params.get("interval_hours", 24))
        days = int(params.get("days", 7))
        max_runs = int(params.get("max_runs", 7))
    except (TypeError, ValueError) as exc:
        raise ApprovalError(400, "Параметры расписания должны быть числами") from exc
    max_days, min_interval, top_runs = (30, 2, 120) if autopilot else (14, 6, 60)
    if not min_interval <= interval <= 168 or not 1 <= days <= max_days or not 1 <= max_runs <= top_runs:
        raise ApprovalError(400, f"Расписание: интервал {min_interval}–168 ч, срок 1–{max_days} дней, "
                                 f"не больше {top_runs} запусков")
    valid_until = params.get("valid_until") or store.iso(store.now() + timedelta(days=days))
    schedule = {"interval_hours": interval, "days": days, "max_runs": max_runs, "valid_until": valid_until}
    if autopilot:
        return _autopilot_content(connection, research, summary, schedule, params)
    content = {**research, "action": "schedule", "schedule": schedule}
    summary = {
        **summary,
        "title": f"Автопоиск по расписанию: каждые {interval} ч до {valid_until[:16].replace('T', ' ')} UTC",
        "will_do": [f"Каждые {interval} ч запускать то же исследование, всего не больше {max_runs} раз"]
                   + summary["will_do"],
        "not_included": ["Генерация и исполнение — для каждой задачи отдельно"] + summary["not_included"],
    }
    return content, summary


def external_plan(settings: Dict[str, Any], proposal: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What an approval allows the external model to do. Depends on settings
    and the proposal only (not on OmniRoute being reachable right now), so the
    hash an operator confirms stays stable; reachability is checked per call."""
    from backend.earner import stages

    if not settings["external_enabled"]:
        return {"enabled": False}
    plan = {"enabled": True, "model": settings["external_model"],
            "month_usd": round(float(settings["external_month_usd"]), 2), "fallback": True}
    if proposal is not None:
        plan["build_provider"] = "external" if stages.is_complex(proposal) else "local"
        plan["max_attempts"] = 2
    return plan


def _external_lines(plan: Dict[str, Any]) -> List[str]:
    if not plan.get("enabled"):
        return ["Только локальная модель (внешние выключены)"]
    usage = budget.external_usage()
    lines = [f"Внешняя модель {plan['model']} через OmniRoute: лимит ${plan['month_usd']:.2f}/мес, "
             f"осталось ${usage['remaining_usd']:.2f}"]
    if plan.get("build_provider") == "external":
        lines.append("Задача оценена как сложная — Builder сразу использует внешнюю модель")
    if plan.get("max_attempts", 1) > 1:
        lines.append("Если Reviewer отклонит — одна повторная генерация внешней моделью с его замечаниями")
    lines.append("Внешняя модель — резерв, если локальная недоступна или ответила неверно")
    return lines


def _autopilot_content(connection: sqlite3.Connection, research: Dict[str, Any], summary: Dict[str, Any],
                       schedule: Dict[str, Any], params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    settings = store.get_settings(connection)
    schedule = {**schedule, "auto_generate": bool(params.get("auto_generate", True)),
                "auto_execute": bool(params.get("auto_execute", True))}
    content = {**research, "action": "autopilot", "schedule": schedule, "external": external_plan(settings)}
    limits = settings["limits"]
    auto = [name for flag, name in ((schedule["auto_generate"], "генерацию"),
                                    (schedule["auto_execute"], "исполнение в gVisor-песочнице")) if flag]
    until = schedule["valid_until"][:16].replace("T", " ")
    summary = {
        **summary,
        "title": f"Автопилот до {until} UTC: поиск каждые {schedule['interval_hours']} ч"
                 + (f"; сам подтверждает {', '.join(auto)}" if auto else ""),
        "will_do": [f"Каждые {schedule['interval_hours']} ч запускать исследование — не больше "
                    f"{schedule['max_runs']} раз за срок"]
                   + ([f"Сам подтверждать {', '.join(auto)} для найденных задач — с теми же проверками хеша и "
                       "бюджета, что и при ручном подтверждении; в журнале — от имени этого гранта"] if auto else [])
                   + summary["will_do"],
        "limits": [f"В сутки: задач ≤ {limits['tasks_per_day']}, вызовов модели ≤ {limits['llm_calls_per_day']}, "
                   f"токенов ≤ {limits['tokens_per_day']}, запусков sandbox ≤ {limits['sandbox_runs_per_day']}",
                   "Остановить: кнопка «Остановить Earner» или «Остановить автопилот»; любое изменение "
                   "настроек тоже останавливает его"]
                  + _external_lines(content["external"]) + summary["limits"],
        "not_included": ["Отправка заказчикам — только вашим кликом по каждому тексту",
                         "Любые операции с кошельком (он только принимает)"] + NOT_INCLUDED,
    }
    return content, summary


def _generate_content(connection: sqlite3.Connection, opp_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from backend.earner import llm, stages

    opp = store.get_opportunity(opp_id, connection)
    if opp is None:
        raise ApprovalError(404, "Задача не найдена")
    if opp["status"] != store.PROPOSED:
        raise ApprovalError(409, f"Генерацию можно разрешить только для предложения (сейчас: {opp['status']})")
    model = llm.describe()
    if not model["local"]:
        raise ApprovalError(409, model["reason"])
    proposal = opp["proposal"] or {}
    if store.canonical_hash(proposal) != opp["proposal_hash"]:
        raise ApprovalError(409, "Предложение не совпадает со своим хешем — оно было изменено")
    external = external_plan(store.get_settings(connection), proposal)
    if stages.delivery_mode(proposal) == stages.REPO_PATCH:
        return _generate_repo_content(opp, proposal, model, external)
    content = {
        "action": "generate",
        "opportunity_id": opp_id,
        "url": opp["url"],
        "title": opp["title"],
        "excerpt_sha256": store.canonical_hash(opp["excerpt"]),
        "proposal": proposal,
        "proposal_hash": opp["proposal_hash"],
        "builder": {"language": "Python 3.11, только стандартная библиотека",
                    "max_tokens": stages.BUILDER_MAX_TOKENS, "reviewer_max_tokens": stages.REVIEWER_MAX_TOKENS},
        "llm": {"host": model["host"], "model": model["model"]},
        "external": external,
    }
    criteria = proposal.get("acceptance_criteria") or []
    summary = {
        "title": f"Генерация решения: {opp['title'][:120]}",
        "will_do": [
            f"Builder ({model['model']}) напишет решение, тесты и README по предложению: {proposal.get('deliverable', '')[:300]}",
            "Код не исполняется: только синтаксический разбор и проверка импортов",
            f"Reviewer проверит результат по {len(criteria)} критериям приёмки и может его отклонить",
        ],
        "data": [f"Источник: {opp['url']}", "Критерии приёмки:"] + [f"• {item}" for item in criteria],
        "limits": [f"2 вызова модели (Builder ≤ {stages.BUILDER_MAX_TOKENS} токенов ответа, "
                   f"Reviewer ≤ {stages.REVIEWER_MAX_TOKENS}); при повторе — ещё 2",
                   "Считается как 1 задача дневного лимита"]
                  + _external_lines(external) + _budget_lines(),
        "not_included": ["Исполнение кода (отдельное подтверждение конкретного артефакта)"] + NOT_INCLUDED,
    }
    return content, summary


def _generate_repo_content(opp: Dict[str, Any], proposal: Dict[str, Any], model: Dict[str, Any],
                           external: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from backend.earner import repo, stages

    owner, name, number = repo.parse_issue_url(opp["url"]) or ("", "", 0)
    max_mb = repo.max_tarball_bytes() // (1024 * 1024)
    context_kb = repo.context_budget() // 1024
    content = {
        "action": "generate",
        "delivery": stages.REPO_PATCH,
        "opportunity_id": opp["id"],
        "url": opp["url"],
        "title": opp["title"],
        "excerpt_sha256": store.canonical_hash(opp["excerpt"]),
        "proposal": proposal,
        "proposal_hash": opp["proposal_hash"],
        "repo": {"owner": owner, "repo": name, "issue": number, "hosts": repo.REPO_HOSTS, "max_mb": max_mb,
                 "context_kb": context_kb},
        "builder": {"output": "unified diff", "locator_max_tokens": stages.LOCATOR_MAX_TOKENS,
                    "max_tokens": stages.REPO_BUILDER_MAX_TOKENS,
                    "reviewer_max_tokens": stages.REPO_REVIEWER_MAX_TOKENS},
        "llm": {"host": model["host"], "model": model["model"]},
        "external": external,
    }
    criteria = proposal.get("acceptance_criteria") or []
    summary = {
        "title": f"Патч к {owner}/{name} (issue #{number}): {opp['title'][:100]}",
        "will_do": [
            f"Скачать архив ветки по умолчанию {owner}/{name}, закрепив коммит (SHA): 3 HTTPS-запроса к "
            f"{', '.join(repo.REPO_HOSTS)}, архив ≤ {max_mb} МБ; он не распаковывается на диск backend",
            "Проверить CONTRIBUTING/AGENTS/README: если репозиторий запрещает вклад от ИИ — остановиться до вызова модели",
            f"Locator ({model['model']}) выберет нужные файлы; модели будет показано не больше {context_kb} КБ кода",
            f"Builder напишет unified diff с тестами по предложению: {proposal.get('deliverable', '')[:240]}",
            "diff применяется без исполнения кода (только разбор текста и ast); CI, .git и удаление файлов запрещены",
            f"Reviewer проверит diff по {len(criteria)} критериям приёмки и может его отклонить",
        ],
        "data": [f"Источник: {opp['url']}", "Критерии приёмки:"] + [f"• {item}" for item in criteria],
        "limits": [f"3 вызова модели (Locator ≤ {stages.LOCATOR_MAX_TOKENS}, Builder ≤ {stages.REPO_BUILDER_MAX_TOKENS}, "
                   f"Reviewer ≤ {stages.REPO_REVIEWER_MAX_TOKENS} токенов ответа); при повторе — ещё 2",
                   "3 запроса из дневного лимита запросов; 1 задача дневного лимита"]
                  + _external_lines(external) + _budget_lines(),
        "not_included": ["Исполнение тестов (отдельное подтверждение конкретного diff)",
                         "Форк, ветка и PR (отдельное подтверждение точного текста и diff)"] + NOT_INCLUDED,
    }
    return content, summary


def _execute_content(connection: sqlite3.Connection, opp_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from backend.earner import sandbox

    opp = store.get_opportunity(opp_id, connection)
    if opp is None:
        raise ApprovalError(404, "Задача не найдена")
    if opp["status"] != store.AWAITING_EXECUTION:
        raise ApprovalError(409, f"Исполнение можно разрешить только после ревью (сейчас: {opp['status']})")
    artifact = opp["artifact"] or {}
    repo_mode = artifact.get("mode") == "repo"
    availability = sandbox.repo_availability() if repo_mode else sandbox.availability()
    if not availability["available"]:
        raise ApprovalError(409, f"Исполнение выключено: {availability['reason']}")
    files = artifact.get("files") or []
    if store.artifact_hash(artifact) != opp["artifact_hash"] or not files:
        raise ApprovalError(409, "Артефакт не совпадает со своим хешем — он был изменён")
    if repo_mode:
        return _execute_repo_content(opp, artifact)
    spec = sandbox.public_spec()
    content = {
        "action": "execute",
        "opportunity_id": opp_id,
        "artifact_hash": opp["artifact_hash"],
        "files": [{"path": f["path"], "sha256": store.canonical_hash(f["content"]), "bytes": len(f["content"])}
                  for f in files],
        "sandbox": spec,
    }
    summary = {
        "title": f"Исполнение проверок: {opp['title'][:120]}",
        "will_do": [f"Запустить {spec['command']} в одноразовом контейнере и удалить его",
                    "Показать вывод тестов; результат не отправляется наружу"],
        "data": [f"• {f['path']} — {len(f['content'])} байт" for f in files]
                + [f"Хеш артефакта: {opp['artifact_hash'][:16]}…"],
        "limits": [f"runtime {spec['runtime']}, сеть: {spec['network']}, монтирования: нет, секреты: нет",
                   f"CPU {spec['cpus']}, RAM {spec['memory']}, PID ≤ {spec['pids']}, время ≤ {spec['timeout_s']} с",
                   f"Rootfs только для чтения, /tmp {spec['tmpfs']}, пользователь {spec['user']}"]
                  + _budget_lines(sandbox_only=True),
        "not_included": NOT_INCLUDED,
    }
    return content, summary


def _execute_repo_content(opp: Dict[str, Any], artifact: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from backend.earner import sandbox

    base = artifact["repo"]
    plan = artifact.get("test_plan") or {}
    spec = sandbox.public_repo_spec()
    files = artifact["files"]
    content = {
        "action": "execute",
        "mode": "repo",
        "opportunity_id": opp["id"],
        "artifact_hash": opp["artifact_hash"],
        "repo": {key: base[key] for key in ("owner", "repo", "commit", "tarball_sha256")},
        "diff_sha256": store.canonical_hash(artifact["diff"]),
        "files": [{"path": f["path"], "sha256": store.canonical_hash(f["content"]), "bytes": len(f["content"])}
                  for f in files],
        "test_plan": plan,
        "sandbox": spec,
    }
    runner = "pytest" if plan.get("runner") == "pytest" else "python -m unittest discover"
    deps = plan.get("dependencies") or []
    summary = {
        "title": f"Тесты репозитория {base['owner']}/{base['repo']}: {opp['title'][:100]}",
        "will_do": [f"Передать архив коммита {base['commit'][:12]} в одноразовый контейнер gVisor и распаковать там"]
                   + ([f"Прогнать {runner} на исходном коммите (baseline)"] if spec["baseline"] else [])
                   + [f"Применить diff ({len(files)} файл(ов)) и прогнать {runner} ещё раз",
                      "Сравнить падения до и после; показать вывод. Наружу ничего не отправляется"],
        "data": [f"• {f['path']} — {len(f['content'])} байт" for f in files]
                + [f"Объявленные зависимости: {', '.join(deps) if deps else 'нет'} (ставятся только из образа, без сети)",
                   f"Хеш артефакта: {opp['artifact_hash'][:16]}…"],
        "diff": artifact["diff"],
        "limits": [f"runtime {spec['runtime']}, образ {spec['image']}, сеть: {spec['network']}, монтирования и секреты: нет",
                   f"CPU {spec['cpus']}, RAM {spec['memory']}, PID ≤ {spec['pids']}, ≤ {spec['timeout_s']} с на прогон",
                   f"Rootfs только для чтения, /tmp {spec['tmpfs']}, пользователь {spec['user']}"]
                  + _budget_lines(sandbox_only=True),
        "not_included": NOT_INCLUDED,
    }
    return content, summary


def _submit_content(connection: sqlite3.Connection, submission_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    row = store.row_dict(connection.execute(
        "SELECT * FROM earner_submissions WHERE id = ?", (submission_id,)).fetchone())
    if row is None:
        raise ApprovalError(404, "Черновик отправки не найден")
    if row["status"] != "draft":
        raise ApprovalError(409, f"Черновик уже не редактируется (статус: {row['status']})")
    opp = store.get_opportunity(row["opportunity_id"], connection)
    if opp is None or opp["status"] != store.CHECKS_PASSED:
        raise ApprovalError(409, "Отправлять можно только результат, прошедший проверки в sandbox")
    from backend.earner import submit

    submit.check_disclosure(row["body"])
    if row["kind"] == "github_pr":
        return submit.pr_approval_content(row, opp)
    content = {"action": "submit", "submission_id": submission_id, "opportunity_id": opp["id"],
               "kind": row["kind"], "target": row["target"], "body": row["body"],
               "artifact_hash": opp["artifact_hash"]}
    summary = {
        "title": f"Отправить комментарий на GitHub: {opp['title'][:100]}",
        "will_do": [f"Опубликовать от имени владельца токена EARNER_GITHUB_TOKEN комментарий в {row['target']}",
                    "Текст ниже — точно тот, что будет отправлен"],
        "data": [row["body"]],
        "limits": ["Один комментарий; повтор — только новым подтверждением",
                   f"Отправок сегодня: {budget.usage()['submissions']['used']} из {budget.usage()['submissions']['limit']}"],
        "not_included": ["PR, регистрация, переписка и финансовые операции"],
    }
    return content, summary


def _budget_lines(sandbox_only: bool = False) -> List[str]:
    usage = budget.usage()
    if sandbox_only:
        return [f"Запуски sandbox сегодня: {usage['sandbox_runs']['used']} из {usage['sandbox_runs']['limit']}"]
    return [
        f"Задачи сегодня: {usage['tasks']['used']} из {usage['tasks']['limit']}",
        f"Вызовы модели сегодня: {usage['llm_calls']['used']} из {usage['llm_calls']['limit']}",
        f"Токены (резерв) сегодня: {usage['tokens']['used']} из {usage['tokens']['limit']}",
    ]


def compute(connection: sqlite3.Connection, action: str, target_id: str,
            params: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if action == "research":
        return _research_content(connection)
    if action == "schedule":
        return _schedule_content(connection, params or {})
    if action == "autopilot":
        return _schedule_content(connection, params or {}, autopilot=True)
    if action == "submit":
        return _submit_content(connection, target_id)
    if action == "generate":
        return _generate_content(connection, target_id)
    if action == "execute":
        return _execute_content(connection, target_id)
    raise ApprovalError(400, f"Неизвестное действие: {action}")


def _target_for(action: str, target_id: Optional[str]) -> str:
    if action == "research":
        return "settings"
    if action in ("schedule", "autopilot"):
        return "schedule"
    if not target_id:
        raise ApprovalError(400, "Нужен id задачи")
    return target_id


# ── lifecycle ────────────────────────────────────────────────────────────────

def get(approval_id: str) -> Optional[Dict[str, Any]]:
    with store.read() as connection:
        return store.row_dict(connection.execute(
            "SELECT * FROM earner_approvals WHERE id = ?", (approval_id,)).fetchone())


def list_approvals(status: Optional[str] = None, target_id: Optional[str] = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
    clauses, values = [], []
    if status:
        clauses.append("status = ?")
        values.append(status)
    if target_id:
        clauses.append("target_id = ?")
        values.append(target_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit)
    with store.read() as connection:
        rows = connection.execute(
            f"SELECT * FROM earner_approvals {where} ORDER BY created_at DESC LIMIT ?", values).fetchall()
    return [store.row_dict(row) for row in rows]


def request(action: str, target_id: Optional[str], actor: str,
            params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a pending approval with the exact content and a human summary.
    Creating one grants nothing; an older pending one for the same target is
    superseded so only the latest screen can be confirmed."""
    if action not in ACTIONS:
        raise ApprovalError(400, f"Неизвестное действие: {action}")
    target = _target_for(action, target_id)
    with store.tx() as connection:
        content, summary = compute(connection, action, target, params)
        connection.execute(
            "UPDATE earner_approvals SET status = ?, status_reason = ? WHERE action = ? AND target_id = ? "
            "AND status = ?", (SUPERSEDED, "Запрошено новое подтверждение", action, target, PENDING))
        approval_id = store.new_id("apr")
        created = store.now()
        connection.execute(
            "INSERT INTO earner_approvals (id, action, target_id, content_hash, content, summary, status, "
            "created_at, expires_at, requested_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval_id, action, target, store.canonical_hash(content), store.dumps(content),
             store.dumps(summary), PENDING, store.iso(created), store.iso(created + ttl()), actor))
        store.log_event(connection, "approval", target, "approval_requested",
                        f"Ожидает подтверждения: {summary['title']}", {"approval_id": approval_id, "action": action},
                        actor)
        row = connection.execute("SELECT * FROM earner_approvals WHERE id = ?", (approval_id,)).fetchone()
    return store.row_dict(row)


def confirm(approval_id: str, content_hash: str, actor: str, fingerprint: str = "") -> Dict[str, Any]:
    # An expired or invalidated approval is marked as such and that mark must
    # be committed even though the confirm itself fails — so the refusal is
    # returned out of the transaction and raised only after it commits.
    with store.tx() as connection:
        outcome = _confirm_in_tx(connection, approval_id, content_hash, actor, fingerprint)
    if isinstance(outcome, ApprovalError):
        raise outcome
    return outcome


def _confirm_in_tx(connection: sqlite3.Connection, approval_id: str, content_hash: str, actor: str,
                   fingerprint: str):
    row = store.row_dict(connection.execute(
        "SELECT * FROM earner_approvals WHERE id = ?", (approval_id,)).fetchone())
    if row is None:
        raise ApprovalError(404, "Подтверждение не найдено")
    if row["status"] != PENDING:
        raise ApprovalError(409, f"Подтверждение уже недействительно (статус: {row['status']})")
    if store.parse_iso(row["expires_at"]) <= store.now():
        _close(connection, row, EXPIRED, "Истёк срок действия")
        return ApprovalError(410, "Срок действия подтверждения истёк — запросите новое")
    if not content_hash or content_hash != row["content_hash"]:
        raise ApprovalError(409, "Содержимое на экране не совпадает с подтверждаемым — обновите страницу")
    try:
        current, _ = compute(connection, row["action"], row["target_id"], (row["content"] or {}).get("schedule"))
    except ApprovalError as exc:
        _close(connection, row, INVALIDATED, exc.detail)
        return ApprovalError(409, f"Подтверждение аннулировано: {exc.detail}")
    if store.canonical_hash(current) != row["content_hash"]:
        _close(connection, row, INVALIDATED, "Содержимое изменилось после запроса подтверждения")
        return ApprovalError(409, "Содержимое изменилось после запроса — подтверждение аннулировано")

    effect = _apply(connection, row, actor)  # BudgetExceeded / 409 here roll the whole tx back
    connection.execute(
        "UPDATE earner_approvals SET status = ?, decided_at = ?, decided_by = ?, decided_fp = ? "
        "WHERE id = ? AND status = ?",
        (CONSUMED, store.iso(), actor, fingerprint, approval_id, PENDING))
    store.log_event(connection, "approval", row["target_id"], "approval_consumed",
                    f"Подтверждено оператором: {row['summary'].get('title', row['action'])}",
                    {"approval_id": approval_id, "action": row["action"], **effect}, actor)
    return {"approval_id": approval_id, "action": row["action"], **effect}


def _close(connection: sqlite3.Connection, row: Dict[str, Any], status: str, reason: str) -> None:
    connection.execute("UPDATE earner_approvals SET status = ?, status_reason = ? WHERE id = ? AND status = ?",
                       (status, reason[:300], row["id"], PENDING))
    store.log_event(connection, "approval", row["target_id"], f"approval_{status}", reason,
                    {"approval_id": row["id"], "action": row["action"]})


def _apply(connection: sqlite3.Connection, row: Dict[str, Any], actor: str) -> Dict[str, Any]:
    action = row["action"]
    content = row["content"]
    if action == "research":
        budget.reserve(connection, "tasks")
        run_id = store.new_id("run")
        connection.execute(
            "INSERT INTO earner_runs (id, trigger, status, approval_id, plan, plan_hash, created_at) "
            "VALUES (?, 'manual', ?, ?, ?, ?, ?)",
            (run_id, store.RUN_QUEUED, row["id"], store.dumps(content), row["content_hash"], store.iso()))
        store.log_event(connection, "run", run_id, "queued", "Исследование поставлено в очередь", actor=actor)
        return {"run_id": run_id}
    if action in ("schedule", "autopilot"):
        schedule = content["schedule"]
        research_plan = {**content, "action": "research"}
        research_plan.pop("schedule", None)
        research_plan.pop("external", None)
        connection.execute("DELETE FROM earner_schedule WHERE id = 1")
        connection.execute(
            "INSERT INTO earner_schedule (id, approval_id, plan, plan_hash, interval_hours, valid_until, max_runs, "
            "next_run_at, created_at, auto_generate, auto_execute) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], store.dumps(research_plan), row["content_hash"], schedule["interval_hours"],
             schedule["valid_until"], schedule["max_runs"], store.iso(), store.iso(),
             int(bool(schedule.get("auto_generate"))), int(bool(schedule.get("auto_execute")))))
        return {"schedule": schedule}
    if action == "generate":
        budget.reserve(connection, "tasks")
        if not store.transition(connection, row["target_id"], store.PROPOSED, store.QUEUED_BUILD,
                                grant_id=row["id"], error=""):
            raise ApprovalError(409, "Задача уже не ждёт этого подтверждения")
        store.log_event(connection, "opportunity", row["target_id"], "queued_build",
                        "Генерация поставлена в очередь", {"approval_id": row["id"]}, actor)
        return {"opportunity_id": row["target_id"]}
    if action == "execute":
        budget.reserve(connection, "sandbox_runs")
        if not store.transition(connection, row["target_id"], store.AWAITING_EXECUTION, store.QUEUED_EXECUTION,
                                grant_id=row["id"], error=""):
            raise ApprovalError(409, "Задача уже не ждёт этого подтверждения")
        store.log_event(connection, "opportunity", row["target_id"], "queued_execution",
                        "Исполнение поставлено в очередь", {"approval_id": row["id"]}, actor)
        return {"opportunity_id": row["target_id"]}
    if action == "submit":
        budget.reserve(connection, "submissions")
        cursor = connection.execute(
            "UPDATE earner_submissions SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'draft'",
            (store.iso(), row["target_id"]))
        if cursor.rowcount != 1:
            raise ApprovalError(409, "Черновик уже отправлен или изменён")
        store.log_event(connection, "opportunity", content["opportunity_id"], "submission_queued",
                        "Отправка поставлена в очередь", {"approval_id": row["id"]}, actor)
        return {"submission_id": row["target_id"]}
    raise ApprovalError(400, f"Неизвестное действие: {action}")


def confirm_by_autopilot(approval_id: str) -> Optional[Dict[str, Any]]:
    """An active autopilot grant confirming one generate/execute approval for
    the operator, who granted exactly that (flags, term, external-model plan).
    Same transaction, hash re-check and budget reservation as a human confirm;
    the actor is the grant. Returns None when the grant does not cover it."""
    with store.tx() as connection:
        grant = store.row_dict(connection.execute(
            "SELECT * FROM earner_schedule WHERE id = 1 AND active = 1").fetchone())
        row = store.row_dict(connection.execute(
            "SELECT * FROM earner_approvals WHERE id = ?", (approval_id,)).fetchone())
        if not grant or not row or row["status"] != PENDING:
            return None
        flag = AUTOPILOT_ACTIONS.get(row["action"])
        if not flag or not grant.get(flag) or store.parse_iso(grant["valid_until"]) <= store.now():
            return None
        if not store.get_settings(connection)["enabled"]:
            return None
        grant_row = store.row_dict(connection.execute(
            "SELECT content, status FROM earner_approvals WHERE id = ?", (grant["approval_id"],)).fetchone())
        if not grant_row or grant_row["status"] != CONSUMED or (grant_row["content"] or {}).get("action") != "autopilot":
            return None
        granted = (grant_row["content"] or {}).get("external") or {"enabled": False}
        asked = (row["content"] or {}).get("external") or {"enabled": False}
        for key in ("enabled", "model", "month_usd"):
            if granted.get(key) != asked.get(key):
                return None  # the operator never agreed to this model or budget
        outcome = _confirm_in_tx(connection, approval_id, row["content_hash"], f"autopilot:{grant['approval_id']}", "")
    if isinstance(outcome, ApprovalError):
        return None
    return outcome


def invalidate_action(connection: sqlite3.Connection, action: str, reason: str) -> int:
    cursor = connection.execute(
        "UPDATE earner_approvals SET status = ?, status_reason = ? WHERE action = ? AND status = ?",
        (INVALIDATED, reason, action, PENDING))
    return cursor.rowcount


def decline(approval_id: str, actor: str, reason: str = "") -> Dict[str, Any]:
    with store.tx() as connection:
        row = store.row_dict(connection.execute(
            "SELECT * FROM earner_approvals WHERE id = ?", (approval_id,)).fetchone())
        if row is None:
            raise ApprovalError(404, "Подтверждение не найдено")
        if row["status"] != PENDING:
            raise ApprovalError(409, f"Подтверждение уже недействительно (статус: {row['status']})")
        connection.execute(
            "UPDATE earner_approvals SET status = ?, status_reason = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (DECLINED, (reason or "Отклонено оператором")[:300], store.iso(), actor, approval_id))
        store.log_event(connection, "approval", row["target_id"], "approval_declined",
                        f"Отклонено оператором: {row['summary'].get('title', row['action'])}",
                        {"approval_id": approval_id, "reason": reason[:300]}, actor)
    return {"approval_id": approval_id, "status": DECLINED}


def invalidate_settings_bound(connection: sqlite3.Connection, reason: str) -> int:
    cursor = connection.execute(
        f"UPDATE earner_approvals SET status = ?, status_reason = ? WHERE status = ? "
        f"AND action IN ({','.join('?' * len(SETTINGS_BOUND))})",
        (INVALIDATED, reason, PENDING, *SETTINGS_BOUND))
    schedule = connection.execute(
        "UPDATE earner_schedule SET active = 0, status_reason = ? WHERE id = 1 AND active = 1", (reason,))
    if schedule.rowcount:
        store.log_event(connection, "schedule", "schedule", "schedule_revoked",
                        f"Автопоиск остановлен: {reason}")
    return cursor.rowcount


def invalidate_target(connection: sqlite3.Connection, target_id: str, reason: str) -> int:
    cursor = connection.execute(
        "UPDATE earner_approvals SET status = ?, status_reason = ? WHERE target_id = ? AND status = ?",
        (INVALIDATED, reason, target_id, PENDING))
    return cursor.rowcount


def expire_stale() -> int:
    with store.tx() as connection:
        cursor = connection.execute(
            "UPDATE earner_approvals SET status = ?, status_reason = 'Истёк срок действия' "
            "WHERE status = ? AND expires_at <= ?", (EXPIRED, PENDING, store.iso()))
        return cursor.rowcount


def revoke_schedule(actor: str) -> bool:
    with store.tx() as connection:
        cursor = connection.execute(
            "UPDATE earner_schedule SET active = 0, status_reason = 'Остановлено оператором' WHERE id = 1 AND active = 1")
        if cursor.rowcount:
            store.log_event(connection, "schedule", "schedule", "schedule_revoked",
                            "Автопоиск остановлен оператором", actor=actor)
        return cursor.rowcount == 1


def get_schedule() -> Optional[Dict[str, Any]]:
    with store.read() as connection:
        row = store.row_dict(connection.execute("SELECT * FROM earner_schedule WHERE id = 1").fetchone())
    if row:
        row.pop("plan", None)
    return row
