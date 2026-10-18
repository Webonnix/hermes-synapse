import { useEffect } from 'react';
import { AtSign, MessageSquarePlus, RefreshCw, Users, X as XIcon } from 'lucide-react';
import { mentionHandle } from './chatApi';
import type { AgentSummary } from './chatApi';
import { statusClass } from './AgentPicker';

/**
 * Every agent at a glance, from the chat: status, what it is doing, and two
 * ways in — open a chat bound to it, or @mention it in the current one.
 */
const COPY = {
  ru: {
    title: 'Агенты',
    refresh: 'Обновить',
    close: 'Закрыть',
    openChat: 'Новый чат',
    mention: 'Упомянуть',
    idle: 'Свободен',
    working: 'Работает',
    error: 'Ошибка',
    disabled: 'Отключён',
    noTask: 'Нет текущей задачи',
    empty: 'Агентов пока нет. Их создают на вкладке «Агенты».',
  },
  en: {
    title: 'Agents',
    refresh: 'Refresh',
    close: 'Close',
    openChat: 'New chat',
    mention: 'Mention',
    idle: 'Idle',
    working: 'Working',
    error: 'Error',
    disabled: 'Disabled',
    noTask: 'No current task',
    empty: 'No agents yet. Create them on the Agents tab.',
  },
};

interface AgentsPanelProps {
  language: 'ru' | 'en';
  agents: AgentSummary[];
  loading: boolean;
  onRefresh: () => void;
  onClose: () => void;
  onOpenChat: (agent: AgentSummary) => void;
  onMention: (agent: AgentSummary) => void;
}

export function AgentsPanel({ language, agents, loading, onRefresh, onClose, onOpenChat, onMention }: AgentsPanelProps) {
  const copy = COPY[language];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Status changes while the panel is open; a light poll keeps it honest.
  useEffect(() => {
    const timer = window.setInterval(onRefresh, 10000);
    return () => window.clearInterval(timer);
  }, [onRefresh]);

  const statusLabel = (agent: AgentSummary) => {
    if (agent.is_enabled === false) return copy.disabled;
    if (agent.status === 'working' || agent.status === 'busy') return copy.working;
    if (agent.status === 'error') return copy.error;
    return copy.idle;
  };

  return (
    <div className="owui-overlay" onClick={onClose}>
      <aside className="owui-drawer" role="dialog" aria-label={copy.title} onClick={(e) => e.stopPropagation()}>
        <div className="owui-drawer-head">
          <Users size={16} />
          <span>{copy.title}</span>
          <button type="button" className="owui-icon-btn" onClick={onRefresh} disabled={loading} aria-label={copy.refresh} title={copy.refresh}>
            <RefreshCw size={15} />
          </button>
          <button type="button" className="owui-icon-btn" onClick={onClose} aria-label={copy.close}>
            <XIcon size={16} />
          </button>
        </div>
        <div className="owui-drawer-body">
          {agents.length === 0 && !loading && <p className="owui-folder-empty">{copy.empty}</p>}
          {agents.map(agent => (
            <article key={agent.id} className="owui-agent-card" aria-label={agent.name}>
              <header>
                <span className={`owui-status-dot${statusClass(agent)}`} title={statusLabel(agent)} />
                <strong>{agent.name}</strong>
                <code>@{mentionHandle(agent)}</code>
              </header>
              <p>{[agent.role, statusLabel(agent)].filter(Boolean).join(' · ')}</p>
              <p className={agent.current_task ? 'owui-agent-task' : ''}>{agent.current_task || copy.noTask}</p>
              {(agent.status === 'working' || agent.status === 'busy') && typeof agent.progress === 'number' && (
                <div className="owui-progress"><i style={{ width: `${Math.max(0, Math.min(100, agent.progress))}%` }} /></div>
              )}
              <div className="owui-agent-card-actions">
                <button type="button" className="owui-btn" onClick={() => onOpenChat(agent)} disabled={agent.is_enabled === false}>
                  <MessageSquarePlus size={13} /> {copy.openChat}
                </button>
                <button type="button" className="owui-btn" onClick={() => onMention(agent)} disabled={agent.is_enabled === false}>
                  <AtSign size={13} /> {copy.mention}
                </button>
              </div>
            </article>
          ))}
        </div>
      </aside>
    </div>
  );
}
