import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VoiceProvidersPanel } from './VoiceProvidersPanel';
import { ClientConversationsPane } from '../../clients/ClientConversationsPane';
import { OPEN_INBOX_EVENT, OPEN_INBOX_KEY } from './inboxApi';

const kinds = {
  openai_compatible: { label: 'OpenAI-совместимый', capabilities: ['tts', 'stt', 'voices'] },
  voice_agent_http: { label: 'Voice agent', capabilities: ['voice_agent', 'voices'] },
};

const savedProvider = {
  id: 'vp-1', name: 'OpenAI', kind: 'openai_compatible', kind_label: 'OpenAI-совместимый', base_url: 'https://api.openai.com/v1',
  enabled: true, capabilities: ['tts', 'stt', 'voices'], config: {}, timeout_seconds: 20, max_retries: 1,
  last_health: {}, has_secret: true, has_webhook_secret: false, webhook_path: '/api/voice/webhook/vp-1',
  created_at: '', updated_at: '',
};

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('VoiceProvidersPanel', () => {
  it('sends the key once and never shows it again', async () => {
    let providers: unknown[] = [];
    const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const method = init?.method || 'GET';
      const ok = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data });
      if (path === '/api/voice/providers' && method === 'GET') return ok({ kinds, failure_policies: [], items: providers });
      if (path === '/api/voice/providers' && method === 'POST') {
        providers = [savedProvider];
        return ok(savedProvider);
      }
      if (path === '/api/voice/targets') return ok([]);
      if (path === '/api/voice/providers/vp-1/voices') return ok({ detail: 'provider down' }, 502);
      return ok({});
    });
    vi.stubGlobal('fetch', handler);
    render(<VoiceProvidersPanel />);

    fireEvent.change(await screen.findByLabelText('Название'), { target: { value: 'OpenAI' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://api.openai.com/v1' } });
    fireEvent.change(screen.getByLabelText('API-ключ'), { target: { value: 'sk-very-secret' } });
    fireEvent.click(screen.getByRole('button', { name: /Добавить провайдера/ }));

    expect(await screen.findByText('сохранён')).toBeInTheDocument();
    const post = handler.mock.calls.find(([url, init]) => String(url) === '/api/voice/providers' && (init as RequestInit)?.method === 'POST');
    expect(JSON.parse(String((post![1] as RequestInit).body)).api_key).toBe('sk-very-secret');
    expect((screen.getByLabelText('API-ключ') as HTMLInputElement).value).toBe('');
    expect(document.body.textContent).not.toContain('sk-very-secret');

    fireEvent.click(screen.getByRole('button', { name: /Голоса/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('provider down');
    expect(screen.getByText('OpenAI', { selector: 'strong' })).toBeInTheDocument();
  });
});

describe('ClientConversationsPane', () => {
  it('lists the client conversations and hands one to the inbox', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => [{
        id: 'ibx-9', platform: 'whatsapp', chat_id: '+7900', display_name: 'Ольга', handling: 'human',
        unread_count: 2, last_message_at: '2026-09-14T10:00:00Z', last_message_preview: 'Жду ответа',
      }],
    })));
    const opened = vi.fn();
    window.addEventListener(OPEN_INBOX_EVENT, opened);
    render(<ClientConversationsPane clientId="cli-1" />);

    const row = (await screen.findByText(/Жду ответа/)).closest('.admin-item-row') as HTMLElement;
    expect(within(row).getByText('2 новых')).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Открыть' }));

    await waitFor(() => expect(opened).toHaveBeenCalled());
    expect(localStorage.getItem(OPEN_INBOX_KEY)).toBe('ibx-9');
    window.removeEventListener(OPEN_INBOX_EVENT, opened);
  });

  it('a role without inbox access sees a refusal, not an empty state', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ detail: 'нет' }) })));
    render(<ClientConversationsPane clientId="cli-1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Нет доступа');
  });
});
