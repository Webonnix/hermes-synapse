/**
 * Content Factory API shapes (backend/content_factory/api.py).
 *
 * Statuses are string unions rather than enums so an unknown value from a newer
 * backend renders as itself instead of crashing the panel — the frontend must
 * degrade, not break, when the two sides are one deploy apart.
 */

export type CFRunStatus =
  | 'QUEUED' | 'RUNNING' | 'WAITING' | 'WAITING_APPROVAL'
  | 'COMPLETED' | 'PARTIALLY_COMPLETED' | 'FAILED' | 'CANCELLED';

export type CFNodeStatus =
  | 'PENDING' | 'QUEUED' | 'RUNNING' | 'WAITING_EXTERNAL' | 'WAITING_APPROVAL'
  | 'RETRYING' | 'COMPLETED' | 'FAILED' | 'SKIPPED' | 'CANCELLED';

export type CFPipelineStatus = 'DRAFT' | 'ACTIVE' | 'PAUSED' | 'ARCHIVED';

export interface CFPipeline {
  id: string;
  name: string;
  description: string;
  status: CFPipelineStatus;
  project_id: string | null;
  publishing_mode: string;
  error_strategy: string;
  active_version_id: string | null;
  draft_version_id: string | null;
  max_cost_per_run: number | null;
  max_concurrent_runs: number;
  created_at: string;
  updated_at: string;
  version?: CFPipelineVersion | null;
  versions?: CFPipelineVersion[];
}

export interface CFPipelineVersion {
  id: string;
  pipeline_id: string;
  version: number;
  nodes: string;
  edges: string;
  variables: string;
  published_at: string | null;
  created_at: string;
}

export interface CFNode {
  id: string;
  type: string;
  name?: string;
  position?: { x: number; y: number };
  config?: Record<string, unknown>;
  inputMapping?: Record<string, unknown>;
  fallbackPolicy?: string[];
  enabled?: boolean;
}

export interface CFEdge {
  source: string;
  target: string;
  branch?: string;
}

export interface CFNodeRun {
  id: string;
  node_id: string;
  node_type: string;
  name: string;
  status: CFNodeStatus;
  attempt: number;
  progress: number;
  provider: string;
  model: string;
  cost: number;
  fallback_index: number;
  duration_ms: number;
  started_at: string | null;
  finished_at: string | null;
  error_code: string;
  error_message: string;
  external_job_id: string;
  output?: string;
}

export interface CFRun {
  id: string;
  pipeline_id: string;
  pipeline_version_id: string;
  status: CFRunStatus;
  progress: number;
  dry_run: number;
  publishing_mode: string;
  actual_cost: number;
  cost_budget: number | null;
  error_code: string;
  error_message: string;
  trace_id: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  nodes?: CFNodeRun[];
  usage?: CFUsageRecord[];
}

export interface CFUsageRecord {
  provider: string;
  model: string;
  operation: string;
  cost: number;
  created_at: string;
}

export interface CFOverview {
  activePipelines: number;
  processing: number;
  reviewRequired: number;
  scheduled: number;
  publishedToday: number;
  failed: number;
  costToday: number;
  costMonth: number;
  liveRuns: CFRun[];
}

export interface CFContentItem {
  id: string;
  title: string;
  description: string;
  status: string;
  language: string;
  quality_score: number | null;
  cost: number;
  pipeline_run_id: string | null;
  created_at: string;
  publications?: CFPublication[];
  assets?: CFAsset[];
}

export interface CFPublication {
  id: string;
  platform: string;
  status: string;
  external_url: string;
  scheduled_at: string | null;
  published_at: string | null;
  error_message: string;
}

export interface CFAsset {
  id: string;
  type: string;
  mime_type: string;
  size_bytes: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  provider: string;
  created_at: string;
}

export interface CFTemplate {
  id: string;
  name: string;
  description: string;
  defaults: Record<string, unknown>;
}

export interface CFPipelineCreateRequest {
  name: string;
  templateId: string;
  activate: boolean;
  options: {
    channelId: string;
    language: string;
    niche: string;
    targetAudience: string;
    brandTone: string;
    preset: string;
    duration: number;
    maxSources: number;
    temperature: number;
    providerTimeoutSeconds: number;
    maxAttempts: number;
    fallbackEnabled: boolean;
    llmFallbackProviders: string[];
    maxCostPerRun: number | null;
    maxConcurrentRuns: number;
    publishingMode: string;
    privacy: string;
    subtitlesEnabled: boolean;
    madeForKids: boolean;
  };
}

