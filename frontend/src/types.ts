import React from 'react';

export interface LLMRunMeta {
  status?: 'success' | 'tool_call' | 'empty' | 'refusal' | 'timeout' | 'provider_error' | 'parse_error' | string;
  model?: string | null;
  provider?: string | null;
  finish_reason?: string | null;
  request_id?: string | null;
  latency_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  tool_iterations?: number | null;
}

export interface ChatMessage {
  id?: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  chat_id?: string | number;
  cost_usd?: number;
  meta?: LLMRunMeta;
  run_id?: string;
  streaming?: boolean;
  thinking?: string;
}

export interface DecisionLog {
  timestamp: string;
  session_id: string;
  model: string;
  latency_ms: number;
  success: boolean;
  error: string | null;
  prompt_tokens_estimate: number;
  user_message: string;
  assistant_response: string;
  traces?: { timestamp: string; agent: string; action: string; message: string; status: string }[];
  agent_id?: string;
  completion_tokens_estimate?: number;
  cost_usd?: number;
}

export interface ActivityLog {
  timestamp: string;
  type: 'active' | 'idle';
  source: string;
  message: string;
  token_cost: number;
}

export interface SystemConfig {
  system_prompt: string;
  model: string;
  fast_mode?: boolean;
  max_history_len?: number;
  condenser_enabled?: boolean;
  condense_trigger_extra?: number;
  max_tokens?: number;
  tool_max_tokens?: number;
  temperature?: number;
  auto_rag?: boolean;
  memory_enabled?: boolean;
  memory_auto_save?: boolean;
  memory_max_items?: number;
  telegram_reply_mode?: 'text' | 'voice' | 'both' | string;
  provider?: 'ollama' | 'openrouter' | 'openai_compatible' | string;
  api_base?: string;
  ollama_base_url?: string;
  openai_api_base?: string;
  ollama_num_ctx?: number;
  ollama_keep_alive?: string | number;
  ollama_think?: boolean | 'low' | 'medium' | 'high' | string;
}

export interface OllamaModel {
  name: string;
  model?: string;
  size?: number;
  digest?: string;
  modified_at?: string;
  size_vram?: number;
  context_length?: number;
  expires_at?: string;
  details?: {
    family?: string;
    parameter_size?: string;
    quantization_level?: string;
    format?: string;
  };
}

export interface OllamaStatus {
  available: boolean;
  base_url: string;
  version?: string;
  models_count?: number;
  running_count?: number;
  error?: string;
  code?: string;
}

export interface AgentModel {
  id: string;
  name: string;
  system_prompt: string;
  model: string;
  created_at?: string;
  agent_type?: string;
  parent_id?: string | null;
  project?: string;
  project_id?: string;
  project_name?: string;
  workspace?: string;
  skills?: string;
  x?: number;
  y?: number;
  temperature?: number;
  role?: string;
  status?: 'idle' | 'working' | 'error' | 'disabled' | string;
  is_enabled?: boolean;
  model_provider?: string;
  model_type?: 'local' | 'external' | string;
  model_params?: Record<string, unknown>;
  current_task?: string;
  last_action?: string;
  last_error?: string;
  progress?: number;
  updated_at?: string;
  recent_events?: AgentEvent[];
  budget_usd_limit?: number | null;
  budget_period?: 'monthly' | 'lifetime' | string;
  tier_id?: string | null;
}

// Spend vs. configured budget for one agent — backend/database.py::get_agent_budget_status.
export interface AgentBudgetStatus {
  agent_id: string;
  budget_usd_limit: number | null;
  budget_period: 'monthly' | 'lifetime' | string;
  used_usd: number;
  remaining_usd: number | null;
  exceeded: boolean;
}

