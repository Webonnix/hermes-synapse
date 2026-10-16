import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { ShareConversation } from './ShareConversation';

function fakeFetch(handlers: Record<string, any>) {
  return vi.fn(async (url: string, init?: any) => {
    const method = (init?.method || 'GET').toUpperCase();
    const body = handlers[`${method} ${url}`];
    if (body === undefined) throw new Error(`unexpected request: ${method} ${url}`);
    return { ok: true, json: async () => body } as any;
  });
}

describe('ShareConversation', () => {
  it('offers to publish when the conversation is not shared', async () => {
    const fetchImpl = fakeFetch({ 'GET /api/share/chat_a': { token: null } });
    render(<ShareConversation chatId="chat_a" fetchImpl={fetchImpl as any} />);
    expect(await screen.findByRole('button', { name: /Поделиться/ })).toBeTruthy();
  });

  it('shows the live link once published, so a forgotten share stays visible', async () => {
    const fetchImpl = fakeFetch({
      'GET /api/share/chat_a': { token: null },
      'POST /api/share/chat_a': { token: 'tok123', url: '/share/tok123', message_count: 4 },
    });
    render(<ShareConversation chatId="chat_a" fetchImpl={fetchImpl as any} />);

    fireEvent.click(await screen.findByRole('button', { name: /Поделиться/ }));

    const link = await screen.findByRole('link', { name: /Публичная ссылка/ });
    expect(link.getAttribute('href')).toContain('/share/tok123');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('reflects an existing share on mount without publishing anything', async () => {
    const fetchImpl = fakeFetch({
      'GET /api/share/chat_a': { token: 'existing', url: '/share/existing' },
    });
    render(<ShareConversation chatId="chat_a" fetchImpl={fetchImpl as any} />);
    await screen.findByRole('link', { name: /Публичная ссылка/ });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('revokes and goes back to offering a new share', async () => {
    const fetchImpl = fakeFetch({
      'GET /api/share/chat_a': { token: 'existing', url: '/share/existing' },
      'DELETE /api/share/chat_a': { revoked: true },
    });
    render(<ShareConversation chatId="chat_a" fetchImpl={fetchImpl as any} />);

    fireEvent.click(await screen.findByRole('button', { name: /Отозвать/ }));

    expect(await screen.findByRole('button', { name: /Поделиться/ })).toBeTruthy();
  });

  it('treats a failed request as "not shared" rather than rendering an error payload', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, json: async () => ({ detail: 'nope' }) }) as any);
    render(<ShareConversation chatId="chat_a" fetchImpl={fetchImpl as any} />);
    await waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    expect(await screen.findByRole('button', { name: /Поделиться/ })).toBeTruthy();
  });

  it('escapes a chat id into the request path', async () => {
    const fetchImpl = fakeFetch({ 'GET /api/share/chat%2Fa': { token: null } });
    render(<ShareConversation chatId="chat/a" fetchImpl={fetchImpl as any} />);
    await screen.findByRole('button', { name: /Поделиться/ });
  });
});
