// Autonomous Earner API client (backend/earner/api.py). The fetch interceptor
// adds the operator's Bearer token; nothing here holds a secret of its own.

export interface BudgetEntry { used: number; limit: number; remaining: number; actual?: number }

export interface EarnerSource { id: string; kind: 'github_issues' | 'rss' | 'demo' | 'freelancer_com' | 'freelancehunt' | 'opire' | 'superteam'; enabled: boolean; query?: string; url?: string; skill_ids?: number[]; types?: string[] }

/** Host each API source needs on the allowlist (mirrors store.SOURCE_HOSTS). */
export const SOURCE_HOSTS: Record<string, string> = {
  github_issues: 'api.github.com', freelancer_com: 'www.freelancer.com', freelancehunt: 'api.freelancehunt.com', opire: 'api.opire.dev',
  superteam: 'superteam.fun',
};

export interface EarnerSettings {
  enabled: boolean;
  directions: string[];
  constraints: string;
  sources: EarnerSource[];
  allowed_hosts: string[];
  limits: Record<string, number>;
  paid_search_enabled: boolean;
  external_enabled: boolean;
  external_model: string;
  external_month_usd: number;
  wallet: { address: string; networks: string[] };
  revision: number;
  updated_at: string;
}

export interface EarnerRun {
  id: string; trigger: string; status: string; approval_id: string; created_at: string;
  started_at: string | null; finished_at: string | null; candidates_new: number; candidates_seen: number;
  evaluated: number; proposed: number; fetches: number; error: string;
  sources_report: { source_id: string; kind: string; ok: boolean; found: number; error?: string; checked_at: string }[];
}

export interface EarnerOverview {
  settings: EarnerSettings;
  budget: {
    day: string; timezone: string; tasks: BudgetEntry; llm_calls: BudgetEntry; tokens: BudgetEntry;
    fetches: BudgetEntry; sandbox_runs: BudgetEntry; sandbox_concurrency: { used: number; limit: number };
    est_cost_usd: number; est_cost_note: string; submissions: BudgetEntry;
    external: ExternalUsage;
  };
  llm: { provider: string; host: string; model: string; local: boolean; reason: string };
  sandbox: { available: boolean; reason: string; runtime: string; image: string; runtimes?: string[] };
  repo_sandbox?: { available: boolean; reason: string; runtime: string; image: string };
  schedule: null | { approval_id: string; interval_hours: number; valid_until: string; max_runs: number; runs_done: number; next_run_at: string; active: number; status_reason: string; auto_generate?: number; auto_execute?: number };
  counts: Record<string, number>;
  pending_approvals: number;
  report: { potential: number; prepared: number; delivered_manual: number; paid_manual: number; paid_onchain: number; demo: number; note: string };
  runs: EarnerRun[];
  income: { wallet: { address: string; networks: string[] }; total_usd: number; linked_usd: number; note: string; cursors: WalletCursor[] };
}

export interface ExternalUsage { month: string; enabled: boolean; model: string; limit_usd: number; used_usd: number; reserved_usd: number; remaining_usd: number }

export interface ExternalModels {
  info: { enabled: boolean; model: string; month_usd: number; available: boolean; reason: string; price_input?: number; price_output?: number };
  usage: ExternalUsage;
  models: { id: string; input: number; output: number; max_output_tokens: number }[];
}

export interface WalletCursor { network: string; address: string; last_block: number; checked_at: string; error: string }

export interface IncomeRow {
  id: string; network: string; token: string; amount: string; amount_usd: number; from_address: string; tx_hash: string;
  block_number: number; opportunity_id: string; link_kind: string; created_at: string; explorer_url: string;
}

export interface IncomeResponse { wallet: { address: string; networks: string[] }; income: IncomeRow[]; cursors: WalletCursor[]; total_usd: number; linked_usd: number; note: string }

export interface Submission {
  id: string; opportunity_id: string; kind: string; target: string; body: string; status: string;
  response_url: string; error: string; created_at: string; updated_at: string; sent_at: string | null;
  title?: string; payload?: { head_branch?: string; base?: { owner: string; repo: string; branch: string; commit: string } };
}

export interface OpportunityRow {
  id: string; run_id: string; source_id: string; source_kind: string; url: string; title: string;
  claimed_reward: string; source_state: string; fetched_at: string; last_seen_at: string; is_demo: number;
  stale: number; status: string; error: string; reject_reason: string; delivered_at: string | null;
  paid_at: string | null; created_at: string; updated_at: string; effort_hours?: number | null; uncertainty?: string | null;
}

export interface Proposal {
  decision: string; reject_reason: string; fit_reason: string; deliverable: string; acceptance_criteria: string[];
  effort_hours: number; dependencies: string[]; payment_terms: string; payment_evidence: string; uncertainty: string;
  risks: string[]; builder_spec: string; source_url: string; reward_claimed: string; payment_confirmed: boolean; is_demo: boolean;
}

export interface ArtifactFile { path: string; content: string; mode?: string }

export interface RepoPhase {
  exit_code: number | null; timed_out: boolean; duration_s?: number; addopts_cleared?: boolean; stdout?: string; stderr?: string;
  tests: { total: number; skipped: number; failed_count: number; failed: string[] } | null;
}

export interface Approval {
  id: string; action: 'research' | 'generate' | 'execute' | 'schedule' | 'autopilot' | 'submit'; target_id: string; content_hash: string;
  summary: { title: string; will_do: string[]; data: string[]; limits: string[]; not_included: string[]; diff?: string };
  status: string; status_reason: string; created_at: string; expires_at: string; requested_by: string;
  decided_at: string | null; decided_by: string;
}

