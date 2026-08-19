import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ClientsTab } from '../ClientsTab';
import { invalidateCurrencySettings } from '../currency/useCurrencySettings';

const currencySettings = {
  baseCurrency: 'USD',
  displayCurrency: 'BYN',
  rates: { USD: '1', BYN: '3.27', KZT: '515.5', CNY: '7.24' },
  currencies: [
    { code: 'USD', nameRu: 'Доллар США', nameEn: 'US Dollar', symbol: '$', decimals: 2, isBase: true },
    { code: 'BYN', nameRu: 'Белорусский рубль', nameEn: 'Belarusian Ruble', symbol: 'BYN', decimals: 2, isBase: false },
    { code: 'KZT', nameRu: 'Казахстанский тенге', nameEn: 'Tenge', symbol: '₸', decimals: 2, isBase: false },
    { code: 'CNY', nameRu: 'Китайский юань', nameEn: 'Yuan', symbol: '¥', decimals: 2, isBase: false },
  ],
  updatedAt: null,
  updatedBy: null,
};

const dashboard = {
  currency: 'BYN', baseCurrency: 'USD',
  totalClients: 24, activeClients: 18,
  connectedAgents: 15, agentsWithErrors: 2, agentsOffline: 1,
  mrr: { usd: '12450.00', display: '40711.50' },
  toInvoice: { usd: '4500.00', display: '14715.00', count: 5 },
  overdue: { usd: '1200.00', display: '3924.00', count: 2 },
  collectedUsd: '9000.00',
};

const midot = {
  id: 'cli-1', type: 'COMPANY', name: 'Midot Project', projectName: 'Веб-платформа',
  description: '', status: 'ACTIVE', primaryContactId: 'cnt-1', responsibleUserId: null,
  createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z', archivedAt: null,
  contacts: [], primaryContact: {
    id: 'cnt-1', clientId: 'cli-1', name: 'Иван Петров', email: 'ivan@midot.com',
    phone: '+375 29 123-45-67', telegram: '', isPrimary: true, createdAt: '', updatedAt: '',
  },
  services: [], agentConnections: [],
  primaryService: {
    id: 'csvc-1', clientId: 'cli-1', serviceId: 'svc-1', serviceName: 'Разработка',
    title: 'Разработка и поддержка', description: '', serviceType: 'RECURRING',
    amount: '1500', currency: 'USD', status: 'ACTIVE', startedAt: null, completedAt: null,
    billing: {
      id: 'b-1', clientServiceId: 'csvc-1', frequency: 'MONTHLY', billingDay: 1,
      nextBillingDate: '2026-09-01', customIntervalDays: null, autoAdvanceBillingDate: true,
    },
  },
  primaryConnection: {
    id: 'cac-1', clientId: 'cli-1', clientServiceId: 'csvc-1', agentId: 'agent-12',
    agentName: 'Агент #12', status: 'CONNECTED', desiredState: 'ACTIVE',
    connectionType: 'BOT', channel: 'telegram', connectedAt: null, disconnectedAt: null,
    lastSeenAt: '2026-08-19T10:00:00Z', lastHealthCheckAt: null, errorCode: null, errorMessage: null,
  },
  paymentStatus: 'PAID',
  primaryAmountUsd: '1500.00',
  primaryAmountDisplay: '4905.00',
  nextBillingDate: '2026-09-01',
  billingFrequency: 'MONTHLY',
};

const shopEasy = {
  ...midot,
  id: 'cli-2', name: 'ShopEasy', projectName: 'Интернет-магазин',
  primaryContact: { ...midot.primaryContact, id: 'cnt-2', name: 'Анна Смирнова', email: 'anna@shopeasy.com' },
  primaryService: {
    ...midot.primaryService, id: 'csvc-2', amount: '800', currency: 'BYN', serviceName: 'Поддержка',
  },
  primaryConnection: { ...midot.primaryConnection, id: 'cac-2', agentName: 'Агент #7', status: 'ERROR', errorMessage: 'Токен отозван' },
  paymentStatus: 'OVERDUE',
  primaryAmountUsd: '244.65',
  primaryAmountDisplay: '800.00',
};

function stubFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    calls.push(`${init?.method || 'GET'} ${path}`);
    const ok = (data: unknown) => ({ ok: true, json: async () => data });
    if (path.startsWith('/api/settings/currency')) return ok(currencySettings);
    if (path.startsWith('/api/clients/dashboard')) {
      if (overrides.dashboardForbidden) {
        return { ok: false, status: 403, json: async () => ({ detail: 'Недостаточно прав: требуется «clients.financials.view»' }) };
      }
      return ok(dashboard);
    }
    if (path.startsWith('/api/clients/services')) return ok([{ id: 'svc-1', name: 'Разработка', description: '', isActive: true }]);
    if (path.startsWith('/api/clients/agents')) return ok([{ id: 'agent-12', name: 'Агент #12', role: '', status: 'idle', isEnabled: true }]);
    if (path.startsWith('/api/clients?')) {
      return ok({
        items: overrides.items ?? [midot, shopEasy],
        total: 2, page: 1, limit: 50, pages: 1, displayCurrency: 'BYN',
      });
    }
    if (path === '/api/clients' && init?.method === 'POST') return ok({ id: 'cli-3' });
    if (path.startsWith('/api/client-agent-connections')) return ok([]);
    if (path.startsWith('/api/client-invoices')) return ok([]);
    if (path.startsWith('/api/client-payments')) return ok([]);
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

