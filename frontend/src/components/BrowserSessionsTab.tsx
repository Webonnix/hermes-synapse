import { useCallback, useEffect, useState, type FormEvent, type MouseEvent, type WheelEvent } from 'react';
import { ArrowLeft, CornerDownLeft, Globe, LogIn, RefreshCw, Save, Trash2 } from 'lucide-react';

type Props = { language: 'ru' | 'en' };

type SavedLogin = {
  name: string;
  label: string;
  start_url: string;
  created_at?: number | null;
  last_login_at?: number | null;
  last_used_at?: number | null;
};

type LoginFrame = {
  open: boolean;
  profile?: string;
  url?: string;
  title?: string;
  screenshot_b64?: string | null;
};

const FRAME_POLL_MS = 1200;

const PRESETS: { name: string; label: string; url: string }[] = [
  { name: 'instagram', label: 'Instagram', url: 'https://www.instagram.com/accounts/login/' },
  { name: 'facebook', label: 'Facebook', url: 'https://www.facebook.com/login/' },
  { name: 'vk', label: 'ВКонтакте', url: 'https://vk.com/' },
  { name: 'tiktok', label: 'TikTok', url: 'https://www.tiktok.com/login' },
  { name: 'linkedin', label: 'LinkedIn', url: 'https://www.linkedin.com/login' },
  { name: 'x', label: 'X (Twitter)', url: 'https://x.com/i/flow/login' },
];

const COPY = {
  ru: {
    title: 'Браузер: сохранённые входы',
    hint: 'Вы сами входите на сайт в окне ниже — пароль и SMS-код идут прямо на страницу, агенту и в чат они не попадают. После этого Vexa работает в вашей сессии: скажите, например, «зайди в мой инстаграм и посмотри директ».',
    saved: 'Сохранённые входы', none: 'Пока нет сохранённых входов.', newLogin: 'Новый вход',
    name: 'Имя (латиница)', url: 'Адрес страницы входа', start: 'Открыть окно входа',
    reopen: 'Войти заново', remove: 'Удалить', removeConfirm: 'Удалить сохранённый вход? Сайт попросит войти снова.',
    lastLogin: 'вход', lastUse: 'использован', never: '—',
    windowTitle: 'Окно входа', save: 'Готово — сохранить вход', back: 'Назад', go: 'Открыть',
    typeHint: 'Кликните по полю на картинке, затем введите текст здесь', send: 'Ввести', enter: 'Enter',
    loading: 'Страница загружается…', idleNote: 'Окно закроется само через 10 минут без действий; вход при этом сохранится.',
  },
  en: {
    title: 'Browser: saved logins',
    hint: 'You sign in to the site yourself in the window below — the password and SMS code go straight to the page, never to the agent or the chat. Vexa then works in your session: e.g. "open my Instagram and check the DMs".',
    saved: 'Saved logins', none: 'No saved logins yet.', newLogin: 'New login',
    name: 'Name (latin)', url: 'Sign-in page address', start: 'Open login window',
    reopen: 'Sign in again', remove: 'Delete', removeConfirm: 'Delete this saved login? The site will ask you to sign in again.',
    lastLogin: 'signed in', lastUse: 'used', never: '—',
    windowTitle: 'Login window', save: 'Done — save login', back: 'Back', go: 'Open',
    typeHint: 'Click a field in the picture, then type here', send: 'Type', enter: 'Enter',
    loading: 'Loading the page…', idleNote: 'The window closes itself after 10 idle minutes; the login is kept.',
  },
} as const;

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error((payload as { detail?: string }).detail || `HTTP ${response.status}`);
  return payload as T;
}

function postJson<T>(url: string, body: unknown): Promise<T> {
  return api<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

function when(ts: number | null | undefined, fallback: string) {
  if (!ts) return fallback;
  return new Intl.DateTimeFormat(undefined, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }).format(ts * 1000);
}

