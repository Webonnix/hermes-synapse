import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ContentFactoryTab } from './ContentFactoryTab';

const overview = {
  activePipelines: 3, processing: 2, reviewRequired: 1, scheduled: 5,
  publishedToday: 7, failed: 1, costToday: 14.82, costMonth: 281.31,
  liveRuns: [{
    id: 'run-live1', pipeline_id: 'pipe-1', pipeline_version_id: 'ver-1', status: 'RUNNING',
    progress: 65, dry_run: 0, publishing_mode: 'REVIEW_REQUIRED', actual_cost: 0.69,
    cost_budget: 2, error_code: '', error_message: '', trace_id: 't1',
    started_at: '2026-08-28T09:00:00Z', completed_at: null, created_at: '2026-08-28T09:00:00Z',
  }],
};

const pipeline = {
  id: 'pipe-1', name: 'Crypto News Daily', description: '', status: 'ACTIVE',
  project_id: null, publishing_mode: 'REVIEW_REQUIRED', error_strategy: 'FAIL_FAST',
  active_version_id: 'ver-1', draft_version_id: 'ver-1', max_cost_per_run: 2,
  max_concurrent_runs: 1, created_at: '2026-08-01T09:00:00Z', updated_at: '2026-08-01T09:00:00Z',
  version: {
    id: 'ver-1', pipeline_id: 'pipe-1', version: 4, published_at: '2026-08-01T09:00:00Z',
    created_at: '2026-08-01T09:00:00Z', variables: '{}',
    nodes: JSON.stringify([
      { id: 'trigger', type: 'manual_trigger', name: 'Start' },
      { id: 'script', type: 'script', name: 'Script Writer' },
      { id: 'publish', type: 'publish', name: 'Publish' },
    ]),
    edges: JSON.stringify([
      { source: 'trigger', target: 'script' },
      { source: 'script', target: 'publish' },
    ]),
  },
};

const run = {
  ...overview.liveRuns[0],
  id: 'run-1', status: 'FAILED', error_code: 'PROVIDER_RATE_LIMIT',
  error_message: 'Video generation provider rate limit exceeded',
  nodes: [
    { id: 'nr1', node_id: 'script', node_type: 'script', name: 'Script Writer', status: 'COMPLETED',
      attempt: 1, progress: 100, provider: 'openai', model: 'gpt-4o', cost: 0.03, fallback_index: 0,
      duration_ms: 1800, started_at: null, finished_at: null, error_code: '', error_message: '',
      external_job_id: '' },
  ],
};

const trace = {
  runId: 'run-1',
  entries: [
    { nodeId: 'script', name: 'Script Writer', type: 'script', status: 'COMPLETED', attempt: 1,
      fallbackIndex: 0, provider: 'openai', model: 'gpt-4o', cost: 0.03, startedAt: null,
      finishedAt: null, durationMs: 1800, errorCode: '', errorMessage: '', logs: [] },
    { nodeId: 'clips', name: 'Visuals', type: 'video_generation', status: 'FAILED', attempt: 3,
      fallbackIndex: 1, provider: 'runway', model: 'gen3a_turbo', cost: 0.54, startedAt: null,
      finishedAt: null, durationMs: 42000, errorCode: 'PROVIDER_RATE_LIMIT',
      errorMessage: 'rate limited', logs: [] },
  ],
};

const settingsPayload = {
  general: { defaultLanguage: 'ru', timezone: 'UTC', defaultPreset: 'YOUTUBE_SHORT', defaultDuration: 45 },
  publishing: { mode: 'REVIEW_REQUIRED', defaultPrivacy: 'private', discloseAiGenerated: true },
  budgets: { maxCostPerRun: 2, dailyBudget: 25, monthlyBudget: 400, onExceeded: 'stop' },
  rendering: { subtitlesEnabled: true, subtitleStyle: 'default', musicGainDb: -18 },
  policies: { denylist: [], minQualityScore: 0.7 },
  notifications: { onFailure: true, onReviewRequired: true, onBudgetReached: true, onPublished: false },
  advanced: { pollSeconds: 5, maxConcurrentRuns: 5, maxParallelNodes: 4 },
};

