# Hermes Autonomy Runtime

Hermes uses a bounded autonomy loop inspired by the specialist contracts in
`agency-agents`, the local structural index in `codebase-memory-mcp`, and the
capability doctor/provider fallbacks in `agent-reach`.

## Execution loop

1. The planner emits stable step IDs, expected outputs, and acceptance criteria.
2. A specialist agent executes each step with the minimum available tool set.
3. The verifier rejects exceptions, empty output, and failed structured results.
4. A failed step receives concrete feedback and may retry up to three times.
5. The plan ledger and a compact outcome are retained in project memory.

## Dev-run executor loop (`backend/dev_runs.py`)

A dev-run is the durable plan → act → observe → verify loop that develops code
in a per-run sandbox. Its guarantees:

- **Observation.** Every tool call's full output is stored on the step and the
  most recent ones are replayed verbatim to the model, budgeted against the
  active context window. The step `summary` stays the short ledger line the
  dashboard renders; the run detail API never ships the raw blobs.
- **Multi-step.** Every tool call in a model response is executed in order (up
  to four per iteration); extras are reported back rather than dropped.
- **Completion is verified, not claimed.** `DONE:` is rejected unless tests have
  passed since the last write; the failure is fed back and the run continues.
  The same gate runs before any `git_push`.
- **Loop protection.** Identical tool+arguments calls that cannot yield new
  information are refused with corrective feedback and then fail the run;
  consecutive failures, a hard iteration ceiling, cost/iteration/wall budgets
  and the kill switch all stop it. Cost is derived from token usage, so a
  `cost_budget` actually binds.
- **Recovery.** Transient provider failures are retried with backoff inside the
  iteration; a crashed iteration is auto-resumed a bounded number of times.
  Approving the Control Plane task a run is waiting on resumes it directly — no
  second manual click.
- **Untrusted output.** Tool results are framed to the model as data, never as
  instructions.

`GET /api/dev-runs/metrics` reports success, autonomous-completion and
human-intervention rates, tool error rate and verification failure rate. Bounds
are tunable through the `DEV_RUNS_*` variables documented in `.env.example`.

## Capability discovery in the chat loop

The chat agent still pre-selects tools by keyword from the user's message —
right for a small local model — but a multi-step request whose later steps need
a tool the first message did not match is no longer stuck. When any tool is
offered, the `list_tools` escape hatch (R0, pure registry metadata) comes with
it: the model describes what it needs, and the matches are activated for the
rest of the turn. Discovery only widens what the model *sees*; the pool it draws
from is already principal-filtered (`backend/tool_permissions.py`), so a
sub-agent or a public-channel agent can never discover its way past its
permissions, and execution still passes the Control Plane risk gates.

## Project memory

The repository is mounted read-only at `/workspace`. The local SQLite index
stores file paths, hashes, languages, symbols, imports, and short structural
summaries. It does not persist source contents. Indexing is incremental and
confined to `PROJECT_WORKSPACE_ROOT`.

## Capability safety

The capability doctor uses side-effect-free probes and ordered fallbacks. An
unavailable reviewed capability may create an R3/R4 proposal in Control Plane,
but no package or MCP server is installed automatically. Approval is a review
gate, not an executable command. Installation requires a separate owner action
after version, license, checksum, platform, scope, and rollback checks.

## API

- `GET /api/autonomy/summary`
- `GET /api/autonomy/capabilities`
- `POST /api/autonomy/capabilities/{id}/propose`
- `POST /api/autonomy/index`
- `GET /api/autonomy/memory/search?q=...`
- `POST /api/autonomy/memory`
- `GET|POST /api/autonomy/plans`
