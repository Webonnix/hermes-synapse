import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentModel, ChatMessage } from '../types';
import { VexaCommandCenter } from './VexaCommandCenter';

const agents: AgentModel[] = [
  {
    id: 'research',
    name: 'Research Agent',
    system_prompt: '',
    model: 'qwen3',
    status: 'working',
    current_task: 'Проверяет источники',
  },
  {
    id: 'reviewer',
    name: 'Review Agent',
    system_prompt: '',
    model: 'qwen3',
    status: 'idle',
  },
];

const messages: ChatMessage[] = [
  { role: 'user', content: 'Проверь состояние проекта' },
  { role: 'assistant', content: '**Проверка завершена.** Ошибок нет.', id: 2 },
];

describe('VexaCommandCenter', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ enabled: false, available: false, browser_fallback: true }),
    }));
  });

  it('shows live orchestration state and sends typed commands', async () => {
    const onCommand = vi.fn().mockReturnValue(true);
    // The energy-core scene loads via a lazy import (Suspense); await act() flushes that
    // microtask so the resulting state update happens inside React's test harness.
    await act(async () => {
      render(
        <VexaCommandCenter
          agents={agents}
          messages={messages}
          isConnected
          isGenerating={false}
          isSpeaking={false}
          micState="off"
          onVoiceToggle={vi.fn()}
          onCommand={onCommand}
          onStop={vi.fn()}
          language="ru"
          micStreamRef={{ current: null }}
          ttsAudioElRef={{ current: null }}
          onOpenAgentChat={vi.fn()}
          chatSessions={[]}
          currentChatId="dashboard"
          getSessionLabel={(id) => id}
          onCreateSession={vi.fn()}
        />,
      );
    });

    expect(screen.getByText('Готова к команде')).toBeInTheDocument();
    expect(screen.getByText('Проверка завершена. Ошибок нет.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Скажите или напишите задачу для Vexa'), {
      target: { value: 'Запусти тесты' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Передать команду' }));
    expect(onCommand).toHaveBeenCalledWith('Запусти тесты');
  });

  it('starts voice capture and exposes active agents', async () => {
    const onVoiceToggle = vi.fn();
    await act(async () => {
      render(
        <VexaCommandCenter
          agents={agents}
          messages={messages}
          isConnected
          isGenerating={false}
          isSpeaking={false}
          micState="off"
          onVoiceToggle={onVoiceToggle}
          onCommand={vi.fn().mockReturnValue(true)}
          onStop={vi.fn()}
          language="ru"
          micStreamRef={{ current: null }}
          ttsAudioElRef={{ current: null }}
          onOpenAgentChat={vi.fn()}
          chatSessions={[]}
          currentChatId="dashboard"
          getSessionLabel={(id) => id}
          onCreateSession={vi.fn()}
        />,
      );
    });

    fireEvent.click(screen.getByRole('button', { name: 'Начать голосовую команду' }));
    expect(onVoiceToggle).toHaveBeenCalledOnce();
    expect(screen.getByText('Research Agent')).toBeInTheDocument();
  });

  it('keeps dialog mode listening after a turn nobody was heard in', async () => {
    const onVoiceToggle = vi.fn();
    const props = {
      agents,
      messages,
      isConnected: true,
      isGenerating: false,
      isSpeaking: false,
      micState: 'off' as const,
      onVoiceToggle,
      onCommand: vi.fn().mockReturnValue(true),
      onStop: vi.fn(),
      language: 'ru' as const,
      micStreamRef: { current: null },
      ttsAudioElRef: { current: null },
      onOpenAgentChat: vi.fn(),
      chatSessions: [],
      currentChatId: 'dashboard',
      getSessionLabel: (id: string) => id,
      onCreateSession: vi.fn(),
    };
    // Dialog mode is only offered in a secure context; jsdom is not one by default.
    vi.stubGlobal('isSecureContext', true);
    let rerender: (ui: React.ReactElement) => void = () => {};
    await act(async () => {
      ({ rerender } = render(<VexaCommandCenter {...props} voiceIdleTick={0} />));
    });

    // Arming dialog mode starts the first listening turn itself.
    fireEvent.click(screen.getByRole('button', { name: /Режим диалога/ }));
    expect(onVoiceToggle).toHaveBeenCalledTimes(1);

    // That turn ends with nothing recognised: no answer arrives, so only the idle
    // tick can restart the microphone.
    vi.useFakeTimers();
    try {
      act(() => {
        rerender(<VexaCommandCenter {...props} voiceIdleTick={1} />);
      });
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(onVoiceToggle).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('opens the agent channel window from the mesh panel and from an agent row', async () => {
    const onOpenAgentChat = vi.fn();
    await act(async () => {
      render(
        <VexaCommandCenter
          agents={agents}
          messages={messages}
          isConnected
          isGenerating={false}
          isSpeaking={false}
          micState="off"
          onVoiceToggle={vi.fn()}
          onCommand={vi.fn().mockReturnValue(true)}
          onStop={vi.fn()}
          language="ru"
          micStreamRef={{ current: null }}
          ttsAudioElRef={{ current: null }}
          onOpenAgentChat={onOpenAgentChat}
          chatSessions={[]}
          currentChatId="dashboard"
          getSessionLabel={(id) => id}
          onCreateSession={vi.fn()}
        />,
      );
    });

    fireEvent.click(screen.getByRole('button', { name: 'Открыть канал с агентом' }));
    expect(onOpenAgentChat).toHaveBeenLastCalledWith();

    fireEvent.click(screen.getByRole('button', { name: /Research Agent/ }));
    expect(onOpenAgentChat).toHaveBeenLastCalledWith('research');
  });

  it('routes the bottom navigation onto the workspace tabs', async () => {
    const onNavigate = vi.fn();
    await act(async () => {
      render(
        <VexaCommandCenter
          agents={agents}
          messages={messages}
          isConnected
          isGenerating={false}
          isSpeaking={false}
          micState="off"
          onVoiceToggle={vi.fn()}
          onCommand={vi.fn().mockReturnValue(true)}
          onStop={vi.fn()}
          language="ru"
          micStreamRef={{ current: null }}
          ttsAudioElRef={{ current: null }}
          onOpenAgentChat={vi.fn()}
          chatSessions={[]}
          currentChatId="dashboard"
          getSessionLabel={(id) => id}
          onCreateSession={vi.fn()}
          onNavigate={onNavigate}
        />,
      );
    });

    fireEvent.click(screen.getByRole('button', { name: 'Протоколы' }));
    expect(onNavigate).toHaveBeenCalledWith('protocols');
  });

  it('keeps a rejected command in the composer and explains the failure', async () => {
    // onCommand returning false means the app refused to send (offline, busy, blocked).
    const onCommand = vi.fn().mockReturnValue(false);
    await act(async () => {
      render(
        <VexaCommandCenter
          agents={agents}
          messages={messages}
          isConnected
          isGenerating={false}
          isSpeaking={false}
          micState="off"
          onVoiceToggle={vi.fn()}
          onCommand={onCommand}
          onStop={vi.fn()}
          language="ru"
          micStreamRef={{ current: null }}
          ttsAudioElRef={{ current: null }}
          onOpenAgentChat={vi.fn()}
          chatSessions={[]}
          currentChatId="dashboard"
          getSessionLabel={(id) => id}
          onCreateSession={vi.fn()}
        />,
      );
    });

    const composer = screen.getByLabelText('Скажите или напишите задачу для Vexa');
    fireEvent.change(composer, { target: { value: 'Запусти тесты' } });
    fireEvent.click(screen.getByRole('button', { name: 'Передать команду' }));

    expect(onCommand).toHaveBeenCalledWith('Запусти тесты');
    expect(composer).toHaveValue('Запусти тесты');
    expect(screen.getByRole('alert')).toHaveTextContent('Команда не отправлена');
  });
});
