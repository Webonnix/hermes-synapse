"""One-click submission: a GitHub issue comment or a pull request from a fork,
sent only after the operator approves the exact text (and, for a PR, the exact
diff).

The draft is built from a fixed template plus the checked artifact — no model
writes outward-facing text, so nothing a task page says can end up posted in
the operator's name. The operator may edit it; any edit invalidates a pending
approval for it. Sending needs a ``submit`` approval bound to the exact text
(approvals.py) and goes out once, through egress (api.github.com only, HTTPS,
pinned IP, no redirects), with the operator's own EARNER_GITHUB_TOKEN. No retry:
a failed send stays failed with its HTTP status. ``submit`` is never part of
the autopilot: every outward step stays a manual confirmation.

A pull request is built with the Git Data API, not ``git push``: the operator's
fork is created (or reused), one commit with exactly the approved files is
created on top of the pinned base commit, a branch points at it, and the PR is
opened from that branch. Every file sent is re-checked against the approved
diff's hashes right before sending.

Every text says plainly that the work was prepared with AI assistance — a
draft without that disclosure cannot be approved: undisclosed automated
contributions are what gets accounts banned. A repository whose own guidance
refuses AI contributions gets no PR at all.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.earner import approvals, egress, repo, store

GITHUB_API_HOSTS = ["api.github.com"]
API = "https://api.github.com"
_ISSUE_URL = repo.ISSUE_URL
MAX_BODY = 60_000
MAX_TITLE = 200

COMMENT_DISCLOSURE = "_Disclosure: this solution was prepared with AI assistance and reviewed by me before posting._"
PR_DISCLOSURE = ("_Disclosure: this pull request was prepared with AI assistance (an automated pipeline run by a "
                 "human operator) and reviewed by me before submission. Happy to adjust anything._")
_DISCLOSED = re.compile(r"\bAI[- ]assist(ed|ance)\b|\bwith (the help of )?AI\b|\bAI-generated\b", re.I)
FORK_POLLS = 10
FORK_POLL_SECONDS = 3.0

Fetcher = Callable[..., egress.FetchResult]


def _github_token() -> str:
    from backend import database

    return database.get_api_key("EARNER_GITHUB_TOKEN") or ""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def check_disclosure(body: str) -> None:
    if not _DISCLOSED.search(body or ""):
        raise approvals.ApprovalError(
            409, "В тексте нет пометки, что работа сделана с помощью ИИ (например, «prepared with AI assistance») — "
                 "без неё отправка не подтверждается")


# ── drafts ───────────────────────────────────────────────────────────────────

def build_body(opp: Dict[str, Any]) -> str:
    artifact = opp.get("artifact") or {}
    files = artifact.get("files") or []
    execution = opp.get("execution") or {}
    proposal = opp.get("proposal") or {}
    criteria = "\n".join(f"- {item}" for item in proposal.get("acceptance_criteria") or [])
    blocks = []
    if artifact.get("mode") == "repo":
        blocks.append(f"<details><summary>Proposed diff</summary>\n\n```diff\n{artifact.get('diff', '')}\n```\n</details>")
    else:
        for item in files:
            language = "python" if item["path"].endswith(".py") else ""
            blocks.append(f"<details><summary><code>{item['path']}</code></summary>\n\n```{language}\n"
                          f"{item['content']}\n```\n</details>")
    body = (
        "Hi! I'd like to work on this issue and have prepared a solution.\n\n"
        f"**What it does:** {proposal.get('deliverable', '')}\n\n"
        + (f"**Acceptance criteria covered:**\n{criteria}\n\n" if criteria else "")
        + f"**Tests:** {(artifact.get('static') or {}).get('test_count', 0)} unit tests, "
          f"result in an isolated sandbox: {execution.get('status', 'not run')}.\n\n"
        + "\n\n".join(blocks)
        + "\n\nIf this direction works for you, I'm happy to open a pull request adapted to the repository's "
          "layout and conventions — could you assign the issue to me?\n\n"
        + COMMENT_DISCLOSURE
    )
    return body[:MAX_BODY]


def _tests_paragraph(execution: Dict[str, Any], base_commit: str) -> str:
    status = execution.get("status")
    runner = execution.get("runner") or "tests"
    patched = (execution.get("patched") or {}).get("tests") or {}
    baseline = (execution.get("baseline") or {}).get("tests") or {}
    if status == "passed":
        line = f"The repository's {runner} suite passes with this change"
        if patched.get("total"):
            line += f" ({patched['total']} tests)"
        return line + " in an isolated sandbox (gVisor, no network)."
    if status == "no_regressions":
        fixed = execution.get("fixed") or []
        return (f"In an isolated sandbox (gVisor, no network) {baseline.get('failed_count', 0)} of "
                f"{baseline.get('total', 0)} tests already fail on the base commit {base_commit[:12]} — most likely "
                "optional dependencies missing from the sandbox. With this change no new test fails"
                + (f", and {len(fixed)} previously failing test(s) now pass." if fixed else "."))
    return f"Sandbox result: {status or 'not run'}."


def build_pr(opp: Dict[str, Any], number: int) -> Tuple[str, str, str]:
    artifact = opp.get("artifact") or {}
    proposal = opp.get("proposal") or {}
    base = artifact.get("repo") or {}
    title = " ".join(f"Fix #{number}: {opp.get('title') or ''}".split())[:MAX_TITLE]
    changes = "\n".join(f"- `{item['path']}`" for item in artifact.get("files") or [])
    tests = artifact.get("tests") or []
    body = (
        f"Fixes #{number}\n\n"
        f"## Summary\n{artifact.get('summary') or proposal.get('deliverable') or ''}\n\n"
        f"## Changes\n{changes}\n\n"
        f"## Tests\n{_tests_paragraph(opp.get('execution') or {}, base.get('commit') or '')}\n"
        + ("".join(f"\n- {name}" for name in tests) + "\n" if tests else "")
        + f"\n---\n{PR_DISCLOSURE}\n"
    )
    message = f"{title}\n\nFixes #{number}\n\nPrepared with AI assistance and reviewed by a human operator."
    return title, body[:MAX_BODY], message


def _pr_checks(opp: Dict[str, Any]) -> Tuple[str, str, int]:
    artifact = opp.get("artifact") or {}
    if artifact.get("mode") != "repo":
        raise approvals.ApprovalError(409, "PR возможен только для патча к репозиторию задачи")
    if store.artifact_hash(artifact) != opp["artifact_hash"]:
        raise approvals.ApprovalError(409, "Артефакт не совпадает со своим хешем — он был изменён")
    policy = (artifact.get("static") or {}).get("ai_policy") or {}
    if policy.get("status") == "forbidden":
        raise approvals.ApprovalError(409, "Репозиторий не принимает вклад, созданный ИИ — PR не создаётся")
    if (opp.get("execution") or {}).get("status") not in ("passed", "no_regressions"):
        raise approvals.ApprovalError(409, "PR — только после успешной проверки тестов репозитория в sandbox")
    parsed = repo.parse_issue_url(opp["url"])
    if not parsed:
        raise approvals.ApprovalError(409, "Задача не является GitHub issue")
    base = artifact["repo"]
    if (parsed[0].lower(), parsed[1].lower()) != (base["owner"].lower(), base["repo"].lower()):
        raise approvals.ApprovalError(409, "Снимок сделан с другого репозитория")
    return parsed


def create_draft(opp_id: str, actor: str, kind: str = "auto") -> Dict[str, Any]:
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        if opp is None:
            raise KeyError(opp_id)
        if opp["is_demo"]:
            raise approvals.ApprovalError(409, "Демо-задача никуда не отправляется")
        if opp["status"] != store.CHECKS_PASSED:
            raise approvals.ApprovalError(409, "Отправлять можно только результат, прошедший проверки в sandbox")
        match = _ISSUE_URL.match(opp["url"])
        if not match:
            raise approvals.ApprovalError(409, "Автоматическая отправка пока поддерживает только GitHub issues")
        if kind not in ("auto", "pr", "comment"):
            raise ValueError("kind: auto, pr или comment")
        repo_mode = (opp.get("artifact") or {}).get("mode") == "repo"
        as_pr = kind == "pr" or (kind == "auto" and repo_mode)
        owner, name, number = match.groups()
        submission_id = store.new_id("sub")
        if as_pr:
            owner, name, number_int = _pr_checks(opp)
            artifact = opp["artifact"]
            title, body, message = build_pr(opp, number_int)
            payload = {
                "base": {"owner": artifact["repo"]["owner"], "repo": artifact["repo"]["repo"],
                         "branch": artifact["repo"]["default_branch"], "commit": artifact["repo"]["commit"]},
                "issue": number_int,
                "head_branch": f"hermes/issue-{number_int}-{submission_id.rsplit('-', 1)[-1][:8]}",
                "commit_message": message,
                "diff_sha256": store.canonical_hash(artifact["diff"]),
                "files": [{"path": f["path"], "sha256": store.canonical_hash(f["content"]),
                           "mode": f.get("mode", "100644")} for f in artifact["files"]],
                "artifact_hash": opp["artifact_hash"],
            }
            kind_value, target = "github_pr", f"{API}/repos/{owner}/{name}/pulls"
            event = "Подготовлен черновик pull request"
        else:
            title, body, payload = "", build_body(opp), {}
            kind_value, target = "github_comment", f"{API}/repos/{owner}/{name}/issues/{number}/comments"
            event = "Подготовлен черновик комментария для GitHub"
        connection.execute("UPDATE earner_submissions SET status = 'superseded', updated_at = ? "
                           "WHERE opportunity_id = ? AND status = 'draft'", (store.iso(), opp_id))
        connection.execute(
            "INSERT INTO earner_submissions (id, opportunity_id, kind, target, title, body, payload, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (submission_id, opp_id, kind_value, target, title, body, store.dumps(payload), store.iso(), store.iso()))
        store.log_event(connection, "opportunity", opp_id, "submission_draft", event,
                        {"submission_id": submission_id, "kind": kind_value}, actor)
    return get(submission_id)


def update_draft(submission_id: str, body: str, actor: str, title: Optional[str] = None) -> Dict[str, Any]:
    text = (body or "").strip()
    if len(text) < 20 or len(text) > MAX_BODY:
        raise ValueError(f"текст: от 20 до {MAX_BODY} символов")
    if not _DISCLOSED.search(text):
        raise ValueError("в тексте должна остаться пометка, что работа сделана с помощью ИИ")
    with store.tx() as connection:
        row = store.row_dict(connection.execute(
            "SELECT * FROM earner_submissions WHERE id = ? AND status = 'draft'", (submission_id,)).fetchone())
        if row is None:
            raise approvals.ApprovalError(409, "Черновик уже отправлен или заменён")
        new_title = row["title"]
        if title is not None:
            if row["kind"] != "github_pr":
                raise ValueError("заголовок есть только у pull request")
            new_title = " ".join(title.split())
            if not 5 <= len(new_title) <= MAX_TITLE:
                raise ValueError(f"заголовок: от 5 до {MAX_TITLE} символов")
        connection.execute("UPDATE earner_submissions SET body = ?, title = ?, updated_at = ? WHERE id = ?",
                           (text, new_title, store.iso(), submission_id))
        approvals.invalidate_target(connection, submission_id, "Текст черновика изменён")
        store.log_event(connection, "opportunity", row["opportunity_id"], "submission_edited",
                        "Текст черновика изменён оператором", {"submission_id": submission_id}, actor)
    return get(submission_id)


def pr_approval_content(row: Dict[str, Any], opp: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """What a PR submit approval binds to: target, title, body, the commit
    message, the branch, the base commit and every file's hash — plus the
    artifact hash, so a changed diff can never ride an old approval."""
    from backend.earner import budget

    _pr_checks(opp)
    payload = row["payload"] or {}
    if payload.get("artifact_hash") != opp["artifact_hash"]:
        raise approvals.ApprovalError(409, "Черновик PR подготовлен для другого diff — создайте его заново")
    artifact = opp["artifact"]
    base = payload["base"]
    content = {"action": "submit", "submission_id": row["id"], "opportunity_id": opp["id"], "kind": row["kind"],
               "target": row["target"], "title": row["title"], "body": row["body"], "payload": payload,
               "artifact_hash": opp["artifact_hash"]}
    usage = budget.usage()["submissions"]
    summary = {
        "title": f"Открыть PR в {base['owner']}/{base['repo']}: {row['title'][:100]}",
        "will_do": [
            "От имени владельца EARNER_GITHUB_TOKEN создать форк репозитория (или взять существующий)",
            f"Создать в форке ветку {payload['head_branch']} с одним коммитом поверх {base['commit'][:12]} "
            f"({len(payload['files'])} файл(ов) — ровно diff ниже)",
            f"Открыть pull request из этой ветки в {base['owner']}/{base['repo']}:{base['branch']}",
            "Заголовок, текст и diff ниже — точно то, что будет опубликовано",
        ],
        "data": [f"Заголовок: {row['title']}", row["body"], f"Сообщение коммита: {payload['commit_message']}"],
        "diff": artifact["diff"],
        "limits": ["Один PR; повтор — только новым подтверждением", "≈8 запросов к api.github.com",
                   f"Отправок сегодня: {usage['used']} из {usage['limit']}"],
        "not_included": ["Слияние, ответы на ревью, переписка и финансовые операции"],
    }
    return content, summary


def get(submission_id: str) -> Optional[Dict[str, Any]]:
    with store.read() as connection:
        return store.row_dict(connection.execute(
            "SELECT * FROM earner_submissions WHERE id = ?", (submission_id,)).fetchone())


def list_for(opp_id: str) -> List[Dict[str, Any]]:
    with store.read() as connection:
        return [store.row_dict(row) for row in connection.execute(
            "SELECT * FROM earner_submissions WHERE opportunity_id = ? ORDER BY created_at DESC", (opp_id,))]


def claim_queued() -> Optional[str]:
    with store.tx() as connection:
        row = connection.execute(
            "SELECT id FROM earner_submissions WHERE status = 'queued' ORDER BY updated_at LIMIT 1").fetchone()
        if not row:
            return None
        cursor = connection.execute(
            "UPDATE earner_submissions SET status = 'sending', updated_at = ? WHERE id = ? AND status = 'queued'",
            (store.iso(), row["id"]))
        return row["id"] if cursor.rowcount == 1 else None


# ── sending ──────────────────────────────────────────────────────────────────

def send(submission_id: str, fetch: Fetcher = egress.fetch) -> Dict[str, Any]:
    row = get(submission_id)
    if row is None or row["status"] != "sending":
        raise ValueError("отправка не в очереди")
    with store.read() as connection:
        approval = store.row_dict(connection.execute(
            "SELECT * FROM earner_approvals WHERE target_id = ? AND action = 'submit' AND status = 'consumed' "
            "ORDER BY decided_at DESC LIMIT 1", (submission_id,)).fetchone())
    content = (approval or {}).get("content") or {}
    if content.get("body") != row["body"] or content.get("target") != row["target"]:
        return _finish(row, "failed", error="Текст или адрес не совпадают с подтверждёнными — не отправлено")
    token = _github_token()
    if not token:
        return _finish(row, "failed", error="Не задан EARNER_GITHUB_TOKEN (Настройки → API-ключи)")
    if row["kind"] == "github_pr":
        return _send_pr(row, content, token, fetch)
    try:
        result = fetch(row["target"], GITHUB_API_HOSTS, method="POST", json_body={"body": row["body"]},
                       accept="application/vnd.github+json",
                       extra_headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    except egress.EgressError as exc:
        return _finish(row, "failed", error=f"Сеть: {exc}")
    if result.status != 201:
        return _finish(row, "failed", error=f"GitHub ответил HTTP {result.status}")
    try:
        url = str(json.loads(result.body.decode("utf-8", errors="replace")).get("html_url") or "")
    except ValueError:
        url = ""
    return _finish(row, "sent", response_url=url if url.startswith("https://github.com/") else "")


class _GitHubStepError(RuntimeError):
    pass


def _gh(fetch: Fetcher, token: str, step: str, url: str, method: str = "GET", body: Any = None,
        ok: Tuple[int, ...] = (200,)) -> Dict[str, Any]:
    try:
        result = fetch(url, GITHUB_API_HOSTS, method=method, json_body=body, accept="application/vnd.github+json",
                       extra_headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    except egress.EgressError as exc:
        raise _GitHubStepError(f"{step}: сеть: {exc}") from exc
    if result.status not in ok:
        hint = ""
        try:
            hint = str(json.loads(result.body.decode("utf-8", errors="replace")).get("message") or "")[:160]
        except (ValueError, AttributeError):
            pass
        raise _GitHubStepError(f"{step}: GitHub ответил HTTP {result.status}" + (f" ({hint})" if hint else ""))
    try:
        data = json.loads(result.body.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        raise _GitHubStepError(f"{step}: ответ не JSON") from exc
    if not isinstance(data, dict):
        raise _GitHubStepError(f"{step}: неожиданный ответ")
    return data


def _approved_files(row: Dict[str, Any], content: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The files to commit, re-derived from the stored artifact and checked
    against the hashes the operator approved."""
    payload = row["payload"] or {}
    if content.get("title") != row["title"] or content.get("payload") != payload:
        raise _GitHubStepError("Заголовок, ветка или файлы не совпадают с подтверждёнными — не отправлено")
    opp = store.get_opportunity(row["opportunity_id"])
    artifact = (opp or {}).get("artifact") or {}
    if (not opp or store.artifact_hash(artifact) != opp["artifact_hash"]
            or opp["artifact_hash"] != payload.get("artifact_hash")
            or content.get("artifact_hash") != opp["artifact_hash"]):
        raise _GitHubStepError("diff изменился после подтверждения — не отправлено")
    if store.canonical_hash(artifact.get("diff") or "") != payload.get("diff_sha256"):
        raise _GitHubStepError("diff не совпадает с подтверждённым — не отправлено")
    approved = {item["path"]: item for item in payload.get("files") or []}
    files = artifact.get("files") or []
    if set(approved) != {f["path"] for f in files}:
        raise _GitHubStepError("набор файлов не совпадает с подтверждённым — не отправлено")
    for item in files:
        if store.canonical_hash(item["content"]) != approved[item["path"]]["sha256"]:
            raise _GitHubStepError(f"{item['path']} не совпадает с подтверждённым — не отправлено")
        if repo.normalize_path(item["path"]) != item["path"] or item["path"].split("/")[0] in (".git", ".github"):
            raise _GitHubStepError(f"{item['path']}: недопустимый путь — не отправлено")
    return files


