import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { ChatTab } from './ChatTab';

describe('ChatTab Component', () => {
  const defaultProps = {
    currentChatId: 'dashboard',
    chatSessions: [
      { id: 'dashboard', title: 'Main Terminal' },
      { id: 'chat_123', title: 'chat_123' }
    ],
    messages: [
      { role: 'assistant' as const, content: 'Hello, Sir.' },
      { role: 'user' as const, content: 'What is the weather today?' }
    ],
    inputValue: '',
    setInputValue: vi.fn(),
    isSpeaking: false,
    setIsSpeaking: vi.fn(),
    micState: 'off' as const,
    micEnabled: false,
    onVoiceToggle: vi.fn(),
    isTTSEnabled: true,
    setIsTTSEnabled: vi.fn(),
    isGenerating: false,
    playingMsgIndex: null,
    setPlayingMsgIndex: vi.fn(),
    config: { system_prompt: 'System prompt', model: 'gpt-4' },
    isConnected: true,
    isUploading: false,
    attachedFile: null,
    setAttachedFile: vi.fn(),
    handleChatFileAttach: vi.fn(),
    speakText: vi.fn(),
    handleClearChat: vi.fn(),
    handleSendMessage: vi.fn(),
    selectChat: vi.fn(),
    handleCreateNewSession: vi.fn(),
    fetchChatSessions: vi.fn(),
    getSessionLabel: (id: string) => id === 'dashboard' ? 'Main Terminal' : id,
    mainChatEndRef: React.createRef<HTMLDivElement>(),
    subagents: [],
    handleSetSessionAgent: vi.fn(),
    fetchWithAuth: vi.fn()
  };

  it('renders messages correctly', () => {
    render(<ChatTab {...defaultProps} />);
    
    expect(screen.getByText('Hello, Sir.')).toBeInTheDocument();
    expect(screen.getByText('What is the weather today?')).toBeInTheDocument();
    
    expect(screen.getByText('VEXA')).toBeInTheDocument();
    expect(screen.getByText('CREATOR')).toBeInTheDocument();
  });

  it('triggers setInputValue on text entry', () => {
    render(<ChatTab {...defaultProps} />);
    
    const input = screen.getByPlaceholderText(/Enter command or request for Vexa/i);
    fireEvent.change(input, { target: { value: 'New message' } });
    
    expect(defaultProps.setInputValue).toHaveBeenCalledWith('New message');
  });

  it('calls handleSendMessage on submit click', () => {
    const props = {
      ...defaultProps,
      inputValue: 'Hello',
      handleSendMessage: vi.fn((e) => e.preventDefault())
    };
    render(<ChatTab {...props} />);
    
    const form = screen.getByRole('button', { name: /send/i }).closest('form');
    if (!form) throw new Error('Form not found');
    fireEvent.submit(form);
    
    expect(props.handleSendMessage).toHaveBeenCalled();
  });

  it('lists active chat sessions in the sidebar', () => {
    render(<ChatTab {...defaultProps} />);
    
    expect(screen.getAllByText('Main Terminal').length).toBeGreaterThan(0);
    expect(screen.getByText('chat_123')).toBeInTheDocument();
  });

  it('disables send while generating', () => {
    render(<ChatTab {...defaultProps} inputValue="hi" isGenerating={true} />);
    const send = screen.getByRole('button', { name: /send/i });
    expect(send).toBeDisabled();
  });

  it('shows a Stop button while generating and calls onStopGeneration', () => {
    const onStopGeneration = vi.fn();
    render(<ChatTab {...defaultProps} isGenerating={true} onStopGeneration={onStopGeneration} />);
    const stop = screen.getByRole('button', { name: /stop/i });
    fireEvent.click(stop);
    expect(onStopGeneration).toHaveBeenCalled();
  });

  it('renders an empty state when there are no messages', () => {
    render(<ChatTab {...defaultProps} messages={[]} />);
    expect(screen.getByText(/no messages yet/i)).toBeInTheDocument();
  });

  it('shows an offline banner when disconnected', () => {
    render(<ChatTab {...defaultProps} isConnected={false} />);
    expect(screen.getByText(/offline/i)).toBeInTheDocument();
  });

  it('shows Retry action for an empty response and calls onRetryLast', () => {
    const onRetryLast = vi.fn();
    render(
      <ChatTab
        {...defaultProps}
        hasLastUserMessage={true}
        onRetryLast={onRetryLast}
        messages={[{ role: 'assistant' as const, content: '', meta: { status: 'empty' } }]}
      />
    );
    const retry = screen.getByRole('button', { name: /retry/i });
    fireEvent.click(retry);
    expect(onRetryLast).toHaveBeenCalled();
  });

  it('filters sessions by search query', () => {
    render(<ChatTab {...defaultProps} />);
    const search = screen.getByPlaceholderText(/search sessions/i);
    fireEvent.change(search, { target: { value: 'zzz-no-match' } });
    // dashboard is always kept; the other session is filtered out.
    expect(screen.queryByText('chat_123')).not.toBeInTheDocument();
  });

  it('renders native streamed content and optional thinking output', () => {
    render(
      <ChatTab
        {...defaultProps}
        isGenerating={true}
        messages={[{
          role: 'assistant' as const,
          content: 'Partial answer',
          thinking: 'Private model trace',
          streaming: true,
          run_id: 'run-1',
          meta: { provider: 'ollama', model: 'qwen3:8b', status: 'streaming' },
        }]}
      />
    );
    expect(screen.getByText('Partial answer')).toBeInTheDocument();
    expect(screen.getByText('Model thinking')).toBeInTheDocument();
    expect(screen.getByText('Private model trace')).toBeInTheDocument();
    expect(screen.queryByText(/cognitive compiling/i)).not.toBeInTheDocument();
  });

  describe('project selector', () => {
    const projectProps = {
      ...defaultProps,
      currentChatId: 'chat_123',
      chatSessions: [
        { id: 'dashboard', title: 'Main Terminal' },
        { id: 'chat_123', title: 'chat_123', project_id: 'proj-1' },
      ],
      projects: [
        { id: 'proj-1', name: 'Client Site', description: '', is_active: true, created_at: '', updated_at: '' },
        { id: 'proj-2', name: 'Internal Tools', description: '', is_active: true, created_at: '', updated_at: '' },
      ],
      subagents: [
        { id: 'agent-a', name: 'Agent A', agent_type: 'agent', project_id: 'proj-1' },
        { id: 'agent-b', name: 'Agent B', agent_type: 'agent', project_id: 'proj-2' },
      ],
      handleSetSessionProject: vi.fn(),
    };

    it('shows the assigned project selected and calls handleSetSessionProject on change', () => {
      render(<ChatTab {...projectProps} />);
      const select = screen.getByDisplayValue('Client Site') as HTMLSelectElement;
      fireEvent.change(select, { target: { value: 'proj-2' } });
      expect(projectProps.handleSetSessionProject).toHaveBeenCalledWith('chat_123', 'proj-2');
    });

    it('scopes the orchestrator dropdown to the selected project\'s agents', () => {
      render(<ChatTab {...projectProps} />);
      // Agent A belongs to proj-1 (the session's active project) and must be offered.
      expect(screen.getByText(/Agent A/)).toBeInTheDocument();
      // Agent B belongs to a different project and must not be.
      expect(screen.queryByText(/Agent B/)).not.toBeInTheDocument();
      // Vexa (the default) is always available regardless of project.
      expect(screen.getByText('Vexa (Main)')).toBeInTheDocument();
    });

    it('offers every agent when no project is assigned to the session', () => {
      render(
        <ChatTab
          {...projectProps}
          chatSessions={[
            { id: 'dashboard', title: 'Main Terminal' },
            { id: 'chat_123', title: 'chat_123' },
          ]}
        />
      );
      expect(screen.getByText(/Agent A/)).toBeInTheDocument();
      expect(screen.getByText(/Agent B/)).toBeInTheDocument();
    });
  });
});
