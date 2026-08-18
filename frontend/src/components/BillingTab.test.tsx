import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BillingTab } from './BillingTab';

const plan = {
  id: 'plan-1', name: 'Подписка Про', description: '', period: 'monthly',
  limit_usd: 5, limit_tokens: null, limit_messages: 100, rate_limit_per_min: 6,
  max_message_chars: 2000, allowed_tools: null, system_prompt_suffix: '',
  welcome_message: '', is_active: true, price_usd: 19, is_purchasable: true,
  duration_days: 30, subagent_id: null, created_at: '', updated_at: '',
};

const subscription = {
  id: 'sub_1', plan_id: 'plan-1', token_id: 'tok-1', binding_id: 'bind-1',
  subagent_id: 'agent-consult', customer_ref: 'Иван Петров', status: 'active',
  auto_renew: false, price_usd: 19, started_at: '2026-08-01T10:00:00Z',
  current_period_start: '2026-08-01T10:00:00Z', current_period_end: '2026-09-01T10:00:00Z',
  canceled_at: null, created_at: '2026-08-01T10:00:00Z', updated_at: '2026-08-01T10:00:00Z',
};

const pendingInvoice = {
  id: 'inv_1', provider: 'nowpayments', provider_invoice_id: '4477', plan_id: 'plan-1',
  binding_id: 'bind-1', subagent_id: 'agent-consult', subscription_id: null, token_id: null,
  amount_usd: 19, pay_currency: '', status: 'pending',
  payment_url: 'https://nowpayments.io/payment/?iid=4477', customer_ref: 'Мария Коэн',
  origin: 'bot', origin_platform: 'telegram', origin_chat_id: '999', purpose: 'new',
  last_status: 'waiting', created_at: '2026-08-02T10:00:00Z', updated_at: '2026-08-02T10:00:00Z',
  paid_at: null, expires_at: null,
};

const binding = {
  id: 'bind-1', subagent_id: 'agent-consult', agent_name: 'Консультант',
  platform: 'telegram', bot_username: 'consult_bot', allowed_chat_ids: [],
  status: 'active', created_at: '', updated_at: '', response_mode: 'auto_labeled',
  access_mode: 'token',
};

const config = {
  provider: 'nowpayments', public_base_url: 'https://hermes.example.net', success_url: '',
  api_key_configured: true, ipn_secret_configured: true, providers: ['manual', 'nowpayments'],
};

const overview = {
  invoices: { invoices: 3, paid: 2, pending: 1, revenue_usd: 38 },
  subscriptions: { total: 2, active: 1, expired: 1, canceled: 0 },
  mrr_usd: 19,
  config,
};

