import { useEffect, useMemo, useRef, useState } from 'react';
import { LayoutGrid, MoreHorizontal, Send } from 'lucide-react';
import type { AgentModel, ChatMessage } from '../../types';
import type { VexaCopy } from './vexaCopy';
import type { InsightMetric, LineChartWidget } from './vexaDashboardTypes';
import { parseLineChartWidget } from './vexaDashboardTypes';
import { MessageLineChart } from './VexaVisuals';

/** Right column: agent circuit, conversation, system insights. */

type AgentVisualStatus = 'online' | 'busy' | 'paused' | 'error' | 'offline';

function agentStatus(agent: AgentModel): AgentVisualStatus {
  const raw = String(agent.status || '').toLowerCase();
  if (agent.is_enabled === false || raw === 'disabled') return 'offline';
  if (raw === 'error') return 'error';
  if (['working', 'running', 'active', 'processing'].includes(raw)) return 'busy';
  if (raw === 'paused') return 'paused';
  if (raw === 'idle' || raw === '') return 'online';
  return 'online';
}

interface AgentCircuitProps {
  copy: VexaCopy;
  agents: AgentModel[];
  activeCount: number;
  selectedAgentId: string;
  onOpenAgent: (agentId: string) => void;
  onOpenAll: () => void;
}

export function AgentCircuitCard({ copy, agents, activeCount, selectedAgentId, onOpenAgent, onOpenAll }: AgentCircuitProps) {
  return (
    <section className="vx-panel is-flex">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.agentCircuit}</h2>
        <span className="vx-panel-aside">{activeCount}/{agents.length}</span>
        <button type="button" className="vx-icon-btn" onClick={onOpenAll} title={copy.openChannel} aria-label={copy.openChannel}>
          <LayoutGrid size={13} />
        </button>
      </div>
      <div className="vx-panel-body is-scroll" style={{ maxHeight: 210 }}>
        {agents.length === 0 && <p className="vx-empty">{copy.noAgentsYet}</p>}
        {agents.slice(0, 10).map(agent => {
          const status = agentStatus(agent);
          const selected = agent.id === selectedAgentId;
          return (
            <button
              type="button"
              key={agent.id}
              className={`vx-agent-row${selected ? ' is-selected' : ''}`}
              onClick={() => onOpenAgent(agent.id)}
              title={`${agent.name} — ${agent.current_task || agent.role || copy.noTask}`}
            >
              <i className={`vx-agent-dot ${selected ? 'is-selected' : `is-${status}`}`} aria-hidden="true" />
              <span>
                <b>{agent.name}</b>
                <small>{agent.current_task || agent.role || copy.specialist}</small>
              </span>
            </button>
          );
        })}
      </div>
      {agents.length > 10 && (
        <div className="vx-panel-body" style={{ paddingTop: 0 }}>
          <button type="button" className="vx-link" onClick={onOpenAll}>{copy.seeAllAgents} ›</button>
        </div>
      )}
    </section>
  );
}

function messageTime(message: ChatMessage): string {
  const raw = (message as unknown as Record<string, unknown>).created_at;
  const date = typeof raw === 'string' ? new Date(raw) : new Date();
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** Strips markdown down to readable plain text for the compact conversation panel. */
function plainText(value: string): string {
  return value
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)]\([^)]*\)/g, '$1')
    .replace(/^[>#\s]*/gm, '')
    .replace(/\*\*|__|\*|_/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function messageWidget(message: ChatMessage): LineChartWidget | null {
  const raw = message as unknown as Record<string, unknown>;
  const widgets = raw.widgets;
  if (!Array.isArray(widgets)) return null;
  for (const candidate of widgets) {
    const parsed = parseLineChartWidget(candidate);
    if (parsed) return parsed;
  }
  return null;
}

interface ConversationProps {
  copy: VexaCopy;
  messages: ChatMessage[];
  disabled: boolean;
  onSend: (text: string) => boolean;
  onOpenFullChat: () => void;
}

export function ConversationCard({ copy, messages, disabled, onSend, onOpenFullChat }: ConversationProps) {
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement | null>(null);

  const visible = useMemo(
    () => messages.filter(message => message.role === 'user' || message.role === 'assistant').slice(-40),
    [messages],
  );

  // Streaming appends to the last message; scrolling on every token would fight the user,
  // so only follow along when they are already at the bottom.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 90;
    if (atBottom) list.scrollTop = list.scrollHeight;
  }, [visible]);

  const submit = () => {
    const text = draft.trim();
    if (!text || disabled) return;
    if (onSend(text)) setDraft('');
  };

  return (
    <section className="vx-panel is-flex is-grow vx-conversation">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.conversationTitle}</h2>
        <button type="button" className="vx-icon-btn" onClick={onOpenFullChat} title={copy.openFullChat} aria-label={copy.openFullChat}>
          <MoreHorizontal size={13} />
        </button>
      </div>

      <div className="vx-messages" ref={listRef}>
        {visible.length === 0 && <p className="vx-empty">{copy.conversationEmpty}</p>}
        {visible.map((message, index) => {
          const widget = message.role === 'assistant' ? messageWidget(message) : null;
          return (
            <div className={`vx-message is-${message.role}`} key={message.id ?? message.run_id ?? index}>
              <p>
                {plainText(message.content)}
                {message.streaming && <i className="vx-cursor" aria-hidden="true" />}
              </p>
              {widget && <MessageLineChart widget={widget} />}
              <time>{messageTime(message)}</time>
            </div>
          );
        })}
      </div>

      <form
        className="vx-quick-ask"
        onSubmit={event => { event.preventDefault(); submit(); }}
      >
        <input
          value={draft}
          onChange={event => setDraft(event.target.value)}
          placeholder={copy.askFollowUp}
          aria-label={copy.askFollowUp}
          disabled={disabled}
        />
        <button type="submit" disabled={disabled || !draft.trim()} title={copy.send} aria-label={`${copy.send} — ${copy.conversationTitle}`}>
          <Send size={14} />
        </button>
      </form>
    </section>
  );
}

interface InsightsProps {
  copy: VexaCopy;
  metrics: InsightMetric[];
}

export function SystemInsightsCard({ copy, metrics }: InsightsProps) {
  const labels: Record<string, string> = {
    efficiency: copy.efficiency,
    responseTime: copy.responseTime,
    accuracy: copy.accuracy,
    stability: copy.stability,
  };

  return (
    <section className="vx-panel">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.insights}</h2>
      </div>
      <div className="vx-panel-body">
        {metrics.map(metric => {
          const ratio = metric.value === null
            ? 0
            : Math.max(0, Math.min(1, (metric.value - metric.min) / (metric.max - metric.min || 1)));
          const label = labels[metric.label] ?? metric.label;
          return (
            <div className="vx-insight-row" key={metric.id} title={metric.updatedAt ? `${label} · ${new Date(metric.updatedAt).toLocaleTimeString()}` : label}>
              <span>{label}</span>
              <div className="vx-insight-track">
                <div
                  className={`vx-insight-fill${metric.status === 'warning' ? ' is-warning' : metric.status === 'critical' ? ' is-critical' : ''}`}
                  style={{ width: `${ratio * 100}%` }}
                />
              </div>
              {metric.formattedValue === null
                ? <strong className="is-unknown">{copy.noData}</strong>
                : <strong>{metric.formattedValue}</strong>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
