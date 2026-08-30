/**
 * CRM additions to «Клиенты»: the sales pipeline (deals), follow-ups (tasks)
 * and client notes. Kept in its own file rather than folded into
 * ClientsTab.test.tsx — that file is already large, and this one has its own
 * fixture set (deals, tasks, notes) that would otherwise crowd it.
 *
 * Same style throughout: stub fetch, render, drive the real DOM. What matters
 * here is the wiring — that an action in the UI produces the right request —
 * not re-testing each presentational component's own rendering logic.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ClientsTab } from '../ClientsTab';
import { invalidateCurrencySettings } from '../currency/useCurrencySettings';

const currencySettings = {
  baseCurrency: 'USD',
  displayCurrency: 'USD',
  rates: { USD: '1', BYN: '3.27' },
  currencies: [
    { code: 'USD', nameRu: 'Доллар США', nameEn: 'US Dollar', symbol: '$', decimals: 2, isBase: true },
    { code: 'BYN', nameRu: 'Белорусский рубль', nameEn: 'Belarusian Ruble', symbol: 'BYN', decimals: 2, isBase: false },
  ],
  updatedAt: null,
  updatedBy: null,
};

const openDeal = {
  id: 'deal-1', title: 'Сайт для Zenith', stage: 'NEW', clientId: null,
  contactName: 'Игорь', contactEmail: '', contactPhone: '', companyName: 'Zenith',
  value: '1500', currency: 'USD', source: 'Сайт', responsibleUserId: null,
  expectedCloseDate: null, lostReason: '',
  createdAt: '2026-08-01T09:00:00.000000+00:00', updatedAt: '2026-08-01T09:00:00.000000+00:00',
  stageChangedAt: '2026-08-01T09:00:00.000000+00:00', wonAt: null, lostAt: null,
};

const pipelineSummary = {
  currency: 'USD', baseCurrency: 'USD',
  openCount: 1, openValue: { usd: '1500.00', display: '1500.00' },
  wonCount: 0, lostCount: 0, winRate: null,
  wonThisMonthCount: 0, wonThisMonthValue: { usd: '0.00', display: '0.00' },
  byStage: {
    NEW: { count: 1, valueUsd: '1500.00' }, CONTACTED: { count: 0, valueUsd: '0.00' },
    QUALIFIED: { count: 0, valueUsd: '0.00' }, PROPOSAL: { count: 0, valueUsd: '0.00' },
    NEGOTIATION: { count: 0, valueUsd: '0.00' }, WON: { count: 0, valueUsd: '0.00' },
    LOST: { count: 0, valueUsd: '0.00' },
  },
};

const openTask = {
  id: 'task-1', clientId: null, dealId: null, title: 'Позвонить клиенту', description: '',
  dueAt: '2026-08-25', status: 'OPEN', priority: 'NORMAL', assignee: '',
  createdAt: '2026-08-01T09:00:00.000000+00:00', updatedAt: '2026-08-01T09:00:00.000000+00:00',
  completedAt: null, overdue: true,
};

const taskStats = { open: 1, overdue: 1, dueToday: 0 };

const midot = {
  id: 'cli-1', type: 'COMPANY', name: 'Midot Project', projectName: 'Веб-платформа',
  description: '', status: 'ACTIVE', primaryContactId: 'cnt-1', responsibleUserId: null,
  createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z', archivedAt: null,
  contacts: [], primaryContact: {
    id: 'cnt-1', clientId: 'cli-1', name: 'Иван Петров', email: 'ivan@midot.com',
    phone: '', telegram: '', isPrimary: true, createdAt: '', updatedAt: '',
  },
  services: [], agentConnections: [], primaryService: null, primaryConnection: null,
  paymentStatus: 'NO_INVOICE', primaryAmountUsd: undefined, primaryAmountDisplay: undefined,
  nextBillingDate: null, billingFrequency: null,
};

const dashboard = {
  currency: 'USD', baseCurrency: 'USD',
  totalClients: 1, activeClients: 1,
  connectedAgents: 0, agentsWithErrors: 0, agentsOffline: 0,
  mrr: { usd: '0.00', display: '0.00' },
  toInvoice: { usd: '0.00', display: '0.00', count: 0 },
  overdue: { usd: '0.00', display: '0.00', count: 0 },
  collectedUsd: '0.00',
};

const clientNote = {
  id: 'note-1', clientId: 'cli-1', author: 'Альберт', body: 'Договорились созвониться в среду',
  pinned: false, createdAt: '2026-08-20T09:00:00Z', updatedAt: '2026-08-20T09:00:00Z',
};

function stubFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    calls.push(`${method} ${path}`);
    const ok = (data: unknown) => ({ ok: true, json: async () => data });

    if (path.startsWith('/api/settings/currency')) return ok(currencySettings);
    if (path.startsWith('/api/clients/dashboard')) return ok(overrides.dashboard ?? dashboard);
    if (path.startsWith('/api/clients/services')) return ok([]);
    if (path.startsWith('/api/clients/agents')) return ok([]);
    if (path.startsWith('/api/clients?')) {
      return ok({ items: overrides.clients ?? [midot], total: 1, page: 1, limit: 50, pages: 1, displayCurrency: 'USD' });
    }
    if (path.match(/^\/api\/clients\/cli-1$/)) return ok(midot);
    if (path.startsWith('/api/clients/cli-1/billing')) return ok(null);
    if (path.startsWith('/api/clients/cli-1/activity')) return ok([]);
    if (path === '/api/clients/cli-1/notes' && method === 'GET') return ok(overrides.notes ?? [clientNote]);
    if (path === '/api/clients/cli-1/notes' && method === 'POST') {
      return ok({ ...clientNote, id: 'note-2', body: JSON.parse(init!.body as string).body });
    }
    if (path.startsWith('/api/client-notes/')) return ok({ ...clientNote });
    if (path.startsWith('/api/client-agent-connections')) return ok([]);
    if (path.startsWith('/api/client-invoices')) return ok([]);
    if (path.startsWith('/api/client-payments')) return ok([]);

    if (path === '/api/deals/pipeline') return ok(overrides.pipeline ?? pipelineSummary);
    if (path === '/api/deals' && method === 'GET') return ok(overrides.deals ?? [openDeal]);
    if (path === '/api/deals' && method === 'POST') {
      const body = JSON.parse(init!.body as string);
      return ok({ ...openDeal, id: 'deal-2', ...body, value: body.value || null });
    }
    if (path.match(/^\/api\/deals\/deal-1$/) && method === 'PATCH') {
      const body = JSON.parse(init!.body as string);
      return ok({ ...openDeal, ...body });
    }
    if (path.match(/^\/api\/deals\/deal-1$/) && method === 'DELETE') return ok({ status: 'success' });
    if (path === '/api/deals/deal-1/convert-to-client' && method === 'POST') {
      return ok({ deal: { ...openDeal, stage: 'WON', clientId: 'cli-9' }, client: { ...midot, id: 'cli-9', name: 'Zenith' } });
    }

    if (path === '/api/client-tasks' && method === 'GET') return ok(overrides.tasks ?? [openTask]);
    if (path === '/api/client-tasks/stats') return ok(overrides.taskStats ?? taskStats);
    if (path === '/api/client-tasks' && method === 'POST') {
      const body = JSON.parse(init!.body as string);
      return ok({ ...openTask, id: 'task-2', ...body, status: 'OPEN', overdue: false });
    }
    if (path === '/api/client-tasks/task-1/complete' && method === 'POST') {
      return ok({ ...openTask, status: 'DONE', overdue: false, completedAt: '2026-08-25T00:00:00Z' });
    }
    if (path.startsWith('/api/client-tasks?clientId=')) return ok([]);

    return ok({});
  });
  vi.stubGlobal('fetch', handler);
  return { handler, calls };
}

beforeEach(() => {
  invalidateCurrencySettings();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ClientsTab — deals (sales pipeline)', () => {
  it('shows the pipeline KPI row and the deal in its stage column', async () => {
    stubFetch();
    render(<ClientsTab pane="deals" />);
    expect(await screen.findByText('Открытых сделок')).toBeInTheDocument();
    expect(screen.getByText('Сайт для Zenith')).toBeInTheDocument();
  });

  it('creates a deal from the drawer', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab pane="deals" />);
    fireEvent.click(await screen.findByRole('button', { name: /Новая сделка/ }));
    const drawer = await screen.findByRole('dialog');

    fireEvent.change(within(drawer).getByLabelText('Название'), { target: { value: 'CRM для Orbita' } });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Создать сделку' }));

    await waitFor(() => {
      const post = handler.mock.calls.find(([url, init]) => String(url) === '/api/deals' && (init as RequestInit)?.method === 'POST');
      expect(post).toBeTruthy();
      const body = JSON.parse((post![1] as RequestInit).body as string);
      expect(body.title).toBe('CRM для Orbita');
    });
  });

  it('refuses to create a deal without a title', async () => {
    stubFetch();
    render(<ClientsTab pane="deals" />);
    fireEvent.click(await screen.findByRole('button', { name: /Новая сделка/ }));
    const drawer = await screen.findByRole('dialog');
    fireEvent.click(within(drawer).getByRole('button', { name: 'Создать сделку' }));
    expect(await within(drawer).findByText(/Укажите название сделки/)).toBeInTheDocument();
  });

  it('moves a deal to a new stage from the card and sends a PATCH', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab pane="deals" />);
    const select = await screen.findByLabelText('Изменить стадию сделки «Сайт для Zenith»');
    fireEvent.change(select, { target: { value: 'CONTACTED' } });

    await waitFor(() => {
      const patch = handler.mock.calls.find(([url, init]) => String(url) === '/api/deals/deal-1' && (init as RequestInit)?.method === 'PATCH');
      expect(patch).toBeTruthy();
      expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({ stage: 'CONTACTED' });
    });
  });

  it('asks for a reason before moving a deal to Lost, and sends it along', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('Слишком дорого');
    const { handler } = stubFetch();
    render(<ClientsTab pane="deals" />);
    const select = await screen.findByLabelText('Изменить стадию сделки «Сайт для Zenith»');
    fireEvent.change(select, { target: { value: 'LOST' } });

    await waitFor(() => {
      const patch = handler.mock.calls.find(([url, init]) => String(url) === '/api/deals/deal-1' && (init as RequestInit)?.method === 'PATCH');
      expect(patch).toBeTruthy();
      expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({ stage: 'LOST', lostReason: 'Слишком дорого' });
    });
  });

  it('deletes an unconverted deal after confirmation, from its drawer', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { handler } = stubFetch();
    render(<ClientsTab pane="deals" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Сайт для Zenith' }));
    const drawer = await screen.findByRole('dialog');
    fireEvent.click(within(drawer).getByRole('button', { name: /Удалить/ }));

    await waitFor(() => {
      expect(handler.mock.calls.some(([url, init]) =>
        String(url) === '/api/deals/deal-1' && (init as RequestInit)?.method === 'DELETE')).toBe(true);
    });
  });

  it('converts a deal into a client through the same client drawer, prefilled', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab pane="deals" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Сайт для Zenith' }));
    const dealDrawer = await screen.findByRole('dialog');
    fireEvent.click(within(dealDrawer).getByRole('button', { name: /Конвертировать в клиента/ }));

    const clientDrawer = await screen.findByRole('dialog', { name: /Клиент из сделки/ });
    expect(within(clientDrawer).getByDisplayValue('Zenith')).toBeInTheDocument();
    fireEvent.click(within(clientDrawer).getByRole('button', { name: 'Создать клиента' }));

    await waitFor(() => {
      const convert = handler.mock.calls.find(([url, init]) =>
        String(url) === '/api/deals/deal-1/convert-to-client' && (init as RequestInit)?.method === 'POST');
      expect(convert).toBeTruthy();
      // The conversion goes through the deal's own endpoint, never the plain
      // client-create one — a converted deal must stay linked.
      const plainCreate = handler.mock.calls.find(([url, init]) =>
        String(url) === '/api/clients' && (init as RequestInit)?.method === 'POST');
      expect(plainCreate).toBeFalsy();
    });
  });
});

describe('ClientsTab — tasks (follow-ups)', () => {
  it('lists tasks with their KPI counts', async () => {
    stubFetch();
    render(<ClientsTab pane="tasks" />);
    expect(await screen.findByText('Позвонить клиенту')).toBeInTheDocument();
    // "Просрочено" also labels the filter tab beside the KPI row — scope to
    // the stat card specifically.
    const overdueLabels = screen.getAllByText('Просрочено');
    const overdueCard = overdueLabels.map(el => el.closest('.admin-stat-card')).find(Boolean) as HTMLElement;
    expect(within(overdueCard).getByText('1')).toBeInTheDocument();
  });

  it('adds a task from the composer', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab pane="tasks" />);
    await screen.findByText('Позвонить клиенту');
    fireEvent.change(screen.getByPlaceholderText('Новая задача…'), { target: { value: 'Отправить КП' } });
    fireEvent.click(screen.getByRole('button', { name: /Добавить/ }));

    await waitFor(() => {
      const post = handler.mock.calls.find(([url, init]) => String(url) === '/api/client-tasks' && (init as RequestInit)?.method === 'POST');
      expect(post).toBeTruthy();
      expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({ title: 'Отправить КП' });
    });
  });

  it('completes a task from its checkbox', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab pane="tasks" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Отметить выполненной: Позвонить клиенту' }));

    await waitFor(() => {
      expect(handler.mock.calls.some(([url, init]) =>
        String(url) === '/api/client-tasks/task-1/complete' && (init as RequestInit)?.method === 'POST')).toBe(true);
    });
  });
});

describe('ClientsTab — client notes', () => {
  it('adds a note to a client from the Заметки tab', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab pane="clients" />);
    fireEvent.click(await screen.findByText('Midot Project'));
    fireEvent.click(await screen.findByRole('button', { name: 'Заметки' }));

    fireEvent.change(await screen.findByPlaceholderText('Что важно запомнить об этом клиенте?'), {
      target: { value: 'Новая заметка про клиента' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Добавить заметку' }));

    await waitFor(() => {
      const post = handler.mock.calls.find(([url, init]) =>
        String(url) === '/api/clients/cli-1/notes' && (init as RequestInit)?.method === 'POST');
      expect(post).toBeTruthy();
      expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({ body: 'Новая заметка про клиента' });
    });
  });

  it('shows an existing note in the list', async () => {
    stubFetch();
    render(<ClientsTab pane="clients" />);
    fireEvent.click(await screen.findByText('Midot Project'));
    fireEvent.click(await screen.findByRole('button', { name: 'Заметки' }));
    expect(await screen.findByText('Договорились созвониться в среду')).toBeInTheDocument();
  });
});
