import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentDesktopsTab } from './AgentDesktopsTab';
import { buildTranscript, normalizeKey, DESKTOP_KEY_RE, type DesktopEvent } from './desktopModel';

const rfbInstances: { url: string; options: unknown; viewOnly: boolean }[] = [];

vi.mock('@novnc/novnc', () => ({
  default: class FakeRFB extends EventTarget {
    viewOnly = true;
    scaleViewport = false;
    resizeSession = false;
    background = '';
    url: string;
    options: unknown;
    constructor(_target: HTMLElement, url: string, options: unknown) {
      super();
      this.url = url;
      this.options = options;
      rfbInstances.push(this);
    }
    disconnect() {}
    focus() {}
  },
}));

const ev = (seq: number, fields: Partial<DesktopEvent>): DesktopEvent => ({ seq, at: 0, type: 'message', ...fields });

describe('buildTranscript', () => {
  it('joins message chunks, updates a tool row in place and resolves a pending permission', () => {
    const items = buildTranscript([
      ev(1, { type: 'turn_start', runtime: 'grok', text: 'build it', run_id: 'r' }),
      ev(2, { type: 'tool', id: 'c1', run_id: 'r', title: 'npm i', kind: 'execute', status: 'pending' }),
      ev(3, { type: 'permission_pending', task_id: 'T-1', title: 'rm -rf', risk_class: 'R3' }),
      ev(4, { type: 'permission', task_id: 'T-1', granted: false, reason: 'task rejected' }),
      ev(5, { type: 'tool', id: 'c1', run_id: 'r', status: 'completed' }),
      ev(6, { type: 'message', text: 'Done, ' }),
      ev(7, { type: 'message', text: 'see the browser.' }),
      ev(8, { type: 'permission', task_id: 'T-2', granted: true }),
      ev(9, { type: 'turn_end', stop_reason: 'end_turn', error: '' }),
    ]);
    expect(items.map(i => i.kind)).toEqual(['task', 'tool', 'permission', 'message', 'end']);
    expect(items[1]).toMatchObject({ title: 'npm i', status: 'completed' });
    expect(items[2]).toMatchObject({ state: 'refused', reason: 'task rejected' });
    expect(items[3]).toMatchObject({ text: 'Done, see the browser.' });
  });

  it('flags a sign-in error from its code, not its wording', () => {
    const [end] = buildTranscript([ev(1, { type: 'turn_end', error: 'whatever', error_code: 'auth_required' })]);
    expect(end).toMatchObject({ kind: 'end', needsLogin: true });
  });
});

describe('desktop keys', () => {
  it('normalizes a typed name into the backend slug rule', () => {
    expect(normalizeKey('  Grok Builder #1 ')).toBe('grok-builder-1');
    expect(DESKTOP_KEY_RE.test(normalizeKey('../etc'))).toBe(true);
    expect(normalizeKey('../etc')).toBe('etc');
    expect(DESKTOP_KEY_RE.test('-bad')).toBe(false);
  });
});

function mockApi(routes: Record<string, unknown>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const path = url.split('?')[0];
    const body = routes[`${init?.method ?? 'GET'} ${path}`];
    if (body === undefined) return new Response(JSON.stringify({ detail: 'nope' }), { status: 404 });
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

const runtimes = [{ id: 'grok', title: 'Grok Build', login: 'grok login' }, { id: 'opencode', title: 'OpenCode', login: 'opencode auth login' }];

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  rfbInstances.length = 0;
});

describe('AgentDesktopsTab', () => {
  it('says so when desktops are off on the server', async () => {
    mockApi({ 'GET /api/agent-desktops': { enabled: false, runtimes, desktops: [] } });
    render(<AgentDesktopsTab language="ru" />);
    expect(await screen.findByText(/выключены на сервере/)).toBeInTheDocument();
  });

  it('shows the live screen of a running desktop, view-only until taken over', async () => {
    localStorage.setItem('hermes_agent_desktop_selected', 'alice');
    localStorage.setItem('vexa_auth_token', 'tok');
    mockApi({
      'GET /api/agent-desktops': { enabled: true, runtimes, desktops: [{ key: 'alice', name: 'n', status: 'running', busy: [] }] },
      'GET /api/agent-desktops/alice/events': { events: [ev(1, { type: 'turn_start', runtime: 'grok', text: 'open the site' })] },
    });
    render(<AgentDesktopsTab language="ru" />);
    expect(await screen.findByText('open the site')).toBeInTheDocument();
    await waitFor(() => expect(rfbInstances).toHaveLength(1));
    expect(rfbInstances[0].url).toMatch(/\/api\/agent-desktops\/alice\/vnc$/);
    expect(rfbInstances[0].options).toMatchObject({ wsProtocols: ['hermes-vnc', 'hermes-auth.tok'] });
    expect(rfbInstances[0].viewOnly).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /Управлять/ }));
    expect(rfbInstances[0].viewOnly).toBe(false);
  });

  it('sends a task to the chosen runtime', async () => {
    localStorage.setItem('hermes_agent_desktop_selected', 'alice');
    const calls = mockApi({
      'GET /api/agent-desktops': { enabled: true, runtimes, desktops: [{ key: 'alice', name: 'n', status: 'running', busy: [] }] },
      'GET /api/agent-desktops/alice/events': { events: [] },
      'POST /api/agent-desktops/alice/turns': { key: 'alice', run_id: 'adr-1' },
    });
    render(<AgentDesktopsTab language="en" />);
    await screen.findByText('alice');
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'opencode' } });
    fireEvent.change(screen.getByLabelText(/What should the agent do/), { target: { value: 'make a site' } });
    fireEvent.click(screen.getByRole('button', { name: /Send/ }));
    await waitFor(() => expect(calls.some(c => c.init?.method === 'POST' && c.url.endsWith('/turns'))).toBe(true));
    const sent = calls.find(c => c.url.endsWith('/turns'))!;
    expect(JSON.parse(String(sent.init?.body))).toEqual({ runtime: 'opencode', text: 'make a site' });
  });

  it('refuses a name the backend would reject, before calling it', async () => {
    const calls = mockApi({ 'GET /api/agent-desktops': { enabled: true, runtimes, desktops: [] } });
    render(<AgentDesktopsTab language="en" />);
    await screen.findByText(/No desktops yet/);
    fireEvent.change(screen.getByLabelText('agent-name'), { target: { value: '###' } });
    fireEvent.click(screen.getByRole('button', { name: /Start/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/lowercase/);
    expect(calls.some(c => c.init?.method === 'POST')).toBe(false);
  });
});
