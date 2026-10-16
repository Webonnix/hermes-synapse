import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LocalModelPanel } from './LocalModelPanel';

const onlineStatus = {
  available: true,
  base_url: 'http://hermes-llama:8080',
  health: 'ok',
  model: 'qwen3.8-27b',
  n_ctx: 196608,
  total_slots: 1,
  build: 'b10920',
  vision: true,
  model_size: 17_678_000_000,
};

function stubFetch(status: unknown, configResponse: unknown = { status: 'success', config: { model: 'qwen3.8-27b' } }) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(input);
    if (path === '/api/llm/status') return { ok: true, json: async () => status };
    if (path === '/api/config') return { ok: true, json: async () => configResponse };
    return { ok: false, status: 404, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('LocalModelPanel', () => {
  it('shows the served model and its context when the agent already uses it', async () => {
    stubFetch(onlineStatus);
    render(<LocalModelPanel activeModel="qwen3.8-27b" onSelectModel={() => {}} />);

    expect(await screen.findByText('Online · b10920')).toBeTruthy();
    expect(screen.getAllByText('qwen3.8-27b').length).toBeGreaterThan(0);
    expect(screen.getByText('192K ctx')).toBeTruthy();
    // Nothing to fix: no mismatch warning, no switch button.
    expect(screen.queryByRole('button', { name: /Use qwen3\.8-27b/ })).toBeNull();
  });

  it('offers to switch a stale configured model to the one the server serves', async () => {
    const fetchMock = stubFetch(onlineStatus);
    const onSelectModel = vi.fn();
    const onActivated = vi.fn();
    render(
      <LocalModelPanel activeModel="unsloth-mtp-q4km:latest" onSelectModel={onSelectModel} onActivated={onActivated} />,
    );

    const button = await screen.findByRole('button', { name: /Use qwen3\.8-27b/ });
    expect(screen.getByText(/but the server serves/)).toBeTruthy();
    fireEvent.click(button);

    await waitFor(() => expect(onActivated).toHaveBeenCalledWith({ model: 'qwen3.8-27b' }));
    expect(onSelectModel).toHaveBeenCalledWith('qwen3.8-27b');
    const configCall = fetchMock.mock.calls.find(([url]) => String(url) === '/api/config');
    expect(configCall).toBeTruthy();
    expect(JSON.parse(String((configCall![1] as RequestInit).body))).toEqual({ model: 'qwen3.8-27b' });
  });

  it('reports a server that is still loading its model instead of calling it offline', async () => {
    stubFetch({
      available: false, base_url: 'http://hermes-llama:8080', health: 'loading',
      error: 'Loading model', code: 'loading',
    });
    render(<LocalModelPanel activeModel="qwen3.8-27b" onSelectModel={() => {}} />);

    expect(await screen.findByText('Loading model…')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Loading model');
    expect(screen.queryByRole('button', { name: /Use / })).toBeNull();
  });
});
