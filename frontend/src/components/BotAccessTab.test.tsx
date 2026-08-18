import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BotAccessTab } from './BotAccessTab';

const plan = {
  id: 'plan-1', name: 'Базовая консультация', description: '5 вопросов',
  period: 'monthly', limit_usd: 2, limit_tokens: 50000, limit_messages: 20,
  rate_limit_per_min: 6, max_message_chars: 2000, allowed_tools: null,
  system_prompt_suffix: '', welcome_message: '', is_active: true,
  price_usd: null, is_purchasable: false, duration_days: 30, subagent_id: null,
  created_at: '2026-08-01T10:00:00Z', updated_at: '2026-08-01T10:00:00Z',
};

const agents = [
  { id: 'agent-consult', name: 'Консультант', system_prompt: '', model: 'qwen3:8b' },
];

const token = {
  id: 'tok-1', display: 'HRM-abc123…', token_prefix: 'abc123', label: 'Иван Петров',
  binding_id: 'bind-1', subagent_id: 'agent-consult', plan_id: 'plan-1',
  status: 'active', max_chats: 1, expires_at: null, period_started_at: '2026-08-01T00:00:00Z',
  used_usd: 0.42, used_tokens_in: 1200, used_tokens_out: 300, used_messages: 4,
  limit_usd: null, limit_tokens: null, limit_messages: null, notes: '',
  created_at: '2026-08-01T10:00:00Z', updated_at: '2026-08-02T10:00:00Z', last_used_at: null,
};

const subscriber = {
  id: 'sub-1', token_id: 'tok-1', binding_id: 'bind-1', platform: 'telegram',
  chat_id: '555', external_user_id: '555', display_name: '@ivan',
  session_id: 'tgbot:bind-1:555', status: 'active', profile: { город: 'Хайфа' },
  notes: '', messages_count: 4,
  first_seen_at: '2026-08-01T11:00:00Z', last_seen_at: '2026-08-02T11:00:00Z',
};

const binding = {
  id: 'bind-1', subagent_id: 'agent-consult', agent_name: 'Консультант',
  platform: 'telegram', bot_username: 'consult_bot', allowed_chat_ids: [],
  status: 'active', created_at: '', updated_at: '', response_mode: 'auto_labeled',
  access_mode: 'token',
};

const overview = {
  tokens: { total: 1, active: 1, suspended: 0, revoked: 0, used_usd: 0.42, used_tokens: 1500, used_messages: 4 },
  subscribers: { total: 1, active: 1, blocked: 0 },
  blocked_turns: 2,
  plans: 1,
};

