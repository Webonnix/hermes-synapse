import { Bot, Hand, Search, ShieldBan } from 'lucide-react';
import type { InboxBinding, InboxConversation } from './inboxTypes';
import { HANDLING_LABELS, PLATFORM_LABELS, formatWhen } from './inboxApi';

export interface ListFilters {
  search: string;
  handling: string;
  status: string;
  /** Set by the service strip above the list; narrows the account picker too. */
  platform: string;
  binding_id: string;
  unread: boolean;
}

const HANDLING_ICON = { ai: Bot, human: Hand, blocked: ShieldBan } as const;

export function ConversationList({
  conversations, filters, bindings, loading, error, selectedId, hasMore,
  onFilters, onSelect, onLoadMore,
}: {
  conversations: InboxConversation[];
  filters: ListFilters;
  bindings: InboxBinding[];
  loading: boolean;
  error: string;
  selectedId: string | null;
  hasMore: boolean;
  onFilters: (next: ListFilters) => void;
  onSelect: (conversation: InboxConversation) => void;
  onLoadMore: () => void;
}) {
  return (
    <aside className="ibx-list glass-panel" aria-label="Разговоры">
      <div className="ibx-list-filters">
        <label className="ibx-search">
          <Search size={14} aria-hidden />
          <input
            className="form-input"
            value={filters.search}
            onChange={e => onFilters({ ...filters, search: e.target.value })}
            placeholder="Поиск по имени и тексту"
            aria-label="Поиск разговоров"
          />
        </label>
        <div className="ibx-filter-row">
          <select className="form-input" aria-label="Кто отвечает" value={filters.handling}
                  onChange={e => onFilters({ ...filters, handling: e.target.value })}>
            <option value="">Все</option>
            <option value="ai">ИИ отвечает</option>
            <option value="human">Оператор</option>
            <option value="blocked">Заблокированы</option>
          </select>
          <select className="form-input" aria-label="Статус" value={filters.status}
                  onChange={e => onFilters({ ...filters, status: e.target.value })}>
            <option value="open">Открытые</option>
            <option value="closed">Закрытые</option>
            <option value="">Любой статус</option>
          </select>
        </div>
        <div className="ibx-filter-row">
          <select className="form-input" aria-label="Аккаунт" value={filters.binding_id}
                  onChange={e => onFilters({ ...filters, binding_id: e.target.value })}>
            <option value="">Все аккаунты</option>
            {bindings.filter(binding => !filters.platform || binding.platform === filters.platform).map(binding => (
              <option key={binding.id} value={binding.id}>
                {(PLATFORM_LABELS[binding.platform] || binding.platform) + ' · ' + (binding.bot_username || binding.id)}
              </option>
            ))}
          </select>
          <label className="ibx-check">
            <input type="checkbox" checked={filters.unread} onChange={e => onFilters({ ...filters, unread: e.target.checked })} />
            <span>Непрочитанные</span>
          </label>
        </div>
      </div>

      {error && <div className="cl-error-banner" role="alert">{error}</div>}

      <div className="ibx-list-scroll">
        {!loading && !error && conversations.length === 0 && (
          <div className="ibx-empty">
            <strong>Разговоров нет</strong>
            <span>Здесь появятся все сообщения клиентов из подключённых каналов.</span>
          </div>
        )}
        {conversations.map(conversation => {
          const Icon = HANDLING_ICON[conversation.handling] ?? Bot;
          return (
            <button
              key={conversation.id}
              type="button"
              className={`ibx-row ${selectedId === conversation.id ? 'is-selected' : ''} ${conversation.unread_count > 0 ? 'is-unread' : ''}`}
              onClick={() => onSelect(conversation)}
            >
              <span className={`ibx-row-icon is-${conversation.handling}`} title={HANDLING_LABELS[conversation.handling]}>
                <Icon size={14} />
              </span>
              <span className="ibx-row-body">
                <span className="ibx-row-top">
                  <strong>{conversation.client_name || conversation.display_name || conversation.chat_id}</strong>
                  <time>{formatWhen(conversation.last_message_at)}</time>
                </span>
                <span className="ibx-row-meta">
                  <em className="ibx-platform">{PLATFORM_LABELS[conversation.platform] || conversation.platform}</em>
                  {conversation.agent_name && <span>{conversation.agent_name}</span>}
                </span>
                <span className="ibx-row-preview">
                  {conversation.last_speaker === 'agent' ? 'Агент: ' : conversation.last_speaker === 'human' ? 'Вы: ' : ''}
                  {conversation.last_message_preview || '—'}
                </span>
              </span>
              {conversation.unread_count > 0 && (
                <span className="ibx-unread" aria-label={`${conversation.unread_count} непрочитанных`}>{conversation.unread_count}</span>
              )}
            </button>
          );
        })}
        {loading && <div className="ibx-empty"><span>Загрузка…</span></div>}
        {hasMore && !loading && (
          <button type="button" className="btn-ghost ibx-more" onClick={onLoadMore}>Показать ещё</button>
        )}
      </div>
    </aside>
  );
}
