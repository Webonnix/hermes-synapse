"""Autonomous Earner: find small paid programming tasks, prepare a solution,
check it in isolation — and leave every outward step to a human.

Pipeline (each arrow that crosses a ⛔ needs a fresh operator approval):

    settings ⛔research→ Scout → Evaluator → proposal
             ⛔generate→ Builder → Reviewer → artifact
             ⛔execute → sandbox checks → result + manual next steps

Nothing here applies to a customer, posts a result or moves money. Delivery and
payment exist only as facts the operator records by hand.

Boundaries (docs/CONTRACTS.md, «Autonomous Earner»):

* **Approvals are a server-side state machine** (approvals.py), not a hidden
  button: bound to (action, target, sha256 of the exact content), expiring,
  single-use, consumed in the same IMMEDIATE transaction that queues the work.
  Only an operator session holding ``earner.approve`` can confirm one; no agent
  tool, backend key or internal token reaches that endpoint.
* **Stages are plain LLM calls without tools** (stages.py). Web pages, task
  descriptions and model output are data; a model that a poisoned page steers
  can still only return JSON that the next step validates.
* **Local model only** (llm.py): requests go to llama.cpp through llm_gate at
  background priority. There is no fallback to a paid provider — an unreachable
  model fails the stage.
* **Egress is opt-in** (egress.py): exact host allowlist set by the operator,
  HTTPS on 443, the vetted IP is pinned for the connection (no DNS rebinding),
  redirects are never followed, size/time/count are capped.
* **Generated code never runs in the backend** (sandbox.py): only in a gVisor
  container without network, mounts or secrets. Without ``runsc`` execution is
  off — there is no fallback to runc.
* **Budgets live in hermes.db** (budget.py) and are reserved atomically before
  work starts; a crash never re-runs a started stage (worker.py).
"""
