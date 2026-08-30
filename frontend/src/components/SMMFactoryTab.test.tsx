import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SMMFactoryTab } from './SMMFactoryTab';
import type { AgentModel } from '../types';

const agents = [{ id: 'agent-1', name: 'DevOps Senior Assistant' }] as unknown as AgentModel[];

const socialPlatforms = [
  { id: 'vk', label: 'VKontakte', delivery: 'poll', required: ['access_token', 'group_id'], account_field: 'group_id' },
  { id: 'instagram', label: 'Instagram', delivery: 'webhook', required: ['access_token', 'ig_user_id', 'app_secret'], account_field: 'ig_user_id' },
  { id: 'facebook', label: 'Facebook', delivery: 'webhook', required: ['access_token', 'page_id', 'app_secret'], account_field: 'page_id' },
  { id: 'whatsapp', label: 'WhatsApp Business', delivery: 'webhook', required: ['access_token', 'phone_number_id', 'app_secret'], account_field: 'phone_number_id' },
];

const bindings = [{
  id: 'vkbind-1', subagent_id: 'agent-1', agent_name: 'DevOps Senior Assistant', platform: 'vk',
  bot_username: 'Моё сообщество (@mygroup)', allowed_chat_ids: [], status: 'active',
  created_at: '2026-08-01T09:00:00Z', updated_at: '2026-08-01T09:00:00Z',
  response_mode: 'draft', access_mode: 'owner_only', external_account_id: '42',
}];

const conversations = [{
  id: 'conv-1', binding_id: 'vkbind-1', platform: 'vk', chat_id: '777', display_name: 'Иван',
  handling_mode: 'ai', updated_at: '2026-08-30T09:00:00Z', created_at: '2026-08-30T08:00:00Z',
  session_id: 'vk:vkbind-1:777', messages_count: 4, last_message_at: '2026-08-30T09:00:00Z',
  last_message_preview: 'сколько стоит?', last_message_role: 'user',
  agent_name: 'DevOps Senior Assistant', account: 'Моё сообщество (@mygroup)', response_mode: 'draft',
}];

const publications = [
  {
    id: 'post-1', title: 'Обновление платформы VEXA', body: 'Представляем новые функции безопасности.',
    platform: 'telegram', account: '@vexa_official', binding_id: '', target_chat_id: '',
    delivery_mode: 'manual', status: 'published', tags: ['обновление'], media_url: '',
    reach: 12400, reach_delta: 8.2, scheduled_at: null, published_at: '2026-07-12T15:30:00Z',
    error: '', created_at: '2026-07-12T10:00:00Z', updated_at: '2026-07-12T15:30:00Z',
  },
  {
    id: 'post-2', title: 'Безопасность платежей', body: 'Как VEXA защищает ваши транзакции.',
    platform: 'instagram', account: '@vexa_payments', binding_id: '', target_chat_id: '',
    delivery_mode: 'manual', status: 'scheduled', tags: ['безопасность'], media_url: '',
    reach: null, reach_delta: null, scheduled_at: '2026-07-15T12:00:00Z', published_at: null,
    error: '', created_at: '2026-07-10T10:00:00Z', updated_at: '2026-07-10T10:00:00Z',
  },
];

const publicationStats = {
  total: 128, published: 98, scheduled: 24, drafts: 6, failed: 0,
  published_share: 76.6, scheduled_share: 18.8, drafts_share: 4.6, failed_share: 0,
  added_last_week: 12, best_reach: 12400, best_reach_delta: 8.2,
  reach_series: [8700, 9400, 11200, 12400],
};

const drafts = [{
  id: 'draft-1', binding_id: 'vkbind-1', platform: 'vk', subagent_id: 'agent-1', chat_id: '777',
  incoming_from: 'Иван', incoming_text: 'сколько стоит?', drafted_reply: 'Зависит от объёма.',
  status: 'pending', created_at: '2026-08-30T09:00:00Z', updated_at: '2026-08-30T09:00:00Z',
}];

function stubFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    void init;
    const path = String(url);
    calls.push(path);
    const body = (() => {
      if (path.startsWith('/api/smm/publications/stats')) return publicationStats;
      if (path.startsWith('/api/smm/publications')) return { items: publications, total: 128, page: 1, per_page: 10, pages: 13 };
      if (path.startsWith('/api/social-platforms')) return socialPlatforms;
      if (path.startsWith('/api/messenger-bindings/conversations')) return conversations;
      if (path.startsWith('/api/messenger-bindings/activity')) return [];
      if (path.startsWith('/api/messenger-bindings')) return bindings;
      if (path.startsWith('/api/channel-replies')) return drafts;
      if (path.startsWith('/api/access/plans')) return [];
      if (path.startsWith('/api/smm/settings')) return { public_base_url: '' };
      return {};
    })();
    return { ok: true, json: async () => (overrides[path] ?? body) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls };
}

