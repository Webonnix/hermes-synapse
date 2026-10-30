import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import App from './App';

// Mock WebSockets
class MockWebSocket {
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: any) => void) | null = null;
  send = vi.fn();
  close = vi.fn();

  constructor(url: string) {
    this.url = url;
    setTimeout(() => {
      if (this.onopen) this.onopen();
    }, 10);
  }
}

vi.stubGlobal('WebSocket', MockWebSocket);

describe('App Component', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    localStorage.setItem("hermes_language", "en");
    window.history.replaceState(null, "", "/");
  });

  it('renders login screen when unauthenticated', async () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: 'VEXA' })).toBeInTheDocument();
    expect(screen.getByText('Secure Access Link')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /request code in telegram/i })).toBeInTheDocument();
  });

  it('does not poll protected tools APIs before authentication', async () => {
    localStorage.setItem('vexa_active_tab', 'tools');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    expect(screen.getByRole('heading', { name: 'VEXA' })).toBeInTheDocument();
    await new Promise(resolve => setTimeout(resolve, 1100));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /request code in telegram/i })).toBeInTheDocument();
  });

  it('requests OTP and shows confirmation message', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: 'success' })
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    
    const requestButton = screen.getByRole('button', { name: /request code in telegram/i });
    fireEvent.click(requestButton);

    expect(screen.getByText(/initializing session and sending code/i)).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText(/authorization code sent to your trusted telegram chat/i)).toBeInTheDocument();
    });

    expect(fetchMock).toHaveBeenCalledWith('/api/auth/request-code', expect.any(Object));
  });

  it('renders main application when authenticated', async () => {
    document.cookie = 'hermes_csrf=mock-csrf; path=/';
    localStorage.setItem('hermes_language', 'en');
    
    const fetchMock = vi.fn().mockImplementation((url) => {
      if (url.includes('/api/config')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ system_prompt: 'System prompt', model: 'gpt-4' })
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([])
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    // The dashboard's own header replaces the old "autonomous voice command center"
    // subtitle; the system-status panel is the stable landmark for "Vexa rendered".
    await waitFor(() => {
      expect(screen.getByText(/system status/i)).toBeInTheDocument();
    });

    const transcriptToggle = screen.getByRole('button', { name: /open a channel with an agent/i });
    fireEvent.click(transcriptToggle);

    await waitFor(() => {
      expect(screen.getByText(/communication link/i)).toBeInTheDocument();
    });

    // The Vexa dashboard's bottom navigation also has a "Settings" entry, so target the
    // workspace sidebar's one explicitly.
    const settingsBtn = screen.getAllByRole('button', { name: 'Settings' })
      .find(button => !button.classList.contains('vx-nav-item'));
    fireEvent.click(settingsBtn!);

    await waitFor(() => {
      expect(screen.getByText('Model & Prompt')).toBeInTheDocument();
    });
  });

  it('opens the GPU server metrics from the sidebar', async () => {
    document.cookie = 'hermes_csrf=mock-csrf; path=/';
    localStorage.setItem('hermes_language', 'en');
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve([]),
    })));

    render(<App />);

    // Metrics belongs to Operations; it remains reachable through navigation.
    fireEvent.click(await screen.findByRole('button', { name: 'Operations' }));
    const metricsBtn = await screen.findByRole('button', { name: 'Metrics' });
    fireEvent.click(metricsBtn);

    expect(await screen.findByText('GPU server metrics')).toBeInTheDocument();
  });
});

describe('OTP recovery', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('hermes_language', 'en');
    history.replaceState(null, '', '/');
  });
  it('does not claim that a code was sent after a failed request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ message: 'Unavailable' }) }));
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /request code in telegram/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unavailable');
    expect(screen.queryByText(/authorization code sent/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /request code in telegram/i })).toBeEnabled();
  });
  it('submits an invalid code once and retries only when the code changes', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => Promise.resolve({
      ok: url.includes('request-code'), json: async () => url.includes('request-code') ? { status: 'success' } : { detail: 'Invalid access code.' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: /request code in telegram/i }));
    const input = await screen.findByRole('textbox', { name: /telegram code/i });
    fireEvent.change(input, { target: { value: '123456' } });
    await screen.findByText(/invalid access code/i);
    await new Promise(resolve => setTimeout(resolve, 100));
    expect(fetchMock.mock.calls.filter(([url]) => url.includes('verify-code'))).toHaveLength(1);
    fireEvent.change(input, { target: { value: '654321' } });
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url.includes('verify-code'))).toHaveLength(2));
  });
});
