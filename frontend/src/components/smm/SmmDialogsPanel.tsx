/**
 * Every conversation on every channel — navigated the way an owner thinks about
 * them: service → account → dialog.
 *
 * The per-binding version of this lives inside a channel's card in
 * MessengerChannelsTab — useful when you are already looking at one account.
 * This is the other question, and the one an owner running several accounts
 * asks first: *who is talking to us right now, anywhere*.
 *
 * A single flat list answered that badly once more than one account was
 * connected: nine Matrix rows all repeating the same account in grey 11px text
 * is not a structure, it is a wall. So the panel is three columns — the
 * services that actually have conversations, the accounts inside the chosen
 * service, and the dialogs inside the chosen account — with «Все» at the top of
 * both columns so the old cross-channel view is still one click away.
 *
 * Search stays global on purpose: it filters first, and the two columns are
 * built from what matched, so a name you half-remember also tells you which
 * account it was on. Selecting a service or account that the search has emptied
 * is possible but honest — the right pane says so rather than silently showing
 * someone else's dialogs.
 *
 * The rows come from one backend call (GET /api/messenger-bindings/conversations)
 * instead of a request per channel — a dozen accounts would otherwise mean a
 * dozen round-trips every time the panel opens.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  AtSign, Bot, Eraser, Facebook, Hand, Hash, Inbox, Instagram, Layers, Mail,
  MessageCircle, MessageSquare, RefreshCw, Send, Users, type LucideIcon,
} from 'lucide-react';
import { styles } from '../../styles';
import type { ConversationMessage, SmmConversation } from '../../types';
import { HANDLING_MODE_LABELS, PLATFORM_LABELS, authHeaders, type Platform } from './platforms';

const conversationUrl = (conversation: SmmConversation, suffix: string) =>
  `/api/messenger-bindings/${conversation.binding_id}/conversations/${conversation.platform}/${encodeURIComponent(conversation.chat_id)}/${suffix}`;

const PLATFORM_ICONS: Record<string, LucideIcon> = {
  telegram: Send,
  matrix: Hash,
  discord: MessageCircle,
  slack: Hash,
  email: Mail,
  vk: Users,
  instagram: Instagram,
  facebook: Facebook,
  whatsapp: MessageCircle,
};

/** Dialogs in these modes are waiting on a human — worth a badge on the branch
 *  that holds them, so an owner sees where to look before opening anything. */
const NEEDS_OWNER = new Set(['waiting', 'notified', 'pending_choice']);

const ALL = '';

type ServiceGroup = { platform: string; label: string; total: number; attention: number; accounts: number };
type AccountGroup = { bindingId: string; label: string; platform: string; agents: string[]; total: number; attention: number };

