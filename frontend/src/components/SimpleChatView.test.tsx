import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import { SimpleChatView } from './SimpleChatView';
import type { ChatMessage } from '../types';

const baseProps = {
  language: 'ru' as const,
  inputValue: '',
  setInputValue: vi.fn(),
  isGenerating: false,
  onStopGeneration: vi.fn(),
  handleSendMessage: vi.fn(),
  isConnected: true,
  micState: 'off' as const,
  onVoiceToggle: vi.fn(),
  chatSessions: [{ id: 'dashboard', title: 'Main' }],
  currentChatId: 'dashboard',
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

const renderChat = (messages: ChatMessage[], language: 'ru' | 'en' = 'ru') =>
  render(<SimpleChatView {...baseProps} language={language} messages={messages} />);

const openStats = (name = 'Подробности генерации') =>
  fireEvent.click(screen.getByRole('button', { name }));

describe('SimpleChatView token speed', () => {
  it('rates by decode time and reports model wait and tool time separately', () => {
    // 900 tokens decoded in 30s, inside 40s of model time (10s of it prompt
    // ingestion) and a 90s turn that also ran tools. The rate is the model's
    // real 30 tok/s — not 22.5 (÷ generation) and not 10 (÷ the whole turn).
    renderChat([
      {
        role: 'assistant',
        content: 'Готово.',
        meta: {
          output_tokens: 900,
          decode_ms: 30000,
          generation_ms: 40000,
          latency_ms: 90000,
          tool_iterations: 2,
        },
      },
    ]);

    // Nothing but the button until it is clicked — the reply stays uncluttered.
    expect(screen.queryByText(/ток\/с/)).not.toBeInTheDocument();
    openStats();
    expect(screen.getByText(/30\.0 ток\/с/)).toBeInTheDocument();
    expect(screen.getByText(/900 токенов/)).toBeInTheDocument();
    // Long durations lose the decimal: "40 с", not "40.0 с".
    expect(screen.getByText(/40 с генерация/)).toBeInTheDocument();
    expect(screen.getByText(/90 с включая инструменты/)).toBeInTheDocument();
  });

  it('falls back to generation time when the provider reports no decode time', () => {
    renderChat([
      {
        role: 'assistant',
        content: 'Готово.',
        meta: { output_tokens: 900, generation_ms: 30000, latency_ms: 90000 },
      },
    ]);
    openStats();
    expect(screen.getByText(/30\.0 ток\/с/)).toBeInTheDocument();
  });

  it('falls back to total latency when generation time is missing', () => {
    renderChat([
      { role: 'assistant', content: 'Ок.', meta: { output_tokens: 100, latency_ms: 5000 } },
    ]);

    openStats();
    expect(screen.getByText(/20\.0 ток\/с/)).toBeInTheDocument();
  });

  it('shows an approximate live rate while the reply streams', () => {
    vi.useFakeTimers();
    const now = Date.now();
    vi.setSystemTime(now);
    const streaming: ChatMessage = {
      role: 'assistant',
      content: '',
      run_id: 'run-1',
      streaming: true,
    };
    const { rerender } = renderChat([{ ...streaming, content: 'x'.repeat(70) }]);

    vi.setSystemTime(now + 2000);
    rerender(
      <SimpleChatView {...baseProps} messages={[{ ...streaming, content: 'x'.repeat(700) }]} />
    );

    // 700 chars ≈ 200 tokens over 2s — shown rounded and marked approximate.
    expect(screen.getByText(/≈ 100 ток\/с/)).toBeInTheDocument();
    vi.useRealTimers();
  });

  it('offers no stats button when the run reported no tokens', () => {
    renderChat([{ role: 'assistant', content: 'Привет', meta: { latency_ms: 1200 } }]);
    expect(screen.queryByRole('button', { name: 'Подробности генерации' })).not.toBeInTheDocument();
  });

  it('closes the stats popover on Escape and on a click outside', () => {
    renderChat([{ role: 'assistant', content: 'Ок.', meta: { output_tokens: 100, decode_ms: 5000, latency_ms: 6000 } }]);
    openStats();
    expect(screen.getByRole('dialog', { name: 'Подробности генерации' })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    openStats();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens an Open WebUI style breakdown from the info button', () => {
    renderChat([
      {
        role: 'assistant',
        content: 'Готово.',
        meta: {
          output_tokens: 200,
          input_tokens: 3314,
          decode_ms: 6439,
          prompt_ms: 2716,
          generation_ms: 9200,
          latency_ms: 9220,
          model: 'qwen-quality:latest',
        },
      },
    ]);

    expect(screen.queryByText('Скорость чтения промпта')).not.toBeInTheDocument();
    openStats();

    expect(screen.getByText('Скорость ответа').nextSibling).toHaveTextContent('31.06 ток/с');
    // 3314 prompt tokens in 2716 ms.
    expect(screen.getByText('Скорость чтения промпта').nextSibling).toHaveTextContent('1220 ток/с');
    expect(screen.getByText('Токенов в промпте').nextSibling).toHaveTextContent('3 314');
    expect(screen.getByText('Модель').nextSibling).toHaveTextContent('qwen-quality:latest');
  });

  it('uses English units in English', () => {
    renderChat(
      [{ role: 'assistant', content: 'Done.', meta: { output_tokens: 50, decode_ms: 2000, generation_ms: 2600 } }],
      'en',
    );
    openStats('Generation details');
    expect(screen.getByText(/25\.0 tok\/s/)).toBeInTheDocument();
  });
});

describe('SimpleChatView project folders', () => {
  const projects = [
    { id: 'p1', name: 'Клиенты', description: '', is_active: true, created_at: '', updated_at: '', parent_id: null },
    { id: 'p2', name: 'Отчёты', description: '', is_active: true, created_at: '', updated_at: '', parent_id: 'p1' },
  ];
  const sessions = [
    { id: 'dashboard', title: 'Main' },
    { id: 'chat_1', title: 'Договор', project_id: 'p1' },
    { id: 'chat_2', title: 'Итоги', project_id: 'p2' },
    { id: 'chat_3', title: 'Без папки чат' },
  ];

  const renderSidebar = (overrides: Record<string, unknown> = {}) =>
    render(
      <SimpleChatView
        {...baseProps}
        messages={[]}
        chatSessions={sessions}
        projects={projects}
        {...overrides}
      />,
    );

  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('shows the folder tree with each chat under its own folder', () => {
    renderSidebar();

    expect(screen.getByText('Клиенты')).toBeInTheDocument();
    // A sub-folder and its chat are visible too — folders start expanded.
    expect(screen.getByText('Отчёты')).toBeInTheDocument();
    expect(screen.getByText('Итоги')).toBeInTheDocument();
    // Nesting shows as indentation: chat_2 sits one level deeper than chat_1.
    const nested = screen.getByText('Итоги').closest('.owui-chat-item') as HTMLElement;
    const shallow = screen.getByText('Договор').closest('.owui-chat-item') as HTMLElement;
    expect(parseInt(nested.style.paddingLeft, 10)).toBeGreaterThan(parseInt(shallow.style.paddingLeft, 10));
    // Unfiled chats keep their own section — Open WebUI's date buckets — and
    // are not hidden by folders. Nothing here carries a timestamp, so they all
    // land in the oldest bucket.
    expect(screen.getByText('Ранее')).toBeInTheDocument();
    expect(screen.getByText('Без папки чат')).toBeInTheDocument();
  });

  it('collapses a folder and hides the chats inside it', () => {
    renderSidebar();

    fireEvent.click(screen.getByText('Клиенты'));

    expect(screen.queryByText('Договор')).not.toBeInTheDocument();
    expect(screen.queryByText('Итоги')).not.toBeInTheDocument();
    expect(screen.getByText('Без папки чат')).toBeInTheDocument();
  });

  it('moves a chat into a folder from its row menu', () => {
    const handleSetSessionProject = vi.fn();
    renderSidebar({ handleSetSessionProject });

    fireEvent.mouseEnter(screen.getByText('Без папки чат').closest('.owui-chat-item')!);
    fireEvent.click(screen.getByRole('button', { name: 'Переместить в папку: Без папки чат' }));
    fireEvent.click(screen.getByRole('button', { name: 'Отчёты' }));

    expect(handleSetSessionProject).toHaveBeenCalledWith('chat_3', 'p2');
  });

  it('creates a folder and refreshes the list', async () => {
    const fetchProjects = vi.fn();
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    renderSidebar({ fetchProjects });

    fireEvent.click(screen.getByRole('button', { name: 'Новая папка' }));
    fireEvent.change(screen.getByLabelText('Название папки…'), { target: { value: 'Юристы' } });
    fireEvent.keyDown(screen.getByLabelText('Название папки…'), { key: 'Enter' });

    await waitFor(() => expect(fetchProjects).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith('/api/projects', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ name: 'Юристы', parent_id: null }),
    }));
  });

  it('lists a chat whose folder no longer exists as unfiled instead of dropping it', () => {
    renderSidebar({ chatSessions: [{ id: 'chat_9', title: 'Осиротевший', project_id: 'gone' }] });

    expect(screen.getByText('Осиротевший')).toBeInTheDocument();
  });
});

