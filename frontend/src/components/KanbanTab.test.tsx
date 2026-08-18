import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { KanbanTab } from './KanbanTab';
import type { DevRun, DevRunRevision } from '../types';

/**
 * The board is where a product gets iterated on, so these cover the loop an
 * owner actually runs: finish a card → refine it into the next revision →
 * inspect the revisions → roll one back. Each assertion checks the request
 * that reaches the backend, not just that a control rendered.
 */

const agents = [{ id: 'agent-a', name: 'Data Analyst', is_enabled: true }] as never;

function makeRun(overrides: Partial<DevRun> = {}): DevRun {
  return {
    id: 'run-aaaaaaaaaaaa', goal: 'Собери лендинг', status: 'done', plan_id: null,
    trace_id: 'trace-1', iter_used: 3, iter_budget: 0, cost_used: 0, cost_budget: null,
    wall_deadline: null, checkpoint_step: null, status_reason: '',
    created_at: '2026-08-18T10:00:00+00:00', updated_at: '2026-08-18T10:20:00+00:00',
    assignee_agent_id: 'agent-a', demo_url: '/demo/site-run-aaaaaaaaaaaa/',
    demo_snapshot_url: '/demo/run-aaaaaaaaaaaa/', parent_run_id: null,
    root_run_id: 'run-aaaaaaaaaaaa', revision: 1, ...overrides,
  };
}

function makeRevision(overrides: Partial<DevRunRevision> = {}): DevRunRevision {
  return { ...makeRun(), is_live: false, has_snapshot: true, ...overrides };
}

