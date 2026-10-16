/**
 * SMM Factory → «Диалоги»: the wiring from the three columns to the inbox API. Same style as the
 * clients tests — stub fetch, render, drive the real DOM, assert on requests.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DialogsSection } from './DialogsSection';
import { OPEN_INBOX_EVENT } from './inboxApi';

const meta = {
  fts: true, unread_conversations: 1, can_reply: true, can_edit_rules: true, can_manage_voice: true,
  can_create_clients: true, can_view_financials: true,
};

const baseConversation = {
  id: 'ibx-1', binding_id: 'bind-1', platform: 'telegram', chat_id: '555', session_id: 'tgbot:bind-1:555',
  subagent_id: 'agent-1', project_id: null, client_id: null, contact_id: null, display_name: '@ivan',
  external_user_id: '555', status: 'open', unread_count: 1, last_message_at: '2026-09-14T10:00:00Z',
  last_message_preview: 'Есть доставка?', last_speaker: 'customer', summary: '', cost_usd: 0.0021,
  last_rule_action: null, last_rule_pattern: null, channel_meta: {}, handling: 'ai', state_mode: null,
  account: 'shop_bot', response_mode: 'draft', agent_name: 'Консультант', client_name: null,
  created_at: '2026-09-14T09:00:00Z', updated_at: '2026-09-14T10:00:00Z',
};

const messages = [
  { id: 1, conversation_id: 'ibx-1', speaker: 'customer', text: 'Есть доставка?', delivery: 'sent', pending_reply_id: null,
    external_event_id: 'm1', consumed_at: null, meta: {}, created_at: '2026-09-14T10:00:00Z', attachments: [] },
  { id: 2, conversation_id: 'ibx-1', speaker: 'agent', text: 'Да, по всему городу.', delivery: 'draft', pending_reply_id: 'draft-1',
    external_event_id: null, consumed_at: null, meta: {}, created_at: '2026-09-14T10:00:05Z', attachments: [] },
];

const targets = [
  { type: 'subagent', id: 'agent-1', name: 'Консультант', enabled: true, available: true },
  { type: 'external_voice', id: 'vt-1', name: 'Голос ElevenLabs', enabled: true, available: true,
    provider_name: 'EL', capability_mode: 'tts', voice_id: 'rachel', failure_policy: 'fallback_internal', health: { ok: true } },
];

const route: Record<string, unknown> = {
  conversation_id: 'ibx-1', pinned: false, target_type: null, target_id: null, pinned_by: null, pinned_at: null,
  last_target_type: 'subagent', last_target_id: 'agent-1', last_source: 'auto', last_reason: '', last_latency_ms: 1,
  last_decided_at: null, target: null,
};

function stubFetch(overrides: { meta?: unknown; metaStatus?: number; list?: unknown; bindings?: unknown } = {}) {
  let conversation: Record<string, unknown> = { ...baseConversation };
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    const ok = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data });

    if (path === '/api/inbox/meta') return ok(overrides.meta ?? meta, overrides.metaStatus ?? 200);
    if (path === '/api/inbox/bindings') return ok(overrides.bindings ?? []);
    if (path === '/api/inbox/route-targets') return ok(targets);
    if (path.startsWith('/api/inbox/conversations?')) return ok(overrides.list ?? { items: [conversation], next_cursor: null });
    if (path === '/api/inbox/conversations/ibx-1' && method === 'GET') return ok(conversation);
    if (path.startsWith('/api/inbox/conversations/ibx-1/messages')) return ok({ items: messages, next_cursor: null });
    if (path === '/api/inbox/conversations/ibx-1/route' && method === 'GET') return ok(route);
    if (path === '/api/inbox/conversations/ibx-1/route' && method === 'POST') {
      const body = JSON.parse(String(init!.body));
      return ok({ ...route, pinned: true, target_type: body.target_type, target_id: body.target_id, target: targets[1] });
    }
    if (path === '/api/inbox/conversations/ibx-1/read') {
      conversation = { ...conversation, unread_count: 0 };
      return ok(conversation);
    }
    if (path === '/api/inbox/conversations/ibx-1/takeover') {
      conversation = { ...conversation, handling: 'human', state_mode: 'owner_takeover' };
      return ok(conversation);
    }
    if (path === '/api/inbox/conversations/ibx-1/reply') return ok({ status: 'sent' });
    if (path === '/api/inbox/conversations/ibx-1/whisper') return ok({ status: 'queued' });
    if (path === '/api/channel-replies/draft-1/send') return ok({ status: 'sent' });
    return ok({});
  });
  vi.stubGlobal('fetch', handler);
  return handler;
}

const requests = (handler: ReturnType<typeof stubFetch>, path: string, method = 'POST') =>
  handler.mock.calls.filter(([url, init]) => String(url) === path && ((init as RequestInit)?.method || 'GET') === method);

async function openConversation() {
  fireEvent.click(await screen.findByRole('button', { name: /@ivan/ }));
  return screen.findByRole('region', { name: 'Переписка' });
}

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('DialogsSection', () => {
  it('lists conversations, opens the thread and marks it read', async () => {
    const handler = stubFetch();
    render(<DialogsSection />);
    const thread = await openConversation();

    expect(await within(thread).findByText('Да, по всему городу.')).toBeInTheDocument();
    expect(within(thread).getByText('черновик')).toBeInTheDocument();
    await waitFor(() => expect(requests(handler, '/api/inbox/conversations/ibx-1/read')).toHaveLength(1));
  });

  it('while the AI answers, the composer sends a whisper, never a reply', async () => {
    const handler = stubFetch();
    render(<DialogsSection />);
    const thread = await openConversation();

    fireEvent.change(await within(thread).findByLabelText('Подсказка агенту'), { target: { value: 'предложи скидку' } });
    fireEvent.click(within(thread).getByRole('button', { name: /Подсказать/ }));

    await waitFor(() => expect(requests(handler, '/api/inbox/conversations/ibx-1/whisper')).toHaveLength(1));
    const [, init] = requests(handler, '/api/inbox/conversations/ibx-1/whisper')[0];
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ text: 'предложи скидку' });
    expect(requests(handler, '/api/inbox/conversations/ibx-1/reply')).toHaveLength(0);
  });

  it('after taking the conversation over, the composer replies as a person', async () => {
    const handler = stubFetch();
    render(<DialogsSection />);
    const thread = await openConversation();

    fireEvent.click(await within(thread).findByRole('button', { name: /Забрать диалог/ }));
    const replyBox = await within(thread).findByLabelText('Ответ клиенту');
    fireEvent.change(replyBox, { target: { value: 'Здравствуйте, это менеджер' } });
    fireEvent.click(within(thread).getByRole('button', { name: /Ответить/ }));

    await waitFor(() => expect(requests(handler, '/api/inbox/conversations/ibx-1/reply')).toHaveLength(1));
    expect(requests(handler, '/api/inbox/conversations/ibx-1/takeover')).toHaveLength(1);
    expect(within(thread).getByRole('button', { name: /Вернуть агенту/ })).toBeInTheDocument();
  });

  it('sends a draft through the existing draft queue', async () => {
    const handler = stubFetch();
    render(<DialogsSection />);
    const thread = await openConversation();

    const draft = (await within(thread).findByText('Да, по всему городу.')).closest('article')!;
    fireEvent.click(within(draft).getByRole('button', { name: /Отправить/ }));

    await waitFor(() => expect(requests(handler, '/api/channel-replies/draft-1/send')).toHaveLength(1));
  });

  it('pins the conversation to an external voice target', async () => {
    const handler = stubFetch();
    render(<DialogsSection />);
    await openConversation();
    const card = await screen.findByRole('complementary', { name: 'Карточка разговора' });

    fireEvent.change(await within(card).findByLabelText('Маршрут'), { target: { value: 'external_voice|vt-1' } });
    fireEvent.click(within(card).getByRole('button', { name: /Закрепить/ }));

    await waitFor(() => expect(requests(handler, '/api/inbox/conversations/ibx-1/route')).toHaveLength(1));
    const [, init] = requests(handler, '/api/inbox/conversations/ibx-1/route')[0];
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ target_type: 'external_voice', target_id: 'vt-1' });
    expect(await within(card).findByText(/Голос ElevenLabs/, { selector: '.ibx-target-chip' })).toBeInTheDocument();
  });

  it('an error body where a list belongs shows an error instead of blanking the page', async () => {
    stubFetch({ list: { detail: 'Unauthorized' } });
    render(<DialogsSection />);
    expect(await screen.findByRole('alert')).toHaveTextContent('неожиданный ответ');
    expect(screen.getByRole('button', { name: /Все диалоги/ })).toBeInTheDocument();
  });

  it('a role without inbox access gets a clear refusal', async () => {
    stubFetch({ meta: { detail: 'Недостаточно прав' }, metaStatus: 403 });
    render(<DialogsSection />);
    expect(await screen.findByText('У вашей роли нет доступа к диалогам.')).toBeInTheDocument();
  });

  it('narrows by service, and the account picker follows the service', async () => {
    const handler = stubFetch({
      bindings: [
        { id: 'bind-1', platform: 'telegram', bot_username: 'shop_bot', subagent_id: 'agent-1', status: 'active', response_mode: 'draft', agent_name: null },
        { id: 'line-1', platform: 'phone', bot_username: 'Офис', subagent_id: 'agent-1', status: 'active', response_mode: 'auto', agent_name: null },
      ],
    });
    render(<DialogsSection />);
    const strip = await screen.findByRole('group', { name: 'Сервис' });

    fireEvent.click(within(strip).getByRole('button', { name: /Телефон/ }));

    await waitFor(() => expect(handler.mock.calls.some(([url]) =>
      String(url).startsWith('/api/inbox/conversations?') && String(url).includes('platform=phone'))).toBe(true));
    const accounts = screen.getByLabelText('Аккаунт') as HTMLSelectElement;
    expect(within(accounts).queryByText(/shop_bot/)).not.toBeInTheDocument();
    expect(within(accounts).getByText(/Офис/)).toBeInTheDocument();
  });

  it('keeps calls, rules and the agents’ memory as sub-sections', async () => {
    stubFetch();
    render(<DialogsSection />);
    const nav = await screen.findByRole('navigation', { name: 'Разделы диалогов' });
    ['Все диалоги', 'Звонки', 'Правила и часы', 'Память агентов'].forEach(name =>
      expect(within(nav).getByRole('button', { name: new RegExp(name) })).toBeInTheDocument());
  });

  it('opens the conversation another section asked for', async () => {
    stubFetch();
    render(<DialogsSection />);
    await screen.findByRole('button', { name: /@ivan/ });

    act(() => {
      window.dispatchEvent(new CustomEvent(OPEN_INBOX_EVENT, { detail: { conversationId: 'ibx-1' } }));
    });

    const thread = await screen.findByRole('region', { name: 'Переписка' });
    expect(await within(thread).findByText('Есть доставка?')).toBeInTheDocument();
  });
});
