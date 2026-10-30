import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ClipboardCopy, ClipboardPaste, Eye, MonitorPlay, MousePointer2, Play, RefreshCw, Send, Square, StopCircle } from 'lucide-react';
import { DesktopViewer, type DesktopViewerHandle } from './DesktopViewer';
import { OpenBotConnect } from './OpenBotConnect';
import {
  COPY, DESKTOP_KEY_RE, buildTranscript, normalizeKey,
  type DesktopEvent, type DesktopKindId, type DesktopListing, type Lang, type TranscriptItem,
} from './desktopModel';
import './agentDesktops.css';

/**
 * «Десктопы агентов»: every agent's own isolated Linux desktop (gVisor) with a
 * live screen, a task box for the CLI agent working in it (Grok Build,
 * OpenCode) and its progress. Backend: backend/agent_runtimes/api.py.
 *
 * Progress is polled from the desktop's event log rather than pushed over
 * /api/ws: the log is bounded and numbered, so polling with `after=<seq>`
 * picks up exactly what was missed after a reload or a dropped connection.
 */

const LIST_POLL_MS = 5000;
const EVENTS_POLL_MS = 1000;
const SELECTED_STORAGE_KEY = 'hermes_agent_desktop_selected';

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return await response.json() as T;
}

const post = <T,>(url: string, body?: unknown) => api<T>(url, {
  method: 'POST',
  headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

function readSelected(): string {
  try { return localStorage.getItem(SELECTED_STORAGE_KEY) || ''; } catch { return ''; }
}

export function AgentDesktopsTab({ language }: { language: string }) {
  const lang: Lang = language === 'en' ? 'en' : 'ru';
  const copy = COPY[lang];
  const [listing, setListing] = useState<DesktopListing | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string>(readSelected);
  const [newKey, setNewKey] = useState('');
  const [newKind, setNewKind] = useState<DesktopKindId>('agent');
  const [screen, setScreen] = useState(0);
  const viewer = useRef<DesktopViewerHandle | null>(null);
  const [desktopClipboard, setDesktopClipboard] = useState('');
  const [clipboardNote, setClipboardNote] = useState('');
  const [manualPaste, setManualPaste] = useState<string | null>(null);
  const [pending, setPending] = useState<string>('');
  const [viewOnly, setViewOnly] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [runtime, setRuntime] = useState('grok');
  const [task, setTask] = useState('');
  const [events, setEvents] = useState<DesktopEvent[]>([]);
  const lastSeq = useRef(0);
  const transcriptEnd = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      setListing(await api<DesktopListing>('/api/agent-desktops'));
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), LIST_POLL_MS);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    try { localStorage.setItem(SELECTED_STORAGE_KEY, selected); } catch { /* private mode */ }
    lastSeq.current = 0;
    setEvents([]);
    setScreen(0);
    setDesktopClipboard('');
    setManualPaste(null);
    setClipboardNote('');
    if (!selected) return;
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await api<{ events: DesktopEvent[] }>(
          `/api/agent-desktops/${encodeURIComponent(selected)}/events?after=${lastSeq.current}`,
        );
        if (cancelled || !payload.events.length) return;
        lastSeq.current = payload.events[payload.events.length - 1].seq;
        setEvents(prev => [...prev, ...payload.events]);
      } catch { /* keep the last transcript; the list poll reports errors */ }
    };
    void load();
    const interval = window.setInterval(() => void load(), EVENTS_POLL_MS);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [selected]);

  useEffect(() => { transcriptEnd.current?.scrollIntoView({ block: 'end' }); }, [events.length]);

  const current = listing?.desktops.find(d => d.key === selected);
  const isRunning = current?.status === 'running';
  const busy = current?.busy.includes(runtime) ?? false;
  const transcript = useMemo(() => buildTranscript(events), [events]);
  const runtimeInfo = listing?.runtimes.find(r => r.id === runtime);
  const isOpenbot = current?.kind === 'openbot';
  const screens = listing?.kinds?.find(k => k.id === (current?.kind ?? 'agent'))?.screens ?? 1;
  const openbotEnabled = listing?.kinds?.some(k => k.id === 'openbot' && k.enabled) ?? false;

  const sendToDesktop = (text: string): boolean => {
    const sent = viewer.current?.sendClipboard(text) ?? false;
    setClipboardNote(sent ? copy.clipboardSent : copy.clipboardNotConnected);
    return sent;
  };

  // Reading the owner's clipboard needs the browser's permission; where it is
  // refused (or the page is not on HTTPS) a paste box does the same job.
  const pasteFromMyClipboard = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { sendToDesktop(text); return; }
    } catch { /* no permission: fall back to the box */ }
    setManualPaste('');
  };

  const copyFromDesktop = () => {
    void navigator.clipboard?.writeText(desktopClipboard)
      .then(() => setClipboardNote(copy.clipboardCopiedOut))
      // Writing is refused (no permission, plain HTTP): show it to select by hand.
      .catch(() => setClipboardNote(desktopClipboard));
  };

  const act = async (label: string, run: () => Promise<unknown>) => {
    setPending(label);
    setError('');
    try { await run(); } catch (exc) { setError((exc as Error).message); }
    setPending('');
    void refresh();
  };

  // A new desktop says its kind; an existing one keeps the kind it was made as.
  const startDesktop = (key: string, kind?: DesktopKindId) => act(`start:${key}`, async () => {
    await post(`/api/agent-desktops/${encodeURIComponent(key)}/start`, kind ? { kind } : undefined);
    setSelected(key);
    setAttempt(a => a + 1);
  });

  const createDesktop = () => {
    const key = normalizeKey(newKey);
    if (!DESKTOP_KEY_RE.test(key)) { setError(copy.invalidKey); return; }
    setNewKey('');
    void startDesktop(key, newKind);
  };

  const sendTask = () => act('send', async () => {
    await post(`/api/agent-desktops/${encodeURIComponent(selected)}/turns`, { runtime, text: task.trim() });
    setTask('');
  });

  if (listing && !listing.enabled) {
    return <div className="ad-root"><div className="ad-card ad-notice">{copy.disabled}</div></div>;
  }

  return (
    <div className="ad-root">
      <header className="ad-header">
        <div>
          <div className="ad-eyebrow"><MonitorPlay size={14} /> {copy.eyebrow}</div>
          <h1 className="ad-title">{copy.title}</h1>
          <p className="ad-subtitle">{copy.subtitle}</p>
        </div>
      </header>

      {error && <div className="ad-error" role="alert">{error}</div>}

      <div className="ad-layout">
        <aside className="ad-card ad-list">
          <form className="ad-new" onSubmit={e => { e.preventDefault(); createDesktop(); }}>
            <input
              className="ad-input"
              value={newKey}
              onChange={e => setNewKey(e.target.value)}
              placeholder={copy.newPlaceholder}
              aria-label={copy.newPlaceholder}
            />
            <button className="ad-button is-primary" type="submit" disabled={!newKey.trim() || !!pending}>
              <Play size={14} /> {copy.create}
            </button>
            {openbotEnabled && (
              <select className="ad-input ad-kind" value={newKind} aria-label={copy.runtime}
                onChange={e => setNewKind(e.target.value as DesktopKindId)}>
                <option value="agent">{copy.kindAgent}</option>
                <option value="openbot">{copy.kindOpenbot}</option>
              </select>
            )}
          </form>
          {listing && !listing.desktops.length && <p className="ad-muted">{copy.empty}</p>}
          <ul className="ad-desktops">
            {listing?.desktops.map(item => (
              <li key={item.key} className={`ad-desktop ${item.key === selected ? 'is-selected' : ''}`}>
                <button className="ad-desktop-main" onClick={() => setSelected(item.key)}>
                  <span className={`ad-dot ${item.status === 'running' ? 'is-on' : ''}`} />
                  <span className="ad-desktop-name">
                    {item.key}{item.kind === 'openbot' && <span className="ad-tag ad-kind-tag">OpenBot</span>}
                  </span>
                  <span className="ad-muted">
                    {item.busy.length ? copy.busy : item.status === 'running' ? copy.running : copy.stopped}
                  </span>
                </button>
                {item.status === 'running' ? (
                  <button className="ad-icon" title={copy.stop} aria-label={`${copy.stop} ${item.key}`}
                    disabled={!!pending}
                    onClick={() => void act(`stop:${item.key}`, () => post(`/api/agent-desktops/${encodeURIComponent(item.key)}/stop`))}>
                    <Square size={14} />
                  </button>
                ) : (
                  <button className="ad-icon" title={copy.start} aria-label={`${copy.start} ${item.key}`}
                    disabled={!!pending} onClick={() => void startDesktop(item.key)}>
                    <Play size={14} />
                  </button>
                )}
              </li>
            ))}
          </ul>
        </aside>

        <section className="ad-card ad-viewer">
          <div className="ad-toolbar">
            <strong className="ad-viewer-title">{selected || '—'}</strong>
            <div className="ad-toolbar-actions">
              {screens > 1 && (
                <div className="ad-screens" role="group">
                  {[copy.screenOpenbot, copy.screenAgents].slice(0, screens).map((label, index) => (
                    <button key={label} className={`ad-button ${screen === index ? 'is-active' : ''}`}
                      aria-pressed={screen === index} disabled={!isRunning} onClick={() => setScreen(index)}>
                      {label}
                    </button>
                  ))}
                </div>
              )}
              <button className={`ad-button ${viewOnly ? '' : 'is-active'}`} disabled={!isRunning}
                onClick={() => setViewOnly(v => !v)}>
                {viewOnly ? <><MousePointer2 size={14} /> {copy.takeOver}</> : <><Eye size={14} /> {copy.viewOnly}</>}
              </button>
              <button className="ad-button" disabled={!isRunning} title={copy.toDesktopTitle}
                onClick={() => void pasteFromMyClipboard()}>
                <ClipboardPaste size={14} /> {copy.toDesktop}
              </button>
              <button className="ad-button" disabled={!isRunning || !desktopClipboard} title={copy.fromDesktopTitle}
                onClick={copyFromDesktop}>
                <ClipboardCopy size={14} /> {copy.fromDesktop}
              </button>
              <button className="ad-button" disabled={!isRunning} onClick={() => setAttempt(a => a + 1)}>
                <RefreshCw size={14} /> {copy.reconnect}
              </button>
            </div>
          </div>
          {manualPaste !== null && (
            <form className="ad-paste" onSubmit={e => {
              e.preventDefault();
              if (manualPaste && sendToDesktop(manualPaste)) setManualPaste(null);
            }}>
              <textarea className="ad-input" rows={2} autoFocus value={manualPaste}
                aria-label={copy.clipboardPlaceholder} placeholder={copy.clipboardPlaceholder}
                onChange={e => setManualPaste(e.target.value)} />
              <button className="ad-button is-primary" type="submit" disabled={!manualPaste}>{copy.clipboardSend}</button>
            </form>
          )}
          {clipboardNote && <p className="ad-muted" role="status">{clipboardNote}</p>}
          {selected && isRunning ? (
            <DesktopViewer ref={viewer} desktopKey={selected} viewOnly={viewOnly} attempt={attempt} screen={screen}
              copy={copy} onDesktopClipboard={setDesktopClipboard} />
          ) : (
            <div className="ad-screen-wrap"><div className="ad-screen-overlay">{copy.notRunning}</div></div>
          )}
        </section>

        <section className="ad-card ad-agent">
          {isOpenbot ? <OpenBotConnect desktopKey={selected} copy={copy} onSendToDesktop={sendToDesktop} /> : <>
          <div className="ad-agent-form">
            <label className="ad-label">
              {copy.runtime}
              <select className="ad-input" value={runtime} onChange={e => setRuntime(e.target.value)}>
                {(listing?.runtimes ?? []).map(r => <option key={r.id} value={r.id}>{r.title}</option>)}
              </select>
            </label>
            <textarea
              className="ad-input ad-task"
              value={task}
              onChange={e => setTask(e.target.value)}
              placeholder={copy.taskPlaceholder}
              aria-label={copy.taskPlaceholder}
              rows={4}
              onKeyDown={e => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && task.trim() && selected && !busy) void sendTask();
              }}
            />
            <div className="ad-agent-actions">
              {busy ? (
                <button className="ad-button" onClick={() => void act('cancel', () =>
                  post(`/api/agent-desktops/${encodeURIComponent(selected)}/turns/cancel`, { runtime }))}>
                  <StopCircle size={14} /> {copy.cancel}
                </button>
              ) : (
                <button className="ad-button is-primary" disabled={!task.trim() || !selected || !!pending}
                  onClick={() => void sendTask()}>
                  <Send size={14} /> {copy.send}
                </button>
              )}
            </div>
          </div>
          <h2 className="ad-section-title">{copy.transcript}</h2>
          <div className="ad-transcript">
            {!transcript.length && <p className="ad-muted">{copy.noEvents}</p>}
            {transcript.map(item => <TranscriptRow key={item.key} item={item} copy={copy} login={runtimeInfo?.login ?? ''} />)}
            <div ref={transcriptEnd} />
          </div>
          </>}
        </section>
      </div>
    </div>
  );
}

