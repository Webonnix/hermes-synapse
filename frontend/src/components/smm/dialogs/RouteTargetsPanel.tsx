import { useEffect, useState } from 'react';
import { Bot, Mic } from 'lucide-react';
import type { RouteTarget } from './inboxTypes';
import { inboxApi } from './inboxApi';
import './inbox.css';

/**
 * Shown on the AI Router screen: who a *conversation* can be routed to. The tier
 * chain below it decides which model serves each call; this list decides which
 * executor answers the turn — an agent, or an external voice target.
 */
export function RouteTargetsPanel({ onOpenVoice }: { onOpenVoice: () => void }) {
  const [targets, setTargets] = useState<RouteTarget[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    inboxApi.targets().then(setTargets).catch(exc => setError((exc as Error).message));
  }, []);

  const agents = targets.filter(t => t.type === 'subagent');
  const voice = targets.filter(t => t.type === 'external_voice');

  return (
    <div className="glass-panel" style={{ padding: 20, marginBottom: 16 }}>
      <div className="admin-section-head">
        <div>
          <h3>Маршрутизация разговоров</h3>
          <p>
            Кому диалоги SMM Factory могут передать ход разговора: агенту или внешнему голосовому провайдеру.
            Модель для каждого вызова агента по-прежнему выбирает цепочка тиров ниже.
          </p>
        </div>
        <button type="button" className="btn-ghost" onClick={onOpenVoice}><Mic size={14} /><span>Внешний голос</span></button>
      </div>
      {error && <div className="cl-error-banner" role="alert">{error}</div>}
      <div className="rt-targets">
        <div className="rt-target is-subagent">
          <strong><Bot size={13} /> Агенты</strong>
          <small>{agents.filter(a => a.available).length} активных из {agents.length}</small>
        </div>
        {voice.map(target => (
          <div key={target.id} className="rt-target is-external_voice">
            <strong><Mic size={13} /> {target.name}</strong>
            <small>{target.capability_mode} · {target.provider_name}</small>
            <small>{target.available ? (target.health?.ok === false ? 'последняя проверка: ошибка' : 'доступна') : 'недоступна'}</small>
          </div>
        ))}
        {voice.length === 0 && (
          <div className="rt-target is-external_voice"><small>Внешних голосовых целей нет</small></div>
        )}
      </div>
    </div>
  );
}
