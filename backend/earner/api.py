"""HTTP surface of the Autonomous Earner (/api/earner).

The auth middleware in main.py already requires an operator session for every
/api/ path; each route here additionally checks an ``earner.*`` permission.
Confirming an approval needs ``earner.approve`` (owner only by default) and an
explicit intent header. Auth is a Bearer token, never a cookie, so a foreign
page cannot ride the operator's session (no CSRF surface); the header is a
second, deliberate signal that a human pressed the confirm button on this
screen. There is no service key: no agent tool or internal caller can reach
``confirm`` without an operator's session token.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import zipfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import permissions
from backend.earner import approvals, budget, llm, sandbox, store, submit, wallet

logger = logging.getLogger("hermes.earner.api")

router = APIRouter(prefix="/api/earner")

INTENT_HEADER = "X-Hermes-Intent"
INTENT_CONFIRM = "earner-approve"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, approvals.ApprovalError):
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    if isinstance(exc, budget.BudgetExceeded):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Не найдено")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Earner API error")
    return HTTPException(status_code=500, detail="Внутренняя ошибка")


async def _call(func, *args, **kwargs):
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except Exception as exc:
        raise _http_error(exc) from None


def _fingerprint(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    token = header.split(" ", 1)[1].strip() if header.startswith("Bearer ") else ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else ""


def _actor(request: Request) -> str:
    return f"operator:{permissions.actor_id(request)}"


# ── overview ─────────────────────────────────────────────────────────────────

_PREPARED = (store.AWAITING_EXECUTION, store.QUEUED_EXECUTION, store.EXECUTING, store.CHECKS_PASSED,
             store.CHECKS_FAILED)


def _overview() -> Dict[str, Any]:
    settings = store.get_settings()
    with store.read() as connection:
        counts = {row["status"]: row["n"] for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM earner_opportunities GROUP BY status")}
        pending = connection.execute(
            "SELECT COUNT(*) FROM earner_approvals WHERE status = 'pending' AND expires_at > ?",
            (store.iso(),)).fetchone()[0]
        report = connection.execute(
            "SELECT "
            "SUM(CASE WHEN status NOT IN (?, ?) THEN 1 ELSE 0 END) AS potential, "
            f"SUM(CASE WHEN status IN ({','.join('?' * len(_PREPARED))}) THEN 1 ELSE 0 END) AS prepared, "
            "SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered, "
            "SUM(CASE WHEN paid_at IS NOT NULL THEN 1 ELSE 0 END) AS paid, "
            "SUM(CASE WHEN paid_at IS NOT NULL AND paid_note LIKE 'on-chain:%' THEN 1 ELSE 0 END) AS paid_onchain, "
            "SUM(is_demo) AS demo FROM earner_opportunities",
            (store.NOT_FIT, store.DISMISSED, *_PREPARED)).fetchone()
    return {
        "settings": settings,
        "budget": budget.usage(),
        "llm": llm.describe(),
        "sandbox": sandbox.availability(),
        "repo_sandbox": sandbox.repo_availability(),
        "schedule": approvals.get_schedule(),
        "counts": counts,
        "pending_approvals": pending,
        "report": {
            "potential": int(report["potential"] or 0),
            "prepared": int(report["prepared"] or 0),
            "delivered_manual": int(report["delivered"] or 0),
            "paid_manual": int(report["paid"] or 0),
            "paid_onchain": int(report["paid_onchain"] or 0),
            "demo": int(report["demo"] or 0),
            "note": ("Найденная возможность, сгенерированный код и пройденные тесты — не заработанные деньги. "
                     "Заработком считается только оплата, которую оператор подтвердил вручную."),
        },
        "runs": store.list_runs(5),
        "income": {key: value for key, value in wallet.list_income(0).items() if key != "income"},
    }


@router.get("/overview")
async def overview(request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(_overview)


class SettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    directions: Optional[List[str]] = None
    constraints: Optional[str] = None
    sources: Optional[List[Dict[str, Any]]] = None
    allowed_hosts: Optional[List[str]] = None
    limits: Optional[Dict[str, int]] = None
    paid_search_enabled: Optional[bool] = None
    external_enabled: Optional[bool] = None
    external_model: Optional[str] = None
    external_month_usd: Optional[float] = None
    wallet: Optional[Dict[str, Any]] = None


@router.put("/settings")
async def put_settings(payload: SettingsRequest, request: Request):
    permissions.require(request, permissions.EARNER_MANAGE)
    fields = payload.model_dump(exclude_none=True)
    return await _call(store.update_settings, fields, _actor(request))


# ── runs and opportunities ───────────────────────────────────────────────────

@router.get("/runs")
async def list_runs(request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(store.list_runs, 50)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    run = await _call(store.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Не найдено")
    return {**run, "events": await _call(store.list_events, run_id, 100)}


@router.get("/opportunities")
async def list_opportunities(request: Request, status: Optional[str] = None):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(store.list_opportunities, status)


@router.get("/opportunities/{opp_id}")
async def get_opportunity(opp_id: str, request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    opp = await _call(store.get_opportunity, opp_id)
    if not opp:
        raise HTTPException(status_code=404, detail="Не найдено")
    opp["events"] = await _call(store.list_events, opp_id, 200)
    opp["approvals"] = await _call(approvals.list_approvals, None, opp_id, 50)
    opp["submissions"] = await _call(submit.list_for, opp_id)
    # Submit approvals target the submission id, not the opportunity.
    for item in opp["submissions"]:
        opp["approvals"].extend(await _call(approvals.list_approvals, None, item["id"], 10))
    return opp


@router.get("/opportunities/{opp_id}/artifact.zip")
async def download_artifact(opp_id: str, request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    opp = await _call(store.get_opportunity, opp_id)
    files = ((opp or {}).get("artifact") or {}).get("files") or []
    if not files:
        raise HTTPException(status_code=404, detail="Артефакта нет")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.writestr(item["path"], item["content"])
        artifact = opp.get("artifact") or {}
        if artifact.get("mode") == "repo":
            archive.writestr("HERMES_EARNER_PATCH.diff", artifact.get("diff") or "")
        review = opp.get("review") or {}
        archive.writestr("HERMES_EARNER_REPORT.txt", "\n".join([
            f"Задача: {opp['title']}",
            f"Источник: {opp['url']}",
            f"Хеш артефакта: {opp['artifact_hash']}",
            f"Reviewer: {'одобрил' if review.get('accepted') else 'не одобрил'} — {review.get('summary', '')}",
            f"Sandbox: {(opp.get('execution') or {}).get('status', 'не запускался')}",
            "Это подготовленный результат. Передача заказчику и оплата — только вручную.",
        ]))
    safe_id = "".join(ch for ch in opp_id if ch.isalnum() or ch == "-")
    return Response(content=buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="earner-{safe_id}.zip"',
                             "X-Content-Type-Options": "nosniff"})


class DismissRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


_DISMISSABLE = (store.NEW, store.PROPOSED, store.NOT_FIT, store.REVIEW_REJECTED, store.AWAITING_EXECUTION,
                store.CHECKS_PASSED, store.CHECKS_FAILED, store.FAILED, store.INTERRUPTED)


def _dismiss(opp_id: str, reason: str, actor: str) -> Dict[str, Any]:
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        if not opp:
            raise KeyError(opp_id)
        if opp["status"] not in _DISMISSABLE:
            raise approvals.ApprovalError(409, f"Нельзя скрыть задачу в статусе {opp['status']}")
        store.transition(connection, opp_id, opp["status"], store.DISMISSED,
                         reject_reason=(reason or "Скрыто оператором")[:500])
        approvals.invalidate_target(connection, opp_id, "Задача скрыта оператором")
        store.log_event(connection, "opportunity", opp_id, "dismissed", reason or "Скрыто оператором", actor=actor)
    return {"status": store.DISMISSED}


@router.post("/opportunities/{opp_id}/dismiss")
async def dismiss(opp_id: str, payload: DismissRequest, request: Request):
    permissions.require(request, permissions.EARNER_MANAGE)
    return await _call(_dismiss, opp_id, payload.reason, _actor(request))


class OutcomeRequest(BaseModel):
    kind: str
    note: str = Field(min_length=3, max_length=1000)


def _record_outcome(opp_id: str, kind: str, note: str, actor: str) -> Dict[str, Any]:
    """A fact the operator states by hand. The system never sets these."""
    with store.tx() as connection:
        opp = store.get_opportunity(opp_id, connection)
        if not opp:
            raise KeyError(opp_id)
        if not opp["artifact_hash"]:
            raise approvals.ApprovalError(409, "Без подготовленного результата нечего передавать")
        if kind == "delivered":
            store.set_opportunity(connection, opp_id, delivered_at=store.iso(), delivered_note=note)
            message = "Оператор вручную отметил передачу результата"
        elif kind == "paid":
            if not opp["delivered_at"]:
                raise approvals.ApprovalError(409, "Сначала отметьте передачу результата")
            store.set_opportunity(connection, opp_id, paid_at=store.iso(), paid_note=note)
            message = "Оператор вручную подтвердил оплату"
        else:
            raise ValueError("kind: delivered или paid")
        store.log_event(connection, "opportunity", opp_id, f"manual_{kind}", message, {"note": note}, actor)
    return {"status": "recorded", "kind": kind}


@router.post("/opportunities/{opp_id}/outcome")
async def record_outcome(opp_id: str, payload: OutcomeRequest, request: Request):
    permissions.require(request, permissions.EARNER_MANAGE)
    return await _call(_record_outcome, opp_id, payload.kind, payload.note.strip(), _actor(request))


# ── approvals ────────────────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    action: str
    target_id: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


@router.post("/approvals")
async def request_approval(payload: ApprovalRequest, request: Request):
    """Prepares a confirmation screen. Grants nothing by itself."""
    permissions.require(request, permissions.EARNER_MANAGE)
    return await _call(approvals.request, payload.action, payload.target_id, _actor(request), payload.params)


@router.get("/approvals")
async def list_approvals(request: Request, status: Optional[str] = None, target_id: Optional[str] = None):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(approvals.list_approvals, status, target_id, 200)


class ConfirmRequest(BaseModel):
    content_hash: str = Field(min_length=64, max_length=64)


@router.post("/approvals/{approval_id}/confirm")
async def confirm_approval(approval_id: str, payload: ConfirmRequest, request: Request):
    permissions.require(request, permissions.EARNER_APPROVE)
    if request.headers.get(INTENT_HEADER) != INTENT_CONFIRM:
        raise HTTPException(status_code=400, detail="Подтверждение принимается только с экрана подтверждения")
    return await _call(approvals.confirm, approval_id, payload.content_hash, _actor(request), _fingerprint(request))


class DeclineRequest(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.post("/approvals/{approval_id}/decline")
async def decline_approval(approval_id: str, payload: DeclineRequest, request: Request):
    permissions.require(request, permissions.EARNER_APPROVE)
    return await _call(approvals.decline, approval_id, _actor(request), payload.reason)


@router.post("/schedule/revoke")
async def revoke_schedule(request: Request):
    permissions.require(request, permissions.EARNER_MANAGE)
    return {"revoked": await _call(approvals.revoke_schedule, _actor(request))}


# ── external models ──────────────────────────────────────────────────────────

@router.get("/external")
async def external_models(request: Request):
    """Priced OmniRoute models the operator may pick, and the current state."""
    permissions.require(request, permissions.EARNER_VIEW)
    info = await llm.external_info()
    try:
        prices = await llm.priced_models()
        models = sorted(({"id": model_id, **price} for model_id, price in prices.items()),
                        key=lambda item: (item["output"], item["id"]))
    except Exception as exc:
        models = []
        info.setdefault("reason", str(exc))
    return {"info": info, "usage": await _call(budget.external_usage), "models": models}


# ── wallet and income ────────────────────────────────────────────────────────

@router.get("/income")
async def income(request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(wallet.list_income)


class LinkRequest(BaseModel):
    opportunity_id: str


def _link_income(income_id: str, opp_id: str, actor: str) -> Dict[str, Any]:
    with store.tx() as connection:
        wallet.link(connection, income_id, opp_id, "manual", actor)
    return {"status": "linked"}


@router.post("/income/{income_id}/link")
async def link_income(income_id: str, payload: LinkRequest, request: Request):
    permissions.require(request, permissions.EARNER_MANAGE)
    return await _call(_link_income, income_id, payload.opportunity_id, _actor(request))


# ── submissions ──────────────────────────────────────────────────────────────

class SubmissionCreate(BaseModel):
    kind: str = Field(default="auto", pattern="^(auto|pr|comment)$")


@router.post("/opportunities/{opp_id}/submission")
async def create_submission(opp_id: str, request: Request, payload: Optional[SubmissionCreate] = None):
    permissions.require(request, permissions.EARNER_MANAGE)
    return await _call(submit.create_draft, opp_id, _actor(request), (payload or SubmissionCreate()).kind)


class SubmissionUpdate(BaseModel):
    body: str = Field(min_length=20, max_length=submit.MAX_BODY)
    title: Optional[str] = Field(default=None, max_length=submit.MAX_TITLE)


@router.put("/submissions/{submission_id}")
async def update_submission(submission_id: str, payload: SubmissionUpdate, request: Request):
    permissions.require(request, permissions.EARNER_MANAGE)
    return await _call(submit.update_draft, submission_id, payload.body, _actor(request), payload.title)


# ── history ──────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(request: Request, entity_id: Optional[str] = None):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(store.list_events, entity_id, 300)


@router.get("/spend")
async def spend(request: Request):
    permissions.require(request, permissions.EARNER_VIEW)
    return await _call(budget.history)