describe('SMMFactoryTab', () => {
  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('opens on the publication register', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    expect(await screen.findByText('Обновление платформы VEXA')).toBeInTheDocument();
    expect(screen.getByText('Показано 1–10 из 128 публикаций')).toBeInTheDocument();
  });

  it('shows the connections panel when asked for it', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Подключения/ }));
    expect(await screen.findByText(/Моё сообщество \(@mygroup\)/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Подключить канал/ })).toBeInTheDocument();
  });

  it('offers the social platforms the backend declares, not a hard-coded list', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Подключения/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Подключить канал/ }));
    const platformSelect = await screen.findByDisplayValue('Telegram');
    expect(within(platformSelect as HTMLSelectElement).getByText('VKontakte')).toBeInTheDocument();
    expect(within(platformSelect as HTMLSelectElement).getByText('WhatsApp Business')).toBeInTheDocument();
  });

  it('asks only for the credential fields that platform requires', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Подключения/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Подключить канал/ }));
    fireEvent.change(await screen.findByDisplayValue('Telegram'), { target: { value: 'whatsapp' } });

    expect(await screen.findByText('Phone number ID (WhatsApp Cloud API)')).toBeInTheDocument();
    expect(screen.getByText('App Secret приложения Meta')).toBeInTheDocument();
    // VK-only fields must not leak into another platform's form.
    expect(screen.queryByText('ID сообщества VK')).not.toBeInTheDocument();
  });

  it('warns that a webhook platform needs a reachable backend', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Подключения/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Подключить канал/ }));
    fireEvent.change(await screen.findByDisplayValue('Telegram'), { target: { value: 'instagram' } });
    expect(await screen.findByText(/Сообщения приходят вебхуком/)).toBeInTheDocument();

    fireEvent.change(screen.getByDisplayValue('Instagram'), { target: { value: 'vk' } });
    expect(await screen.findByText(/Bots Long Poll/)).toBeInTheDocument();
  });

  it('sends a social connection to the shared social endpoint', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Подключения/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Подключить канал/ }));
    fireEvent.change(await screen.findByDisplayValue('Telegram'), { target: { value: 'vk' } });
    // Two selects on the form: the platform picker and the agent picker.
    fireEvent.change(screen.getAllByRole('combobox')[1], { target: { value: 'agent-1' } });

    const inputIn = (label: string) =>
      screen.getByText(label).querySelector('input') as HTMLInputElement;
    fireEvent.change(inputIn('Ключ доступа'), { target: { value: 'vk1.secret' } });
    fireEvent.change(inputIn('ID сообщества VK'), { target: { value: '42' } });

    fireEvent.click(screen.getByRole('button', { name: 'Подключить' }));
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([path, init]) => path === '/api/agents/agent-1/social' && Boolean(init));
      expect(post).toBeTruthy();
      expect(JSON.parse(post![1]!.body as string)).toMatchObject({
        platform: 'vk',
        credentials: { access_token: 'vk1.secret', group_id: '42' },
      });
    });
  });

  it('shows every channel’s conversations in one cross-channel list', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Диалоги/ }));
    expect(await screen.findByText('Иван')).toBeInTheDocument();
    // The row names the account and agent, because in this panel the channel is
    // not implied by where you are.
    expect(screen.getByText(/Моё сообщество \(@mygroup\).*DevOps Senior Assistant/)).toBeInTheDocument();
  });

  it('counts waiting drafts on the tab itself', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    const draftsTab = await screen.findByRole('button', { name: /Черновики/ });
    await waitFor(() => expect(within(draftsTab).getByText('1')).toBeInTheDocument());

    fireEvent.click(draftsTab);
    expect(await screen.findByDisplayValue('Зависит от объёма.')).toBeInTheDocument();
    // The connections list is a different panel and must not render here.
    expect(screen.queryByRole('button', { name: /Подключить канал/ })).not.toBeInTheDocument();
  });

  it('keeps the public webhook address in the section settings', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Настройки/ }));

    const input = await screen.findByPlaceholderText('https://vexa.example.com');
    fireEvent.change(input, { target: { value: 'https://vexa.example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Сохранить/ }));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([path, init]) => path === '/api/smm/settings' && init?.method === 'PUT');
      expect(put).toBeTruthy();
    });
  });
  // ── Публикации ────────────────────────────────────────────────────────────

  it('shows the KPI row and the reach of the best post', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    expect(await screen.findByText('+12 за неделю')).toBeInTheDocument();
    expect(screen.getByText('Всего публикаций')).toBeInTheDocument();
    expect(screen.getByText('76.6% от всех')).toBeInTheDocument();
    // 12.4K is also the reach of the top row, so the assertion is scoped to
    // the KPI card rather than matching whichever comes first.
    const bestReach = screen.getByText('Лучший охват').closest('.admin-stat-card') as HTMLElement;
    expect(within(bestReach).getByText('12.4K')).toBeInTheDocument();
  });

  it('shows an unmeasured reach as “—”, never as zero', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    const row = (await screen.findByText('Безопасность платежей')).closest('tr') as HTMLElement;
    expect(within(row).getByText('—')).toBeInTheDocument();
    expect(within(row).getByText('Ожидается')).toBeInTheDocument();
    expect(within(row).queryByText('0')).not.toBeInTheDocument();
  });

  it('asks the backend for the chosen status rather than filtering in the browser', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    await screen.findByText('Обновление платформы VEXA');
    fireEvent.change(screen.getByLabelText('Статус'), { target: { value: 'scheduled' } });

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([path]) =>
        String(path).startsWith('/api/smm/publications?') && String(path).includes('status=scheduled'))).toBe(true);
    });
  });

  it('debounces the search box into one query parameter', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    await screen.findByText('Обновление платформы VEXA');
    fireEvent.change(screen.getByLabelText('Поиск публикаций'), { target: { value: 'безопас' } });

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([path]) =>
        String(path).includes('q=%D0%B1%D0%B5%D0%B7%D0%BE%D0%BF%D0%B0%D1%81'))).toBe(true);
    });
  });

  it('pages through the register on the backend', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    await screen.findByText('Обновление платформы VEXA');
    fireEvent.click(screen.getByRole('button', { name: 'Страница 2' }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([path]) =>
        String(path).startsWith('/api/smm/publications?') && String(path).includes('page=2'))).toBe(true);
    });
  });

  it('publishes a post through the backend instead of flipping the row itself', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    await screen.findByText('Безопасность платежей');
    fireEvent.click(screen.getByRole('button', { name: 'Ещё действия: Безопасность платежей' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: /Отметить опубликованной/ }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([path, init]) =>
        path === '/api/smm/publications/post-2/publish' && init?.method === 'POST')).toBe(true);
    });
  });

  // ── Дизайнер ──────────────────────────────────────────────────────────────

  it('creates a publication from the designer', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Создать публикацию/ }));

    fireEvent.change(await screen.findByLabelText('Название'), { target: { value: 'Итоги недели' } });
    fireEvent.change(screen.getByLabelText('Текст публикации'), { target: { value: 'Главные новости.' } });
    fireEvent.change(screen.getByLabelText('Хештеги'), { target: { value: '#новости, обновление' } });
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([path, init]) =>
        path === '/api/smm/publications' && init?.method === 'POST');
      expect(post).toBeTruthy();
      expect(JSON.parse(post![1]!.body as string)).toMatchObject({
        title: 'Итоги недели',
        platform: 'telegram',
        status: 'draft',
        delivery_mode: 'manual',
        tags: ['новости', 'обновление'],
      });
    });
  });

  it('refuses to save a publication without a title', async () => {
    const { fetchMock } = stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Создать публикацию/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Сохранить' }));

    expect(await screen.findByText('Укажите название публикации')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([path, init]) =>
      path === '/api/smm/publications' && init?.method === 'POST')).toBe(false);
  });

  it('says plainly when a platform cannot be posted to by the backend', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Создать публикацию/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Twitter / X' }));

    expect(await screen.findByText(/не публикуется бэкендом/)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Автоматически/ })).toBeDisabled();
  });

  it('counts the composed text against the platform limit', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    fireEvent.click(await screen.findByRole('button', { name: /Создать публикацию/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Twitter / X' }));
    fireEvent.change(screen.getByLabelText('Текст публикации'), { target: { value: 'a'.repeat(300) } });

    expect(await screen.findByText(/300 \/ 280/)).toBeInTheDocument();
    expect(screen.getByText(/превышение на 20/)).toBeInTheDocument();
  });

  it('loads an existing post into the designer from the table', async () => {
    stubFetch();
    render(<SMMFactoryTab agents={agents} />);
    await screen.findByText('Обновление платформы VEXA');
    fireEvent.click(screen.getByRole('button', { name: 'Редактировать: Обновление платформы VEXA' }));

    expect(await screen.findByDisplayValue('Обновление платформы VEXA')).toBeInTheDocument();
    expect(screen.getByDisplayValue('обновление')).toBeInTheDocument();
    expect(screen.getByText('Редактирование публикации')).toBeInTheDocument();
  });
});