export interface CFAgentRow {
  id: string;
  name: string;
  role: string;
  status: string;
  model?: string;
  modelProvider?: string;
  modelType?: string;
  providerChain?: string[];
  budgetUsdLimit?: number | null;
  budgetFallbackToLocal?: boolean;
  skills?: string[];
}

export interface CFProviderRow {
  id: string;
  category: string;
  model: string;
  free: boolean;
  asyncJob: boolean;
  concurrency: number;
  isDefault: boolean;
}

export interface CFChannel {
  id: string;
  platform: string;
  account_name: string;
  status: string;
  connected_at: string | null;
  last_error: string;
  proxy_enabled: number;
  proxy_host: string;
  analytics_updated_at: string | null;
}

export interface CFAnalyticsPoint {
  publication_id: string;
  timestamp: string;
  views: number;
  likes: number;
  comments: number;
  shares: number;
  engagement_rate: number;
}

export interface CFAnalyticsOverview {
  windowDays: number;
  publications: number;
  views: number;
  likes: number;
  comments: number;
  shares: number;
  engagementRate: number;
  items: CFAnalyticsPoint[];
  series: CFAnalyticsPoint[];
}

export interface CFTraceEntry {
  nodeId: string;
  name: string;
  type: string;
  status: CFNodeStatus;
  attempt: number;
  fallbackIndex: number;
  provider: string;
  model: string;
  cost: number;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number;
  errorCode: string;
  errorMessage: string;
  logs: string[];
}

/** Anything the backend broadcasts with a `cf.` prefix (§72). */
export interface CFEvent {
  id: string;
  type: string;
  timestamp: string;
  pipelineRunId: string;
  nodeRunId: string;
  data: Record<string, unknown>;
}

/** §76 — the frontend switches on `code`, never on the message text. */
export interface CFErrorEnvelope {
  error: {
    code: string;
    message: string;
    retryable: boolean;
    details: Record<string, unknown>;
    traceId: string;
  };
}

export interface CFValidationItem {
  code: string;
  message: string;
}

export interface CFValidation {
  errors: CFValidationItem[];
  warnings: CFValidationItem[];
  versionId?: string;
}

// ── Control-centre aggregations (backend/content_factory/dashboard.py) ───────

export interface CFQueueItem {
  kind: 'RUN' | 'SCHEDULE';
  id: string;
  pipelineId: string;
  name: string;
  status: string;
  progress?: number;
  expression?: string;
  timezone?: string;
  at: string | null;
}

export interface CFQueue {
  items: CFQueueItem[];
  active: number;
  capacity: number;
}

export interface CFActivityItem {
  type: 'run' | 'publication';
  status: string;
  title: string;
  detail: string;
  at: string | null;
  refId: string;
  url?: string;
}

export interface CFStatsPoint {
  date: string;
  created: number;
  published: number;
  cost: number;
  views: number;
}

export interface CFStats {
  series: CFStatsPoint[];
  today: CFStatsPoint;
  delta: {
    created: number | null;
    published: number | null;
    cost: number | null;
    views: number | null;
  };
  totals: {
    publications: number;
    views: number;
    followersDelta: number;
    engagementRate: number;
  };
}

export interface CFLiveRun {
  run?: CFRun;
  pipeline?: CFPipeline;
  version?: { id?: string; version?: number; nodes?: string; edges?: string };
  nodes?: CFNodeRun[];
  current?: CFNodeRun | null;
  assets?: CFAsset[];
  logs?: { at: string | null; nodeId: string; text: string; level?: string }[];
}

export interface CFAgentDetail extends CFAgentRow {
  description?: string;
  enabled?: boolean;
  temperature?: number;
  nodeTypes?: string[];
  windowDays?: number;
  metrics?: {
    completed: number;
    failed: number;
    avgDurationMs: number;
    cost: number;
    successRate: number | null;
  };
  recentRuns?: {
    node_id: string;
    node_type: string;
    status: CFNodeStatus;
    provider: string;
    model: string;
    cost: number;
    duration_ms: number;
    attempt: number;
    error_code: string;
    created_at: string;
    pipeline_run_id: string;
  }[];
}

export interface CFIntegrationCategory {
  category: string;
  providers: CFProviderRow[];
  connected: boolean;
  paidAvailable: boolean;
}