type Copy = typeof COPY[Lang];

function TranscriptRow({ item, copy, login }: { item: TranscriptItem; copy: Copy; login: string }) {
  switch (item.kind) {
    case 'task':
      return <div className="ad-row is-task"><span className="ad-tag">{item.runtime}</span>{item.text}</div>;
    case 'message':
      return <div className="ad-row is-message">{item.text}</div>;
    case 'tool':
      return (
        <div className={`ad-row is-tool status-${item.status || 'pending'}`}>
          <span className="ad-tag">{item.toolKind || 'tool'}</span>{item.title}
          <span className="ad-muted"> · {item.status}</span>
        </div>
      );
    case 'plan':
      return (
        <ul className="ad-row is-plan">
          {item.entries.map((entry, index) => (
            <li key={index} className={`status-${entry.status}`}>{entry.content}</li>
          ))}
        </ul>
      );
    case 'permission':
      return (
        <div className={`ad-row is-permission state-${item.state}`}>
          <span className="ad-tag">{item.taskId || 'control plane'}</span>
          {item.title} — {item.state === 'pending' ? copy.awaiting : item.state === 'granted' ? copy.granted : copy.refused}
          {item.reason && <span className="ad-muted"> ({item.reason})</span>}
        </div>
      );
    case 'end':
      if (item.error) {
        return (
          <div className="ad-row is-error">
            {item.needsLogin ? <>{copy.loginHint} <code>{login}</code></> : item.error}
          </div>
        );
      }
      return <div className="ad-row is-end">{item.stopReason === 'cancelled' ? copy.cancelled : copy.done}</div>;
  }
}
