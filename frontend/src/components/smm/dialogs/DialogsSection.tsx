import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Brain, Inbox, Layers, ListFilter, PhoneCall } from 'lucide-react';
import { ConversationList, type ListFilters } from './ConversationList';
import { ConversationThread } from './ConversationThread';
import { ConversationSidePanel } from './ConversationSidePanel';
import { RulesPanel } from './RulesPanel';
import { PhonePanel } from './PhonePanel';
import { SmmDialogsPanel } from '../SmmDialogsPanel';
import { INBOX_UPDATE_EVENT, OPEN_INBOX_EVENT, OPEN_INBOX_KEY, PLATFORM_LABELS, inboxApi } from './inboxApi';
import type { InboxBinding, InboxConversation, InboxMessage, InboxMeta, RouteState, RouteTarget } from './inboxTypes';
import './inbox.css';

type View = 'conversations' | 'phone' | 'rules' | 'memory';

const VIEWS: [View, string, typeof Inbox][] = [
  ['conversations', 'Все диалоги', Inbox],
  ['phone', 'Звонки', PhoneCall],
  ['rules', 'Правила и часы', ListFilter],
  ['memory', 'Память агентов', Brain],
];

const VIEW_KEY = 'smm_dialogs_view';

const DEFAULT_FILTERS: ListFilters = { search: '', handling: '', status: 'open', platform: '', binding_id: '', unread: false };

function takeRequestedConversation(): string | null {
  try {
    const id = localStorage.getItem(OPEN_INBOX_KEY);
    if (id) localStorage.removeItem(OPEN_INBOX_KEY);
    return id;
  } catch {
    return null;
  }
}

function initialView(requested: boolean): View {
  if (requested) return 'conversations';
  try {
    const stored = localStorage.getItem(VIEW_KEY) as View | null;
    return stored && VIEWS.some(([key]) => key === stored) ? stored : 'conversations';
  } catch {
    return 'conversations';
  }
}

/**
 * SMM Factory → «Диалоги»: every customer conversation from every connected channel
 * and phone line — the archive (backend/inbox.py), takeover/whisper, conversation
 * routing and the CRM link — plus calls, rules and the agents' memory.
 *
 * «Все диалоги» is three columns (list · thread · card) under a service strip, so
 * the service → account → dialog path of the old SMM panel is kept. «Память агентов»
 * is that old panel itself: it reads conversation_states/messages — what the agent
 * remembers, including dialogs from before the archive existed — and is the only
 * place that clears a memory.
 */