export function SmmDialogsPanel() {
  const [conversations, setConversations] = useState<SmmConversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [busyKey, setBusyKey] = useState('');
  const [openHistoryKey, setOpenHistoryKey] = useState('');
  const [historyMessages, setHistoryMessages] = useState<ConversationMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [platform, setPlatform] = useState<string>(ALL);
  const [bindingId, setBindingId] = useState<string>(ALL);

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
  const matches = useMemo(() => (
    needle
      ? conversations.filter(c => `${c.display_name} ${c.chat_id} ${c.agent_name} ${c.account} ${PLATFORM_LABELS[c.platform as Platform] || c.platform}`.toLowerCase().includes(needle))
      : conversations
  ), [conversations, needle]);

  const services = useMemo<ServiceGroup[]>(() => {
    const byPlatform = new Map<string, ServiceGroup & { bindings: Set<string> }>();
    for (const conversation of matches) {
      let group = byPlatform.get(conversation.platform);
      if (!group) {
        group = {
          platform: conversation.platform,
          label: PLATFORM_LABELS[conversation.platform as Platform] || conversation.platform,
          total: 0, attention: 0, accounts: 0, bindings: new Set<string>(),
        };
        byPlatform.set(conversation.platform, group);
      }
      group.total += 1;
      group.bindings.add(conversation.binding_id);
      if (NEEDS_OWNER.has(conversation.handling_mode)) group.attention += 1;
    }
    return [...byPlatform.values()]
      .map(({ bindings, ...group }) => ({ ...group, accounts: bindings.size }))
      .sort((a, b) => b.total - a.total || a.label.localeCompare(b.label));
  }, [matches]);

  const accounts = useMemo<AccountGroup[]>(() => {
    const byBinding = new Map<string, AccountGroup & { agentSet: Set<string> }>();
    for (const conversation of matches) {
      if (platform !== ALL && conversation.platform !== platform) continue;
      let group = byBinding.get(conversation.binding_id);
      if (!group) {
        group = {
          bindingId: conversation.binding_id,
          label: conversation.account || conversation.binding_id,
          platform: conversation.platform,
          agents: [], total: 0, attention: 0, agentSet: new Set<string>(),
        };
        byBinding.set(conversation.binding_id, group);
      }
      group.total += 1;
      if (conversation.agent_name) group.agentSet.add(conversation.agent_name);
      if (NEEDS_OWNER.has(conversation.handling_mode)) group.attention += 1;
    }
    return [...byBinding.values()]
      .map(({ agentSet, ...group }) => ({ ...group, agents: [...agentSet] }))
      .sort((a, b) => b.total - a.total || a.label.localeCompare(b.label));
  }, [matches, platform]);

  // A branch can disappear under you — the list reloads every 15 s, and the
  // search narrows it as you type. Falling back to «Все» beats leaving the
  // right pane empty with no hint of why.
  useEffect(() => {
    if (platform !== ALL && !services.some(service => service.platform === platform)) setPlatform(ALL);
  }, [services, platform]);
  useEffect(() => {
    if (bindingId !== ALL && !accounts.some(account => account.bindingId === bindingId)) setBindingId(ALL);
  }, [accounts, bindingId]);

  const shown = matches.filter(conversation =>
    (platform === ALL || conversation.platform === platform)
    && (bindingId === ALL || conversation.binding_id === bindingId));

  const pickService = (next: string) => {
    setPlatform(next);
    setBindingId(ALL);
    setOpenHistoryKey('');
  };

  const totalAttention = matches.filter(c => NEEDS_OWNER.has(c.handling_mode)).length;
  const selectedService = services.find(service => service.platform === platform);
  const selectedAccount = accounts.find(account => account.bindingId === bindingId);

  const branchButton = (
    { id, active, icon: Icon, title, subtitle, total, attention, onClick }:
    { id: string; active: boolean; icon: LucideIcon; title: string; subtitle: string; total: number; attention: number; onClick: () => void },
  ) => (
    <button key={id} type="button" className={`smm-branch${active ? ' is-active' : ''}`} onClick={onClick}
      title={subtitle ? `${title} — ${subtitle}` : title}>
      <Icon size={15} className="smm-branch-icon" />
      <span className="smm-branch-text">
        <span className="smm-branch-title">{title}</span>
        <span className="smm-branch-sub">{subtitle}</span>
      </span>
      <span className="smm-branch-counts">
        {attention > 0 && <span className="smm-branch-badge" title="Ждут вашего решения">{attention}</span>}
        <span className="smm-branch-total">{total}</span>
      </span>
    </button>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <input
          className="form-input"
          style={{ flex: 1 }}
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder={`Поиск среди ${conversations.length} диалогов — имя, id чата, агент, аккаунт или сервис`}
        />
        <button type="button" className="btn-ghost" onClick={fetchConversations} title="Обновить">
          <RefreshCw size={14} /><span>Обновить</span>
        </button>
      </div>
      <p style={{ ...styles.formHelp, margin: 0, maxWidth: 'none' }}>
        Слева — сервис, затем аккаунт этого сервиса, справа — его переписки, свежие сверху. 🤖 — пусть отвечает ИИ;
        ✋ — взять диалог на себя, бот замолчит; 💬 — показать переписку, которую помнит агент; 🧽 — очистить её.
        Переписка сохраняется, даже пока бот молчит, поэтому ИИ продолжит с того же места.
      </p>

      {error && <div className="glass-panel" style={{ padding: '10px 14px', color: 'var(--danger)', fontSize: '0.85rem' }}>{error}</div>}

      {loading ? (
        <span style={{ fontSize: '0.85rem', color: 'var(--text-dim, #8c94a3)' }}>Загрузка...</span>
      ) : conversations.length === 0 ? (
        <span style={{ fontSize: '0.85rem', color: 'var(--text-dim, #8c94a3)' }}>Пока никто не писал ни в один канал.</span>
      ) : (
        <div className="smm-dialogs-layout">
          <div className="smm-branch-column">
            <span className="smm-branch-heading">Сервис</span>
            {branchButton({
              id: 'all-services', active: platform === ALL, icon: Layers, title: 'Все сервисы',
              subtitle: services.length === 1 ? '1 сервис' : `${services.length} сервиса(ов)`,
              total: matches.length, attention: totalAttention, onClick: () => pickService(ALL),
            })}
            {services.map(service => branchButton({
              id: service.platform,
              active: platform === service.platform,
              icon: PLATFORM_ICONS[service.platform] || MessageCircle,
              title: service.label,
              subtitle: service.accounts === 1 ? '1 аккаунт' : `${service.accounts} аккаунта(ов)`,
              total: service.total, attention: service.attention,
              onClick: () => pickService(service.platform),
            }))}
          </div>

          <div className="smm-branch-column">
            <span className="smm-branch-heading">
              Аккаунт{selectedService ? ` · ${selectedService.label}` : ''}
            </span>
            {branchButton({
              id: 'all-accounts', active: bindingId === ALL, icon: Inbox, title: 'Все аккаунты',
              subtitle: platform === ALL ? 'по всем сервисам' : selectedService?.label || '',
              total: accounts.reduce((sum, account) => sum + account.total, 0),
              attention: accounts.reduce((sum, account) => sum + account.attention, 0),
              onClick: () => { setBindingId(ALL); setOpenHistoryKey(''); },
            })}
            {accounts.map(account => branchButton({
              id: account.bindingId,
              active: bindingId === account.bindingId,
              icon: AtSign,
              title: account.label,
              subtitle: [
                platform === ALL ? (PLATFORM_LABELS[account.platform as Platform] || account.platform) : '',
                account.agents.join(', '),
              ].filter(Boolean).join(' · '),
              total: account.total, attention: account.attention,
              onClick: () => { setBindingId(account.bindingId); setOpenHistoryKey(''); },
            }))}
            {accounts.length === 0 && (
              <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8c94a3)', padding: '4px 2px' }}>
                Нет аккаунтов с диалогами.
              </span>
            )}
          </div>

          <div className="smm-dialogs-pane">
            <span className="smm-branch-heading">
              {selectedAccount ? selectedAccount.label : selectedService ? selectedService.label : 'Все диалоги'}
              {' · '}{shown.length}
            </span>
            {shown.length === 0 ? (
              <span style={{ fontSize: '0.85rem', color: 'var(--text-dim, #8c94a3)' }}>
                {needle ? 'Здесь совпадений нет — попробуйте «Все сервисы».' : 'В этом аккаунте пока нет переписок.'}
              </span>
            ) : (
              <div className="admin-item-list" style={{ marginBottom: 0 }}>
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
                          <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8c94a3)' }}>
                            {/* Whatever the columns already pin down is not repeated here. */}
                            {[
                              platform === ALL ? (PLATFORM_LABELS[conversation.platform as Platform] || conversation.platform) : '',
                              bindingId === ALL ? (conversation.account || conversation.binding_id) : '',
                              conversation.agent_name,
                            ].filter(Boolean).join(' · ')}
                          </span>
                          <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8c94a3)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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
                            <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8c94a3)' }}>Загрузка переписки...</span>
                          ) : historyMessages.length === 0 ? (
                            <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8c94a3)' }}>
                              Пусто — агент ничего не помнит об этом человеке.
                            </span>
                          ) : (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 320, overflowY: 'auto' }}>
                              {historyMessages.map(message => (
                                <div key={message.id} style={{
                                  alignSelf: message.role === 'assistant' ? 'flex-end' : 'flex-start',
                                  maxWidth: '82%', padding: '7px 10px', borderRadius: 10, fontSize: '0.8rem',
                                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                                  background: message.role === 'assistant' ? 'rgba(53,212,154,.10)' : 'rgba(255,255,255,.04)',
                                  border: `1px solid ${message.role === 'assistant' ? 'rgba(53,212,154,.22)' : 'rgba(255,255,255,.08)'}`,
                                }}>
                                  <span style={{ display: 'block', fontSize: '0.68rem', color: 'var(--text-dim, #8c94a3)', marginBottom: 2 }}>
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
        </div>
      )}
    </div>
  );
}

export default SmmDialogsPanel;
