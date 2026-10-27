import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrowserSessionsTab } from './BrowserSessionsTab';

type Call = { url: string; init?: RequestInit };

function mockApi(state: { open: string | null }) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url === '/api/browser/sessions') {
      return { ok: true, json: async () => ({ profiles: [{ name: 'instagram', label: 'Instagram', start_url: 'https://www.instagram.com/accounts/login/' }], login_open: state.open }) };
    }
    if (url === '/api/browser/login/start') {
      state.open = 'instagram';
      return { ok: true, json: async () => ({ open: true }) };
    }
    if (url === '/api/browser/login/frame') {
      return { ok: true, json: async () => ({ open: true, profile: 'instagram', url: 'https://www.instagram.com/', screenshot_b64: 'AAAA', title: 'Instagram' }) };
    }
    return { ok: true, json: async () => ({ ok: true }) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

describe('BrowserSessionsTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('lists saved logins and opens a login window for a preset', async () => {
    const calls = mockApi({ open: null });
    render(<BrowserSessionsTab language="en" />);
    expect(await screen.findByText('Instagram', { selector: 'strong' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /open login window/i }));
    await waitFor(() => expect(calls.some(call => call.url === '/api/browser/login/start')).toBe(true));
    const start = calls.find(call => call.url === '/api/browser/login/start');
    expect(JSON.parse(String(start?.init?.body))).toMatchObject({ profile: 'instagram', url: 'https://www.instagram.com/accounts/login/' });
  });

  it('forwards clicks as fractions of the frame and typed text as-is', async () => {
    const calls = mockApi({ open: 'instagram' });
    render(<BrowserSessionsTab language="en" />);
    const image = await screen.findByRole('img');
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 640, height: 400, right: 640, bottom: 400, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.click(image, { clientX: 320, clientY: 100 });
    fireEvent.change(screen.getByLabelText(/click a field/i), { target: { value: 'secret-pass' } });
    fireEvent.click(screen.getByRole('button', { name: 'Type' }));

    await waitFor(() => {
      const inputs = calls.filter(call => call.url === '/api/browser/login/input').map(call => JSON.parse(String(call.init?.body)));
      expect(inputs).toContainEqual({ type: 'click', x: 0.5, y: 0.25 });
      expect(inputs).toContainEqual({ type: 'type', text: 'secret-pass' });
    });
  });
});
