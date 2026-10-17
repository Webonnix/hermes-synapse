import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentLearningPanel } from './AgentLearningPanel';

const track = {
  id: 'devsecops:security', agent_id: 'devsecops', title: 'Безопасность', enabled: true,
  topics: ['supply chain attacks', 'container hardening'], feeds: [{ kind: 'osv_lockfiles' }],
  cadence_hours: 168, max_sources: 8, max_items: 5,
  next_run_at: '2026-10-20T02:00:00Z', last_run_at: '2026-10-13T02:10:00Z', last_status: 'ok',
};

const run = {
  id: 'run-1', track_id: track.id, agent_id: 'devsecops', trigger: 'schedule', status: 'ok',
  started_at: '2026-10-13T02:00:00Z', finished_at: '2026-10-13T02:10:00Z',
  sources_count: 7, items_new: 3, items_merged: 1, items_quarantined: 1, items_dropped: 0,
  model: 'local', cost_usd: 0.0123, note_path: 'Hermes/Learning/devsecops/2026-10-13-security.md',
  note_pending: 0, error: '',
};

const activeItem = {
  id: 'item-a', agent_id: 'devsecops', track_id: track.id, title: 'Пиновать образы по digest',
  summary: 'Тег можно перезаписать, digest — нет.', why_it_matters: '', how_to_apply: 'image@sha256:…',
  tags: ['docker'], confidence: 'high', source_urls: ['https://example.org/post'], status: 'active',
  quarantine_reason: '', pinned: 0, created_at: '2026-10-13T02:09:00Z', expires_at: null,
};

const quarantinedItem = {
  ...activeItem, id: 'item-q', title: 'Подозрительный пункт', status: 'quarantined',
  quarantine_reason: 'обращение к агенту в тексте источника',
};

function stubFetch(overrides: Record<string, (init?: RequestInit) => { ok: boolean; status?: number; json: () => Promise<unknown> }> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    const key = `${method} ${path}`;
    if (overrides[key]) return overrides[key](init);
    if (path.startsWith('/api/agent-learning/tracks?')) return { ok: true, json: async () => [track] };
    if (path.startsWith('/api/agent-learning/runs?')) return { ok: true, json: async () => [run] };
    if (path.includes('status=quarantined')) return { ok: true, json: async () => [quarantinedItem] };
    if (path.startsWith('/api/agent-learning/items?')) return { ok: true, json: async () => [activeItem] };
    if (method === 'POST' && path.startsWith('/api/agent-learning/items/')) {
      return { ok: true, json: async () => ({ ...quarantinedItem, status: 'active' }) };
    }
    if (method === 'PUT') {
      return { ok: true, json: async () => ({ ...track, ...JSON.parse(String(init?.body || '{}')) }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const calls = (fetchMock: ReturnType<typeof stubFetch>) =>
  fetchMock.mock.calls.map(([input, init]) => `${(init as RequestInit | undefined)?.method || 'GET'} ${String(input)}`);

describe('AgentLearningPanel', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders tracks, run history and the quarantine block with its reason', async () => {
    const fetchMock = stubFetch();
    render(<AgentLearningPanel agentId="devsecops" />);

    expect(within(await screen.findByTestId('learning-track-devsecops:security')).getByText('Безопасность')).toBeInTheDocument();
    expect(screen.getByDisplayValue(/supply chain attacks/)).toBeInTheDocument();

    const row = await screen.findByTestId('learning-run-run-1');
    expect(within(row).getByText('готово')).toBeInTheDocument();
    expect(within(row).getByText('7')).toBeInTheDocument();
    expect(within(row).getByText('$0.0123')).toBeInTheDocument();
    expect(within(row).getByText('Заметка Obsidian')).toBeInTheDocument();

    const quarantine = screen.getByTestId('learning-quarantine');
    expect(within(quarantine).getByText(/обращение к агенту/)).toBeInTheDocument();
    expect(within(quarantine).getByText('Одобрить')).toBeInTheDocument();
    expect(screen.getByText('Пиновать образы по digest')).toBeInTheDocument();

    expect(calls(fetchMock)).toContain('GET /api/agent-learning/tracks?agent_id=devsecops');
    expect(calls(fetchMock)).toContain('GET /api/agent-learning/runs?track_id=devsecops%3Asecurity&limit=20');
  });

  it('approves a quarantined item and reloads the lists', async () => {
    const fetchMock = stubFetch();
    render(<AgentLearningPanel agentId="devsecops" />);

    const quarantine = await screen.findByTestId('learning-quarantine');
    fireEvent.click(within(quarantine).getByText('Одобрить'));

    await waitFor(() => expect(calls(fetchMock)).toContain('POST /api/agent-learning/items/item-q/approve'));
    await waitFor(() => {
      const itemLoads = calls(fetchMock).filter(call => call.includes('status=quarantined'));
      expect(itemLoads.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('pins an item with an explicit body', async () => {
    const fetchMock = stubFetch();
    render(<AgentLearningPanel agentId="devsecops" />);

    const card = await screen.findByTestId('learning-item-item-a');
    fireEvent.click(within(card).getByText('Закрепить'));

    await waitFor(() => {
      const pinCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/agent-learning/items/item-a/pin');
      expect(pinCall?.[1]?.body).toBe(JSON.stringify({ pinned: true }));
    });
  });

  it('saves the cadence and enabled flag through PUT', async () => {
    const fetchMock = stubFetch();
    render(<AgentLearningPanel agentId="devsecops" />);

    fireEvent.change(await screen.findByLabelText('Частота'), { target: { value: '24' } });
    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([input, init]) =>
        String(input) === '/api/agent-learning/tracks/devsecops%3Asecurity' && init?.method === 'PUT');
      expect(put?.[1]?.body).toBe(JSON.stringify({ cadence_hours: 24 }));
    });
    expect(await screen.findByDisplayValue('Каждый день')).toBeInTheDocument();
  });

  it('shows the 409 detail when a run is already going', async () => {
    stubFetch({
      'POST /api/agent-learning/tracks/devsecops%3Asecurity/run': () => ({
        ok: false, status: 409, json: async () => ({ detail: 'Уже идёт прогон обучения, дождитесь его окончания' }),
      }),
    });
    render(<AgentLearningPanel agentId="devsecops" />);

    fireEvent.click(await screen.findByText('Запустить сейчас'));
    expect(await screen.findByText(/Уже идёт прогон обучения/)).toBeInTheDocument();
  });

  it('survives an error object where a list was expected', async () => {
    stubFetch({
      'GET /api/agent-learning/items?agent_id=devsecops&status=active': () => ({
        ok: true, json: async () => ({ detail: 'something odd' }),
      }),
    });
    render(<AgentLearningPanel agentId="devsecops" />);

    expect(within(await screen.findByTestId('learning-track-devsecops:security')).getByText('Безопасность')).toBeInTheDocument();
    expect(await screen.findByText('Пусто.')).toBeInTheDocument();
  });

  it('renders an owner-only note on 403 instead of an error', async () => {
    stubFetch({
      'GET /api/agent-learning/tracks?agent_id=devsecops': () => ({
        ok: false, status: 403, json: async () => ({ detail: 'Forbidden' }),
      }),
    });
    render(<AgentLearningPanel agentId="devsecops" />);

    expect(await screen.findByText('Раздел доступен только владельцу.')).toBeInTheDocument();
    expect(screen.queryByText('Forbidden')).not.toBeInTheDocument();
  });
});