export function DialogsSection({ onOpenClient }: { onOpenClient?: (clientId: string) => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(() => takeRequestedConversation());
  const [view, setView] = useState<View>(() => initialView(Boolean(selectedId)));
  const [meta, setMeta] = useState<InboxMeta | null>(null);
  const [metaError, setMetaError] = useState('');
  const [bindings, setBindings] = useState<InboxBinding[]>([]);
  const [targets, setTargets] = useState<RouteTarget[]>([]);
  const [filters, setFilters] = useState<ListFilters>(DEFAULT_FILTERS);
  const [conversations, setConversations] = useState<InboxConversation[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [conversation, setConversation] = useState<InboxConversation | null>(null);
  const [messages, setMessages] = useState<InboxMessage[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [threadLoading, setThreadLoading] = useState(false);
  const [threadError, setThreadError] = useState('');
  const [route, setRoute] = useState<RouteState | null>(null);
  // Async loaders compare against the *current* selection when they resolve, so a
  // slow response for a conversation the operator already left cannot overwrite
  // the thread now on screen. Declared before the loading effect so it is synced first.
  const selectedRef = useRef<string | null>(selectedId);
  useEffect(() => { selectedRef.current = selectedId; }, [selectedId]);

  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, view);
    } catch {
      /* storage unavailable — the view just is not remembered */
    }
  }, [view]);

  useEffect(() => {
    inboxApi.meta()
      .then(result => { setMeta(result); setMetaError(''); })
      .catch(exc => setMetaError(exc.status === 403 ? 'У вашей роли нет доступа к диалогам.' : (exc as Error).message));
    inboxApi.bindings().then(setBindings).catch(() => setBindings([]));
    inboxApi.targets().then(setTargets).catch(() => setTargets([]));
  }, []);

  /** Services that have at least one connected account or line, in a stable order. */
  const services = useMemo(() => {
    const seen = new Map<string, number>();
    bindings.forEach(binding => seen.set(binding.platform, (seen.get(binding.platform) || 0) + 1));
    return [...seen.entries()]
      .map(([platform, accounts]) => ({ platform, accounts, label: PLATFORM_LABELS[platform] || platform }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [bindings]);

  const loadList = useCallback(async (append = false, cursor: string | null = null) => {
    setListLoading(true);
    try {
      const page = await inboxApi.conversations({ ...filters, cursor: append ? cursor : null });
      setConversations(prev => (append ? [...prev, ...page.items.filter(item => !prev.some(p => p.id === item.id))] : page.items));
      setNextCursor(page.next_cursor);
      setListError('');
    } catch (exc) {
      setListError((exc as Error).message);
      if (!append) setConversations([]);
    } finally {
      setListLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    const timer = setTimeout(() => void loadList(), filters.search ? 300 : 0);
    return () => clearTimeout(timer);
  }, [loadList, filters.search]);

  const loadThread = useCallback(async (id: string, { quiet = false } = {}) => {
    if (!quiet) setThreadLoading(true);
    try {
      const [detail, page, routeState] = await Promise.all([
        inboxApi.conversation(id), inboxApi.messages(id), inboxApi.route(id).catch(() => null),
      ]);
      if (selectedRef.current !== id) return;
      setConversation(detail);
      setMessages(page.items);
      setOlderCursor(page.next_cursor);
      setRoute(routeState);
      setThreadError('');
      if (detail.unread_count > 0) {
        const read = await inboxApi.markRead(id).catch(() => null);
        if (read && selectedRef.current === id) {
          setConversation(read);
          setConversations(prev => prev.map(item => (item.id === id ? { ...item, unread_count: 0 } : item)));
        }
      }
    } catch (exc) {
      if (selectedRef.current === id) setThreadError((exc as Error).message);
    } finally {
      if (!quiet) setThreadLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setConversation(null);
      setMessages([]);
      setRoute(null);
      return;
    }
    void loadThread(selectedId);
  }, [selectedId, loadThread]);

  // Live updates: the frame only says *which* conversation changed; everything is
  // re-read over REST, where permissions are enforced. Coalesced so a burst of
  // messages is one refetch.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pendingIds = new Set<string>();
    const onUpdate = (event: Event) => {
      const id = (event as CustomEvent<{ conversation_id?: string }>).detail?.conversation_id;
      if (id) pendingIds.add(id);
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        const ids = pendingIds;
        pendingIds = new Set();
        void loadList();
        const current = selectedRef.current;
        if (current && ids.has(current)) void loadThread(current, { quiet: true });
      }, 400);
    };
    const onOpen = (event: Event) => {
      // Always drain the stored request too: left behind, it would reopen this
      // conversation the next time the section mounts.
      const stored = takeRequestedConversation();
      const id = (event as CustomEvent<{ conversationId?: string }>).detail?.conversationId || stored;
      if (id) {
        setView('conversations');
        setSelectedId(id);
      }
    };
    window.addEventListener(INBOX_UPDATE_EVENT, onUpdate);
    window.addEventListener(OPEN_INBOX_EVENT, onOpen);
    return () => {
      if (timer) clearTimeout(timer);
      window.removeEventListener(INBOX_UPDATE_EVENT, onUpdate);
      window.removeEventListener(OPEN_INBOX_EVENT, onOpen);
    };
  }, [loadList, loadThread]);

  const applyConversation = (updated: InboxConversation) => {
    setConversation(updated);
    setConversations(prev => prev.map(item => (item.id === updated.id ? { ...item, ...updated } : item)));
  };

  const runThread = async (action: () => Promise<void>) => {
    try {
      await action();
      setThreadError('');
    } catch (exc) {
      setThreadError((exc as Error).message);
    }
  };

  const refreshSelected = async () => {
    if (selectedRef.current) await loadThread(selectedRef.current, { quiet: true });
  };

  const pickService = (platform: string) => setFilters(prev => ({ ...prev, platform, binding_id: '' }));

  const denied = metaError && view !== 'memory' ? (
    <div className="glass-panel ibx-denied"><Inbox size={22} /><strong>{metaError}</strong></div>
  ) : null;

  return (
    <div className="ibx-root" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <nav className="admin-subnav is-wrap" aria-label="Разделы диалогов">
        {VIEWS.map(([key, label, Icon]) => (
          <button key={key} type="button" className={view === key ? 'is-active' : ''} onClick={() => setView(key)}>
            <Icon size={15} /><span>{label}</span>
            {key === 'conversations' && Number(meta?.unread_conversations) > 0 && (
              <span className="admin-subnav-count">{meta?.unread_conversations}</span>
            )}
          </button>
        ))}
      </nav>

      {denied || (view === 'memory' ? (
        <SmmDialogsPanel />
      ) : view === 'phone' ? (
        <PhonePanel />
      ) : view === 'rules' ? (
        <RulesPanel bindings={bindings} canEdit={Boolean(meta?.can_edit_rules)} />
      ) : (
        <>
          {services.length > 0 && (
            <div className="ibx-services" role="group" aria-label="Сервис">
              <button type="button" className={`ibx-service ${filters.platform === '' ? 'is-active' : ''}`} onClick={() => pickService('')}>
                <Layers size={13} /><span>Все сервисы</span>
              </button>
              {services.map(service => (
                <button
                  key={service.platform}
                  type="button"
                  className={`ibx-service ${filters.platform === service.platform ? 'is-active' : ''}`}
                  onClick={() => pickService(service.platform)}
                  title={service.accounts === 1 ? '1 аккаунт' : `Аккаунтов: ${service.accounts}`}
                >
                  <span>{service.label}</span>
                  <em>{service.accounts}</em>
                </button>
              ))}
            </div>
          )}
          <div className={`ibx-layout ${selectedId ? 'has-selection' : ''}`}>
            <ConversationList
              conversations={conversations}
              filters={filters}
              bindings={bindings}
              loading={listLoading}
              error={listError}
              selectedId={selectedId}
              hasMore={Boolean(nextCursor)}
              onFilters={setFilters}
              onSelect={item => setSelectedId(item.id)}
              onLoadMore={() => void loadList(true, nextCursor)}
            />
            <ConversationThread
              conversation={conversation}
              messages={messages}
              loading={threadLoading}
              error={threadError}
              meta={meta}
              hasOlder={Boolean(olderCursor)}
              onBack={() => setSelectedId(null)}
              onLoadOlder={() => void runThread(async () => {
                if (!conversation || !olderCursor) return;
                const page = await inboxApi.messages(conversation.id, olderCursor);
                setMessages(prev => [...page.items, ...prev]);
                setOlderCursor(page.next_cursor);
              })}
              onTakeover={() => runThread(async () => { if (conversation) applyConversation(await inboxApi.takeover(conversation.id)); await refreshSelected(); })}
              onResume={() => runThread(async () => { if (conversation) applyConversation(await inboxApi.resume(conversation.id)); await refreshSelected(); })}
              onClose={() => runThread(async () => { if (conversation) applyConversation(await inboxApi.close(conversation.id)); })}
              onReopen={() => runThread(async () => { if (conversation) applyConversation(await inboxApi.reopen(conversation.id)); })}
              onReply={async text => {
                if (!conversation) return;
                try {
                  await inboxApi.reply(conversation.id, text);
                  setThreadError('');
                } catch (exc) {
                  setThreadError((exc as Error).message);
                  throw exc;
                } finally {
                  await refreshSelected();
                }
              }}
              onWhisper={async text => {
                if (!conversation) return;
                try {
                  await inboxApi.whisper(conversation.id, text);
                  setThreadError('');
                } catch (exc) {
                  setThreadError((exc as Error).message);
                  throw exc;
                }
                await refreshSelected();
              }}
              onSendDraft={async (message, text) => {
                if (!message.pending_reply_id) return;
                try {
                  await inboxApi.sendDraft(message.pending_reply_id, text);
                  setThreadError('');
                } catch (exc) {
                  setThreadError((exc as Error).message);
                }
                await refreshSelected();
              }}
              onDiscardDraft={async message => {
                if (!message.pending_reply_id) return;
                try {
                  await inboxApi.discardDraft(message.pending_reply_id);
                  setThreadError('');
                } catch (exc) {
                  setThreadError((exc as Error).message);
                }
                await refreshSelected();
              }}
            />
            <ConversationSidePanel
              conversation={conversation}
              meta={meta}
              route={route}
              targets={targets}
              onLinked={applyConversation}
              onRouteChanged={setRoute}
              onBlocked={() => void refreshSelected()}
              onOpenClient={onOpenClient}
            />
          </div>
        </>
      ))}
    </div>
  );
}
