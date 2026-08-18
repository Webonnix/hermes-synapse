import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
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
    expect(screen.getByText(/30\.0 ток\/с/)).toBeInTheDocument();
  });

  it('falls back to total latency when generation time is missing', () => {
    renderChat([
      { role: 'assistant', content: 'Ок.', meta: { output_tokens: 100, latency_ms: 5000 } },
    ]);

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

  it('says nothing when the run reported no tokens', () => {
    renderChat([{ role: 'assistant', content: 'Привет', meta: { latency_ms: 1200 } }]);
    expect(screen.queryByText(/ток\/с/)).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: 'Подробности генерации' }));

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
    expect(screen.getByText(/25\.0 tok\/s/)).toBeInTheDocument();
  });
});
