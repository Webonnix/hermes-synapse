import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppHeader } from './AppHeader';

const summary = {
  state: { kill_switch: false, reason: '', updated_by: 'system', updated_at: '2026-07-25T09:00:00Z' },
  counts: { awaiting_approval: 3 },
  pending_approvals: [], tasks: [], events: [],
  policy: { risk_levels: ['R0', 'R1', 'R2', 'R3', 'R4'], unknown_tools: 'R4', r4_double_confirmation: true },
} as never;

describe('AppHeader', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('shows the awaiting-approval badge from polled summary and opens Processes on click', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => summary }));
    vi.stubGlobal('fetch', fetchMock);
    const onOpen = vi.fn();
    render(<AppHeader language="en" onOpenProcesses={onOpen} onOpenBrowserView={() => undefined} />);
    await waitFor(() => expect(screen.getByTestId('approvals-badge')).toHaveTextContent('3'));
    fireEvent.click(screen.getByRole('button', { name: /awaiting approval/i }));
    expect(onOpen).toHaveBeenCalled();
  });

  it('always renders the kill switch and asks confirmation before engaging it', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => summary }));
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<AppHeader language="en" onOpenProcesses={() => undefined} onOpenBrowserView={() => undefined} />);
    const killButton = await screen.findByRole('button', { name: /emergency stop/i });
    fireEvent.click(killButton);
    expect(window.confirm).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalledWith('/api/control-plane/kill', expect.anything());
  });

  it('reuses an externally provided summary without polling', () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ active: false }) }));
    vi.stubGlobal('fetch', fetchMock);
    render(<AppHeader language="en" onOpenProcesses={() => undefined} onOpenBrowserView={() => undefined} summary={summary} />);
    expect(screen.getByTestId('approvals-badge')).toHaveTextContent('3');
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining('/api/control-plane/summary'));
  });
});
