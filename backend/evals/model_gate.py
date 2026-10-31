"""Quality gate for a model swap: run golden sets against the configured local model.

    docker exec -i hermes-web-backend python -m backend.evals.model_gate [--json]

The privacy gateway's local secret pass and tool selection depend on the model,
its prompt and its sampling, so a new model (or a new build of the same one)
must pass before it serves traffic: a swap once turned that pass into "[]" for
everything. The personal-data check runs the whole gateway (patterns, Russian
name model and the local model) on golden messages.
Calls go through llm_gate at background priority. Exit 1 when a threshold fails.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
THRESHOLDS = {"secret_recall": 0.85, "secret_clean_precision": 0.9, "pii_recall": 0.9,
              "pii_clean_precision": 0.9, "tool_accuracy": 0.8}


async def secret_scores() -> dict:
    from backend.privacy.detectors.local_llm import _ask_local_model

    cases = json.loads((HERE / "golden_secrets.json").read_text(encoding="utf-8"))
    expected = found = clean = clean_ok = 0
    misses = []
    for case in cases:
        spans = await _ask_local_model(case["text"])
        joined = " ".join(spans)
        for item in case["must"]:
            expected += 1
            if item in joined or any(span in item and len(span) >= len(item) // 2 for span in spans):
                found += 1
            else:
                misses.append(item)
        if case.get("clean"):
            clean += 1
            clean_ok += not spans
    return {"secret_recall": found / expected if expected else 1.0,
            "secret_clean_precision": clean_ok / clean if clean else 1.0, "secret_misses": misses}


async def pii_scores() -> dict:
    from backend.privacy.gateway import redact_text
    from backend.privacy.vault import PlaceholderVault

    cases = json.loads((HERE / "golden_pii.json").read_text(encoding="utf-8"))
    expected = hidden = clean = clean_ok = 0
    misses, noisy = [], []
    for case in cases:
        redacted = await redact_text(case["text"], PlaceholderVault())
        for item in case["must"]:
            expected += 1
            if item in redacted:
                misses.append(item)
            else:
                hidden += 1
        if case.get("clean"):
            clean += 1
            if redacted == case["text"]:
                clean_ok += 1
            else:
                noisy.append(redacted)
    return {"pii_recall": hidden / expected if expected else 1.0,
            "pii_clean_precision": clean_ok / clean if clean else 1.0, "pii_misses": misses, "pii_noise": noisy}


async def tool_scores() -> dict:
    from backend.agent import agent_instance
    from backend.llamacpp_client import LlamaCppClient
    from backend.llm_gate import PRIORITY_BACKGROUND
    from backend.tools import TOOLS_SCHEMA

    cases = json.loads((HERE / "golden_tools.json").read_text(encoding="utf-8"))
    names = {name for case in cases for name in case["expect"]} | {"list_tools", "web_search", "set_alarm"}
    tools = [tool for tool in TOOLS_SCHEMA if tool.get("function", {}).get("name") in names]
    client = LlamaCppClient(agent_instance.local_base_url, timeout=300)
    correct, wrong = 0, []
    for case in cases:
        result = await client.chat(model=agent_instance.configured_model, temperature=0.0, max_tokens=512,
                                   think=False, tools=tools, priority=PRIORITY_BACKGROUND,
                                   messages=[{"role": "system", "content": "Ты ассистент. Вызывай инструмент, только если он нужен."},
                                             {"role": "user", "content": case["message"]}])
        called = [call.get("function", {}).get("name") or call.get("name") for call in result.tool_calls]
        ok = called[:1] == case["expect"][:1] if case["expect"] else not called
        correct += ok
        if not ok:
            wrong.append({"message": case["message"], "expected": case["expect"], "called": called})
    return {"tool_accuracy": correct / len(cases), "tool_errors": wrong}


async def main() -> int:
    from backend.agent import agent_instance

    started = time.monotonic()
    report = {"model": agent_instance.configured_model, **await secret_scores(), **await pii_scores(), **await tool_scores()}
    report["seconds"] = round(time.monotonic() - started, 1)
    report["failed"] = [name for name, floor in THRESHOLDS.items() if report[name] < floor]
    if "--json" in sys.argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for name, floor in THRESHOLDS.items():
            print(f"{name}: {report[name]:.2f} (порог {floor})")
        for item in report["secret_misses"]:
            print("  секрет пропущен:", item)
        for item in report["pii_misses"]:
            print("  персональные данные не скрыты:", item)
        for item in report["pii_noise"]:
            print("  лишняя маска:", item)
        for item in report["tool_errors"]:
            print("  инструмент:", item)
        print("ПРОВАЛ: " + ", ".join(report["failed"]) if report["failed"] else "ОК: модель проходит гейт")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
