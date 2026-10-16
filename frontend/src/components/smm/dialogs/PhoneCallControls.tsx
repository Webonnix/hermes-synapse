import { useCallback, useEffect, useState } from 'react';
import { PhoneCall, PhoneForwarded, PhoneOff } from 'lucide-react';
import type { InboxConversation, PhoneCallRecord } from './inboxTypes';
import { CALL_END_LABELS, CALL_STATUS_LABELS, INBOX_UPDATE_EVENT, formatDuration, formatWhen, phoneApi } from './inboxApi';
import { CallDialog } from './CallDialog';

/** Calls of a phone conversation, with live control of the one in progress. */
export function PhoneCallControls({ conversation, canControl }: { conversation: InboxConversation; canControl: boolean }) {
  const [calls, setCalls] = useState<PhoneCallRecord[]>([]);
  const [error, setError] = useState('');
  const [dialog, setDialog] = useState(false);

  const load = useCallback(async () => {
    try {
      setCalls(await phoneApi.calls({ conversation_id: conversation.id, limit: 5 }));
    } catch {
      setCalls([]);
    }
  }, [conversation.id]);

  useEffect(() => {
    void load();
    const onUpdate = (event: Event) => {
      if ((event as CustomEvent<{ conversation_id?: string }>).detail?.conversation_id === conversation.id) void load();
    };
    window.addEventListener(INBOX_UPDATE_EVENT, onUpdate);
    const timer = setInterval(() => void load(), 10000);
    return () => { window.removeEventListener(INBOX_UPDATE_EVENT, onUpdate); clearInterval(timer); };
  }, [conversation.id, load]);

  const active = calls.find(call => call.status === 'active' || call.status === 'ringing');
  const last = calls[0];
  const act = async (action: () => Promise<unknown>) => {
    setError('');
    try {
      await action();
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <section className="ibx-side-block">
      <h4><PhoneCall size={13} /> Звонки</h4>
      {active ? (
        <>
          <div className="ph-live"><i className="dot" /> {CALL_STATUS_LABELS[active.status]} · {formatWhen(active.started_at)}</div>
          {canControl && active.status === 'active' && (
            <div className="ph-actions">
              <button type="button" className="btn-ghost" onClick={() => void act(() => phoneApi.transferCall(active.id))}>
                <PhoneForwarded size={13} /><span>Перевести на меня</span>
              </button>
              <button type="button" className="btn-ghost danger" onClick={() => void act(() => phoneApi.hangupCall(active.id))}>
                <PhoneOff size={13} /><span>Завершить</span>
              </button>
            </div>
          )}
        </>
      ) : last ? (
        <div className="cl-muted">
          Последний: {CALL_STATUS_LABELS[last.status] || last.status}
          {last.end_reason ? ` (${CALL_END_LABELS[last.end_reason] || last.end_reason})` : ''}, {formatDuration(last.duration_s)}
          {last.first_audio_ms_avg !== null ? `, отклик ${(last.first_audio_ms_avg / 1000).toFixed(1)} с` : ''}
        </div>
      ) : <span className="cl-muted">Звонков не было</span>}
      {!active && conversation.chat_id.startsWith('+') && (
        <button type="button" className="btn-ghost" onClick={() => setDialog(true)}><PhoneCall size={13} /><span>Перезвонить через ИИ</span></button>
      )}
      {error && <div className="cl-error-banner" role="alert">{error}</div>}
      {dialog && (
        <CallDialog defaultNumber={conversation.chat_id} defaultLineId={conversation.binding_id} clientId={conversation.client_id}
                    onClose={() => { setDialog(false); void load(); }} />
      )}
    </section>
  );
}
