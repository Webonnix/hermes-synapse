import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DevRunsTab } from './DevRunsTab';

const runningRun = {
  id: 'run-abc123def456', goal: 'Add a /health endpoint', status: 'running' as const,
  plan_id: 'plan-1', trace_id: 'trace-1', iter_used: 40, iter_budget: 200,
  cost_used: 0.12, cost_budget: 1.0, wall_deadline: null, checkpoint_step: '3',
  status_reason: '', created_at: '2026-07-25T09:00:00Z', updated_at: '2026-07-25T09:10:00Z',
};

const runDetails = {
  ...runningRun,
  steps: [
    { id: 's1', run_id: runningRun.id, seq: 1, phase: 'plan', tool: '', summary: 'Plan plan-1 (verify): Map scope; Add endpoint; Test', status: 'done', created_at: '2026-07-25T09:00:10Z' },
    { id: 's2', run_id: runningRun.id, seq: 2, phase: 'act', tool: 'dev_write_file', summary: '{"path": "app.py", "bytes": 120}', status: 'done', created_at: '2026-07-25T09:01:00Z' },
  ],
};

function stubFetch(overrides: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    if (path.startsWith('/api/dev-runs?')) return { ok: true, json: async () => [runningRun] };
    if (path === `/api/dev-runs/${runningRun.id}` && method === 'GET') return { ok: true, json: async () => runDetails };
    if (path.endsWith('/pause') || path.endsWith('/resume') || path.endsWith('/cancel')) {
      return { ok: true, json: async () => ({ ...runningRun, status: 'paused' }) };
    }
    if (path === '/api/dev-runs' && method === 'POST') {
      return { ok: true, json: async () => ({ ...runningRun, id: 'run-new111222333', status: 'planned' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  Object.assign(fetchMock, overrides);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('DevRunsTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the runs table with status and progress', async () => {
    stubFetch();
    render(<DevRunsTab language="en" />);
    expect(await screen.findByText('Add a /health endpoint')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByText(/run-abc123def456/)).toBeInTheDocument();
  });

  it('opens the live step feed when a run is selected', async () => {
    stubFetch();
    render(<DevRunsTab language="en" />);
    fireEvent.click(await screen.findByText('Add a /health endpoint'));
    expect(await screen.findByText(/dev_write_file/)).toBeInTheDocument();
    expect(screen.getByText(/Plan plan-1/)).toBeInTheDocument();
    // Budget progress bars are visible in the inspector.
    expect(screen.getByText('Iterations')).toBeInTheDocument();
    expect(screen.getByText('$0.120 / $1.000')).toBeInTheDocument();
  });

  it('sends pause command for a running run', async () => {
    const fetchMock = stubFetch();
    render(<DevRunsTab language="en" />);
    fireEvent.click(await screen.findByText('Add a /health endpoint'));
    fireEvent.click(await screen.findByRole('button', { name: /pause/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/dev-runs/${runningRun.id}/pause`,
      expect.objectContaining({ method: 'POST' }),
    ));
  });

  it('creates a new run from the goal input', async () => {
    const fetchMock = stubFetch();
    render(<DevRunsTab language="en" />);
    const input = await screen.findByPlaceholderText(/Run goal/);
    fireEvent.change(input, { target: { value: 'Refactor utils' } });
    fireEvent.click(screen.getByRole('button', { name: /start/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/dev-runs',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ goal: 'Refactor utils' }) }),
    ));
  });
});