// A named preset of feature-flag defaults an agent can be assigned to — backend/agent_tiers.py.
export interface AgentTier {
  id: string;
  name: string;
  description: string;
  budget_usd_limit_default: number | null;
  budget_period_default: 'monthly' | 'lifetime' | string;
  allow_external_provider: boolean;
  allow_messenger: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

// A dedicated Telegram bot for one agent — backend/agent_messenger_governance.py.
export interface AgentTelegramBinding {
  id: string;
  subagent_id: string;
  platform: 'telegram' | string;
  bot_username: string;
  allowed_chat_ids: string[];
  status: 'awaiting_approval' | 'approved' | 'active' | 'failed' | 'revoked' | string;
  control_task_id?: string;
  created_at: string;
  updated_at: string;
}

// A dedicated Element/Matrix account for one agent — backend/agent_messenger_governance.py.
export interface AgentMatrixBinding {
  id: string;
  subagent_id: string;
  platform: 'matrix' | string;
  bot_username: string; // resolved Matrix user id, e.g. "@agent:matrix.org" — reuses Telegram's column name
  allowed_chat_ids: string[]; // Matrix room ids
  status: 'awaiting_approval' | 'approved' | 'active' | 'failed' | 'revoked' | string;
  control_task_id?: string;
  created_at: string;
  updated_at: string;
}

// Cross-agent, cross-platform row from GET /api/messenger-bindings — powers the
// central "Каналы связи" admin page (MessengerChannelsTab.tsx).
export interface MessengerBinding {
  id: string;
  subagent_id: string;
  agent_name: string;
  platform: string;
  bot_username: string;
  allowed_chat_ids: string[];
  status: 'awaiting_approval' | 'approved' | 'active' | 'failed' | 'revoked' | string;
  control_task_id?: string;
  created_at: string;
  updated_at: string;
  system_prompt_override?: string | null;
  response_mode: 'draft' | 'auto_labeled' | string;
  /** 'owner_only' answers the allow-list only; 'token' opens the bot to anyone
   * holding an access token issued in the "Доступ к ботам" panel. */
  access_mode: 'owner_only' | 'token' | string;
  default_plan_id?: string | null;
  welcome_message?: string | null;
  last_error?: string | null;
}

// One entry from GET /api/messenger-bindings/activity — backend/channel_activity.py's
// in-memory feed of what bot_access_gate.authorize() decided about an incoming message.
export interface ChannelActivityEvent {
  ts: number;
  binding_id: string;
  platform: string;
  kind: 'proceed' | 'reply' | 'ignore' | 'invite' | 'error' | string;
  detail: string;
  chat_id: string;
  sender: string;
}

// A drafted reply from a 'draft'-mode channel binding, queued for the owner
// to review, optionally edit, and explicitly send — see backend/channel_replies.py.
export interface PendingChannelReply {
  id: string;
  binding_id: string;
  platform: string;
  subagent_id: string;
  chat_id: string;
  incoming_from: string;
  incoming_text: string;
  drafted_reply: string;
  status: 'pending' | 'sent' | 'discarded' | string;
  created_at: string;
  updated_at: string;
  sent_at?: string | null;
}

// An external LLM provider an agent can be bound to (agent.model_provider references
// its `id`, or the literal "ollama" for the always-available local model). The API key
// itself is never returned by the backend — see backend/provider_governance.py.
export interface ProviderBinding {
  id: string;
  name: string;
  provider_type: string;
  api_base: string;
  status: 'awaiting_approval' | 'approved' | 'active' | 'failed' | 'revoked' | string;
  control_task_id?: string;
  created_at: string;
  updated_at: string;
}

export interface AgentEvent {
  id: number;
  agent_id: string;
  timestamp: string;
  event_type: string;
  message: string;
  status: string;
  task?: string;
  metadata?: Record<string, unknown>;
}

export interface WorkflowTask {
  id: string;
  parent_id?: string | null;
  origin: string;
  requester: string;
  goal: string;
  tool_name?: string | null;
  tool_arguments?: Record<string, unknown>;
  assignee: string;
  risk_class: 'R0' | 'R1' | 'R2' | 'R3' | 'R4';
  autonomy_level: 'L0' | 'L1' | 'L2' | 'L3' | 'L4' | 'L5';
  data_class: string;
  status: 'queued' | 'running' | 'blocked' | 'awaiting_approval' | 'approved' | 'done' | 'failed' | 'killed' | 'rejected';
  approvals_required: number;
  approval_count: number;
  approval_required: boolean;
  budget_commands: number;
  budget_tokens: number;
  budget_wallclock_s: number;
  commands_used: number;
  tokens_used: number;
  acceptance: string[];
  rollback: string;
  result: string;
  error: string;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface WorkflowEvent {
  id: number;
  evidence_id: string;
  task_id?: string | null;
  event_type: string;
  actor: string;
  message: string;
  risk_class: string;
  confidence: string;
  output_hash: string;
  metadata?: Record<string, unknown>;
  created_at: string;
}

export interface ControlPlaneSummary {
  state: {
    kill_switch: boolean;
    reason: string;
    updated_by: string;
    updated_at: string;
  };
  counts: Record<string, number>;
  pending_approvals: WorkflowTask[];
  tasks: WorkflowTask[];
  events: WorkflowEvent[];
  policy: {
    risk_levels: string[];
    unknown_tools: string;
    r4_double_confirmation: boolean;
  };
}

export interface AutonomyCapability {
  id: string;
  label: string;
  required: boolean;
  status: 'ready' | 'missing';
  active_provider?: string | null;
  install_available: boolean;
  providers: Array<{ id: string; status: 'ready' | 'missing' | 'broken'; detail: string }>;
}

export interface AutonomyPlan {
  id: string;
  goal: string;
  tier: string;
  status: 'planned' | 'running' | 'completed' | 'failed' | string;
  capabilities: string[];
  steps: Array<{
    id: string;
    agent: string;
    title: string;
    status: string;
    attempts: number;
  }>;
  updated_at: string;
}

export interface AutonomySummary {
  workspace: string;
  capabilities: {
    status: 'ready' | 'degraded';
    ready: number;
    total: number;
    checked_at: string;
    capabilities: AutonomyCapability[];
  };
  memory: {
    files: number;
    bytes: number;
    fresh_at?: string | null;
    entries: number;
  };
  plans: AutonomyPlan[];
  proposals: Array<{
    id: string;
    capability_id: string;
    status: string;
    risk_class: string;
    control_task_id?: string | null;
    plan?: {
      recipe?: {
        package?: string;
        version?: string;
        license?: string;
      };
      enabled?: boolean;
      isolation?: Record<string, boolean>;
    };
    created_at: string;
    updated_at?: string;
  }>;
}

export interface SystemStats {
  available?: boolean;
  cpu_load_percent?: number | null;
  ram_used_percent?: number | null;
  ram_total_gb?: number | null;
  disk_used_percent?: number | null;
  disk_total_gb?: number | null;
  disk_used_gb?: number | null;
  status: string;
  scope?: string;
  source?: string;
  warning?: string;
  host_error?: string;
  unavailable?: string[];
  error?: string | null;
  collected_at?: string;
  age_seconds?: number | null;
  stale?: boolean;
  host?: HostTelemetry;
  runtime?: Omit<SystemStats, 'host' | 'runtime'>;
}

export interface HostTelemetry {
  hostname?: string;
  os?: string;
  kernel?: string;
  architecture?: string;
  uptime_seconds?: number;
  boot_time?: string | null;
  process_count?: number;
  cpu?: {
    model?: string;
    logical_cores?: number;
    usage_percent?: number | null;
    load_1m?: number | null;
    load_5m?: number | null;
    load_15m?: number | null;
  };
  memory?: {
    total_bytes?: number;
    used_bytes?: number;
    available_bytes?: number;
    usage_percent?: number | null;
    swap_total_bytes?: number;
    swap_used_bytes?: number;
  };
  disks?: Array<{
    device?: string;
    mountpoint?: string;
    filesystem?: string;
    total_bytes?: number;
    used_bytes?: number;
    available_bytes?: number;
    usage_percent?: number | null;
  }>;
  network?: {
    primary_ip?: string | null;
    rx_bytes?: number;
    tx_bytes?: number;
    rx_bytes_per_second?: number;
    tx_bytes_per_second?: number;
    interfaces?: Array<{
      name?: string;
      rx_bytes?: number;
      tx_bytes?: number;
      rx_bytes_per_second?: number;
      tx_bytes_per_second?: number;
    }>;
  };
  gpus?: Array<{
    index?: number;
    name?: string;
    uuid?: string;
    driver_version?: string;
    memory_total_bytes?: number | null;
    memory_used_bytes?: number | null;
    memory_usage_percent?: number | null;
    utilization_percent?: number | null;
    memory_utilization_percent?: number | null;
    temperature_celsius?: number | null;
    power_draw_watts?: number | null;
    power_limit_watts?: number | null;
    fan_percent?: number | null;
  }>;
  containers?: Array<{
    name?: string;
    image?: string;
    state?: string;
    health?: string;
    status?: string;
    ports?: string;
    cpu_percent?: number | null;
    memory_percent?: number | null;
    memory_usage?: string | null;
    network_io?: string | null;
    block_io?: string | null;
    pids?: number | null;
  }>;
}

export interface RenderedListItem {
  indent: number;
  content: React.ReactNode[];
}

export interface AppSettings {
  language: string; // BCP-47 short code: 'ru', 'en', 'he', 'de', 'es', 'fr'
}

export interface ChatSession {
  id: string;
  title: string;
  agent_id?: string;
  /** ISO timestamp of the session's most recent message; absent for a brand-new chat. */
  updated_at?: string | null;
}

export type DevRunStatus =
  | 'backlog' | 'planned' | 'running' | 'paused' | 'awaiting_approval'
  | 'verifying' | 'done' | 'failed' | 'cancelled';

export interface DevRunStep {
  id: string;
  run_id: string;
  seq: number;
  phase: string;
  tool: string;
  summary: string;
  status: string;
  created_at: string;
}

export interface DevRun {
  id: string;
  goal: string;
  status: DevRunStatus;
  plan_id: string | null;
  trace_id: string | null;
  iter_used: number;
  iter_budget: number;
  cost_used: number;
  cost_budget: number | null;
  wall_deadline: string | null;
  checkpoint_step: string | null;
  status_reason: string;
  created_at: string;
  updated_at: string;
  assignee_agent_id?: string | null;
  demo_url?: string | null;
  sandbox_container?: string | null;
  steps?: DevRunStep[];
}

export interface DevRunEvent {
  type: 'dev_run_event';
  run_id: string;
  status: DevRunStatus;
  event: string;
  summary: string;
}

// ── Public bot access (backend/bot_access.py) ───────────────────────────────

export interface AccessPlan {
  id: string;
  name: string;
  description: string;
  period: 'daily' | 'weekly' | 'monthly' | 'lifetime' | string;
  limit_usd: number | null;
  limit_tokens: number | null;
  limit_messages: number | null;
  rate_limit_per_min: number;
  max_message_chars: number;
  /** null means "the default public tool set"; an array narrows it further. */
  allowed_tools: string[] | null;
  system_prompt_suffix: string;
  welcome_message: string;
  is_active: boolean;
  /** Set together with is_purchasable, this turns a quota preset into a
   *  subscription a stranger can buy from the bot itself. */
  price_usd: number | null;
  is_purchasable: boolean;
  /** Length of one paid period; null on a lifetime (one-off) purchase. */
  duration_days: number | null;
  /** null means "any agent" (a shared preset). Set, this tariff can only be
   *  issued/sold for that one subagent — the backend refuses a mismatch. */
  subagent_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AccessToken {
  id: string;
  /** Masked form (HRM-abc123…) — the full string only ever exists in `plaintext`. */
  display: string;
  token_prefix: string;
  /** Present only in the response that issued the token, never on later reads. */
  plaintext?: string;
  label: string;
  binding_id: string;
  subagent_id: string;
  plan_id: string | null;
  status: 'active' | 'suspended' | 'revoked' | string;
  max_chats: number;
  expires_at: string | null;
  period_started_at: string;
  used_usd: number;
  used_tokens_in: number;
  used_tokens_out: number;
  used_messages: number;
  limit_usd: number | null;
  limit_tokens: number | null;
  limit_messages: number | null;
  notes: string;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
}

export interface SubscriberUsageRow {
  id: number;
  ts: string;
  model: string;
  provider: string;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  latency_ms: number;
  status: string;
  detail: string;
}

export interface AccessTokenDetail {
  token: AccessToken;
  plan: AccessPlan | null;
  limits: Record<string, unknown>;
  lifetime: { turns: number; tokens_in: number; tokens_out: number; cost_usd: number; avg_latency_ms: number };
  recent: SubscriberUsageRow[];
  subscribers: BotSubscriber[];
}

export interface BotSubscriber {
  id: string;
  token_id: string;
  binding_id: string;
  platform: string;
  chat_id: string;
  external_user_id: string;
  display_name: string;
  session_id: string;
  status: 'active' | 'blocked' | string;
  profile: Record<string, string>;
  notes: string;
  messages_count: number;
  first_seen_at: string;
  last_seen_at: string;
}

export interface SubscriberCard {
  subscriber: BotSubscriber;
  token: AccessToken | null;
  plan: AccessPlan | null;
  usage: { turns: number; tokens_in: number; tokens_out: number; cost_usd: number };
  conversation: { id: number; role: string; content: string; cost_usd: number }[];
}

export interface AccessOverview {
  tokens: {
    total: number; active: number; suspended: number; revoked: number;
    used_usd: number; used_tokens: number; used_messages: number;
  };
  subscribers: { total: number; active: number; blocked: number };
  blocked_turns: number;
  plans: number;
}


// ── Billing (backend/payments.py) ───────────────────────────────────────────

export interface AccessSubscription {
  id: string;
  plan_id: string;
  token_id: string;
  binding_id: string;
  subagent_id: string;
  customer_ref: string;
  status: 'active' | 'expired' | 'canceled' | string;
  auto_renew: boolean;
  price_usd: number | null;
  started_at: string;
  current_period_start: string;
  /** null means a lifetime purchase — it never lapses. */
  current_period_end: string | null;
  canceled_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PaymentInvoice {
  id: string;
  provider: string;
  provider_invoice_id: string | null;
  plan_id: string;
  binding_id: string;
  subagent_id: string;
  subscription_id: string | null;
  token_id: string | null;
  amount_usd: number;
  pay_currency: string;
  status: 'pending' | 'paid' | 'expired' | 'failed' | string;
  payment_url: string;
  customer_ref: string;
  /** 'bot' when the sale started inside a chat, 'admin' when the owner raised it. */
  origin: string;
  origin_platform: string;
  origin_chat_id: string;
  purpose: 'new' | 'renewal' | string;
  /** Last status word the provider reported, kept for the admin view. */
  last_status: string;
  created_at: string;
  updated_at: string;
  paid_at: string | null;
  expires_at: string | null;
}

export interface BillingConfig {
  provider: string;
  public_base_url: string;
  success_url: string;
  /** Booleans only — the API never returns the secrets themselves. */
  api_key_configured: boolean;
  ipn_secret_configured: boolean;
  providers: string[];
}

export interface BillingOverview {
  invoices: { invoices: number; paid: number; pending: number; revenue_usd: number };
  subscriptions: { total: number; active: number; expired: number; canceled: number };
  mrr_usd: number;
  config: BillingConfig;
}
