import { useEffect, useRef, useState } from 'react';

export function ReauthDialog({ language }: { language: string }) {
  const complete = useRef<((ok: boolean) => void) | null>(null);
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const en = language === 'en';

  useEffect(() => {
    const request = (event: Event) => {
      complete.current = (event as CustomEvent<(ok: boolean) => void>).detail;
      setPassword(''); setCode(''); setError(''); setOpen(true);
    };
    window.addEventListener('hermes-reauth', request);
    return () => { window.removeEventListener('hermes-reauth', request); complete.current?.(false); };
  }, []);

  const close = (ok: boolean) => {
    complete.current?.(ok); complete.current = null;
    setPassword(''); setCode(''); setOpen(false);
  };
  const run = async (url: string, payload?: object) => {
    setBusy(true); setError('');
    try {
      const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        ...(payload ? { body: JSON.stringify(payload) } : {}) });
      const data = await response.json();
      if (!response.ok || data.status !== 'success') throw new Error(data.message || data.detail || (en ? 'Verification failed' : 'Не удалось подтвердить вход'));
      if (payload) close(true);
    } catch (exc) { setError((exc as Error).message); }
    finally { setBusy(false); }
  };
  if (!open) return null;
  return <div style={{ position: 'fixed', inset: 0, zIndex: 10000, background: '#000b', display: 'grid', placeItems: 'center' }}>
    <form role="dialog" aria-modal="true" aria-labelledby="reauth-title"
      onSubmit={event => { event.preventDefault(); void run('/api/auth/reauth', { password, code }); }}
      style={{ background: '#111827', color: '#fff', padding: 28, borderRadius: 16, width: 'min(420px, 90vw)', display: 'grid', gap: 16 }}>
      <h2 id="reauth-title">{en ? 'Confirm your identity' : 'Подтвердите вход'}</h2>
      <p>{en ? 'This action requires your password and a fresh Telegram code.' : 'Для этого действия нужны пароль и свежий код из Telegram.'}</p>
      <label>{en ? 'Password (if configured)' : 'Пароль (если настроен)'}<input autoFocus type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></label>
      <button type="button" disabled={busy} onClick={() => void run('/api/auth/request-code')}>{en ? 'Send Telegram code' : 'Получить код в Telegram'}</button>
      <label>{en ? 'Telegram code' : 'Код из Telegram'}<input inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required value={code} onChange={e => setCode(e.target.value)} /></label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy}>{en ? 'Confirm' : 'Подтвердить'}</button>
      <button type="button" disabled={busy} onClick={() => close(false)}>{en ? 'Cancel' : 'Отмена'}</button>
    </form>
  </div>;
}