describe('ClientsTab', () => {
  it('renders the KPI row in the display currency', async () => {
    stubFetch();
    render(<ClientsTab />);
    expect(await screen.findByText('Всего клиентов')).toBeInTheDocument();
    expect(screen.getByText('24')).toBeInTheDocument();
    expect(screen.getByText('Активных: 18')).toBeInTheDocument();
    // 40 711,50 BYN — formatted from the value the backend already converted.
    const mrr = screen.getByText('MRR').closest('.admin-stat-card');
    expect(mrr?.textContent).toContain('40');
    expect(mrr?.textContent).toContain('711');
  });

  it('keeps the source currency visible next to the converted amount', async () => {
    stubFetch();
    render(<ClientsTab />);
    const row = (await screen.findByText('Midot Project')).closest('tr');
    expect(row).toBeTruthy();
    // Converted (BYN) is primary, the agreed USD price stays underneath.
    expect(row!.textContent).toContain('4');
    expect(row!.textContent).toContain('905');
    expect(row!.textContent).toContain('≈');
    expect(row!.textContent).toContain('1');
    expect(row!.textContent).toContain('500');
  });

  it('does not repeat the original when the currencies already match', async () => {
    stubFetch();
    render(<ClientsTab />);
    const row = (await screen.findByText('ShopEasy')).closest('tr');
    // Priced in BYN, displayed in BYN — nothing to disambiguate.
    expect(row!.textContent).not.toContain('≈');
  });

  it('shows the agent connection state per row', async () => {
    stubFetch();
    render(<ClientsTab />);
    const connected = (await screen.findByText('Midot Project')).closest('tr');
    expect(within(connected!).getByText('Агент #12')).toBeInTheDocument();
    expect(within(connected!).getByText('online')).toBeInTheDocument();

    const failing = screen.getByText('ShopEasy').closest('tr');
    expect(within(failing!).getByText('Агент #7')).toBeInTheDocument();
    expect(within(failing!).getByText('ошибка')).toBeInTheDocument();
  });

  it('shows payment status', async () => {
    stubFetch();
    render(<ClientsTab />);
    expect(await screen.findByText('Оплачено')).toBeInTheDocument();
    expect(screen.getByText('Просрочено')).toBeInTheDocument();
  });

  it('sends search and filters to the backend rather than filtering locally', async () => {
    const { calls } = stubFetch();
    render(<ClientsTab />);
    await screen.findByText('Midot Project');

    fireEvent.change(screen.getByLabelText('Поиск клиентов'), { target: { value: 'midot' } });
    await waitFor(() => {
      expect(calls.some(call => call.includes('search=midot'))).toBe(true);
    });

    fireEvent.change(screen.getByLabelText('Подключение агента'), { target: { value: 'ERROR' } });
    await waitFor(() => {
      expect(calls.some(call => call.includes('agentStatus=ERROR'))).toBe(true);
    });
  });

  it('renders the empty state when there are no clients', async () => {
    stubFetch({ items: [] });
    render(<ClientsTab />);
    expect(await screen.findByText('Клиентов пока нет')).toBeInTheDocument();
  });

  it('opens the create drawer with USD preselected', async () => {
    stubFetch();
    render(<ClientsTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Добавить клиента/ }));
    const drawer = await screen.findByRole('dialog');
    expect(within(drawer).getByLabelText('Валюта')).toHaveValue('USD');
    expect(within(drawer).getByText('Не подключать сейчас')).toBeInTheDocument();
  });

  it('creates a client with amount and currency as separate fields', async () => {
    const { handler } = stubFetch();
    render(<ClientsTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Добавить клиента/ }));
    const drawer = await screen.findByRole('dialog');

    fireEvent.change(within(drawer).getByPlaceholderText('Например, Midot Project'), { target: { value: 'Новый клиент' } });
    fireEvent.change(within(drawer).getByPlaceholderText('Разработка'), { target: { value: 'Консалтинг' } });
    fireEvent.change(within(drawer).getByPlaceholderText('1500'), { target: { value: '2000' } });
    fireEvent.change(within(drawer).getByLabelText('Валюта'), { target: { value: 'BYN' } });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Добавить клиента' }));

    await waitFor(() => {
      const post = handler.mock.calls.find(
        ([url, init]) => String(url) === '/api/clients' && (init as RequestInit)?.method === 'POST',
      );
      expect(post).toBeTruthy();
      const body = JSON.parse((post![1] as RequestInit).body as string);
      expect(body.name).toBe('Новый клиент');
      // Amount stays a string and currency stays its own field, all the way to
      // the wire — never "2000 BYN" in one value.
      expect(body.service.amount).toBe('2000');
      expect(body.service.currency).toBe('BYN');
    });
  });

  it('hides every money column for a role without financial access', async () => {
    const rowWithoutMoney = { ...midot };
    delete (rowWithoutMoney as Record<string, unknown>).paymentStatus;
    delete (rowWithoutMoney as Record<string, unknown>).primaryAmountUsd;
    delete (rowWithoutMoney as Record<string, unknown>).primaryAmountDisplay;

    stubFetch({ dashboardForbidden: true, items: [rowWithoutMoney] });
    render(<ClientsTab />);

    expect(await screen.findByText('Midot Project')).toBeInTheDocument();
    expect(screen.queryByText('MRR')).not.toBeInTheDocument();
    expect(screen.queryByText('Стоимость')).not.toBeInTheDocument();
    expect(screen.queryByText('Счета')).not.toBeInTheDocument();
    expect(screen.queryByText('Платежи')).not.toBeInTheDocument();
  });

  it('shows the total count, not the page size', async () => {
    stubFetch();
    render(<ClientsTab />);
    expect(await screen.findByText(/Показано 1–2 из 2/)).toBeInTheDocument();
  });
});