function stubFetch(extra: (path: string, method: string) => unknown | undefined = () => undefined) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    calls.push(`${method} ${path}`);
    const override = extra(path, method);
    if (override) return override as Response;
    if (path.endsWith('/overview')) return { ok: true, json: async () => overview };
    if (path.endsWith('/pipelines')) return { ok: true, json: async () => ({ items: [pipeline] }) };
    if (path.endsWith('/templates')) {
      return { ok: true, json: async () => ({ items: [
        { id: 'ai_news_shorts', name: 'AI NEWS SHORTS', description: 'Daily news short', defaults: {} },
      ] }) };
    }
    if (path.includes('/pipelines/pipe-1/validate')) {
      return { ok: true, json: async () => ({ errors: [], warnings: [
        { code: 'NO_FALLBACK', message: 'script: no fallback provider' },
      ] }) };
    }
    if (path.includes('/pipelines/pipe-1') && method === 'GET') {
      return { ok: true, json: async () => pipeline };
    }
    if (path.includes('/runs?pipeline_id')) return { ok: true, json: async () => ({ items: [run] }) };
    if (path.endsWith('/runs/run-1/trace')) return { ok: true, json: async () => trace };
    if (path.endsWith('/runs/run-1')) return { ok: true, json: async () => run };
    if (path.endsWith('/content?limit=100')) return { ok: true, json: async () => ({ items: [] }) };
    if (path.endsWith('/channels')) return { ok: true, json: async () => ({ items: [] }) };
    if (path.endsWith('/agents')) {
      return { ok: true, json: async () => ({ items: [
        { id: 'cf_script', name: 'Script Writer', role: 'Writer', status: 'enabled',
          model: 'gpt-4o', modelType: 'external', providerChain: ['openai', 'local-llm'],
          budgetUsdLimit: 30, budgetFallbackToLocal: true, skills: [] },
      ] }) };
    }
    // Guarded: /integrations/providers also ends in "/providers" and answers a
    // different shape.
    if (path.endsWith('/providers') && !path.includes('/integrations')) {
      return { ok: true, json: async () => ({
        items: [{ id: 'mock-llm', category: 'LLM', model: 'mock', free: true, asyncJob: false,
          concurrency: 8, isDefault: true }],
        storage: { storage: 'nextcloud', status: 'connected' },
      }) };
    }
    if (path.endsWith('/queue')) {
      return { ok: true, json: async () => ({
        active: 1, capacity: 5,
        items: [{ kind: 'RUN', id: 'run-1', pipelineId: 'pipe-1', name: 'Crypto News Daily',
          status: 'RUNNING', progress: 65, at: '2026-08-28T09:00:00Z' }],
      }) };
    }
    if (path.endsWith('/activity')) {
      return { ok: true, json: async () => ({ items: [
        { type: 'run', status: 'FAILED', title: 'Crypto News Daily', detail: 'PROVIDER_RATE_LIMIT',
          at: '2026-08-28T09:10:00Z', refId: 'run-1' },
      ] }) };
    }
    if (path.includes('/stats')) {
      return { ok: true, json: async () => ({
        series: [
          { date: '2026-08-27', created: 1, published: 0, cost: 0.2, views: 10 },
          { date: '2026-08-28', created: 2, published: 1, cost: 0.5, views: 45200 },
        ],
        today: { date: '2026-08-28', created: 2, published: 1, cost: 0.5, views: 45200 },
        delta: { created: 100, published: null, cost: 150, views: null },
        totals: { publications: 2, views: 45200, followersDelta: 12, engagementRate: 8.5 },
      }) };
    }
    if (path.endsWith('/live')) {
      return { ok: true, json: async () => ({
        run, pipeline, nodes: run.nodes, current: null,
        assets: [{ id: 'asset-1', type: 'RENDER', mime_type: 'video/mp4', size_bytes: 1024,
          duration: 45, width: 1080, height: 1920, provider: 'ffmpeg', created_at: '2026-08-28T09:20:00Z' }],
        logs: [{ at: '2026-08-28T09:05:00Z', nodeId: 'script', text: 'script via openai/gpt-4o' }],
      }) };
    }
    if (path.includes('/agents/cf_script')) {
      return { ok: true, json: async () => ({
        id: 'cf_script', name: 'Script Writer', role: 'Writer', status: 'enabled', model: 'gpt-4o',
        modelType: 'external', providerChain: ['openai', 'local-llm'], windowDays: 7,
        metrics: { completed: 9, failed: 1, avgDurationMs: 4320, cost: 0.27, successRate: 90 },
        recentRuns: [],
      }) };
    }
    if (path.endsWith('/integrations')) {
      return { ok: true, json: async () => ({
        categories: [
          { category: 'LLM', connected: true, paidAvailable: true, providers: [
            { id: 'openai', category: 'LLM', model: 'gpt-4o', free: false, asyncJob: false,
              concurrency: 8, isDefault: true }] },
          { category: 'VIDEO', connected: false, paidAvailable: false, providers: [] },
        ],
        channels: [],
        storage: { storage: 'local', status: 'connected' },
        mockProviders: false,
        publishReady: false,
        warnings: [
          { code: 'LOCAL_STORAGE', message: 'Ассеты пишутся на локальный диск' },
          { code: 'NO_CHANNEL', message: 'Не подключён ни один канал публикации' },
        ],
      }) };
    }
    if (path.endsWith('/integrations/providers')) {
      return { ok: true, json: async () => ({
        items: [
          {
            id: 'stability', category: 'IMAGE',
            label: { ru: 'Stability AI', en: 'Stability AI' }, hint: { ru: '', en: '' },
            docs: 'https://platform.stability.ai/account/keys',
            fields: [
              { name: 'apiKey', label: { ru: 'API-ключ', en: 'API key' }, type: 'secret', required: true, placeholder: '' },
              { name: 'usdPerImage', label: { ru: 'Цена', en: 'USD per image' }, type: 'number', required: false, placeholder: '' },
            ],
            enabled: true, model: '', options: {},
            secrets: { apiKey: 'none' }, configured: false, active: false, isDefault: false,
          },
        ],
        defaults: {},
      }) };
    }
    if (path.endsWith('/integrations/llm')) {
      return { ok: true, json: async () => ({
        local: { id: 'local-llm', name: 'local-llm', api_base: 'http://hermes-ollama:11434',
          default_model: 'qwen3:8b', status: 'active', local: true },
        bindings: [],
      }) };
    }
    if (path.endsWith('/storage/config')) {
      return { ok: true, json: async () => ({
        backend: 'local',
        nextcloud: { url: '', username: '', root: '', publicUrl: '', verifyTls: true, shareDays: 7 },
        s3: { bucket: '', endpoint: '', region: '', publicUrl: '' },
        local: { root: '' },
        secrets: { nextcloudPassword: 'none', s3AccessKey: 'none', s3SecretKey: 'none' },
        envBackend: 'local', backends: ['env', 'local', 'nextcloud', 's3'], requireRemote: false,
        health: { storage: 'local', status: 'connected' },
      }) };
    }
    if (path.endsWith('/storage/usage')) {
      return { ok: true, json: async () => ({
        backend: { storage: 'local', status: 'connected' },
        totalFiles: 3, totalBytes: 2048,
        byType: [{ type: 'RENDER', files: 1, bytes: 1024 }],
      }) };
    }
    if (path.endsWith('/settings') && method === 'GET') {
      return { ok: true, json: async () => settingsPayload };
    }
    if (path.endsWith('/settings') && method === 'PUT') {
      return { ok: true, json: async () => settingsPayload };
    }
    if (path.includes('/analytics/overview')) return { ok: true, json: async () => ({
      windowDays: 7, publications: 2, views: 45200, likes: 2100, comments: 320,
      shares: 88, engagementRate: 8.5, items: [], series: [
        { publication_id: 'p1', timestamp: '2026-08-27T10:00:00Z', views: 22000, likes: 900, comments: 120, shares: 30, engagement_rate: 4.8 },
        { publication_id: 'p1', timestamp: '2026-08-28T10:00:00Z', views: 45200, likes: 2100, comments: 320, shares: 88, engagement_rate: 8.5 },
      ],
    }) };
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}
describe('ContentFactoryTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('opens on the mission-control screen, not a summary page', async () => {
    // §5: the first question is "what is running right now", so the pipeline
    // workbench is the landing view rather than a page in front of it.
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    expect(await screen.findByLabelText('Select pipeline')).toBeInTheDocument();
    expect(await screen.findByLabelText('Pipeline stages')).toBeInTheDocument();
  });

  it('shows the production KPIs above the workbench', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    expect(await screen.findByText('$14.82')).toBeInTheDocument();
    expect(screen.getByText('$281.31')).toBeInTheDocument();
    expect(screen.getByText('Published today')).toBeInTheDocument();
  });

  it('fills the queue panel from the backend, capacity included', async () => {
    // The rail used to print "runs.length/10"; both halves are now measured.
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    expect(await screen.findByText('1/5')).toBeInTheDocument();
    expect(screen.getAllByText('Crypto News Daily').length).toBeGreaterThan(0);
  });

  it('shows real run logs rather than a placeholder', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    expect(await screen.findByText(/script via openai\/gpt-4o/)).toBeInTheDocument();
  });

  it('lists produced assets instead of empty preview tiles', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    expect(await screen.findByText(/RENDER · 45s/)).toBeInTheDocument();
  });

  it('surfaces the backend error code rather than a generic message', async () => {
    // §76/§103: the owner needs to know it was a rate limit, not "an error".
    stubFetch((path) => path.endsWith('/pipelines')
      ? { ok: false, json: async () => ({ detail: { error: {
          code: 'PROVIDER_RATE_LIMIT', message: 'Video provider rate limit exceeded',
          retryable: true, details: {}, traceId: 'abc',
        } } }) }
      : undefined);
    render(<ContentFactoryTab language="en" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Video provider rate limit exceeded');
  });

  it('offers a test run that the backend will force into dry-run', async () => {
    const { calls } = stubFetch();
    render(<ContentFactoryTab language="en" />);
    // The button only enables once a pipeline is selected.
    await screen.findByDisplayValue('Crypto News Daily');
    fireEvent.click(screen.getByRole('button', { name: /Test run/ }));
    await waitFor(() => {
      expect(calls.some((call) => call.includes('POST') && call.includes('/pipelines/pipe-1/runs'))).toBe(true);
    });
  });

  it('loads measured metrics for the agent that was selected', async () => {
    // The inspector used to print 100% whenever a run had completed; it now
    // shows what the backend measured, or nothing.
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Script Writer' }));
    expect(await screen.findByText('90%')).toBeInTheDocument();
  });

  it('warns on the integrations page about what will break', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Integrations' }));
    expect(await screen.findByText(/локальный диск/)).toBeInTheDocument();
    expect(screen.getByText(/канал публикации/)).toBeInTheDocument();
    expect(screen.getByText('NOT CONNECTED')).toBeInTheDocument();
  });

  it('reports whether publishing is actually possible', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Integrations' }));
    expect(await screen.findByText('Publishing ready:')).toBeInTheDocument();
    expect(screen.getByText('no')).toBeInTheDocument();
  });

  it('shows storage usage by asset type', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Storage' }));
    expect(await screen.findByText('RENDER')).toBeInTheDocument();
    expect(screen.getByText(/1 · 1\.0 KB/)).toBeInTheDocument();
  });

  it('switches the asset store to Nextcloud without touching the host', async () => {
    const { calls, fetchMock } = stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Storage' }));

    fireEvent.click(await screen.findByRole('button', { name: /Nextcloud/ }));
    fireEvent.change(screen.getByLabelText(/Nextcloud URL/), {
      target: { value: 'http://192.168.0.200:8080' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Save/ }));

    await waitFor(() => {
      expect(calls.some((call) => call.startsWith('PUT') && call.includes('/storage/config'))).toBe(true);
    });
    const request = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PUT',
    );
    expect(JSON.parse(String((request?.[1] as RequestInit).body)).backend).toBe('nextcloud');
  });

  it('warns that local disk cannot serve a publishable link', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Storage' }));
    expect(await screen.findByText(/local disk cannot provide/)).toBeInTheDocument();
  });

  it('connects an image provider and never echoes its key', async () => {
    const { calls, fetchMock } = stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Integrations' }));

    fireEvent.click(await screen.findByRole('button', { name: /Configure/ }));
    const keyBox = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(keyBox, { target: { value: 'sk-test-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(calls.some((call) => call.startsWith('PUT') && call.includes('/integrations/providers/stability'))).toBe(true);
    });
    const request = fetchMock.mock.calls.find(
      ([url, init]) => String(url).includes('/integrations/providers/stability')
        && (init as RequestInit | undefined)?.method === 'PUT',
    );
    expect(JSON.parse(String((request?.[1] as RequestInit).body)).apiKey).toBe('sk-test-key');
    // The card renders what the backend sent back, which never includes a key.
    expect(screen.queryByDisplayValue('sk-test-key')).toBeNull();
  });

  it('saves settings only after an explicit save', async () => {
    const { calls } = stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Settings' }));

    const timezone = await screen.findByDisplayValue('UTC');
    fireEvent.change(timezone, { target: { value: 'Europe/Riga' } });
    // Editing alone must not reach the backend.
    expect(calls.some((call) => call.startsWith('PUT'))).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: /Save/ }));
    await waitFor(() => {
      expect(calls.some((call) => call.startsWith('PUT') && call.includes('/settings'))).toBe(true);
    });
  });

  it('asks before switching publishing to fully automatic', async () => {
    // §36: FULL_AUTO removes the human gate in front of a real audience.
    stubFetch();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Settings' }));

    const mode = await screen.findByDisplayValue('REVIEW_REQUIRED');
    fireEvent.change(mode, { target: { value: 'FULL_AUTO' } });

    expect(confirm).toHaveBeenCalled();
    // Declined, so the mode is unchanged.
    expect(screen.getByDisplayValue('REVIEW_REQUIRED')).toBeInTheDocument();
  });

  it('exposes the operational settings and keeps backward-compatible defaults', async () => {
    stubFetch();
    render(<ContentFactoryTab language="en" />);
    fireEvent.click(within(screen.getByRole('navigation', { name: 'CONTENT FACTORY' })).getByRole('button', { name: 'Settings' }));

    expect(await screen.findByText('Generation & models')).toBeInTheDocument();
    expect(screen.getByText('Retries & fallback')).toBeInTheDocument();
    expect(screen.getByText('Network & proxy')).toBeInTheDocument();
    expect(screen.getAllByText('Storage')).toHaveLength(2);
    expect(screen.getByText('Workers & performance')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Require a proxy for publishing/ })).not.toBeChecked();
    expect(screen.getByLabelText('Sources')).toHaveValue(6);
    expect(screen.getByLabelText('Parallel nodes')).toHaveValue(4);
  });
});
