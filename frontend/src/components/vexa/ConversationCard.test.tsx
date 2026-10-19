import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '../../types';
import { VEXA_COPY } from './vexaCopy';
import { ConversationCard } from './VexaRightSidebar';

const copy = VEXA_COPY.ru;

function renderCard(messages: ChatMessage[], overrides: Partial<Parameters<typeof ConversationCard>[0]> = {}) {
  const props = {
    copy,
    messages,
    disabled: false,
    generating: false,
    onSend: vi.fn().mockReturnValue(true),
    onStop: vi.fn(),
    onOpenFullChat: vi.fn(),
    ...overrides,
  };
  render(<ConversationCard {...props} />);
  return props;
}

describe('ConversationCard', () => {
  it('renders assistant replies as markdown', () => {
    renderCard([{ role: 'assistant', content: '## Итог\n- **Bitcoin** растёт\n- Рынок спокоен', id: 1 }]);
    expect(screen.getByRole('heading', { name: 'Итог' })).toBeInTheDocument();
    expect(screen.getByText('Bitcoin').tagName).toBe('STRONG');
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('shows a thinking line as soon as a command is sent, before the first stream frame', () => {
    renderCard([{ role: 'user', content: 'Что нового?' }], { generating: true });
    expect(screen.getByText(copy.vexaTyping)).toBeInTheDocument();
  });

  it('says which tool is running while the reply has no text yet', () => {
    renderCard(
      [{ role: 'assistant', content: '', run_id: 'r1', streaming: true, activity: 'web_search' }],
      { generating: true },
    );
    expect(screen.getByText(copy.toolWebSearch)).toBeInTheDocument();
    expect(screen.queryByText(copy.vexaTyping)).not.toBeInTheDocument();
  });

  it('sends on Enter, keeps Shift+Enter for a new line', () => {
    const props = renderCard([]);
    const input = screen.getByLabelText(copy.askFollowUp);
    fireEvent.change(input, { target: { value: 'Привет' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(props.onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(props.onSend).toHaveBeenCalledWith('Привет');
  });

  it('turns send into stop while a reply is generating', () => {
    const props = renderCard([{ role: 'user', content: 'Привет' }], { generating: true });
    fireEvent.click(screen.getByRole('button', { name: copy.stop }));
    expect(props.onStop).toHaveBeenCalledOnce();
  });
});
