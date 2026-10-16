import { useEffect, useState } from 'react';
import { Briefcase, Mic, Pin, PinOff, ShieldBan, UserPlus } from 'lucide-react';
import type { InboxConversation, InboxMeta, RouteState, RouteTarget } from './inboxTypes';
import { inboxApi } from './inboxApi';
import { PhoneCallControls } from './PhoneCallControls';

const SOURCE_LABELS: Record<string, string> = { auto: 'авто', manual: 'закреплён вручную', fallback: 'резервный' };

export function ConversationSidePanel({
  conversation, meta, route, targets, onLinked, onRouteChanged, onBlocked, onOpenClient,
}: {
  conversation: InboxConversation | null;
  meta: InboxMeta | null;
  route: RouteState | null;
  targets: RouteTarget[];
  onLinked: (conversation: InboxConversation) => void;
  onRouteChanged: (route: RouteState) => void;
  onBlocked: () => void;
  onOpenClient?: (clientId: string) => void;
}) {
  const [clientSearch, setClientSearch] = useState('');
  const [found, setFound] = useState<Array<{ id: string; name: string }>>([]);
  const [tasks, setTasks] = useState<Array<{ id: string; title: string; status: string; overdue?: boolean }>>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [targetKey, setTargetKey] = useState('');

  useEffect(() => {
    setClientSearch('');
    setFound([]);
    setError('');
    setTargetKey('');
  }, [conversation?.id]);

  useEffect(() => {
    const clientId = conversation?.client_id;
    if (!clientId) {
      setTasks([]);
      return;
    }
    let cancelled = false;
    inboxApi.clientTasks(clientId)
      .then(items => { if (!cancelled) setTasks(items.filter(task => task.status === 'OPEN').slice(0, 5)); })
      .catch(() => { if (!cancelled) setTasks([]); });
    return () => { cancelled = true; };
  }, [conversation?.client_id]);

  useEffect(() => {
    const needle = clientSearch.trim();
    if (needle.length < 2) {
      setFound([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      inboxApi.searchClients(needle)
        .then(result => { if (!cancelled) setFound(result.items.slice(0, 8)); })
        .catch(() => { if (!cancelled) setFound([]); });
    }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [clientSearch]);

  if (!conversation) return <aside className="ibx-side glass-panel ibx-side-empty" />;

  const canReply = Boolean(meta?.can_reply);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pinnedKey = route?.pinned && route.target_type && route.target_id ? `${route.target_type}|${route.target_id}` : '';
  const lastTarget = targets.find(t => t.type === route?.last_target_type && t.id === route?.last_target_id);

  return (
    <aside className="ibx-side glass-panel" aria-label="Карточка разговора">
      <section className="ibx-side-block">
        <h4><Briefcase size={13} /> Клиент</h4>
        {conversation.client_id ? (
          <>
            <strong className="ibx-side-name">{conversation.client_name || conversation.client_id}</strong>
            {onOpenClient && (
              <button type="button" className="cl-link-btn" onClick={() => onOpenClient(conversation.client_id!)}>Открыть карточку</button>
            )}
            {tasks.length > 0 && (
              <ul className="ibx-side-tasks">
                {tasks.map(task => <li key={task.id} className={task.overdue ? 'is-overdue' : ''}>{task.title}</li>)}
              </ul>
            )}
          </>
        ) : (
          <>
            <span className="cl-muted">Не связан с клиентом CRM</span>
            {canReply && (
              <>
                <input className="form-input" value={clientSearch} onChange={e => setClientSearch(e.target.value)}
                       placeholder="Найти клиента" aria-label="Найти клиента" />
                {found.length > 0 && (
                  <ul className="ibx-side-found">
                    {found.map(client => (
                      <li key={client.id}>
                        <button type="button" className="btn-ghost" disabled={busy}
                                onClick={() => run(async () => onLinked(await inboxApi.linkClient(conversation.id, { client_id: client.id })))}>
                          {client.name}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {meta?.can_create_clients && (
                  <button type="button" className="btn-ghost" disabled={busy}
                          onClick={() => run(async () => onLinked(await inboxApi.linkClient(conversation.id, {
                            create: { name: conversation.display_name || conversation.chat_id },
                          })))}>
                    <UserPlus size={13} /><span>Создать клиента</span>
                  </button>
                )}
              </>
            )}
          </>
        )}
      </section>

      {conversation.platform === 'phone' && <PhoneCallControls conversation={conversation} canControl={canReply} />}

      <section className="ibx-side-block">
        <h4><Mic size={13} /> Кто отвечает</h4>
        <div className="cl-info-row"><span>Агент канала</span><strong>{conversation.agent_name || conversation.subagent_id || '—'}</strong></div>
        <div className="cl-info-row"><span>Режим ответов</span><strong>{conversation.response_mode === 'auto_labeled' ? 'сразу, с пометкой' : 'черновики'}</strong></div>
        {route?.last_source && (
          <div className="cl-info-row">
            <span>Последний ход</span>
            <strong>
              {lastTarget ? lastTarget.name : route.last_target_id} · {SOURCE_LABELS[route.last_source] || route.last_source}
            </strong>
          </div>
        )}
        {route?.last_reason && <div className="cl-hint">{route.last_reason}</div>}
        {route?.pinned && route.target && (
          <div className={`ibx-target-chip is-${route.target.type}`}>
            <Pin size={12} /> {route.target.name}
            {route.target.type === 'external_voice' && <em>{route.target.capability_mode} · {route.target.provider_name}</em>}
            {route.target.type === 'external_voice' && route.target.health?.ok === false && <em className="is-bad">недоступен</em>}
          </div>
        )}
        {canReply && (
          <div className="ibx-route-pick">
            <select className="form-input" aria-label="Маршрут" value={targetKey || pinnedKey}
                    onChange={e => setTargetKey(e.target.value)}>
              <option value="">Авто (агент канала)</option>
              <optgroup label="Агенты">
                {targets.filter(t => t.type === 'subagent').map(t => (
                  <option key={t.id} value={`subagent|${t.id}`} disabled={!t.available}>{t.name}{t.available ? '' : ' (отключён)'}</option>
                ))}
              </optgroup>
              {targets.some(t => t.type === 'external_voice') && (
                <optgroup label="Внешний голос">
                  {targets.filter(t => t.type === 'external_voice').map(t => (
                    <option key={t.id} value={`external_voice|${t.id}`} disabled={!t.available}>
                      {t.name} · {t.capability_mode}{t.available ? '' : ' (недоступен)'}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            <button type="button" className="btn-ghost" disabled={busy || (targetKey || pinnedKey) === pinnedKey}
                    onClick={() => run(async () => {
                      const key = targetKey;
                      if (!key) {
                        onRouteChanged(await inboxApi.unpinRoute(conversation.id));
                      } else {
                        const [target_type, target_id] = key.split('|');
                        onRouteChanged(await inboxApi.pinRoute(conversation.id, { target_type, target_id }));
                      }
                      setTargetKey('');
                    })}>
              {targetKey === '' && pinnedKey ? <><PinOff size={13} /><span>Снять</span></> : <><Pin size={13} /><span>Закрепить</span></>}
            </button>
          </div>
        )}
        {conversation.handling === 'human' && route?.pinned && (
          <div className="cl-hint">Маршрут сохранён и сработает после «Вернуть агенту».</div>
        )}
      </section>

      <section className="ibx-side-block">
        <h4>Канал</h4>
        <div className="cl-info-row"><span>ID чата</span><strong className="ibx-mono">{conversation.chat_id}</strong></div>
        {conversation.last_rule_pattern && (
          <div className="cl-info-row"><span>Правило</span><strong>{conversation.last_rule_action} · {conversation.last_rule_pattern}</strong></div>
        )}
        {meta?.can_view_financials && typeof conversation.cost_usd === 'number' && (
          <div className="cl-info-row"><span>Расход на ИИ</span><strong>${conversation.cost_usd.toFixed(4)}</strong></div>
        )}
        {meta?.can_edit_rules && conversation.handling !== 'blocked' && (
          <button type="button" className="btn-ghost danger" disabled={busy}
                  onClick={() => run(async () => {
                    if (!window.confirm('Заблокировать отправителя? Будет создано правило, его можно снять во вкладке «Правила».')) return;
                    await inboxApi.block(conversation.id);
                    onBlocked();
                  })}>
            <ShieldBan size={13} /><span>Заблокировать отправителя</span>
          </button>
        )}
      </section>

      {error && <div className="cl-error-banner" role="alert">{error}</div>}
    </aside>
  );
}