export interface EarnerEvent { id: number; entity_type: string; entity_id: string; kind: string; message: string; actor: string; created_at: string }

export interface OpportunityDetail extends OpportunityRow {
  excerpt: string; labels: string[]; source_created_at: string; grant_id: string;
  evaluation: Record<string, unknown>; proposal: Proposal | Record<string, never>; proposal_hash: string;
  artifact: {
    files?: ArtifactFile[];
    static?: {
      imports?: string[]; risks: string[]; test_files?: string[]; test_count: number; bytes?: number;
      files_changed?: number; added?: number; removed?: number; new_files?: string[]; tests_touched?: string[];
      context?: string[]; picked?: string[]; ai_policy?: { status: string; evidence: { path: string; text: string; kind: string }[] };
    };
    notes?: string; model?: string; provider?: string; attempt?: number;
    mode?: 'repo'; diff?: string; summary?: string; tests?: string[];
    repo?: { owner: string; repo: string; default_branch: string; commit: string; tarball_sha256: string; tarball_bytes: number; license: string; language: string };
    test_plan?: { runner: string; pythonpath: string[]; dependencies: string[] };
  };
  artifact_hash: string;
  review: { verdict?: string; accepted?: boolean; criteria?: { criterion: string; met: boolean; note: string }[]; quality_issues?: string[]; risks?: string[]; summary?: string; note?: string };
  execution: {
    status?: string; exit_code?: number | null; duration_s?: number; stdout?: string; stderr?: string; runtime?: string; detail?: string;
    mode?: 'repo'; runner?: string; baseline?: RepoPhase | null; patched?: RepoPhase | null;
    new_failures?: string[]; fixed?: string[]; deps_missing?: string[]; touched_tests_passed?: string[];
  };
  delivered_note: string; paid_note: string;
  submissions: Submission[];
  events: EarnerEvent[];
  approvals: Approval[];
}

export interface SpendHistory {
  days: { day: string; tasks: number; llm_calls: number; tokens_reserved: number; tokens_used: number; fetches: number; sandbox_runs: number; est_cost_usd: number }[];
  calls: { id: string; day: string; entity_id: string; stage: string; model: string; status: string; reserved_tokens: number; prompt_tokens: number; completion_tokens: number; duration_ms: number; est_cost_usd: number; error: string; created_at: string; provider?: string; cost_usd?: number }[];
  months?: { month: string; usd_reserved: number; usd_used: number }[];
}

export class EarnerApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function earnerApi<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api/earner${path}`, { ...init, headers });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = typeof data?.detail === 'string' ? data.detail : `Запрос не удался (${response.status})`;
    throw new EarnerApiError(detail, response.status);
  }
  return await response.json() as T;
}

export function confirmApproval(approval: Approval) {
  return earnerApi<Record<string, unknown>>(`/approvals/${encodeURIComponent(approval.id)}/confirm`, {
    method: 'POST',
    // The server refuses a confirm without this header: it marks a request
    // made from the confirmation screen, not from some other code path.
    headers: { 'X-Hermes-Intent': 'earner-approve' },
    body: JSON.stringify({ content_hash: approval.content_hash }),
  });
}

export async function downloadArtifact(oppId: string) {
  const response = await fetch(`/api/earner/opportunities/${encodeURIComponent(oppId)}/artifact.zip`);
  if (!response.ok) throw new EarnerApiError(`Скачивание не удалось (${response.status})`, response.status);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `earner-${oppId}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Only https links from the collector become clickable; anything else stays text. */
export function safeHttpsUrl(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname.endsWith('.invalid')) return null;
    return url.toString();
  } catch {
    return null;
  }
}

export const STATUS_LABEL: Record<string, { label: string; tone: 'info' | 'wait' | 'ok' | 'bad' | 'muted' | 'run' }> = {
  new: { label: 'найдено', tone: 'info' },
  evaluating: { label: 'оценка', tone: 'run' },
  proposed: { label: 'ждёт решения о генерации', tone: 'wait' },
  rejected_by_evaluator: { label: 'отклонено Evaluator', tone: 'muted' },
  queued_build: { label: 'в очереди на генерацию', tone: 'run' },
  building: { label: 'генерация', tone: 'run' },
  reviewing: { label: 'ревью', tone: 'run' },
  rejected_by_reviewer: { label: 'отклонено Reviewer', tone: 'bad' },
  awaiting_execution: { label: 'ждёт решения об исполнении', tone: 'wait' },
  queued_execution: { label: 'в очереди на sandbox', tone: 'run' },
  executing: { label: 'sandbox', tone: 'run' },
  checks_passed: { label: 'проверки пройдены', tone: 'ok' },
  checks_failed: { label: 'проверки не пройдены', tone: 'bad' },
  failed: { label: 'ошибка', tone: 'bad' },
  interrupted: { label: 'прервано', tone: 'bad' },
  dismissed: { label: 'скрыто', tone: 'muted' },
};

export const ACTION_LABEL: Record<string, string> = {
  research: 'Исследование', generate: 'Генерация', execute: 'Исполнение', schedule: 'Расписание', autopilot: 'Автопилот', submit: 'Отправка',
};

export const APPROVAL_STATUS: Record<string, string> = {
  pending: 'ожидает', consumed: 'подтверждено', declined: 'отклонено', expired: 'истекло',
  superseded: 'заменено', invalidated: 'аннулировано',
};

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}