export interface CFIntegrations {
  categories: CFIntegrationCategory[];
  channels: CFChannel[];
  storage: Record<string, unknown>;
  storageBackend?: string;
  mockProviders: boolean;
  publishReady: boolean;
  warnings: { code: string; message: string }[];
}

export interface CFStorageConfig {
  backend: 'env' | 'local' | 'nextcloud' | 's3';
  nextcloud: {
    url: string;
    username: string;
    root: string;
    publicUrl: string;
    verifyTls: boolean;
    shareDays: number;
  };
  s3: { bucket: string; endpoint: string; region: string; publicUrl: string };
  local: { root: string };
  /** Where each credential comes from — never the credential itself (§89). */
  secrets: Record<string, 'env' | 'stored' | 'none'>;
  /** What the environment alone would select, so "env" is an informed choice. */
  envBackend: string;
  backends: string[];
  requireRemote: boolean;
  health?: Record<string, unknown>;
}

export interface CFProviderField {
  name: string;
  label: { ru: string; en: string };
  type: 'text' | 'secret' | 'number';
  required: boolean;
  placeholder: string;
}

export interface CFMediaProvider {
  id: string;
  category: string;
  label: { ru: string; en: string };
  hint: { ru: string; en: string };
  docs: string;
  fields: CFProviderField[];
  enabled: boolean;
  model: string;
  options: Record<string, string | number>;
  secrets: Record<string, 'env' | 'stored' | 'none'>;
  configured: boolean;
  /** In the registry right now — what a pipeline would actually get. */
  active: boolean;
  isDefault: boolean;
}

export interface CFLLMBinding {
  id: string;
  name: string;
  api_base: string;
  status: string;
  default_model?: string;
  control_task_id?: string | null;
  local: boolean;
}

export interface CFLLMEndpoints {
  local: CFLLMBinding;
  bindings: CFLLMBinding[];
}

export interface CFStorageUsage {
  backend: Record<string, unknown>;
  totalFiles: number;
  totalBytes: number;
  byType: { type: string; files: number; bytes: number }[];
}

export interface CFSettings {
  general: {
    defaultLanguage: string;
    timezone: string;
    defaultPreset: string;
    defaultDuration: number;
    contentNiche: string;
    targetAudience: string;
    brandTone: string;
  };
  generation: {
    maxSources: number;
    temperature: number;
    maxTokens: number;
    imageParallelism: number;
    clipDuration: number;
    providerTimeoutSeconds: number;
    llmFallbackProviders: string[];
    imageFallbackProviders: string[];
    videoFallbackProviders: string[];
    voiceFallbackProviders: string[];
  };
  publishing: {
    mode: string;
    defaultPrivacy: string;
    discloseAiGenerated: boolean;
    madeForKids: boolean;
    youtubeCategoryId: string;
    defaultTags: string[];
  };
  budgets: {
    maxCostPerRun: number | null;
    dailyBudget: number | null;
    monthlyBudget: number | null;
    onExceeded: string;
  };
  rendering: {
    subtitlesEnabled: boolean;
    subtitleStyle: string;
    subtitleFont: string;
    subtitlePosition: string;
    musicGainDb: number;
    normalizeAudio: boolean;
    loudnessTargetLufs: number;
    transition: string;
    watermarkAssetId: string;
  };
  policies: {
    denylist: string[];
    minQualityScore: number;
    requireSources: boolean;
    minSources: number;
    maxSourceAgeDays: number;
    blockProfanity: boolean;
  };
  retry: {
    maxAttempts: number;
    initialDelaySeconds: number;
    maxDelaySeconds: number;
    backoffMultiplier: number;
    fallbackEnabled: boolean;
  };
  network: {
    requireProxyForPublishing: boolean;
    connectionTimeoutSeconds: number;
    analyticsRefreshMinutes: number;
  };
  storage: {
    retentionDays: number;
    keepSourceAssets: boolean;
    keepFailedRuns: boolean;
    signedUrlMinutes: number;
  };
  notifications: {
    onFailure: boolean;
    onReviewRequired: boolean;
    onBudgetReached: boolean;
    onPublished: boolean;
    onProviderFallback: boolean;
    onPolicyBlocked: boolean;
  };
  advanced: {
    pollSeconds: number;
    maxConcurrentRuns: number;
    maxParallelNodes: number;
    leaseSeconds: number;
    logRetentionDays: number;
  };
}