def _send_pr(row: Dict[str, Any], content: Dict[str, Any], token: str, fetch: Fetcher) -> Dict[str, Any]:
    try:
        files = _approved_files(row, content)
        payload = row["payload"]
        base = payload["base"]
        upstream = f"{API}/repos/{base['owner']}/{base['repo']}"
        login = str(_gh(fetch, token, "Пользователь токена", f"{API}/user").get("login") or "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            raise _GitHubStepError("Пользователь токена: GitHub не вернул login")
        if login.lower() == base["owner"].lower():
            raise _GitHubStepError("Токен принадлежит владельцу репозитория — PR из форка не нужен, отправьте вручную")
        fork = _gh(fetch, token, "Форк", f"{upstream}/forks", method="POST", body={"default_branch_only": True},
                   ok=(200, 202))
        fork_name = str(fork.get("full_name") or "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", fork_name) \
                or fork_name.split("/")[0].lower() != login.lower():
            raise _GitHubStepError("Форк: GitHub вернул неожиданное имя форка")
        forked = f"{API}/repos/{fork_name}"
        for attempt in range(FORK_POLLS):
            try:
                _gh(fetch, token, "Форк", forked)
                break
            except _GitHubStepError:
                if attempt == FORK_POLLS - 1:
                    raise _GitHubStepError("Форк: не стал доступен за отведённое время — отправьте заново позже")
                _sleep(FORK_POLL_SECONDS)
        base_commit = _gh(fetch, token, "Базовый коммит", f"{upstream}/git/commits/{base['commit']}")
        base_tree = str((base_commit.get("tree") or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", base_tree):
            raise _GitHubStepError("Базовый коммит: нет дерева")
        modes = {item["path"]: item.get("mode") for item in payload["files"]}
        tree = _gh(fetch, token, "Дерево файлов в форке", f"{forked}/git/trees", method="POST", ok=(201,), body={
            "base_tree": base_tree,
            "tree": [{"path": f["path"], "mode": modes.get(f["path"]) if modes.get(f["path"]) in ("100644", "100755")
                      else "100644", "type": "blob", "content": f["content"]} for f in files],
        })
        commit = _gh(fetch, token, "Коммит в форке", f"{forked}/git/commits", method="POST", ok=(201,), body={
            "message": payload["commit_message"], "tree": tree.get("sha"), "parents": [base["commit"]],
        })
        commit_sha = str(commit.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise _GitHubStepError("Коммит в форке: GitHub не вернул SHA")
        _gh(fetch, token, "Ветка в форке", f"{forked}/git/refs", method="POST", ok=(201,),
            body={"ref": f"refs/heads/{payload['head_branch']}", "sha": commit_sha})
        pull = _gh(fetch, token, "Pull request", row["target"], method="POST", ok=(201,), body={
            "title": row["title"], "body": row["body"], "head": f"{login}:{payload['head_branch']}",
            "base": base["branch"], "maintainer_can_modify": True,
        })
    except _GitHubStepError as exc:
        return _finish(row, "failed", error=str(exc))
    url = str(pull.get("html_url") or "")
    return _finish(row, "sent", response_url=url if url.startswith("https://github.com/") else "")


def _finish(row: Dict[str, Any], status: str, error: str = "", response_url: str = "") -> Dict[str, Any]:
    what = "Pull request открыт" if row["kind"] == "github_pr" else "Комментарий опубликован"
    with store.tx() as connection:
        connection.execute(
            "UPDATE earner_submissions SET status = ?, error = ?, response_url = ?, updated_at = ?, sent_at = ? "
            "WHERE id = ?", (status, error[:500], response_url, store.iso(), store.iso() if status == "sent" else None,
                             row["id"]))
        if status == "sent":
            opp = store.get_opportunity(row["opportunity_id"], connection)
            if opp and not opp["delivered_at"]:
                note = f"{what}: {response_url}" if row["kind"] == "github_pr" else \
                    f"Комментарий с решением опубликован: {response_url}"
                store.set_opportunity(connection, row["opportunity_id"], delivered_at=store.iso(), delivered_note=note)
        store.log_event(connection, "opportunity", row["opportunity_id"], f"submission_{status}",
                        f"{what}: {response_url}" if status == "sent" else f"Отправка не удалась: {error}",
                        {"submission_id": row["id"], "kind": row["kind"]})
    return get(row["id"])
