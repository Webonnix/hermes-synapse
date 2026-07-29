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
  });

  it('renders login screen when unauthenticated', async () => {
    render(<App />);
    expect(screen.getByText('HERMES')).toBeInTheDocument();
    expect(screen.getByText('Secure Access Link')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /request code in telegram/i })).toBeInTheDocument();
  });

  it('does not poll protected tools APIs before authentication', async () => {
    localStorage.setItem('jarvis_active_tab', 'tools');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    expect(screen.getByText('HERMES')).toBeInTheDocument();
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
    localStorage.setItem('jarvis_auth_token', 'mock_token');
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
});
