import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentsAdminTab } from './AgentsAdminTab';
import { translate } from '../i18n';

const t = (key: string) => translate('ru', key);

interface StubOptions {
  routerStats?: unknown;
  providers?: unknown[];
  sessionStatus?: unknown;
  combos?: unknown;
  connections?: unknown;
  onProposeCredential?: (body: unknown) => unknown;
  routerOverview?: unknown;
  routerTiers?: unknown[];
  onCreateRouterTier?: (body: unknown) => unknown;
  onBindSelf?: () => unknown;
  routerModels?: unknown;
  projects?: unknown[];
}

const defaultRouterOverview = {
  active_combo_label: 'Локальная модель', active_combo_tier_count: 1,
  token_savings_pct: null, requests_24h: 0, providers_healthy: 0, providers_total: 0,
};

function stubFetch(opts: StubOptions) {
  const {
    routerStats, providers = [],
    sessionStatus = { configured: false, pending_task_id: null },
    combos = null, connections = null, onProposeCredential,
    routerOverview = defaultRouterOverview, routerTiers = [], onCreateRouterTier, onBindSelf,
    routerModels = { available: true, models: [], error: null },
    projects = [],
  } = opts;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === '/api/providers') return { ok: true, json: async () => providers };
    if (path === '/api/agent-tiers') return { ok: true, json: async () => [] };
    if (path === '/api/projects') return { ok: true, json: async () => projects };
    // Fetched whenever an agent is selected for edit (startEdit -> fetchBudgetStatus)
    // — without this, the fallback `[]` below makes budgetStatus.used_usd
    // undefined and the inspector's `.toFixed(4)` throws.
    if (path.endsWith('/usage')) {
      return {
        ok: true,
        json: async () => ({
          agent_id: '', budget_usd_limit: null, budget_period: 'monthly',
          used_usd: 0, remaining_usd: null, exceeded: false, by_provider: [],
        }),
      };
    }
    if (path === '/api/router/stats') {
      if (routerStats === 'network-error') throw new Error('network error');
      return { ok: true, json: async () => routerStats };
    }
    if (path === '/api/router/session-status') return { ok: true, json: async () => sessionStatus };
    if (path === '/api/router/combos') return { ok: true, json: async () => combos };
    if (path === '/api/router/connections') return { ok: true, json: async () => connections };
    if (path === '/api/router/overview') return { ok: true, json: async () => routerOverview };
    if (path === '/api/router/tiers' && (!init || init.method === undefined)) return { ok: true, json: async () => routerTiers };
    if (path === '/api/router/tiers' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      const result = onCreateRouterTier ? onCreateRouterTier(body) : { id: 'rtier-new', ...body };
      return { ok: true, json: async () => result };
    }
    if (path.startsWith('/api/router/tiers/') && init?.method === 'DELETE') {
      return { ok: true, json: async () => ({ status: 'success' }) };
    }
    if (path === '/api/router/session-credential' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      const result = onProposeCredential ? onProposeCredential(body) : { status: 'awaiting_approval', task_id: 'T-1' };
      return { ok: true, json: async () => result };
    }
    if (path === '/api/router/bind-self' && init?.method === 'POST') {
      const result = onBindSelf ? onBindSelf() : { status: 'awaiting_approval', task_id: 'T-2' };
      return { ok: true, json: async () => result };
    }
    if (path === '/api/router/models') return { ok: true, json: async () => routerModels };
    return { ok: true, json: async () => [] };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AgentsAdminTab — AI Router section', () => {
  it('shows the not-configured state when ROUTER_API_KEY is missing', async () => {
    stubFetch({
      routerStats: {
        available: false, reachable: null, model_count: null, provider_count: null, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: 'ROUTER_API_KEY not configured',
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText(/ROUTER_API_KEY в Настройках/)).toBeInTheDocument());
  });

  it('shows the unreachable state when the key is set but the sidecar is down', async () => {
    stubFetch({
      routerStats: {
        available: true, reachable: false, model_count: null, provider_count: null, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: 'not reachable',
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText(/контейнер 9router не отвечает/)).toBeInTheDocument());
  });

  it('renders the model/provider catalog once the sidecar responds', async () => {
    stubFetch({
      routerStats: {
        available: true, reachable: true,
        model_count: 679, provider_count: 68,
        sample_models: ['anthropic/claude-opus-4', 'glm/glm-5'],
        dashboard_url: 'http://localhost:20128', error: null,
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    // The raw 9Router catalog snapshot is now a demoted diagnostic <details>
    // section (the AI Router tab's primary view is Hermes's own fallback
    // chain), but its content is still present, just collapsed.
    fireEvent.click(screen.getByText(/Необработанные данные из дашборда 9Router/));
    await waitFor(() => expect(screen.getByText('679')).toBeInTheDocument());
    expect(screen.getByText('68')).toBeInTheDocument();
    expect(screen.getByText('anthropic/claude-opus-4')).toBeInTheDocument();
  });

  it('shows a retry option when the request itself fails', async () => {
    stubFetch({ routerStats: 'network-error' });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    fireEvent.click(screen.getByText(/Необработанные данные из дашборда 9Router/));
    await waitFor(() => expect(screen.getByText('Повторить')).toBeInTheDocument());
  });

  it('shows provider bindings in the same tab as the router stats (merged view)', async () => {
    stubFetch({
      routerStats: {
        available: true, reachable: true,
        model_count: 679, provider_count: 68, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: null,
      },
      providers: [{
        id: 'prov-1', name: 'deepseek', provider_type: 'openai_compatible',
        api_base: 'https://api.deepseek.com/v1', status: 'active', created_at: '', updated_at: '',
      }],
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('deepseek')).toBeInTheDocument());
    // "Подключённые провайдеры" is the one native table for agent provider
    // bindings on this tab now — no separate nav tab for it any more.
    expect(screen.getAllByText('Подключённые провайдеры')).toHaveLength(1);
    fireEvent.click(screen.getByText(/Необработанные данные из дашборда 9Router/));
    await waitFor(() => expect(screen.getByText('679')).toBeInTheDocument());
  });

  it('offers to connect a 9Router session when none is configured', async () => {
    stubFetch({
      routerStats: {
        available: true, reachable: true, model_count: 1, provider_count: 1, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: null,
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('Подключить')).toBeInTheDocument());
    expect(screen.queryByText('Комбо')).not.toBeInTheDocument();
  });

  it('submits the dashboard password and shows the pending-approval state', async () => {
    const fetchMock = stubFetch({
      routerStats: {
        available: true, reachable: true, model_count: 1, provider_count: 1, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: null,
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('Подключить')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Подключить'));

    const input = await screen.findByPlaceholderText('пароль из Dashboard 9Router');
    fireEvent.change(input, { target: { value: 'hunter2' } });
    fireEvent.click(screen.getByText('Подключить'));

    await waitFor(() => expect(screen.getByText(/Ожидает подтверждения/)).toBeInTheDocument());
    const proposeCall = fetchMock.mock.calls.find(call => call[0] === '/api/router/session-credential');
    expect(proposeCall).toBeTruthy();
    expect(JSON.parse(String((proposeCall![1] as RequestInit).body))).toEqual({ password: 'hunter2' });
  });

  it('renders combos and connected accounts once a session is configured', async () => {
    stubFetch({
      routerStats: {
        available: true, reachable: true, model_count: 1, provider_count: 1, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: null,
      },
      sessionStatus: { configured: true, pending_task_id: null },
      combos: { available: true, error: null, combos: [{ name: 'hermes-primary', tiers: [{ model: 'claude-opus-4.7' }, { model: 'glm-5.1' }] }] },
      connections: { available: true, error: null, connections: [{ name: 'claude-subscription', type: 'oauth', status: 'active' }] },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('hermes-primary')).toBeInTheDocument());
    expect(screen.getByText('claude-opus-4.7 → glm-5.1')).toBeInTheDocument();
    expect(screen.getByText('claude-subscription')).toBeInTheDocument();
  });

  it('shows empty-state guidance when configured but nothing is set up in 9Router yet', async () => {
    stubFetch({
      routerStats: {
        available: true, reachable: true, model_count: 1, provider_count: 1, sample_models: [],
        dashboard_url: 'http://localhost:20128', error: null,
      },
      sessionStatus: { configured: true, pending_task_id: null },
      combos: { available: true, error: null, combos: [] },
      connections: { available: true, error: null, connections: [] },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    fireEvent.click(screen.getByText(/Необработанные данные из дашборда 9Router/));
    await waitFor(() => expect(screen.getByText(/Комбо ещё не создано/)).toBeInTheDocument());
    expect(screen.getByText(/Аккаунты ещё не подключены/)).toBeInTheDocument();
  });

  it('renders the native fallback-chain stat cards from the overview endpoint', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      routerOverview: {
        active_combo_label: 'Подписка', active_combo_tier_count: 3,
        token_savings_pct: 34, requests_24h: 1284, providers_healthy: 4, providers_total: 5,
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('Подписка')).toBeInTheDocument());
    expect(screen.getByText('-34%')).toBeInTheDocument();
    expect(screen.getByText('1284')).toBeInTheDocument();
    expect(screen.getByText('4/5')).toBeInTheDocument();
  });

  it('always shows the implicit local tier plus every configured tier, with a quota bar', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      routerTiers: [{
        id: 'rtier-1', label: 'Подписка', tier_rank: 1, kind: 'binding',
        provider_binding_id: 'prov-1', model_override: 'claude-opus-4.7',
        quota_limit: 200, quota_window_hours: 24, is_active: true,
        created_at: '', updated_at: '',
        quota: { used: 122, limit: 200, window_hours: 24, resets_at: null, pct: 61, exhausted: false },
      }],
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('claude-opus-4.7')).toBeInTheDocument());
    // Appears both as the always-present tier-0 card and the default
    // active-combo stat-card label when no tier outranks it.
    expect(screen.getAllByText('Локальная модель').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('61% квоты')).toBeInTheDocument();
  });

  it('submits a new fallback tier and refetches the chain', async () => {
    const fetchMock = stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      providers: [{
        id: 'prov-1', name: 'deepseek', provider_type: 'openai_compatible',
        api_base: 'https://api.deepseek.com/v1', status: 'active', created_at: '', updated_at: '',
      }],
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('Добавить тир')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Добавить тир'));

    fireEvent.change(await screen.findByPlaceholderText('Подписка'), { target: { value: 'Дешёвый' } });
    fireEvent.change(screen.getByPlaceholderText('deepseek-chat'), { target: { value: 'deepseek-chat' } });
    const providerSelect = screen.getByDisplayValue('— выберите —');
    fireEvent.change(providerSelect, { target: { value: 'prov-1' } });
    fireEvent.click(screen.getByText('Добавить', { selector: 'span' }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(c => c[0] === '/api/router/tiers' && (c[1] as RequestInit)?.method === 'POST');
      expect(call).toBeTruthy();
    });
    const [, init] = fetchMock.mock.calls.find(c => c[0] === '/api/router/tiers' && (c[1] as RequestInit)?.method === 'POST')!;
    const body = JSON.parse(String((init as RequestInit).body));
    expect(body).toMatchObject({ label: 'Дешёвый', kind: 'binding', provider_binding_id: 'prov-1', model_override: 'deepseek-chat' });
  });

  it('deletes a tier only after confirmation', async () => {
    const fetchMock = stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      routerTiers: [{
        id: 'rtier-1', label: 'Подписка', tier_rank: 1, kind: 'binding',
        provider_binding_id: 'prov-1', model_override: 'claude-opus-4.7',
        quota_limit: null, quota_window_hours: 24, is_active: true,
        created_at: '', updated_at: '',
        quota: { used: 3, limit: null, window_hours: 24, resets_at: null, pct: null, exhausted: false },
      }],
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('claude-opus-4.7')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Удалить тир'));
    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(c => c[0] === '/api/router/tiers/rtier-1' && (c[1] as RequestInit)?.method === 'DELETE');
      expect(call).toBeTruthy();
    });
  });

  it('shows 9Router-native connections in the main view, not just the collapsed diagnostics', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      sessionStatus: { configured: true, pending_task_id: null },
      combos: { available: true, error: null, combos: [] },
      connections: {
        available: true, error: null,
        connections: [{ id: 'c1', name: 'KImiK3', provider: 'kimi', testStatus: 'active', isActive: true }],
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    // No click into the collapsed "Необработанные данные…" details — this
    // must be visible in the main flow, right under "Подключённые провайдеры".
    await waitFor(() => expect(screen.getByText('KImiK3')).toBeInTheDocument());
    expect(screen.getByText('kimi')).toBeInTheDocument();
    expect(screen.getAllByText('active').length).toBeGreaterThanOrEqual(1);
  });

  it('offers a quick-bind action for 9Router connections when no binding points at it yet', async () => {
    const fetchMock = stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      sessionStatus: { configured: true, pending_task_id: null },
      combos: { available: true, error: null, combos: [] },
      connections: { available: true, error: null, connections: [{ id: 'c1', name: 'DeepSeek API', provider: 'deepseek', testStatus: 'active', isActive: true }] },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('Быстро подключить 9Router как провайдера')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Быстро подключить 9Router как провайдера'));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(c => c[0] === '/api/router/bind-self' && (c[1] as RequestInit)?.method === 'POST');
      expect(call).toBeTruthy();
    });
    await waitFor(() => expect(screen.getByText(/один Telegram \/approve T-2/)).toBeInTheDocument());
  });

  it('skips the quick-bind action once a provider binding already points at 9Router', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      providers: [{
        id: 'prov-9r', name: '9Router', provider_type: 'openai_compatible',
        api_base: 'http://9router:20128/v1', status: 'active', created_at: '', updated_at: '',
      }],
      sessionStatus: { configured: true, pending_task_id: null },
      combos: { available: true, error: null, combos: [] },
      connections: { available: true, error: null, connections: [{ id: 'c1', name: 'DeepSeek API', provider: 'deepseek', testStatus: 'active', isActive: true }] },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('DeepSeek API')).toBeInTheDocument());
    expect(screen.queryByText('Быстро подключить 9Router как провайдера')).not.toBeInTheDocument();
    expect(screen.getByText(/9Router уже добавлен как провайдер/)).toBeInTheDocument();
  });

  it('labels a direct binding distinctly from a 9Router-routed one, and gives a friendly status for a pending approval', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      providers: [
        { id: 'prov-1', name: 'deepseek', provider_type: 'openai_compatible', api_base: 'https://api.deepseek.com/v1', status: 'active', created_at: '', updated_at: '' },
        { id: 'prov-2', name: '9router', provider_type: 'openai_compatible', api_base: 'http://9router:20128/v1', status: 'awaiting_approval', created_at: '', updated_at: '' },
      ],
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('прямой биндинг')).toBeInTheDocument());
    expect(screen.getByText('через governance-биндинг')).toBeInTheDocument();
    expect(screen.getByText('ждёт /approve')).toBeInTheDocument();
  });

  it('shows auth type and an expiry hint for an OAuth 9Router connection', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      sessionStatus: { configured: true, pending_task_id: null },
      combos: { available: true, error: null, combos: [] },
      connections: {
        available: true, error: null,
        connections: [{ id: 'c1', name: 'KImiK3', provider: 'kimi', testStatus: 'active', isActive: true, authType: 'oauth', expiresIn: 900 }],
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('OAuth')).toBeInTheDocument());
    expect(screen.getByText('истекает через 15м')).toBeInTheDocument();
  });

  it('shows a clean "not used yet" hint for an unlimited tier with no traffic instead of "0 requests"', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      routerTiers: [{
        id: 'rtier-1', label: 'Бесплатный', tier_rank: 3, kind: 'binding',
        provider_binding_id: 'prov-1', model_override: 'claude-sonnet-4.5',
        quota_limit: null, quota_window_hours: 24, is_active: true,
        created_at: '', updated_at: '',
        quota: { used: 0, limit: null, window_hours: 24, resets_at: null, pct: null, exhausted: false },
      }],
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('claude-sonnet-4.5')).toBeInTheDocument());
    expect(screen.getByText('не использован —')).toBeInTheDocument();
    expect(screen.queryByText(/запросов, 24ч · без лимита/)).not.toBeInTheDocument();
  });

  it('swaps the tier form\'s free-text Модель field for a catalog picker once a 9Router binding is chosen', async () => {
    const fetchMock = stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      providers: [
        { id: 'prov-9r', name: '9router', provider_type: 'openai_compatible', api_base: 'http://9router:20128/v1', status: 'active', created_at: '', updated_at: '' },
        { id: 'prov-ds', name: 'deepseek', provider_type: 'openai_compatible', api_base: 'https://api.deepseek.com/v1', status: 'active', created_at: '', updated_at: '' },
      ],
      routerModels: {
        available: true, error: null,
        models: [{ id: 'ds/deepseek-chat', owned_by: 'ds' }, { id: 'kimi/kimi-k3', owned_by: 'kimi' }],
      },
    });
    render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getByText('Добавить тир')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Добавить тир'));

    // Direct (non-9Router) binding first — still the plain text input.
    fireEvent.change(await screen.findByPlaceholderText('Подписка'), { target: { value: 'Прямой DS' } });
    fireEvent.change(screen.getByDisplayValue('— выберите —'), { target: { value: 'prov-ds' } });
    expect(screen.getByPlaceholderText('deepseek-chat')).toBeInTheDocument();

    // Switch to the 9Router binding — the field becomes a catalog select.
    fireEvent.change(screen.getByDisplayValue('deepseek (https://api.deepseek.com/v1)'), { target: { value: 'prov-9r' } });
    expect(screen.queryByPlaceholderText('deepseek-chat')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('ds/deepseek-chat')).toBeInTheDocument());
    expect(screen.getByText('kimi/kimi-k3')).toBeInTheDocument();

    fireEvent.change(screen.getByDisplayValue('— выберите модель —'), { target: { value: 'ds/deepseek-chat' } });
    fireEvent.click(screen.getByText('Добавить', { selector: 'span' }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(c => c[0] === '/api/router/tiers' && (c[1] as RequestInit)?.method === 'POST');
      expect(call).toBeTruthy();
    });
    const [, init] = fetchMock.mock.calls.find(c => c[0] === '/api/router/tiers' && (c[1] as RequestInit)?.method === 'POST')!;
    const body = JSON.parse(String((init as RequestInit).body));
    expect(body).toMatchObject({ provider_binding_id: 'prov-9r', model_override: 'ds/deepseek-chat' });
  });

  it('draws a connecting arrow between fallback-chain tier cards and colors the quota bar by status', async () => {
    stubFetch({
      routerStats: { available: false, reachable: null, model_count: null, provider_count: null, sample_models: [], dashboard_url: '', error: null },
      routerTiers: [
        {
          id: 'rtier-1', label: 'Подписка', tier_rank: 1, kind: 'binding',
          provider_binding_id: 'prov-1', model_override: 'claude-opus-4.7',
          quota_limit: 200, quota_window_hours: 24, is_active: true, created_at: '', updated_at: '',
          quota: { used: 122, limit: 200, window_hours: 24, resets_at: null, pct: 61, exhausted: false },
        },
        {
          id: 'rtier-2', label: 'Дешёвый', tier_rank: 2, kind: 'binding',
          provider_binding_id: 'prov-2', model_override: 'glm-5.1',
          quota_limit: 500, quota_window_hours: 24, is_active: true, created_at: '', updated_at: '',
          quota: { used: 0, limit: 500, window_hours: 24, resets_at: null, pct: 0, exhausted: false },
        },
      ],
    });
    const { container } = render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
    fireEvent.click(screen.getByText(t('adminNavRouter')));
    await waitFor(() => expect(screen.getAllByText('claude-opus-4.7').length).toBeGreaterThan(0));
    // One arrow before TIER 1 and one between TIER 1 and TIER 2 (TIER 0 -> 1 -> 2).
    expect(container.querySelectorAll('.admin-tier-arrow').length).toBe(2);
    expect(container.querySelector('.admin-quota-fill.is-active')).toBeTruthy();
    expect(container.querySelector('.admin-quota-fill.is-standby')).toBeTruthy();
  });

  describe('projects', () => {
    it('lists projects in the Проекты section with their assigned agent count', async () => {
      stubFetch({
        projects: [
          { id: 'proj-1', name: 'Сайт клиента', description: 'лендинг', is_active: true, created_at: '', updated_at: '' },
        ],
      });
      const agents = [
        { id: 'a1', name: 'Agent 1', system_prompt: 'sp', model: 'm', project_id: 'proj-1' },
        { id: 'a2', name: 'Agent 2', system_prompt: 'sp', model: 'm', project_id: null },
      ];
      render(<AgentsAdminTab agents={agents} models={[]} fetchAgents={() => {}} t={t} />);
      fireEvent.click(await screen.findByText(t('adminNavProjects')));
      expect(await screen.findByText('Сайт клиента')).toBeInTheDocument();
      expect(screen.getByText(/1 агент/)).toBeInTheDocument();
    });

    it('creates a project from the add-project form', async () => {
      const fetchMock = stubFetch({ projects: [] });
      render(<AgentsAdminTab agents={[]} models={[]} fetchAgents={() => {}} t={t} />);
      fireEvent.click(await screen.findByText(t('adminNavProjects')));
      // Empty list auto-opens the add form (same pattern as Tiers/Router).
      fireEvent.change(await screen.findByPlaceholderText('Сайт клиента X'), { target: { value: 'Новый проект' } });
      fireEvent.click(screen.getByText('Add', { selector: 'span' }));
      await waitFor(() => {
        const call = fetchMock.mock.calls.find(c => c[0] === '/api/projects' && (c[1] as RequestInit)?.method === 'POST');
        expect(call).toBeTruthy();
      });
    });

    it('offers the fetched projects in the agent edit form\'s project select', async () => {
      stubFetch({
        projects: [
          { id: 'proj-1', name: 'Сайт клиента', description: '', is_active: true, created_at: '', updated_at: '' },
        ],
      });
      const agents = [{ id: 'a1', name: 'Agent 1', system_prompt: 'sp', model: 'm' }];
      render(<AgentsAdminTab agents={agents} models={[]} fetchAgents={() => {}} t={t} />);
      fireEvent.click(await screen.findByTitle(t('editAgent')));
      await waitFor(() => expect(screen.getByText('Сайт клиента')).toBeInTheDocument());
    });
  });
});
