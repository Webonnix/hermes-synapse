/**
 * Every conversation on every channel, in one list.
 *
 * The per-binding version of this lives inside a channel's card in
 * MessengerChannelsTab — useful when you are already looking at one account.
 * This is the other question, and the one an owner running several accounts
 * asks first: *who is talking to us right now, anywhere*. It is a separate
 * panel rather than a filter on the connections list because the answer is
 * ordered by conversation, not by account.
 *
 * The rows come from one backend call (GET /api/messenger-bindings/conversations)
 * instead of a request per channel — a dozen accounts would otherwise mean a
 * dozen round-trips every time the panel opens.
 */

import { useEffect, useState } from 'react';
import { Bot, Eraser, Hand, MessageSquare, RefreshCw } from 'lucide-react';
import { styles } from '../../styles';
import type { ConversationMessage, SmmConversation } from '../../types';
import { HANDLING_MODE_LABELS, PLATFORM_LABELS, authHeaders, type Platform } from './platforms';

const conversationUrl = (conversation: SmmConversation, suffix: string) =>
  `/api/messenger-bindings/${conversation.binding_id}/conversations/${conversation.platform}/${encodeURIComponent(conversation.chat_id)}/${suffix}`;

export function SmmDialogsPanel() {
  const [conversations, setConversations] = useState<SmmConversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [busyKey, setBusyKey] = useState('');
  const [openHistoryKey, setOpenHistoryKey] = useState('');
  const [historyMessages, setHistoryMessages] = useState<ConversationMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const fetchConversations = () => {
    fetch('/api/messenger-bindings/conversations', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setConversations(Array.isArray(data) ? data : []))
      .catch(() => setError('Не удалось загрузить диалоги'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchConversations();
    const interval = setInterval(fetchConversations, 15000);
    return () => clearInterval(interval);
  }, []);

  const keyOf = (conversation: SmmConversation) =>
    `${conversation.binding_id}|${conversation.platform}|${conversation.chat_id}`;

  const setMode = async (conversation: SmmConversation, action: 'enable_ai' | 'take_over') => {
    setBusyKey(keyOf(conversation));
    setError('');
    try {
      const response = await fetch(conversationUrl(conversation, 'takeover'), {
        method: 'POST', headers: authHeaders(), body: JSON.stringify({ action }),
      });
      if (!response.ok) throw new Error('Не удалось изменить режим диалога');
      fetchConversations();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось изменить режим диалога');
    } finally {
      setBusyKey('');
    }
  };

  const toggleHistory = async (conversation: SmmConversation) => {
    const key = keyOf(conversation);
    if (openHistoryKey === key) {
      setOpenHistoryKey('');
      return;
    }
    setOpenHistoryKey(key);
    setHistoryLoading(true);
    setHistoryMessages([]);
    try {
      const response = await fetch(conversationUrl(conversation, 'history?limit=200'), { headers: authHeaders() });
      const data = await response.json();
      setHistoryMessages(Array.isArray(data.messages) ? data.messages : []);
    } catch {
      setHistoryMessages([]);
    } finally {
      setHistoryLoading(false);
    }
  };

  const clearHistory = async (conversation: SmmConversation) => {
    if (!window.confirm(`Очистить переписку с «${conversation.display_name || conversation.chat_id}»? Агент забудет контекст — это необратимо.`)) return;
    setBusyKey(keyOf(conversation));
    try {
      await fetch(conversationUrl(conversation, 'history'), { method: 'DELETE', headers: authHeaders() });
      setOpenHistoryKey('');
      fetchConversations();
    } finally {
      setBusyKey('');
    }
  };

  const needle = search.trim().toLowerCase();
  const shown = needle
    ? conversations.filter(c => `${c.display_name} ${c.chat_id} ${c.agent_name} ${c.account}`.toLowerCase().includes(needle))
    : conversations;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <input
          className="form-input"
          style={{ flex: 1 }}
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder={`Поиск среди ${conversations.length} диалогов — имя, id чата, агент или аккаунт`}
        />
        <button type="button" className="btn-ghost" onClick={fetchConversations} title="Обновить">
          <RefreshCw size={14} /><span>Обновить</span>
        </button>
      </div>
      <p style={{ ...styles.formHelp, margin: 0 }}>
        Все переписки со всех подключённых аккаунтов, свежие сверху. 🤖 — пусть отвечает ИИ; ✋ — взять диалог
        на себя, бот замолчит; 💬 — показать переписку, которую помнит агент; 🧽 — очистить её. Переписка
        сохраняется, даже пока бот молчит, поэтому ИИ продолжит с того же места.
      </p>

      {error && <div className="glass-panel" style={{ padding: '10px 14px', color: 'var(--danger)', fontSize: '0.85rem' }}>{error}</div>}

      {loading ? (
        <span style={{ fontSize: '0.85rem', color: 'var(--text-dim, #8a94a6)' }}>Загрузка...</span>
      ) : shown.length === 0 ? (
        <span style={{ fontSize: '0.85rem', color: 'var(--text-dim, #8a94a6)' }}>
          {conversations.length === 0 ? 'Пока никто не писал ни в один канал.' : 'Никто не найден.'}
        </span>
      ) : (
        <div className="admin-item-list">
          {shown.map(conversation => {
            const key = keyOf(conversation);
            const busy = busyKey === key;
            const takenOver = conversation.handling_mode === 'owner_takeover';
            const aiOn = conversation.handling_mode === 'ai';
            return (
              <div key={conversation.id} className="admin-item-row" style={{ display: 'block' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
                  <div style={{ minWidth: 0 }}>
                    <strong style={{ fontSize: '0.85rem' }}>{conversation.display_name || conversation.chat_id}</strong>
                    <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)' }}>
                      {PLATFORM_LABELS[conversation.platform as Platform] || conversation.platform}
                      {' · '}{conversation.account || conversation.binding_id}
                      {' · '}{conversation.agent_name}
                    </span>
                    <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {conversation.messages_count > 0
                        ? `${conversation.messages_count} сообщ.${conversation.last_message_preview ? ` · ${conversation.last_message_role === 'assistant' ? 'бот' : 'он'}: ${conversation.last_message_preview}` : ''}`
                        : 'переписка пуста'}
                    </span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                    <span className={`admin-status-chip ${takenOver ? 'is-active' : ''}`}>
                      {HANDLING_MODE_LABELS[conversation.handling_mode] || conversation.handling_mode}
                    </span>
                    {!aiOn && (
                      <button type="button" className="icon-btn" title="Подключить ИИ к этому диалогу" disabled={busy}
                        onClick={() => void setMode(conversation, 'enable_ai')}>
                        <Bot size={14} />
                      </button>
                    )}
                    {!takenOver && (
                      <button type="button" className="icon-btn" title="Взять диалог на себя — бот замолчит" disabled={busy}
                        onClick={() => void setMode(conversation, 'take_over')}>
                        <Hand size={14} />
                      </button>
                    )}
                    <button type="button" className="icon-btn" title="Показать переписку" disabled={busy}
                      onClick={() => void toggleHistory(conversation)}>
                      <MessageSquare size={14} />
                    </button>
                    <button type="button" className="icon-btn danger" title="Очистить переписку" disabled={busy || conversation.messages_count === 0}
                      onClick={() => void clearHistory(conversation)}>
                      <Eraser size={14} />
                    </button>
                  </div>
                </div>

                {openHistoryKey === key && (
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid rgba(255,255,255,.07)' }}>
                    {historyLoading ? (
                      <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>Загрузка переписки...</span>
                    ) : historyMessages.length === 0 ? (
                      <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>
                        Пусто — агент ничего не помнит об этом человеке.
                      </span>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 320, overflowY: 'auto' }}>
                        {historyMessages.map(message => (
                          <div key={message.id} style={{
                            alignSelf: message.role === 'assistant' ? 'flex-end' : 'flex-start',
                            maxWidth: '82%', padding: '7px 10px', borderRadius: 10, fontSize: '0.8rem',
                            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                            background: message.role === 'assistant' ? 'rgba(46,179,139,.10)' : 'rgba(255,255,255,.04)',
                            border: `1px solid ${message.role === 'assistant' ? 'rgba(77,222,180,.22)' : 'rgba(255,255,255,.08)'}`,
                          }}>
                            <span style={{ display: 'block', fontSize: '0.68rem', color: 'var(--text-dim, #8a94a6)', marginBottom: 2 }}>
                              {message.role === 'assistant' ? 'Бот' : 'Собеседник'}
                              {message.timestamp ? ` · ${message.timestamp.slice(0, 16).replace('T', ' ')}` : ''}
                            </span>
                            {message.content}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default SmmDialogsPanel;
