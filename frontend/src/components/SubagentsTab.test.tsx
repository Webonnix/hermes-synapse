import { createRef } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SubagentsTab } from './SubagentsTab';
import type { SystemConfig } from '../types';

const localModels = [
  { id: 'qwen-mtp:latest', name: 'qwen-mtp:latest' },
  { id: 'qwen-quality:latest', name: 'qwen-quality:latest' },
];

const catalog = {
  available: true,
  error: null,
  models: [
    { id: 'ag/gemini-3.7-flash-high', owned_by: 'ag' },
    { id: 'ag/gemini-pro-agent', owned_by: 'ag' },
    { id: 'ds/deepseek-chat', owned_by: 'ds' },
  ],
};

function stubFetch(routerModels: unknown = catalog) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === '/api/skills') return { ok: true, json: async () => ({}) };
    if (path === '/api/router/models') {
      if (routerModels === 'network-error') throw new Error('network error');
      return { ok: true, json: async () => routerModels };
    }
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** The tab takes ~40 props; only the model-picker ones matter here. */
function renderTab(overrides: Record<string, unknown> = {}) {
  const noop = () => {};
  const props = {
    currentChatId: '__create__',
    subagents: [], messages: [], inputValue: '', setInputValue: noop,
    isSpeaking: false, setIsSpeaking: noop, isGenerating: false,
    playingMsgIndex: null, setPlayingMsgIndex: noop,
    config: {} as SystemConfig, isConnected: true,
    newAgentId: '', setNewAgentId: noop,
    newAgentName: '', setNewAgentName: noop,
    newAgentPrompt: '', setNewAgentPrompt: noop,
    newAgentModel: '', setNewAgentModel: noop,
    newAgentSkills: '', setNewAgentSkills: noop,
    newAgentTemperature: 0.5, setNewAgentTemperature: noop,
    isCreatingAgent: false,
    editingAgentId: '', setEditingAgentId: noop,
    editAgentName: '', setEditAgentName: noop,
    editAgentPrompt: '', setEditAgentPrompt: noop,
    editAgentModel: '', setEditAgentModel: noop,
    editAgentSkills: '', setEditAgentSkills: noop,
    editAgentTemperature: 0.5, setEditAgentTemperature: noop,
    isUpdatingAgent: false,
    speakText: noop, handleSendMessage: noop, selectChat: noop,
    handleCreateSubagent: noop, handleUpdateSubagent: noop, handleDeleteSubagent: noop,
    setCurrentChatId: noop,
    subagentChatEndRef: createRef<HTMLDivElement>(),
    models: localModels,
    ...overrides,
  };
  return render(<SubagentsTab {...(props as any)} />);
}

const modelSelect = () => screen.getByRole('combobox');
const optionLabels = () =>
  Array.from(modelSelect().querySelectorAll('option')).map(o => o.getAttribute('value'));
const groupLabels = () =>
  Array.from(modelSelect().querySelectorAll('optgroup')).map(g => g.getAttribute('label'));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SubagentsTab model picker', () => {
  it('offers the live local models and the 9Router catalog, grouped by source', async () => {
    stubFetch();
    renderTab();

    await waitFor(() => expect(groupLabels()).toContain('9Router · ag'));
    expect(groupLabels()).toEqual([
      'llama.cpp (локальная)', '9Router · ag', '9Router · ds',
    ]);
    expect(optionLabels()).toEqual([
      '', 'qwen-mtp:latest', 'qwen-quality:latest',
      'ag/gemini-3.7-flash-high', 'ag/gemini-pro-agent', 'ds/deepseek-chat',
      '__custom__',
    ]);
  });

  it('carries none of the retired hardcoded models', async () => {
    stubFetch();
    renderTab();

    await waitFor(() => expect(groupLabels()).toContain('9Router · ag'));
    for (const stale of ['qwen3:8b', 'google/gemini-2.5-pro', 'openai/gpt-4o', 'deepseek/deepseek-r1']) {
      expect(optionLabels()).not.toContain(stale);
    }
  });

  it('keeps a configured router model the catalog dropped visible and labelled', async () => {
    stubFetch();
    renderTab({ newAgentModel: 'anthropic/claude-opus-4' });

    await waitFor(() => expect(groupLabels()).toContain('настроено сейчас (нет в каталоге)'));
    expect(optionLabels()).toContain('anthropic/claude-opus-4');
    expect((modelSelect() as HTMLSelectElement).value).toBe('anthropic/claude-opus-4');
  });

  it('labels a configured model the local server no longer serves', async () => {
    stubFetch();
    renderTab({ newAgentModel: 'qwen3:8b' });

    await waitFor(() => expect(groupLabels()).toContain('настроено сейчас (нет на локальном сервере)'));
    expect((modelSelect() as HTMLSelectElement).value).toBe('qwen3:8b');
  });

  it('does not brand a model as missing while both catalogs are still empty', async () => {
    stubFetch('network-error');
    renderTab({ models: [], newAgentModel: 'ag/gemini-pro-agent' });

    await waitFor(() => expect(screen.queryByText('Загрузка каталога 9Router…')).toBeNull());
    expect(groupLabels()).toEqual([]);
    expect(optionLabels()).toEqual(['ag/gemini-pro-agent', '__custom__']);
  });

  it('keeps the custom escape hatch, prefilled with the current model', async () => {
    stubFetch();
    const setNewAgentModel = vi.fn();
    renderTab({ newAgentModel: 'ds/deepseek-chat', setNewAgentModel });

    await waitFor(() => expect(groupLabels()).toContain('9Router · ds'));
    fireEvent.change(modelSelect(), { target: { value: '__custom__' } });

    const input = screen.getByPlaceholderText(/ag\/gemini-3.7-flash-high/) as HTMLInputElement;
    expect(input.value).toBe('ds/deepseek-chat');
    // Selecting the escape hatch must not rewrite the agent's model on its own.
    expect(setNewAgentModel).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'ag/some-unlisted-model' } });
    expect(setNewAgentModel).toHaveBeenCalledWith('ag/some-unlisted-model');
  });
});