function stubFetch(extra: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    if (path === '/api/billing/overview') return { ok: true, json: async () => ({ ...overview, ...extra }) };
    if (path === '/api/billing/subscriptions' && method === 'GET') return { ok: true, json: async () => [subscription] };
    if (path === '/api/billing/invoices' && method === 'GET') return { ok: true, json: async () => [pendingInvoice] };
    if (path === '/api/access/plans') return { ok: true, json: async () => [plan] };
    if (path === '/api/messenger-bindings') return { ok: true, json: async () => [binding] };
    if (path === '/api/billing/invoices/inv_1/mark-paid') {
      return { ok: true, json: async () => ({ plaintext: 'HRM-PAIDTOKEN0123456789abcdef' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('BillingTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('reports recurring revenue and subscription health up front', async () => {
    stubFetch();
    render(<BillingTab />);

    await waitFor(() => expect(screen.getByText('MRR')).toBeInTheDocument());
    expect(screen.getByText('$19.00')).toBeInTheDocument();   // MRR
    expect(screen.getByText('$38.00')).toBeInTheDocument();   // cash received
    expect(screen.getByText('в пересчёте на 30 дней')).toBeInTheDocument();
    expect(screen.getByText('оплачено счетов: 2')).toBeInTheDocument();
  });

  it('lists a subscription with its plan, price and paid-until date', async () => {
    stubFetch();
    render(<BillingTab />);

    await waitFor(() => expect(screen.getByText(/Иван Петров · Подписка Про/)).toBeInTheDocument());
    expect(screen.getByText('Активна')).toBeInTheDocument();
    expect(screen.getByText(/до 01\.09\.2026/)).toBeInTheDocument();
  });

  it('shows where an invoice came from and links to its payment page', async () => {
    stubFetch();
    render(<BillingTab />);
    await waitFor(() => expect(screen.getByText('Счета')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Счета'));
    await waitFor(() => expect(screen.getByText(/Мария Коэн · куплен в чате/)).toBeInTheDocument());
    expect(screen.getByText('Ожидает оплаты')).toBeInTheDocument();
    expect(screen.getByTitle('Страница оплаты')).toHaveAttribute(
      'href', 'https://nowpayments.io/payment/?iid=4477',
    );
    expect(screen.getByText(/Статус у платёжной системы: waiting/)).toBeInTheDocument();
  });

  it('hands over the token once when the owner confirms a payment by hand', async () => {
    stubFetch();
    render(<BillingTab />);
    await waitFor(() => expect(screen.getByText('Счета')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Счета'));

    const confirm = await screen.findByTitle(/Подтвердить оплату вручную/);
    fireEvent.click(confirm);

    await waitFor(() => expect(screen.getByText('Токен выпущен по оплате')).toBeInTheDocument());
    expect(screen.getByDisplayValue('HRM-PAIDTOKEN0123456789abcdef')).toBeInTheDocument();
    expect(screen.getByText(/повторно эта строка не показывается/)).toBeInTheDocument();
  });

  it('spells out the callback address the provider has to reach', async () => {
    stubFetch();
    render(<BillingTab />);
    await waitFor(() => expect(screen.getByText('Приём оплаты')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Приём оплаты'));
    await waitFor(() =>
      expect(screen.getByText('https://hermes.example.net/api/payments/webhook')).toBeInTheDocument(),
    );
  });

  it('warns when the provider is selected but its secrets are missing', async () => {
    stubFetch({ config: { ...config, ipn_secret_configured: false } });
    render(<BillingTab />);
    await waitFor(() => expect(screen.getByText('Приём оплаты')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Приём оплаты'));
    await waitFor(() =>
      expect(screen.getByText(/Не хватает ключей/)).toBeInTheDocument(),
    );
  });

  it('only offers to sell a plan assigned to a different agent through its own bot', async () => {
    const ownPlan = { ...plan, id: 'plan-own', name: 'Тариф консультанта', subagent_id: 'agent-consult' };
    const otherPlan = { ...plan, id: 'plan-other', name: 'Тариф другого агента', subagent_id: 'agent-other' };
    const genericPlan = { ...plan, id: 'plan-generic', name: 'Общий тариф', subagent_id: null, price_usd: 9, is_purchasable: true };

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === '/api/billing/overview') return { ok: true, json: async () => overview };
      if (path === '/api/billing/subscriptions') return { ok: true, json: async () => [] };
      if (path === '/api/billing/invoices') return { ok: true, json: async () => [] };
      if (path === '/api/access/plans') return { ok: true, json: async () => [ownPlan, otherPlan, genericPlan] };
      if (path === '/api/messenger-bindings') return { ok: true, json: async () => [binding] };
      return { ok: true, json: async () => ({}) };
    }));

    render(<BillingTab />);
    await waitFor(() => expect(screen.getByText('Счета')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Счета'));

    const planSelect = await waitFor(() => {
      const select = screen.getAllByRole('combobox').find(el =>
        Array.from((el as HTMLSelectElement).options).some(o => o.text.includes('Тариф консультанта') || o.text.includes('Общий тариф'))
      );
      if (!select) throw new Error('plan select not rendered yet');
      return select;
    });
    const optionTexts = Array.from((planSelect as HTMLSelectElement).options).map(o => o.text);
    expect(optionTexts.some(t => t.includes('Тариф консультанта'))).toBe(true);
    expect(optionTexts.some(t => t.includes('Общий тариф'))).toBe(true);
    expect(optionTexts.some(t => t.includes('Тариф другого агента'))).toBe(false);
  });
});
