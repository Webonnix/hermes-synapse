/**
 * The external-model half of the Integrations screen.
 *
 * Two behaviours are worth pinning because both were unreachable before and
 * both fail silently rather than loudly:
 *
 * - a text node uses the *default* LLM provider, and until this screen could
 *   set it, that default was always the local model whatever was connected;
 * - an active endpoint with no model configured accepts work and then fails at
 *   the node, so the screen has to say so before a run does.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProvidersPanel } from './ProvidersPanel';
import type { CFLLMEndpoints } from './types';

const llm = (overrides: Partial<CFLLMEndpoints> = {}): CFLLMEndpoints => ({
  local: {
    id: 'local-llm', name: 'local-llm', api_base: 'http://hermes-llama:8080',
    default_model: 'qwen3:8b', status: 'active', local: true,
  },
  bindings: [{
    id: 'prov-omr', name: 'omniroute', api_base: 'http://omniroute:20128/v1',
    default_model: 'ag/gemini-3.7-flash-high', status: 'active', local: false,
  }],
  default: 'local-llm',
  ...overrides,
});

const noop = async () => {};
const props = {
  providers: [],
  language: 'ru' as const,
  onSave: noop,
  onDelete: noop,
  onTest: async () => ({}),
  onCreateLlm: async () => ({}),
  onDeleteLlm: noop,
};

describe('ProvidersPanel — external language models', () => {
  it('marks the local model as the default when none is stored', () => {
    render(<ProvidersPanel {...props} llm={llm({ default: '' })}
      onSetDefault={noop} onUpdateLlm={noop} onLoadLlmModels={async () => []} />);
    expect(screen.getByTitle('По умолчанию для текстовых нод')).toBeInTheDocument();
  });

  it('can point the default at an external provider', async () => {
    const onSetDefault = vi.fn(async () => {});
    render(<ProvidersPanel {...props} llm={llm()}
      onSetDefault={onSetDefault} onUpdateLlm={noop} onLoadLlmModels={async () => []} />);

    fireEvent.click(screen.getByRole('button', { name: /По умолчанию/ }));
    await waitFor(() => expect(onSetDefault).toHaveBeenCalledWith('LLM', 'prov-omr'));
  });

  it('does not offer a provider still awaiting approval as the default', () => {
    render(<ProvidersPanel {...props}
      llm={llm({ bindings: [{ ...llm().bindings[0], status: 'awaiting_approval' }] })}
      onSetDefault={noop} onUpdateLlm={noop} onLoadLlmModels={async () => []} />);
    expect(screen.queryByRole('button', { name: /По умолчанию/ })).not.toBeInTheDocument();
  });

  it('warns when an active provider has no model — the node would fail, not the screen', () => {
    render(<ProvidersPanel {...props}
      llm={llm({ bindings: [{ ...llm().bindings[0], default_model: '' }] })}
      onSetDefault={noop} onUpdateLlm={noop} onLoadLlmModels={async () => []} />);
    expect(screen.getByText(/не выбрана модель/)).toBeInTheDocument();
  });

  it('turns the model field into a list once the endpoint answers', async () => {
    const onLoadLlmModels = vi.fn(async () => ['ag/gemini-pro-agent', 'ds/deepseek-chat']);
    render(<ProvidersPanel {...props} llm={llm()}
      onSetDefault={noop} onUpdateLlm={noop} onLoadLlmModels={onLoadLlmModels} />);

    fireEvent.click(screen.getByRole('button', { name: 'Список моделей' }));
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());
    expect(screen.getByRole('option', { name: 'ds/deepseek-chat' })).toBeInTheDocument();
  });

  it('stays a text field when the endpoint cannot list its models', async () => {
    render(<ProvidersPanel {...props} llm={llm()}
      onSetDefault={noop} onUpdateLlm={noop} onLoadLlmModels={async () => []} />);

    fireEvent.click(screen.getByRole('button', { name: 'Список моделей' }));
    await waitFor(() => expect(screen.getByText(/впишите имя модели вручную/)).toBeInTheDocument());
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('saves a changed model, and only once it actually changed', async () => {
    const onUpdateLlm = vi.fn(async () => {});
    render(<ProvidersPanel {...props} llm={llm()}
      onSetDefault={noop} onUpdateLlm={onUpdateLlm} onLoadLlmModels={async () => []} />);

    const save = screen.getByRole('button', { name: 'Сохранить' });
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByDisplayValue('ag/gemini-3.7-flash-high'),
      { target: { value: 'ag/gemini-pro-agent' } });
    fireEvent.click(save);
    await waitFor(() => expect(onUpdateLlm).toHaveBeenCalledWith('prov-omr', 'ag/gemini-pro-agent'));
  });
});
