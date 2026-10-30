import React from 'react';
import { Share2, Link2, X as XIcon } from 'lucide-react';

/**
 * Publish / revoke a public read-only snapshot of the current conversation.
 *
 * The snapshot is built and sanitized server-side (backend/shares.py); this is
 * only the control surface. It deliberately shows the live state — "published"
 * vs "not published" — because a share the owner has forgotten about is the
 * failure mode worth designing against.
 */

export function authHeaders(): Record<string, string> {

  return { 'Content-Type': 'application/json',  };
}

export interface ShareState {
  token: string | null;
  url?: string;
  message_count?: number;
  created_at?: string;
}

interface Props {
  chatId: string;
  /** Injected in tests; defaults to the real fetch. */
  fetchImpl?: typeof fetch;
  onNotify?: (message: string) => void;
}

export const ShareConversation: React.FC<Props> = ({ chatId, fetchImpl, onNotify }) => {
  const doFetch = fetchImpl || fetch;
  const [share, setShare] = React.useState<ShareState>({ token: null });
  const [busy, setBusy] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const res = await doFetch(`/api/share/${encodeURIComponent(chatId)}`, { headers: authHeaders() });
      const data = await res.json();
      // Guard the shape: an error payload is an object too, and trusting it
      // blindly is how a failed request ends up rendered as real state.
      setShare(data && typeof data === 'object' ? { token: data.token ?? null, ...data } : { token: null });
    } catch {
      setShare({ token: null });
    }
  }, [chatId, doFetch]);

  React.useEffect(() => { void load(); }, [load]);

  const publish = async () => {
    setBusy(true);
    try {
      const res = await doFetch(`/api/share/${encodeURIComponent(chatId)}`, {
        method: 'POST', headers: authHeaders(),
      });
      const data = await res.json();
      if (data && data.token) {
        setShare(data);
        onNotify?.('Публичная ссылка создана. Прежняя, если была, больше не работает.');
      }
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    setBusy(true);
    try {
      await doFetch(`/api/share/${encodeURIComponent(chatId)}`, { method: 'DELETE', headers: authHeaders() });
      setShare({ token: null });
      onNotify?.('Ссылка отозвана — страница больше не открывается.');
    } finally {
      setBusy(false);
    }
  };

  const fullUrl = share.token ? `${window.location.origin}/share/${share.token}` : '';

  const chip: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: '8px', marginLeft: '8px',
    background: 'rgba(255,255,255,0.02)', padding: '6px 12px', borderRadius: '8px',
    border: '1px solid rgba(255,255,255,0.06)',
  };
  const button: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: '6px', background: 'transparent',
    border: 'none', color: 'var(--accent-cyan)', cursor: busy ? 'wait' : 'pointer',
    fontSize: '0.74rem', padding: 0,
  };

  if (!share.token) {
    return (
      <div style={chip}>
        <button type="button" style={button} onClick={publish} disabled={busy}
                title="Опубликовать неизменяемый снимок диалога по публичной ссылке">
          <Share2 size={13} /> Поделиться
        </button>
      </div>
    );
  }

  return (
    <div style={chip}>
      <Link2 size={13} color="var(--accent-cyan)" />
      <a href={fullUrl} target="_blank" rel="noopener noreferrer"
         style={{ fontSize: '0.74rem', color: 'var(--accent-cyan)', textDecoration: 'none' }}>
        Публичная ссылка
      </a>
      <button type="button" style={{ ...button, color: 'var(--text-dim)' }} onClick={revoke}
              disabled={busy} title="Отозвать ссылку">
        <XIcon size={13} />
      </button>
    </div>
  );
};

export default ShareConversation;
