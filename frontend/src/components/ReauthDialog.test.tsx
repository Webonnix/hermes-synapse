import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ReauthDialog } from './ReauthDialog';

afterEach(() => vi.unstubAllGlobals());

describe('fresh identity confirmation', () => {
  it('cancels the protected action without submitting credentials', () => {
    const complete = vi.fn();
    render(<ReauthDialog language="ru" />);
    act(() => window.dispatchEvent(new CustomEvent('hermes-reauth', { detail: complete })));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Отмена' }));
    expect(complete).toHaveBeenCalledWith(false);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('completes only after the server accepts password and fresh OTP', async () => {
    const complete = vi.fn();
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'success' }) });
    vi.stubGlobal('fetch', fetcher);
    render(<ReauthDialog language="en" />);
    act(() => window.dispatchEvent(new CustomEvent('hermes-reauth', { detail: complete })));
    fireEvent.change(screen.getByLabelText('Password (if configured)'), { target: { value: 'private' } });
    fireEvent.change(screen.getByLabelText('Telegram code'), { target: { value: '123456' } });
    fireEvent.submit(screen.getByRole('dialog'));
    await waitFor(() => expect(complete).toHaveBeenCalledWith(true));
    expect(fetcher).toHaveBeenCalledWith('/api/auth/reauth', expect.objectContaining({ body: JSON.stringify({ password: 'private', code: '123456' }) }));
  });
});
