import { useEffect, useState } from 'react';
import { MessageSquare } from 'lucide-react';
import type { InboxConversation } from '../smm/dialogs/inboxTypes';
import { HANDLING_LABELS, PLATFORM_LABELS, formatWhen, inboxApi, openInboxConversation } from '../smm/dialogs/inboxApi';

/** «Разговоры» in a client's card: every inbox conversation linked to this client,
 *  on any channel, each one a click away from its thread in SMM Factory → Диалоги. */
export function ClientConversationsPane({ clientId }: { clientId: string }) {
  const [items, setItems] = useState<InboxConversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    inboxApi.clientConversations(clientId)
      .then(result => { if (!cancelled) { setItems(result); setError(''); } })
      .catch(exc => {
        if (cancelled) return;
        setItems([]);
        setError(exc.status === 403 ? 'Нет доступа к диалогам.' : (exc as Error).message);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [clientId]);

  if (loading) return <div className="admin-empty">Загрузка разговоров…</div>;
  if (error) return <div className="cl-error-banner" role="alert">{error}</div>;
  if (items.length === 0) {
    return (
      <div className="admin-empty-cta">
        <MessageSquare size={22} />
        <strong>Разговоров пока нет</strong>
        <span>Когда клиент напишет в подключённый канал, переписка появится здесь и в SMM Factory → «Диалоги».</span>
      </div>
    );
  }
  return (
    <div className="admin-item-list">
      {items.map(item => (
        <div key={item.id} className="admin-item-row" style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <strong style={{ fontSize: '0.85rem' }}>
              {PLATFORM_LABELS[item.platform] || item.platform} · {item.display_name || item.chat_id}
            </strong>
            <span className="cl-muted" style={{ display: 'block', fontSize: '0.76rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {formatWhen(item.last_message_at)} · {item.last_message_preview || '—'}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
            <span className="admin-status-chip">{HANDLING_LABELS[item.handling]}</span>
            {item.unread_count > 0 && <span className="admin-status-chip is-active">{item.unread_count} новых</span>}
            <button type="button" className="btn-ghost" onClick={() => openInboxConversation(item.id)}>Открыть</button>
          </div>
        </div>
      ))}
    </div>
  );
}
