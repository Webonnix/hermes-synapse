import { useEffect, useState } from 'react';
import { PhoneCall, X } from 'lucide-react';
import type { PhoneLine } from './inboxTypes';
import { openInboxConversation, phoneApi } from './inboxApi';

/**
 * «Позвонить»: the AI agent of the chosen line calls a number and talks with the person.
 * Used from a client card, from a phone conversation and from «Звонки» (SMM Factory → Диалоги).
 */
export function CallDialog({ defaultNumber = '', defaultLineId = '', clientId = null, onClose }: {
  defaultNumber?: string;
  defaultLineId?: string;
  clientId?: string | null;
  onClose: () => void;
}) {
  const [lines, setLines] = useState<PhoneLine[]>([]);
  const [canCall, setCanCall] = useState(true);
  const [lineId, setLineId] = useState(defaultLineId);
  const [number, setNumber] = useState(defaultNumber);
  const [purpose, setPurpose] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [started, setStarted] = useState<{ conversationId: string | null; number: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    phoneApi.overview()
      .then(overview => {
        if (cancelled) return;
        const usable = overview.lines.filter(line => line.enabled && line.outbound_enabled && line.subagent_id);
        setLines(usable);
        setCanCall(overview.can_call);
        if (!defaultLineId || !usable.some(line => line.id === defaultLineId)) setLineId(usable[0]?.id || '');
      })
      .catch(exc => { if (!cancelled) setError((exc as Error).message); });
    return () => { cancelled = true; };
  }, [defaultLineId]);

  const submit = async () => {
    setBusy(true);
    setError('');
    try {
      const call = await phoneApi.startCall({ line_id: lineId, to: number, purpose, client_id: clientId });
      setStarted({ conversationId: call.conversation_id, number: call.remote_number });
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ibx-modal-backdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="ibx-modal glass-panel" role="dialog" aria-modal="true" aria-label="Позвонить">
        <header>
          <h3><PhoneCall size={16} /> Звонок через ИИ-агента</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть"><X size={16} /></button>
        </header>
        {started ? (
          <div className="ibx-modal-body">
            <p>Звоним на {started.number}. Разговор и расшифровка появятся в SMM Factory → «Диалоги».</p>
            {started.conversationId && (
              <button type="button" className="btn-primary" onClick={() => { openInboxConversation(started.conversationId!); onClose(); }}>
                Открыть разговор
              </button>
            )}
          </div>
        ) : (
          <div className="ibx-modal-body">
            {!canCall && <div className="cl-error-banner" role="alert">У вашей роли нет права на исходящие звонки.</div>}
            {canCall && lines.length === 0 && !error && (
              <div className="admin-empty">Нет линии с включёнными исходящими звонками и назначенным агентом — настройте в SMM Factory → «Диалоги» → «Звонки».</div>
            )}
            <label className="ph-field">Линия
              <select className="form-input" value={lineId} onChange={e => setLineId(e.target.value)} aria-label="Линия">
                {lines.map(line => <option key={line.id} value={line.id}>{line.name} · {line.number || line.provider_label}</option>)}
              </select>
            </label>
            <label className="ph-field">Номер
              <input className="form-input" value={number} onChange={e => setNumber(e.target.value)} placeholder="+972501234567" aria-label="Номер" />
            </label>
            <label className="ph-field">Зачем звоним (агент скажет это собеседнику)
              <textarea className="form-input" rows={3} value={purpose} onChange={e => setPurpose(e.target.value)}
                        placeholder="Подтвердить запись на четверг в 15:00" aria-label="Цель звонка" />
            </label>
            {error && <div className="cl-error-banner" role="alert">{error}</div>}
            <div className="ibx-modal-actions">
              <button type="button" className="btn-ghost" onClick={onClose}>Отмена</button>
              <button type="button" className="btn-primary" disabled={busy || !canCall || !lineId || !number.trim()} onClick={() => void submit()}>
                <PhoneCall size={14} /><span>{busy ? 'Звоню…' : 'Позвонить'}</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