describe('SimpleChatView model picker', () => {
  const catalog = {
    local: [
      { id: 'qwen-mtp:latest', name: 'qwen-mtp:latest', source: 'local', owned_by: 'llama.cpp', parameter_size: '8.0B', quantization: 'Q4_K_M', size_label: '5.2 GB' },
      { id: 'llama3:70b', name: 'llama3:70b', source: 'local', owned_by: 'llama.cpp', parameter_size: '70B' },
    ],
    router: [
      { id: 'kimi/kimi-k3', name: 'kimi-k3', source: 'router', owned_by: 'kimi', capabilities: { tools: true }, context_length: 200000 },
      { id: 'ds/deepseek-chat', name: 'deepseek-chat', source: 'router', owned_by: 'ds', capabilities: {} },
    ],
    local_error: null,
    router_error: null,
    default: { id: 'qwen-mtp:latest', source: 'local', name: 'qwen-mtp:latest' },
  };

  const mockCatalog = (payload: unknown = catalog) =>
    vi.spyOn(globalThis, 'fetch').mockImplementation(((url: string) =>
      Promise.resolve({
        ok: true,
        json: async () => (String(url).startsWith('/api/chat/models') ? payload : {}),
      } as Response)) as typeof fetch);

  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  const renderPicker = (overrides: Record<string, unknown> = {}) =>
    render(<SimpleChatView {...baseProps} messages={[]} {...overrides} />);

  it('lists local and OmniRoute models in separate provider groups', async () => {
    mockCatalog();
    renderPicker();

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));

    // Local models group under Strata; router models under whoever owns them.
    await screen.findByText('Strata');
    const groups = [...document.querySelectorAll('.owui-model-group-label')]
      .map(el => el.textContent?.replace(/\d+$/, '') || '');
    expect(groups).toEqual(['Strata', 'ds', 'kimi']);
    expect(screen.getByText('kimi-k3')).toBeInTheDocument();
    // Local rows carry the size/quant line straight from the server's /v1/models.
    expect(screen.getByText('8.0B · Q4_K_M · 5.2 GB')).toBeInTheDocument();
  });

  it('filters down to just the external catalogue', async () => {
    mockCatalog();
    renderPicker();

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Внешние/ }));

    expect(screen.getByText('kimi-k3')).toBeInTheDocument();
    expect(screen.queryByText('llama3:70b')).not.toBeInTheDocument();
  });

  it('searches across both sources', async () => {
    mockCatalog();
    renderPicker();

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));
    fireEvent.change(await screen.findByLabelText('Поиск модели'), { target: { value: 'deepseek' } });

    // Only the matching row survives — the trigger keeps showing the model the
    // chat is actually on, so the assertion is scoped to the list itself.
    const rows = [...document.querySelectorAll('.owui-model-row-name')].map(el => el.textContent);
    expect(rows).toEqual(['deepseek-chat']);
  });

  it('reports the picked model as {id, source} so the frame can be resolved server-side', async () => {
    mockCatalog();
    const onModelSelect = vi.fn();
    renderPicker({ onModelSelect });

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));
    fireEvent.click(await screen.findByText('kimi-k3'));

    expect(onModelSelect).toHaveBeenCalledWith({ id: 'kimi/kimi-k3', source: 'router' });
  });

  it('shows why a source is empty instead of pretending it has no models', async () => {
    mockCatalog({
      ...catalog,
      router: [],
      router_error: 'OmniRoute недоступен — запущен ли контейнер «omniroute»?',
    });
    renderPicker();

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));

    expect(await screen.findByText(/OmniRoute недоступен/)).toBeInTheDocument();
  });

  it('keeps working when the catalogue endpoint is unreachable', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));
    renderPicker();

    // The trigger falls back to the "pick a model" prompt; the composer is
    // untouched, so messages still go to the backend's configured default.
    expect(await screen.findByRole('button', { name: /Выбрать модель/ })).toBeInTheDocument();
    expect(screen.getByLabelText('Спросите что-нибудь у Vexa')).toBeInTheDocument();
  });

  it('clears a remembered model the catalogue no longer has', async () => {
    // A selection lives in localStorage forever. A model no longer served locally
    // (or one whose OmniRoute credentials went away) would otherwise keep sitting
    // in the navbar while every turn silently ran on the configured default.
    mockCatalog();
    const onModelSelect = vi.fn();
    renderPicker({ modelSelection: { id: 'ag/gone-4-6', source: 'router' }, onModelSelect });

    await waitFor(() => expect(onModelSelect).toHaveBeenCalledWith(null));
  });

  it('leaves a still-valid selection alone', async () => {
    mockCatalog();
    const onModelSelect = vi.fn();
    renderPicker({ modelSelection: { id: 'kimi/kimi-k3', source: 'router' }, onModelSelect });

    await screen.findByRole('button', { name: /kimi-k3/ });
    expect(onModelSelect).not.toHaveBeenCalled();
  });

  it('does not clear a selection before the catalogue has loaded', async () => {
    // An empty catalogue means "not fetched yet", never "your model is gone".
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));
    const onModelSelect = vi.fn();
    renderPicker({ modelSelection: { id: 'kimi/kimi-k3', source: 'router' }, onModelSelect });

    await screen.findByRole('button', { name: /kimi\/kimi-k3/ });
    expect(onModelSelect).not.toHaveBeenCalled();
  });

  it('marks the local model that is loaded in VRAM', async () => {
    mockCatalog({
      ...catalog,
      local: [{ ...catalog.local[0], loaded: true }, catalog.local[1]],
    });
    renderPicker();

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));

    const badges = [...document.querySelectorAll('.owui-badge')].map(el => el.textContent);
    expect(badges).toContain('в памяти');
  });

  it('says which tags were withheld rather than dropping them silently', async () => {
    mockCatalog({
      ...catalog,
      local_hidden: [{ id: 'qwen-mtp:latest.bak-20260905', reason: 'backup' }],
    });
    renderPicker();

    fireEvent.click(await screen.findByRole('button', { name: /qwen-mtp:latest/ }));

    expect(await screen.findByText(/qwen-mtp:latest\.bak-20260905 \(резервная копия\)/)).toBeInTheDocument();
  });
});
