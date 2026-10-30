import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentDesktopsTab } from './AgentDesktopsTab';
import { buildTranscript, normalizeKey, DESKTOP_KEY_RE, type DesktopEvent } from './desktopModel';

type FakeRfb = EventTarget & { url: string; options: unknown; viewOnly: boolean; pasted: { text: string; viewOnly: boolean }[] };
const rfbInstances: FakeRfb[] = [];

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
    pasted: { text: string; viewOnly: boolean }[] = [];
    disconnect() {}
    focus() {}
    // Like noVNC: text sent while view-only is dropped.
    clipboardPasteFrom(text: string) {
      if (!this.viewOnly) this.pasted.push({ text, viewOnly: this.viewOnly });
    }
  },
}));

const connect = async () => {
  await waitFor(() => expect(rfbInstances).toHaveLength(1));
  await act(async () => { rfbInstances[0].dispatchEvent(new Event('connect')); });
};

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
    mockApi({
      'GET /api/agent-desktops': { enabled: true, runtimes, desktops: [{ key: 'alice', name: 'n', status: 'running', busy: [] }] },
      'GET /api/agent-desktops/alice/events': { events: [ev(1, { type: 'turn_start', runtime: 'grok', text: 'open the site' })] },
    });
    render(<AgentDesktopsTab language="ru" />);
    expect(await screen.findByText('open the site')).toBeInTheDocument();
    await waitFor(() => expect(rfbInstances).toHaveLength(1));
    expect(rfbInstances[0].url).toMatch(/\/api\/agent-desktops\/alice\/vnc\?screen=0$/);
    expect(rfbInstances[0].options).toMatchObject({ wsProtocols: ['hermes-vnc'] });
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
    await screen.findByRole('option', { name: 'OpenCode' });
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

  it('creates an OpenBot desktop when that kind is chosen', async () => {
    const calls = mockApi({
      'GET /api/agent-desktops': { enabled: true, runtimes, desktops: [], kinds: [
        { id: 'agent', enabled: true, screens: 1 }, { id: 'openbot', enabled: true, screens: 2 }] },
      'POST /api/agent-desktops/team-bot/start': { key: 'team-bot', kind: 'openbot' },
    });
    render(<AgentDesktopsTab language="en" />);
    await screen.findByText(/No desktops yet/);
    fireEvent.change(screen.getByLabelText('agent-name'), { target: { value: 'team-bot' } });
    fireEvent.change(await screen.findByDisplayValue('Agent (Grok / OpenCode)'), { target: { value: 'openbot' } });
    fireEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/team-bot/start'))).toBe(true));
    const sent = calls.find(c => c.url.endsWith('/team-bot/start'))!;
    expect(JSON.parse(String(sent.init?.body))).toEqual({ kind: 'openbot' });
  });

  it('shows an OpenBot desktop with two screens and how to connect it', async () => {
    localStorage.setItem('hermes_agent_desktop_selected', 'bot');
    mockApi({
      'GET /api/agent-desktops': { enabled: true, runtimes, kinds: [
        { id: 'agent', enabled: true, screens: 1 }, { id: 'openbot', enabled: true, screens: 2 }],
        desktops: [{ key: 'bot', name: 'n', status: 'running', busy: [], kind: 'openbot' }] },
      'GET /api/agent-desktops/bot/events': { events: [] },
      'GET /api/agent-desktops/bot/model-access': {
        base_url: 'http://hermes-backend:8000/agent-llm/v1', model: 'qwen-local', api_key: 'bot.secret',
        models: ['qwen-local', 'omniroute/ds/deepseek-flash'] },
    });
    render(<AgentDesktopsTab language="en" />);
    expect(await screen.findByText('http://hermes-backend:8000/agent-llm/v1')).toBeInTheDocument();
    // The key is masked until asked for, and no task box is offered.
    expect(screen.queryByText('bot.secret')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/What should the agent do/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show' }));
    expect(screen.getByText('bot.secret')).toBeInTheDocument();
    expect(screen.getByText('omniroute/ds/deepseek-flash')).toBeInTheDocument();
    await waitFor(() => expect(rfbInstances).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: 'Agents screen' }));
    await waitFor(() => expect(rfbInstances.at(-1)!.url).toMatch(/vnc\?screen=1$/));
  });

  describe('clipboard', () => {
    const openbotListing = {
      'GET /api/agent-desktops': { enabled: true, runtimes, kinds: [
        { id: 'agent', enabled: true, screens: 1 }, { id: 'openbot', enabled: true, screens: 2 }],
        desktops: [{ key: 'bot', name: 'n', status: 'running', busy: [], kind: 'openbot' }] },
      'GET /api/agent-desktops/bot/events': { events: [] },
      'GET /api/agent-desktops/bot/model-access': {
        base_url: 'http://hermes-backend:8000/agent-llm/v1', model: 'qwen-local', api_key: 'bot.secret', models: ['qwen-local'] },
    };

    it('copying a connect value also puts it on the desktop clipboard, even while view-only', async () => {
      localStorage.setItem('hermes_agent_desktop_selected', 'bot');
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(async () => {}), readText: vi.fn() } });
      mockApi(openbotListing);
      render(<AgentDesktopsTab language="en" />);
      await screen.findByText('http://hermes-backend:8000/agent-llm/v1');
      await connect();
      fireEvent.click(screen.getByRole('button', { name: 'Copy API key' }));
      expect(rfbInstances[0].pasted.map(p => p.text)).toEqual(['bot.secret']);
      expect(rfbInstances[0].viewOnly).toBe(true);  // view-only is back on afterwards
      expect(await screen.findAllByText(/Text is on the desktop clipboard/)).not.toHaveLength(0);
    });

    it('sends my clipboard to the desktop, or offers a paste box when reading it is refused', async () => {
      localStorage.setItem('hermes_agent_desktop_selected', 'bot');
      const readText = vi.fn().mockResolvedValueOnce('hello desktop').mockRejectedValueOnce(new Error('denied'));
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(async () => {}), readText } });
      mockApi(openbotListing);
      render(<AgentDesktopsTab language="en" />);
      await screen.findByText('http://hermes-backend:8000/agent-llm/v1');
      await connect();
      fireEvent.click(screen.getByRole('button', { name: /To desktop/ }));
      await waitFor(() => expect(rfbInstances[0].pasted.map(p => p.text)).toEqual(['hello desktop']));
      fireEvent.click(screen.getByRole('button', { name: /To desktop/ }));
      const box = await screen.findByLabelText(/Paste text here/);
      fireEvent.change(box, { target: { value: 'typed by hand' } });
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      expect(rfbInstances[0].pasted.map(p => p.text)).toEqual(['hello desktop', 'typed by hand']);
    });

    it('never writes the desktop clipboard to mine until I ask', async () => {
      localStorage.setItem('hermes_agent_desktop_selected', 'bot');
      const writeText = vi.fn(async () => {});
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText, readText: vi.fn() } });
      mockApi(openbotListing);
      render(<AgentDesktopsTab language="en" />);
      await screen.findByText('http://hermes-backend:8000/agent-llm/v1');
      await connect();
      const fromDesktop = screen.getByRole('button', { name: /From desktop/ });
      expect(fromDesktop).toBeDisabled();
      await act(async () => {
        rfbInstances[0].dispatchEvent(new CustomEvent('clipboard', { detail: { text: 'rm -rf ~' } }));
      });
      expect(writeText).not.toHaveBeenCalled();
      fireEvent.click(fromDesktop);
      await waitFor(() => expect(writeText).toHaveBeenCalledWith('rm -rf ~'));
    });
  });
});
