import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { SimpleChatView } from './SimpleChatView';
import type { ChatMessage } from '../types';

/**
 * The unified chat: one conversation that reaches every agent — bound in the
 * navbar, @mentioned per message, seen in the agents panel — plus the
 * per-chat settings, branching, queue and export around it.
 */

const agents = [
  { id: 'agent_coder', name: 'Coder', role: 'dev', status: 'working', current_task: 'Ревью PR', progress: 40, is_enabled: true },
  { id: 'agent_smm', name: 'SMM', role: 'posts', status: 'idle', is_enabled: true },
];

type FetchCall = { url: string; init?: RequestInit };
let calls: FetchCall[] = [];

function stubFetch(settings: Record<string, unknown> = {}) {
  calls = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(((url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    const path = String(url);
    let body: unknown = {};
    if (path === '/api/subagents') body = agents;
    else if (path.endsWith('/settings')) body = { system_prompt: '', temperature: null, top_p: null, max_tokens: null, think: null, ephemeral: false, uncensored: false, ...settings };
    else if (path.endsWith('/fork')) body = { status: 'success', new_session_id: 'chat_a_fork_1' };
    else if (path.startsWith('/api/chat/models')) body = { local: [], router: [], default: { id: '', source: 'local', name: '' } };
    return Promise.resolve({ ok: true, json: async () => body } as Response);
  }) as typeof fetch);
}

const baseProps = {
  language: 'ru' as const,
  setInputValue: vi.fn(),
  isGenerating: false,
  onStopGeneration: vi.fn(),
  handleSendMessage: vi.fn(),
  isConnected: true,
  micState: 'off' as const,
  onVoiceToggle: vi.fn(),
  chatSessions: [
    { id: 'dashboard', title: 'Main' },
    { id: 'chat_a', title: 'Работа', agent_id: 'agent_coder' },
  ],
  currentChatId: 'chat_a',
  selectChat: vi.fn(),
  handleCreateNewSession: vi.fn(),
  getSessionLabel: (id: string) => id,
  fetchChatSessions: vi.fn(),
  mainChatEndRef: React.createRef<HTMLDivElement>(),
  attachedFile: null,
  setAttachedFile: vi.fn(),
  handleChatFileAttach: vi.fn(),
  isUploading: false,
  onSwitchToImmersive: vi.fn(),
  isTTSEnabled: false,
  setIsTTSEnabled: vi.fn(),
  isSpeaking: false,
  setIsSpeaking: vi.fn(),
};

function renderChat(overrides: Record<string, unknown> = {}, messages: ChatMessage[] = []) {
  return render(<SimpleChatView {...baseProps} inputValue="" messages={messages} {...overrides} />);
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  stubFetch();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('agents in the chat', () => {
  it("names the chat's agent in the navbar and binds another one", async () => {
    const onSetSessionAgent = vi.fn();
    renderChat({ onSetSessionAgent });
    const trigger = await screen.findByRole('button', { name: 'Агент этого чата: Coder' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: /SMM/ }));
    expect(onSetSessionAgent).toHaveBeenCalledWith('chat_a', 'agent_smm');
  });

  it('labels the session row with its agent', async () => {
    renderChat();
    expect(await screen.findByText('@Coder')).toBeInTheDocument();
  });

  it('sends an @mention to that agent with the mention stripped', async () => {
    const handleSendMessage = vi.fn();
    renderChat({ handleSendMessage, inputValue: '@SMM пост на завтра' });
    await screen.findByText(/агент ответит в этом чате/);
    fireEvent.submit(screen.getByRole('textbox', { name: /Спросите/ }).closest('form') as HTMLFormElement);
    expect(handleSendMessage).toHaveBeenCalledWith(expect.anything(), { agentId: 'agent_smm', text: 'пост на завтра' });
  });

  it('offers agents while a handle is typed and completes on Enter', async () => {
    const setInputValue = vi.fn();
    renderChat({ setInputValue, inputValue: '@sm' });
    const option = await screen.findByRole('option', { name: /SMM/ });
    expect(option).toHaveClass('is-active');
    fireEvent.keyDown(screen.getByRole('textbox', { name: /Спросите/ }), { key: 'Enter' });
    expect(setInputValue).toHaveBeenCalledWith('@SMM ');
  });

  it('labels a reply that came from a mentioned agent', () => {
    renderChat({}, [
      { role: 'user', content: '@SMM пост' },
      { role: 'assistant', content: 'Вот пост', meta: { agent_id: 'agent_smm', agent_name: 'SMM' } },
    ]);
    expect(screen.getByTitle('через @-упоминание')).toHaveTextContent('SMM');
  });

  it('lists agents with status and opens a chat bound to one', async () => {
    renderChat();
    fireEvent.click(screen.getByRole('button', { name: 'Агенты' }));
    const card = await screen.findByRole('article', { name: 'Coder' });
    expect(card).toHaveTextContent('Ревью PR');
    expect(card).toHaveTextContent('Работает');
    fireEvent.click(screen.getAllByRole('button', { name: /Новый чат/ }).find(b => card.contains(b)) as HTMLElement);
    await waitFor(() => expect(baseProps.selectChat).toHaveBeenCalled());
    const bind = calls.find(c => /\/api\/history\/chat_\d+\/agent$/.test(c.url));
    expect(JSON.parse(String(bind?.init?.body))).toEqual({ agent_id: 'agent_coder' });
  });
});

