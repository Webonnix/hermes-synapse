/**
 * The Google Calendar half of «Календарь»: connection status, event chips
 * colour-coded by type, and the create/edit/delete round trip through
 * /api/calendar/*. CRM tasks/deals-on-the-calendar are already covered in
 * ClientsTabCrm.test.tsx's "calendar" describe block; this file is about the
 * part that talks to Google.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ClientsTab } from '../ClientsTab';
import { invalidateCurrencySettings } from '../currency/useCurrencySettings';

const currencySettings = {
  baseCurrency: 'USD', displayCurrency: 'USD', rates: { USD: '1' },
  currencies: [{ code: 'USD', nameRu: 'Доллар США', nameEn: 'US Dollar', symbol: '$', decimals: 2, isBase: true }],
  updatedAt: null, updatedBy: null,
};

const dashboard = {
  currency: 'USD', baseCurrency: 'USD', totalClients: 0, activeClients: 0,
  connectedAgents: 0, agentsWithErrors: 0, agentsOffline: 0,
  mrr: { usd: '0.00', display: '0.00' }, toInvoice: { usd: '0.00', display: '0.00', count: 0 },
  overdue: { usd: '0.00', display: '0.00', count: 0 }, collectedUsd: '0.00',
};

const pipelineSummary = {
  currency: 'USD', baseCurrency: 'USD', openCount: 0, openValue: { usd: '0.00', display: '0.00' },
  wonCount: 0, lostCount: 0, winRate: null, wonThisMonthCount: 0, wonThisMonthValue: { usd: '0.00', display: '0.00' },
  byStage: Object.fromEntries(
    ['NEW', 'CONTACTED', 'QUALIFIED', 'PROPOSAL', 'NEGOTIATION', 'WON', 'LOST'].map(s => [s, { count: 0, valueUsd: '0.00' }]),
  ),
};

const today = new Date();
const iso = (d: Date) => d.toISOString().slice(0, 10);
const plusDT = (days: number, hhmm: string) => {
  const d = new Date(today);
  d.setDate(d.getDate() + days);
  return `${iso(d)}T${hhmm}:00`;
};

let googleEvents: Array<Record<string, unknown>> = [];

function resetEvents() {
  googleEvents = [
    {
      id: 'g1', title: 'Созвон с Zenith', start: plusDT(0, '10:00'), end: plusDT(0, '10:30'),
      allDay: false, location: '', description: '', link: 'https://calendar.google.com/g1', eventType: 'CALL',
    },
  ];
}

function stubFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    calls.push(`${method} ${path}`);
    const ok = (data: unknown) => ({ ok: true, json: async () => data });

    if (path.startsWith('/api/settings/currency')) return ok(currencySettings);
    if (path.startsWith('/api/clients/dashboard')) return ok(dashboard);
    if (path.startsWith('/api/clients/services')) return ok([]);
    if (path.startsWith('/api/clients/agents')) return ok([]);
    if (path.startsWith('/api/clients?')) return ok({ items: [], total: 0, page: 1, limit: 50, pages: 1, displayCurrency: 'USD' });
    if (path.startsWith('/api/client-agent-connections')) return ok([]);
    if (path.startsWith('/api/client-invoices')) return ok([]);
    if (path.startsWith('/api/client-payments')) return ok([]);
    if (path === '/api/deals/pipeline') return ok(pipelineSummary);
    if (path === '/api/deals') return ok([]);
    if (path === '/api/client-tasks') return ok([]);
    if (path === '/api/client-tasks/stats') return ok({ open: 0, overdue: 0, dueToday: 0 });

    if (path === '/api/calendar/status') {
      return ok(overrides.status ?? { connected: true });
    }
    if (path.startsWith('/api/calendar/events') && method === 'GET') {
      return ok(googleEvents);
    }
    if (path === '/api/calendar/events' && method === 'POST') {
      const body = JSON.parse(init!.body as string);
      const created = {
        id: 'g2', title: body.title, start: `${body.date}T${body.time}:00`,
        end: `${body.date}T${body.endTime || '11:00'}:00`, allDay: false,
        location: body.location || '', description: body.description || '', link: '', eventType: body.eventType,
      };
      googleEvents = [...googleEvents, created];
      return ok(created);
    }
    if (path.match(/^\/api\/calendar\/events\/g1$/) && method === 'PATCH') {
      const body = JSON.parse(init!.body as string);
      googleEvents = googleEvents.map(e => (e.id === 'g1' ? { ...e, title: body.title ?? e.title } : e));
      return ok(googleEvents.find(e => e.id === 'g1'));
    }
    if (path.match(/^\/api\/calendar\/events\/g1$/) && method === 'DELETE') {
      googleEvents = googleEvents.filter(e => e.id !== 'g1');
      return ok({ status: 'success', id: 'g1' });
    }

    return ok({});
  });
  vi.stubGlobal('fetch', handler);
  return { handler, calls };
}

async function openCalendar() {
  fireEvent.click(await screen.findByRole('button', { name: 'Календарь' }));
}

beforeEach(() => {
  invalidateCurrencySettings();
  resetEvents();
});

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Календарь — Google integration', () => {
  it('shows a Google event on its day, colour-coded by type', async () => {
    stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    const chip = await screen.findByText('Созвон с Zenith');
    expect(chip.className).toContain('is-google');
    expect(chip.className).toContain('is-call');
  });

  it('lists the event in the agenda for the selected (today) cell', async () => {
    stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    const agenda = await screen.findByRole('complementary', { name: /События:/ });
    // findByText, not getByText: the agenda renders instantly (it's a static
    // container), but its content depends on the events fetch, which
    // resolves a tick later — the assertion has to wait for that, not just
    // for the container to exist.
    expect(await within(agenda).findByText(/10:00 · Созвон с Zenith/)).toBeInTheDocument();
  });

  it('shows the upcoming-events panel once connected', async () => {
    stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    expect(await screen.findByText('Предстоящие события')).toBeInTheDocument();
  });

  it('shows a not-connected banner and hides the create button when Google is not linked', async () => {
    stubFetch({ status: { connected: false, reason: 'no_credentials' } });
    render(<ClientsTab />);
    await openCalendar();
    expect(await screen.findByText(/Google Календарь не подключен/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Создать событие/ })).not.toBeInTheDocument();
    expect(screen.queryByText('Предстоящие события')).not.toBeInTheDocument();
  });

  it('creates a Google Calendar event through the drawer', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    fireEvent.click(await screen.findByRole('button', { name: /Создать событие/ }));
    const drawer = await screen.findByRole('dialog');

    fireEvent.click(within(drawer).getByRole('button', { name: 'Обучение' }));
    fireEvent.change(within(drawer).getByPlaceholderText('Например, Созвон с клиентом'), {
      target: { value: 'Обучение команды' },
    });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Создать' }));

    await waitFor(() => {
      const post = handler.mock.calls.find(([url, init]) =>
        String(url) === '/api/calendar/events' && (init as RequestInit)?.method === 'POST');
      expect(post).toBeTruthy();
      const body = JSON.parse((post![1] as RequestInit).body as string);
      expect(body).toMatchObject({ title: 'Обучение команды', eventType: 'TRAINING' });
    });
  });

  it('refuses to create an event without a title', async () => {
    stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    fireEvent.click(await screen.findByRole('button', { name: /Создать событие/ }));
    const drawer = await screen.findByRole('dialog');
    fireEvent.click(within(drawer).getByRole('button', { name: 'Создать' }));
    expect(await within(drawer).findByText(/Укажите название события/)).toBeInTheDocument();
  });

  it('edits an existing event, pre-filling its current values', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    const agenda = await screen.findByRole('complementary', { name: /События:/ });
    fireEvent.click(await within(agenda).findByText(/10:00 · Созвон с Zenith/));
    const drawer = await screen.findByRole('dialog', { name: 'Редактирование события' });
    expect(within(drawer).getByDisplayValue('Созвон с Zenith')).toBeInTheDocument();
    // The type it was created with (CALL) is pre-selected.
    expect(within(drawer).getByRole('button', { name: 'Звонок' })).toHaveClass('is-active');

    fireEvent.change(within(drawer).getByDisplayValue('Созвон с Zenith'), { target: { value: 'Перенесённый созвон' } });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Сохранить' }));

    await waitFor(() => {
      const patch = handler.mock.calls.find(([url, init]) =>
        String(url) === '/api/calendar/events/g1' && (init as RequestInit)?.method === 'PATCH');
      expect(patch).toBeTruthy();
      expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({ title: 'Перенесённый созвон' });
    });
  });

  it('deletes an event after confirmation and removes it from the grid', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { handler } = stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    const agenda = await screen.findByRole('complementary', { name: /События:/ });
    fireEvent.click(await within(agenda).findByText(/10:00 · Созвон с Zenith/));
    const drawer = await screen.findByRole('dialog', { name: 'Редактирование события' });
    fireEvent.click(within(drawer).getByRole('button', { name: /Удалить/ }));

    await waitFor(() => {
      expect(handler.mock.calls.some(([url, init]) =>
        String(url) === '/api/calendar/events/g1' && (init as RequestInit)?.method === 'DELETE')).toBe(true);
    });
    await waitFor(() => {
      expect(screen.queryByText('Созвон с Zenith')).not.toBeInTheDocument();
    });
  });

  it('does not delete when the confirmation is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { handler } = stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    const agenda = await screen.findByRole('complementary', { name: /События:/ });
    fireEvent.click(await within(agenda).findByText(/10:00 · Созвон с Zenith/));
    const drawer = await screen.findByRole('dialog', { name: 'Редактирование события' });
    fireEvent.click(within(drawer).getByRole('button', { name: /Удалить/ }));

    expect(handler.mock.calls.some(([url, init]) =>
      String(url) === '/api/calendar/events/g1' && (init as RequestInit)?.method === 'DELETE')).toBe(false);
    expect(screen.getByText('Созвон с Zenith')).toBeInTheDocument();
  });

  it('opens the event in Google Calendar via its link', async () => {
    stubFetch();
    render(<ClientsTab />);
    await openCalendar();
    const link = await screen.findByRole('link', { name: /Google Календарь/ });
    expect(link).toHaveAttribute('href', 'https://calendar.google.com/g1');
    expect(link).toHaveAttribute('target', '_blank');
  });
});
