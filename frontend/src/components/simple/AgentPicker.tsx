import { useEffect, useRef, useState } from 'react';
import { Bot, ChevronDown } from 'lucide-react';
import { MAIN_AGENT_ID } from './chatApi';
import type { AgentSummary } from './chatApi';

/** CSS modifier for an agent's runtime status dot. */
export function statusClass(agent: Pick<AgentSummary, 'status' | 'is_enabled'>): string {
  if (agent.is_enabled === false) return ' is-disabled';
  if (agent.status === 'working' || agent.status === 'busy') return ' is-working';
  if (agent.status === 'error') return ' is-error';
  return '';
}

/**
 * Which agent this chat talks to, chosen in the navbar. The binding is the
 * session's agent_id (POST /api/history/{id}/agent): every turn in the chat goes
 * to that agent unless a message @mentions another one.
 */
interface AgentPickerProps {
  agents: AgentSummary[];
  currentAgentId: string;
  onSelect: (agentId: string) => void;
  disabled?: boolean;
  label: string;
  mainLabel: string;
}

export function AgentPicker({ agents, currentAgentId, onSelect, disabled, label, mainLabel }: AgentPickerProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const current = agents.find(agent => agent.id === currentAgentId);
  const currentName = current?.name || mainLabel;

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', close);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const pick = (agentId: string) => {
    setOpen(false);
    if (agentId !== currentAgentId) onSelect(agentId);
  };

  return (
    <div className="owui-menu-wrap" ref={wrapRef}>
      <button
        type="button"
        className="owui-agent-trigger"
        onClick={() => setOpen(v => !v)}
        disabled={disabled}
        title={label}
        aria-label={`${label}: ${currentName}`}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <Bot size={14} />
        <span>{currentName}</span>
        <ChevronDown size={13} />
      </button>
      {open && (
        <div className="owui-menu owui-agent-menu" role="listbox" style={{ top: '100%', left: 0, right: 'auto', position: 'absolute' }}>
          <button
            type="button"
            role="option"
            aria-selected={currentAgentId === MAIN_AGENT_ID}
            className={currentAgentId === MAIN_AGENT_ID ? 'is-current' : ''}
            onClick={() => pick(MAIN_AGENT_ID)}
          >
            <span className="owui-status-dot" />{mainLabel}
          </button>
          {agents.map(agent => (
            <button
              key={agent.id}
              type="button"
              role="option"
              aria-selected={currentAgentId === agent.id}
              className={currentAgentId === agent.id ? 'is-current' : ''}
              disabled={agent.is_enabled === false}
              onClick={() => pick(agent.id)}
            >
              <span className={`owui-status-dot${statusClass(agent)}`} />
              {agent.name}
              {agent.role && <small>{agent.role}</small>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