function stubFetch(overrides: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    if (path === '/api/access/overview') return { ok: true, json: async () => overview };
    if (path === '/api/access/plans' && method === 'GET') return { ok: true, json: async () => [plan] };
    if (path === '/api/access/tokens' && method === 'GET') return { ok: true, json: async () => [token] };
    if (path === '/api/access/subscribers' && method === 'GET') return { ok: true, json: async () => [subscriber] };
    if (path === '/api/messenger-bindings') return { ok: true, json: async () => [binding] };
    if (path === '/api/access/tokens' && method === 'POST') {
      return { ok: true, json: async () => ({ tokens: [{ ...token, id: 'tok-2', plaintext: 'HRM-PLAINTEXTVALUE0123456789ab' }] }) };
    }
    if (path === `/api/access/subscribers/${subscriber.id}`) {
      return {
        ok: true,
        json: async () => ({
          subscriber, token, plan, limits: {},
          usage: { turns: 4, tokens_in: 1200, tokens_out: 300, cost_usd: 0.42 },
          conversation: [
            { id: 1, role: 'user', content: 'Здравствуйте', cost_usd: 0 },
            { id: 2, role: 'assistant', content: 'Добрый день!', cost_usd: 0.01 },
          ],
        }),
      };
    }
    return { ok: true, json: async () => ({}) };
  });
  Object.assign(fetchMock, overrides);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('BotAccessTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows cross-bot totals and the issued tokens with their spend', async () => {
    stubFetch();
    render(<BotAccessTab agents={agents} />);

    await waitFor(() => expect(screen.getByText('HRM-abc123…')).toBeInTheDocument());
    expect(screen.getByText('Иван Петров')).toBeInTheDocument();
    expect(screen.getByText(/Израсходовано: \$0\.4200 из \$2\.0000/)).toBeInTheDocument();
    expect(screen.getByText(/1 500 токенов/)).toBeInTheDocument();
    // The blocked-turn count is what tells the owner a limit is actually biting.
    expect(screen.getByText('отклонено 2')).toBeInTheDocument();
  });

  it('shows a freshly issued token exactly once, with the warning that it cannot be shown again', async () => {
    stubFetch();
    render(<BotAccessTab agents={agents} />);
    // The target channel arrives from /api/messenger-bindings, and the button
    // stays disabled until one is selected — wait for that, not just for paint.
    const issueButton = await screen.findByRole('button', { name: 'Выдать' });
    await waitFor(() => expect(issueButton).toBeEnabled());

    fireEvent.click(issueButton);

    await waitFor(() => expect(screen.getByText(/Выдано токенов: 1/)).toBeInTheDocument());
    expect(screen.getByDisplayValue('HRM-PLAINTEXTVALUE0123456789ab')).toBeInTheDocument();
    expect(screen.getByText(/показать эти строки повторно невозможно/)).toBeInTheDocument();
  });

  it('warns when no channel is open to token holders yet', async () => {
    stubFetch({});
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === '/api/access/overview') return { ok: true, json: async () => overview };
      if (path === '/api/messenger-bindings') {
        return { ok: true, json: async () => [{ ...binding, access_mode: 'owner_only' }] };
      }
      return { ok: true, json: async () => [] };
    }));
    render(<BotAccessTab agents={agents} />);

    await waitFor(() => expect(screen.getByText(/Нет ни одного канала в режиме доступа по токенам/)).toBeInTheDocument());
  });

  it('opens a subscriber card with their spend and stored conversation', async () => {
    stubFetch();
    render(<BotAccessTab agents={agents} />);
    await waitFor(() => expect(screen.getByText('Абоненты')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Абоненты'));
    await waitFor(() => expect(screen.getByText('@ivan')).toBeInTheDocument());

    fireEvent.click(screen.getByText('@ivan'));
    await waitFor(() => expect(screen.getByText('Здравствуйте')).toBeInTheDocument());
    expect(screen.getByText('Добрый день!')).toBeInTheDocument();
    expect(screen.getByText('Базовая консультация')).toBeInTheDocument();
  });

  it('lists plan limits so the owner can see what a tariff actually grants', async () => {
    stubFetch();
    render(<BotAccessTab agents={agents} />);
    await waitFor(() => expect(screen.getByText('Тарифы')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Тарифы'));
    await waitFor(() => expect(screen.getByText('Базовая консультация')).toBeInTheDocument());
    expect(screen.getByText(/В месяц · \$2\.0000 · 50 000 токенов · 20 сообщений/)).toBeInTheDocument();
    expect(screen.getByText(/Инструменты: набор по умолчанию/)).toBeInTheDocument();
  });

  it('only offers a bot the tariffs assigned to its own agent (or unassigned ones)', async () => {
    // Two bots, two agents: a plan scoped to the *other* agent must not show up
    // as issuable for this one — the backend would refuse it anyway.
    const otherBinding = { ...binding, id: 'bind-2', subagent_id: 'agent-other', agent_name: 'Другой агент', bot_username: 'other_bot' };
    const ownPlan = { ...plan, id: 'plan-own', name: 'Тариф консультанта', subagent_id: 'agent-consult' };
    const otherPlan = { ...plan, id: 'plan-other', name: 'Тариф другого агента', subagent_id: 'agent-other' };
    const genericPlan = { ...plan, id: 'plan-generic', name: 'Общий тариф', subagent_id: null };

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === '/api/access/overview') return { ok: true, json: async () => overview };
      if (path === '/api/access/plans') return { ok: true, json: async () => [ownPlan, otherPlan, genericPlan] };
      if (path === '/api/access/tokens') return { ok: true, json: async () => [] };
      if (path === '/api/access/subscribers') return { ok: true, json: async () => [] };
      if (path === '/api/messenger-bindings') return { ok: true, json: async () => [binding, otherBinding] };
      return { ok: true, json: async () => ({}) };
    }));

    render(<BotAccessTab agents={agents} />);
    await waitFor(() => expect(screen.getByText('Выдать токен')).toBeInTheDocument());

    const planSelect = await waitFor(() => {
      const select = screen.getAllByRole('combobox').find(el =>
        Array.from((el as HTMLSelectElement).options).some(o => o.text.includes('Без тарифа'))
      );
      if (!select) throw new Error('plan select not rendered yet');
      return select;
    });
    const optionTexts = Array.from((planSelect as HTMLSelectElement).options).map(o => o.text);
    expect(optionTexts).toContain('Тариф консультанта');
    expect(optionTexts).toContain('Общий тариф');
    expect(optionTexts).not.toContain('Тариф другого агента');
  });
});