describe('queue', () => {
  it('holds a message written during a reply and sends it once the reply ends', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onSendText = vi.fn(() => true);
    const setInputValue = vi.fn();
    const { rerender } = render(
      <SimpleChatView {...baseProps} messages={[]} inputValue="следующий вопрос" setInputValue={setInputValue}
        isGenerating onSendText={onSendText} />,
    );
    fireEvent.submit(screen.getByRole('textbox', { name: /Спросите/ }).closest('form') as HTMLFormElement);
    expect(setInputValue).toHaveBeenCalledWith('');
    expect(screen.getByTitle('В очереди')).toHaveTextContent('следующий вопрос');
    expect(onSendText).not.toHaveBeenCalled();

    rerender(
      <SimpleChatView {...baseProps} messages={[]} inputValue="" setInputValue={setInputValue}
        isGenerating={false} onSendText={onSendText} />,
    );
    await act(async () => { vi.advanceTimersByTime(500); });
    expect(onSendText).toHaveBeenCalledWith('следующий вопрос', { agentId: null });
    expect(screen.queryByTitle('В очереди')).not.toBeInTheDocument();
  });
});

describe('branches, settings and export', () => {
  it('branches the chat from a message', async () => {
    renderChat({}, [
      { id: 11, role: 'user', content: 'q1' },
      { id: 12, role: 'assistant', content: 'a1' },
    ]);
    fireEvent.click(screen.getAllByRole('button', { name: 'Ветка отсюда' })[0]);
    await waitFor(() => expect(baseProps.selectChat).toHaveBeenCalledWith('chat_a_fork_1'));
    const fork = calls.find(c => c.url === '/api/history/chat_a/fork');
    expect(JSON.parse(String(fork?.init?.body))).toEqual({ up_to_message_id: 11, title: 'Ветка: Работа' });
  });

  it('saves per-chat settings, leaving empty fields as defaults', async () => {
    renderChat();
    fireEvent.click(screen.getByRole('button', { name: 'Настройки чата' }));
    fireEvent.change(screen.getByLabelText(/Инструкции для этого чата/), { target: { value: 'Только по-английски' } });
    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '0.2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(calls.some(c => c.init?.method === 'PUT')).toBe(true));
    const put = calls.find(c => c.init?.method === 'PUT');
    expect(put?.url).toBe('/api/history/chat_a/settings');
    expect(JSON.parse(String(put?.init?.body))).toMatchObject({
      system_prompt: 'Только по-английски', temperature: 0.2, top_p: null, max_tokens: null, think: null, ephemeral: false,
    });
  });

  it('marks an incognito chat and offers no branching in it', async () => {
    stubFetch({ ephemeral: true });
    renderChat({}, [{ id: 11, role: 'user', content: 'q1' }]);
    expect(await screen.findByText('Инкогнито: чат не сохраняется и удалится при выходе')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Ветка отсюда' })).not.toBeInTheDocument();
  });

  it('exports the conversation as Markdown', () => {
    const createObjectURL = vi.fn(() => 'blob:x');
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    renderChat({}, [{ role: 'user', content: 'Привет' }, { role: 'assistant', content: 'Здравствуйте' }]);
    fireEvent.click(screen.getByRole('button', { name: 'Экспорт чата' }));
    fireEvent.click(screen.getByRole('button', { name: 'Markdown (.md)' }));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });
});