export function BrowserSessionsTab({ language }: Props) {
  const copy = COPY[language];
  const [logins, setLogins] = useState<SavedLogin[]>([]);
  const [openProfile, setOpenProfile] = useState<string | null>(null);
  const [frame, setFrame] = useState<LoginFrame | null>(null);
  const [name, setName] = useState('instagram');
  const [url, setUrl] = useState(PRESETS[0].url);
  const [label, setLabel] = useState(PRESETS[0].label);
  const [text, setText] = useState('');
  const [address, setAddress] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const loadLogins = useCallback(async () => {
    try {
      const result = await api<{ profiles: SavedLogin[]; login_open: string | null }>('/api/browser/sessions');
      setLogins(result.profiles);
      setOpenProfile(result.login_open);
      setError('');
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    }
  }, []);

  useEffect(() => { void loadLogins(); }, [loadLogins]);

  useEffect(() => {
    if (!openProfile) {
      setFrame(null);
      return undefined;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await api<LoginFrame>('/api/browser/login/frame');
        if (cancelled) return;
        if (!next.open) {
          setOpenProfile(null);
          void loadLogins();
          return;
        }
        setFrame(current => (next.screenshot_b64 || !current ? next : { ...current, url: next.url || current.url }));
      } catch {
        /* keep the last frame; the next poll retries */
      }
    };
    void poll();
    const interval = window.setInterval(() => void poll(), FRAME_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [openProfile, loadLogins]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      setError('');
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : String(actionError));
    } finally {
      setBusy(false);
    }
  };

  const startLogin = (profile: string, startUrl: string, profileLabel?: string) => run(async () => {
    await postJson('/api/browser/login/start', { profile, url: startUrl, label: profileLabel || undefined });
    setOpenProfile(profile);
  });

  const stopLogin = () => run(async () => {
    await postJson('/api/browser/login/stop', {});
    setOpenProfile(null);
    await loadLogins();
  });

  const removeLogin = (profile: string) => {
    if (!window.confirm(copy.removeConfirm)) return;
    void run(async () => {
      await api(`/api/browser/sessions/${encodeURIComponent(profile)}`, { method: 'DELETE' });
      await loadLogins();
    });
  };

  const send = (event: Record<string, unknown>) => postJson('/api/browser/login/input', event)
    .catch(inputError => setError(inputError instanceof Error ? inputError.message : String(inputError)));

  const clickFrame = (event: MouseEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    void send({ type: 'click', x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height });
  };

  const scrollFrame = (event: WheelEvent<HTMLDivElement>) => {
    void send({ type: 'scroll', dy: Math.max(-2000, Math.min(2000, event.deltaY)) });
  };

  const submitText = (event: FormEvent) => {
    event.preventDefault();
    if (!text) return;
    const value = text;
    setText('');
    void send({ type: 'type', text: value });
  };

  const submitAddress = (event: FormEvent) => {
    event.preventDefault();
    // An empty address reloads the current page — a first visit sometimes paints blank.
    const target = address.trim() || frame?.url || '';
    if (target) void send({ type: 'navigate', url: target });
  };

  const pickPreset = (presetName: string) => {
    const preset = PRESETS.find(item => item.name === presetName);
    if (!preset) return;
    setName(preset.name);
    setUrl(preset.url);
    setLabel(preset.label);
  };

  return (
    <div className="browser-sessions">
      <header className="browser-sessions-head">
        <Globe size={18} />
        <div><h3>{copy.title}</h3><p>{copy.hint}</p></div>
        <button type="button" className="icon-btn" onClick={() => void loadLogins()} aria-label="Refresh"><RefreshCw size={15} /></button>
      </header>

      {error && <div className="control-alert">{error}</div>}

      {openProfile ? (
        <section className="browser-login-window" aria-label={copy.windowTitle}>
          <div className="browser-login-bar">
            <button type="button" onClick={() => void send({ type: 'back' })} aria-label={copy.back} title={copy.back}><ArrowLeft size={15} /></button>
            <form onSubmit={submitAddress}>
              <input value={address} onChange={event => setAddress(event.target.value)} placeholder={frame?.url || ''} aria-label="URL" />
              <button type="submit">{copy.go}</button>
            </form>
            <strong>{openProfile}</strong>
            <button type="button" className="browser-login-save" disabled={busy} onClick={() => void stopLogin()}><Save size={15} />{copy.save}</button>
          </div>
          <div className="browser-login-screen" onWheel={scrollFrame}>
            {frame?.screenshot_b64
              ? <img src={`data:image/jpeg;base64,${frame.screenshot_b64}`} alt={frame.title || copy.windowTitle} onClick={clickFrame} draggable={false} />
              : <p>{copy.loading}</p>}
          </div>
          <form className="browser-login-type" onSubmit={submitText}>
            <input value={text} onChange={event => setText(event.target.value)} placeholder={copy.typeHint} autoComplete="off" aria-label={copy.typeHint} />
            <button type="submit" disabled={!text}>{copy.send}</button>
            <button type="button" onClick={() => void send({ type: 'key', key: 'Enter' })}><CornerDownLeft size={14} />{copy.enter}</button>
            <button type="button" onClick={() => void send({ type: 'key', key: 'Tab' })}>Tab</button>
            <button type="button" onClick={() => void send({ type: 'key', key: 'Backspace' })}>⌫</button>
          </form>
          <small className="browser-login-note">{copy.idleNote}</small>
        </section>
      ) : (
        <section className="browser-login-new" aria-label={copy.newLogin}>
          <h4>{copy.newLogin}</h4>
          <div className="browser-login-presets">
            {PRESETS.map(preset => (
              <button key={preset.name} type="button" className={name === preset.name ? 'is-active' : ''} onClick={() => pickPreset(preset.name)}>{preset.label}</button>
            ))}
          </div>
          <div className="browser-login-fields">
            <label><span>{copy.name}</span><input value={name} onChange={event => { setName(event.target.value.toLowerCase()); setLabel(''); }} pattern="[a-z0-9][a-z0-9_-]{0,31}" /></label>
            <label><span>{copy.url}</span><input value={url} onChange={event => setUrl(event.target.value)} /></label>
            <button type="button" disabled={busy || !name || !url} onClick={() => void startLogin(name, url, label)}><LogIn size={15} />{copy.start}</button>
          </div>
        </section>
      )}

      <section className="browser-login-list" aria-label={copy.saved}>
        <h4>{copy.saved}</h4>
        {!logins.length && <p className="control-empty">{copy.none}</p>}
        {logins.map(item => (
          <div key={item.name} className="browser-login-row">
            <span><strong>{item.label}</strong><small>{item.name} · {item.start_url}</small></span>
            <small>{copy.lastLogin}: {when(item.last_login_at, copy.never)} · {copy.lastUse}: {when(item.last_used_at, copy.never)}</small>
            <button type="button" disabled={busy || Boolean(openProfile)} onClick={() => void startLogin(item.name, item.start_url || 'https://', item.label)}><LogIn size={14} />{copy.reopen}</button>
            <button type="button" className="is-danger" disabled={busy || Boolean(openProfile)} onClick={() => removeLogin(item.name)} aria-label={copy.remove} title={copy.remove}><Trash2 size={14} /></button>
          </div>
        ))}
      </section>
    </div>
  );
}