function stubFetch(runs: DevRun[], lineage: DevRunRevision[] = []) {
  // `_init` is unused here but keeps the recorded call a [path, init] pair,
  // which the assertions below read to check the POST bodies.
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(input);
    if (path.startsWith('/api/dev-runs?')) return { ok: true, json: async () => runs };
    if (path.endsWith('/lineage')) return { ok: true, json: async () => lineage };
    if (path.includes('/feedback?status=open')) return { ok: true, json: async () => [] };
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);
  return { fetchMock };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('KanbanTab — product revisions', () => {
  it('refining a finished card creates the next revision of the same product', async () => {
    const { fetchMock } = stubFetch([makeRun()]);
    render(<KanbanTab language="ru" agents={agents} />);

    fireEvent.click(await screen.findByRole('button', { name: /Доработать/ }));
    // The panel says which product is being refined, so the goal field can be
    // about the change alone instead of restating the whole brief.
    expect(screen.getByText(/Доработка:/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Доработать'), { target: { value: 'сделать шапку липкой' } });
    fireEvent.click(screen.getByRole('button', { name: 'Начать сразу' }));

    await waitFor(() => {
      const created = fetchMock.mock.calls.find(([path, init]) =>
        path === '/api/dev-runs' && (init as RequestInit)?.method === 'POST');
      expect(created).toBeTruthy();
      expect(JSON.parse((created![1] as RequestInit).body as string)).toMatchObject({
        goal: 'сделать шапку липкой',
        parent_run_id: 'run-aaaaaaaaaaaa',
        start: true,
      });
    });
  });

  it('a card that is still executing cannot be forked mid-write', async () => {
    stubFetch([makeRun({ status: 'running' })]);
    render(<KanbanTab language="ru" agents={agents} />);
    await screen.findByText('Собери лендинг');
    expect(screen.queryByRole('button', { name: /Доработать/ })).toBeNull();
  });

  it('shows the revision history and rolls the live URL back to an older build', async () => {
    const first = makeRevision({ id: 'run-aaaaaaaaaaaa', revision: 1, goal: 'Собери лендинг' });
    const second = makeRevision({
      id: 'run-bbbbbbbbbbbb', revision: 2, goal: 'Липкая шапка', is_live: true,
      parent_run_id: 'run-aaaaaaaaaaaa', demo_snapshot_url: '/demo/run-bbbbbbbbbbbb/',
    });
    const { fetchMock } = stubFetch([second, first], [first, second]);
    render(<KanbanTab language="ru" agents={agents} />);

    fireEvent.click((await screen.findAllByRole('button', { name: /Версии/ }))[0]);
    await screen.findByText('Версии продукта');
    expect(screen.getByText('текущая')).toBeTruthy();

    // Only the revision that is not live offers a promote — the live one has
    // nothing to promote to.
    const promote = await screen.findByRole('button', { name: /Сделать текущей/ });
    fireEvent.click(promote);

    await waitFor(() => {
      const promoted = fetchMock.mock.calls.find(([path]) =>
        String(path) === '/api/dev-runs/run-aaaaaaaaaaaa/promote');
      expect(promoted).toBeTruthy();
    });
  });

  it('marks a revision whose build was deleted as unopenable instead of offering a dead link', async () => {
    const gone = makeRevision({ id: 'run-cccccccccccc', revision: 2, has_snapshot: false, goal: 'Удалённая сборка' });
    stubFetch([makeRun()], [makeRevision({ is_live: true }), gone]);
    render(<KanbanTab language="ru" agents={agents} />);

    fireEvent.click((await screen.findAllByRole('button', { name: /Версии/ }))[0]);
    await screen.findByText('Сборка удалена');
    // No promote offered for a revision with nothing behind it.
    expect(screen.queryByRole('button', { name: /Сделать текущей/ })).toBeNull();
  });

  it('surfaces a failed lineage load instead of showing an empty history', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith('/api/dev-runs?')) return { ok: true, json: async () => [makeRun()] };
      if (path.endsWith('/lineage')) return { ok: false, status: 500, json: async () => ({}) };
      if (path.includes('/feedback?status=open')) return { ok: true, json: async () => [] };
      return { ok: true, json: async () => ({}) };
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);
    render(<KanbanTab language="ru" agents={agents} />);

    fireEvent.click((await screen.findAllByRole('button', { name: /Версии/ }))[0]);
    await screen.findByText(/HTTP 500/);
    expect(screen.getByText('У этой задачи ещё нет версий.')).toBeTruthy();
  });

  it('a single-revision product shows no version chrome', async () => {
    stubFetch([makeRun({ demo_url: null })]);
    render(<KanbanTab language="ru" agents={agents} />);
    await screen.findByText('Собери лендинг');
    expect(screen.queryByText('v1')).toBeNull();
    expect(screen.queryByRole('button', { name: /Версии/ })).toBeNull();
  });
});

describe('KanbanTab — queued continuations', () => {
  it('explains that a continuation is waiting for the revision it continues', async () => {
    const parent = makeRun({ id: 'run-aaaaaaaaaaaa', status: 'running', revision: 1 });
    const child = makeRun({
      id: 'run-bbbbbbbbbbbb', status: 'planned', revision: 2, goal: 'Липкая шапка',
      parent_run_id: 'run-aaaaaaaaaaaa', root_run_id: 'run-aaaaaaaaaaaa', demo_url: null,
    });
    stubFetch([child, parent]);
    render(<KanbanTab language="ru" agents={agents} />);

    await screen.findByText('Липкая шапка');
    expect(screen.getByText(/Ждёт завершения v1/)).toBeTruthy();
  });

  it('does not label a queued card as waiting once its parent has finished', async () => {
    const parent = makeRun({ id: 'run-aaaaaaaaaaaa', status: 'done', revision: 1 });
    const child = makeRun({
      id: 'run-bbbbbbbbbbbb', status: 'planned', revision: 2, goal: 'Липкая шапка',
      parent_run_id: 'run-aaaaaaaaaaaa', root_run_id: 'run-aaaaaaaaaaaa', demo_url: null,
    });
    stubFetch([child, parent]);
    render(<KanbanTab language="ru" agents={agents} />);

    await screen.findByText('Липкая шапка');
    expect(screen.queryByText(/Ждёт завершения/)).toBeNull();
  });
});
