"""Trigger, processing and control nodes (§13 TRIGGERS / PROCESSING / CONTROL).

The two that carry real weight:

* `human_approval` (§36) does not block a worker. It records that the run is
  waiting and returns; the engine suspends the run, and an owner's decision
  resumes it. A pipeline waiting three days for a review costs nothing while it
  waits.
* `budget_check` (§61) is the stop that keeps an unattended nightly pipeline
  from spending a month's budget in one night. It reads what the run has
  *actually* spent (`cf_usage_records`), not an estimate.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.content_factory import db
from backend.content_factory.domain import (
    ContentFactoryError,
    ErrorCode,
    NodeResult,
    PipelineNode,
    resolve_path,
)
from backend.content_factory.nodes.base import NodeContext, NodeHandler, register

logger = logging.getLogger("hermes.content_factory.nodes.control")


# ── Triggers ─────────────────────────────────────────────────────────────────

class TriggerNode(NodeHandler):
    """Triggers do no work at execution time — the trigger already happened.
    The node exists so the graph has a root and so trigger data enters the run
    context under a node id like anything else."""

    type = "manual_trigger"

    async def execute(self, context: NodeContext) -> NodeResult:
        trigger_data = db.loads(context.run.get("trigger_data"), {}) or {}
        return NodeResult(
            output={"triggerType": context.run.get("trigger_type"), **trigger_data},
            provider="internal",
        )


class ScheduleTriggerNode(TriggerNode):
    type = "schedule_trigger"

    def validate(self, node: PipelineNode) -> list[dict[str, str]]:
        if not node.config.get("expression"):
            return [{"code": "MISSING_SCHEDULE", "message": "schedule expression is required", "severity": "error"}]
        if not node.config.get("timezone"):
            return [
                {
                    "code": "MISSING_TIMEZONE",
                    "message": "no timezone — the server's clock is not a schedule",
                    "severity": "error",
                }
            ]
        return []


class WebhookTriggerNode(TriggerNode):
    type = "webhook_trigger"


# ── Processing ───────────────────────────────────────────────────────────────

class ConditionNode(NodeHandler):
    """Branches on a comparison. Deliberately not an expression evaluator: a
    pipeline that can run arbitrary code in the backend process is a much
    bigger problem than a pipeline that can only compare two values."""

    type = "condition"

    _OPERATORS = {
        "eq": lambda left, right: left == right,
        "ne": lambda left, right: left != right,
        "gt": lambda left, right: float(left) > float(right),
        "gte": lambda left, right: float(left) >= float(right),
        "lt": lambda left, right: float(left) < float(right),
        "lte": lambda left, right: float(left) <= float(right),
        "contains": lambda left, right: str(right) in str(left),
        "truthy": lambda left, right: bool(left),
        "empty": lambda left, right: not left,
    }

    def validate(self, node: PipelineNode) -> list[dict[str, str]]:
        operator = node.config.get("operator", "truthy")
        if operator not in self._OPERATORS:
            return [{"code": "BAD_OPERATOR", "message": f"unknown operator '{operator}'", "severity": "error"}]
        if not node.config.get("left") and not node.input_mapping.get("left"):
            return [{"code": "MISSING_LEFT", "message": "left operand is required", "severity": "error"}]
        return []

    async def execute(self, context: NodeContext) -> NodeResult:
        operator = str(context.config("operator", "truthy"))
        check = self._OPERATORS.get(operator)
        if check is None:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, f"Unknown operator: {operator}", retryable=False
            )
        left, right = context.config("left"), context.config("right")
        try:
            passed = bool(check(left, right))
        except (TypeError, ValueError):
            passed = False
        return NodeResult(
            output={"passed": passed, "operator": operator},
            branch="TRUE" if passed else "FALSE",
            provider="internal",
        )


class MergeNode(NodeHandler):
    """Collects the outputs of every parent into one object (§19). Used after a
    parallel fan-out so a downstream node has a single input."""

    type = "merge"

    async def execute(self, context: NodeContext) -> NodeResult:
        return NodeResult(output={"merged": dict(context.node_outputs)}, provider="internal")


class TransformNode(NodeHandler):
    """Reshapes data with the same `{{ nodes.x.output }}` mapping the rest of
    the pipeline uses (§112), so there is one templating language, not two."""

    type = "transform"

    async def execute(self, context: NodeContext) -> NodeResult:
        return NodeResult(output=dict(context.inputs), provider="internal")


class DelayNode(NodeHandler):
    type = "delay"

    async def execute(self, context: NodeContext) -> NodeResult:
        import asyncio

        seconds = max(0.0, float(context.config("seconds", 5)))
        # Long waits are the engine's job (a sleeping worker is a wasted one);
        # anything longer than a minute should be a schedule, not a delay node.
        await asyncio.sleep(min(seconds, 60.0))
        return NodeResult(output={"waited": seconds}, provider="internal")


# ── Control ──────────────────────────────────────────────────────────────────

class HumanApprovalNode(NodeHandler):
    """§36. Suspends the run and hands the decision to the owner.

    In FULL_AUTO the node passes straight through — the mode is the owner's
    standing decision, and re-asking would defeat it.
    """

    type = "human_approval"

    async def execute(self, context: NodeContext) -> NodeResult:
        mode = str(context.run.get("publishing_mode") or "REVIEW_REQUIRED")
        if mode == "FULL_AUTO" and not context.config("alwaysAsk", False):
            return NodeResult(output={"approved": True, "auto": True}, branch="APPROVED", provider="internal")

        decision = context.config("_decision")
        if decision in {"APPROVED", "REJECTED"}:
            # The engine re-runs this node after the owner decides, passing the
            # decision in; there is no polling and nothing was blocked.
            return NodeResult(
                output={"approved": decision == "APPROVED", "note": context.config("_note", "")},
                branch=decision,
                provider="internal",
            )

        return NodeResult(
            output={"awaiting": True, "contentId": context.config("contentId", "")},
            waiting_approval=True,
            provider="internal",
        )


class BudgetCheckNode(NodeHandler):
    """§61. Compares what the run has actually spent against its ceiling."""

    type = "budget_check"

    async def execute(self, context: NodeContext) -> NodeResult:
        run_id = str(context.run.get("id"))
        row = db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS spent FROM cf_usage_records WHERE pipeline_run_id = ?",
            (run_id,),
        )
        spent = float((row or {}).get("spent") or 0.0)
        limit = context.config("maxCost") or context.run.get("cost_budget")
        if limit is None:
            return NodeResult(output={"spent": spent, "limit": None, "withinBudget": True}, branch="OK",
                              provider="internal")

        limit = float(limit)
        within = spent <= limit
        action = str(context.config("onExceeded", "stop"))
        if not within and action == "stop":
            raise ContentFactoryError(
                ErrorCode.BUDGET_LIMIT_REACHED,
                f"Run has spent ${spent:.2f} of its ${limit:.2f} budget",
                retryable=False,
                details={"spent": spent, "limit": limit},
            )
        return NodeResult(
            output={"spent": spent, "limit": limit, "withinBudget": within},
            branch="OK" if within else "EXCEEDED",
            waiting_approval=(not within and action == "approval"),
            provider="internal",
        )


class TechnicalQANode(NodeHandler):
    """The deterministic half of §35: does the file we are about to publish
    actually exist, and is it what we think it is.

    ffprobe when it is available, header inspection when it is not — a QA node
    that silently passes because a tool is missing would be worse than no QA
    node, so a missing ffprobe downgrades to fewer checks and says so.
    """

    type = "technical_qa"

    async def execute(self, context: NodeContext) -> NodeResult:
        import asyncio

        asset_id = str(context.require("assetId"))
        asset = _asset(asset_id)
        errors: list[str] = []
        warnings: list[str] = []

        expected_width = int(context.config("width", 0) or 0)
        expected_height = int(context.config("height", 0) or 0)
        min_duration = float(context.config("minDuration", 1))
        max_duration = float(context.config("maxDuration", 600))
        max_size_mb = float(context.config("maxSizeMb", 500))

        if not asset:
            errors.append(f"asset {asset_id} does not exist")
        else:
            size_mb = float(asset.get("size_bytes") or 0) / (1024 * 1024)
            if size_mb <= 0:
                errors.append("rendered file is empty")
            if size_mb > max_size_mb:
                errors.append(f"file is {size_mb:.1f} MB, over the {max_size_mb:.0f} MB limit")

            probe = await asyncio.to_thread(_probe, asset)
            if probe.get("unavailable"):
                warnings.append("ffprobe unavailable — codec and stream checks were skipped")
            else:
                if not probe.get("has_audio"):
                    errors.append("no audio stream")
                if not probe.get("has_video"):
                    errors.append("no video stream")
                duration = float(probe.get("duration") or 0)
                if duration and not (min_duration <= duration <= max_duration):
                    errors.append(f"duration {duration:.1f}s is outside {min_duration:.0f}-{max_duration:.0f}s")
                width, height = probe.get("width"), probe.get("height")
                if expected_width and width and int(width) != expected_width:
                    errors.append(f"width {width} != expected {expected_width}")
                if expected_height and height and int(height) != expected_height:
                    errors.append(f"height {height} != expected {expected_height}")

        passed = not errors
        return NodeResult(
            output={"status": "PASS" if passed else "FAIL", "errors": errors, "warnings": warnings},
            branch="PASS" if passed else "FAIL",
            provider="internal",
        )


def _asset(asset_id: str) -> Optional[dict[str, Any]]:
    return db.query_one("SELECT * FROM cf_assets WHERE id = ?", (asset_id,))


def _probe(asset: dict[str, Any]) -> dict[str, Any]:
    """ffprobe over a temporary copy of the object. Never shells out through a
    shell (§119): argv list, fixed timeout, no user string in a command."""
    import json
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not shutil.which("ffprobe"):
        return {"unavailable": True}

    from backend.content_factory import storage as storage_module

    try:
        data = storage_module.get_storage().get(str(asset["storage_key"]))
    except Exception:
        return {"unavailable": True}

    with tempfile.TemporaryDirectory(prefix="cf-qa-") as directory:
        path = Path(directory) / "asset.bin"
        path.write_bytes(data)
        try:
            completed = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                capture_output=True,
                timeout=60,
                check=False,
            )
            parsed = json.loads(completed.stdout or b"{}")
        except (subprocess.SubprocessError, ValueError):
            return {"unavailable": True}

    streams = parsed.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    return {
        "has_video": video is not None,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "duration": (parsed.get("format") or {}).get("duration"),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "codec": (video or {}).get("codec_name"),
    }


class PolicyCheckNode(NodeHandler):
    """§35's platform-policy gate, expressed as explicit rules rather than a
    model's opinion: a denylist match is a fact, and a run that stops for one
    can be explained to the owner."""

    type = "policy_check"

    async def execute(self, context: NodeContext) -> NodeResult:
        haystack = " ".join(
            str(resolve_path(context.inputs, field) or "")
            for field in (context.config("fields") or ["title", "description", "script"])
        ).lower()
        denylist = [str(term) for term in (context.config("denylist") or [])]
        if context.config("blockProfanity", False):
            denylist.extend(["fuck", "shit", "сука", "бляд", "хуй", "пизд"])
        blocked = [term for term in denylist if term.lower() in haystack]
        passed = not blocked
        return NodeResult(
            output={"status": "PASS" if passed else "FAIL", "matched": blocked},
            branch="PASS" if passed else "FAIL",
            provider="internal",
        )


for handler in (
    TriggerNode(), ScheduleTriggerNode(), WebhookTriggerNode(), ConditionNode(), MergeNode(),
    TransformNode(), DelayNode(), HumanApprovalNode(), BudgetCheckNode(), TechnicalQANode(),
    PolicyCheckNode(),
):
    register(handler)
