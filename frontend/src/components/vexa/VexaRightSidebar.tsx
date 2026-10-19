import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Check, Copy, LayoutGrid, MoreHorizontal, Square, Volume2, VolumeX } from 'lucide-react';
import type { AgentModel, ChatMessage } from '../../types';
import type { VexaCopy } from './vexaCopy';
import type { InsightMetric, LineChartWidget } from './vexaDashboardTypes';
import { parseLineChartWidget } from './vexaDashboardTypes';
import { MessageLineChart } from './VexaVisuals';
import { useSmoothText } from './useSmoothText';
import { Markdown } from '../simple/Markdown';

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

/**
 * The message's own timestamp, or '' when it has none. History rows carry no time, and
 * falling back to "now" stamped every old reply with the current minute.
 */
function messageTime(message: ChatMessage): string {
  const raw = (message as unknown as Record<string, unknown>).created_at;
  if (typeof raw !== 'string') return '';
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
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

/** What the "typing" line says while the reply has no text yet: the tool at work, else thinking. */
function activityLabel(copy: VexaCopy, message: ChatMessage | null): string {
  const tool = (message?.activity || '').toLowerCase();
  if (tool) {
    if (tool.includes('web_search') || tool.includes('search_web')) return copy.toolWebSearch;
    if (tool.includes('fetch') || tool.includes('browse') || tool.includes('browser')) return copy.toolWebFetch;
    if (tool.includes('calendar')) return copy.toolCalendar;
    if (tool.includes('memory')) return copy.toolMemory;
    if (tool.includes('obsidian') || tool.includes('note')) return copy.toolNotes;
    if (tool.includes('command') || tool.startsWith('git_') || tool.startsWith('dev_')) return copy.toolCommand;
    if (tool.includes('image')) return copy.toolImage;
    if (tool.includes('market') || tool.includes('price')) return copy.toolMarket;
    if (tool.includes('weather')) return copy.toolWeather;
    return `${copy.vexaUsingTool}: ${message?.activity}`;
  }
  if (message?.thinking) return copy.vexaReasoning;
  return copy.vexaTyping;
}

function TypingIndicator({ label }: { label: string }) {
  return (
    <span className="vx-typing" role="status" aria-live="polite">
      <span className="vx-typing-dots" aria-hidden="true"><i /><i /><i /></span>
      <span className="vx-typing-label">{label}</span>
    </span>
  );
}

function CopyButton({ copy, text }: { copy: VexaCopy; text: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard blocked (insecure origin) — the text is still selectable */ }
  };
  return (
    <button type="button" className="vx-msg-action" onClick={onCopy} title={copied ? copy.copied : copy.copyMessage} aria-label={copy.copyMessage}>
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

interface MessageBubbleProps {
  copy: VexaCopy;
  message: ChatMessage;
  /** This reply is the one being read aloud on request. */
  playing: boolean;
  onSpeak?: () => void;
  onStopSpeech?: () => void;
}

function MessageBubble({ copy, message, playing, onSpeak, onStopSpeech }: MessageBubbleProps) {
  const isAssistant = message.role === 'assistant';
  const streaming = Boolean(message.streaming);
  const smooth = useSmoothText(message.content || '', isAssistant && streaming);
  const typing = smooth.length < (message.content || '').length;
  const widget = isAssistant ? messageWidget(message) : null;
  const waiting = isAssistant && streaming && !smooth;
  const time = messageTime(message);

  return (
    <div className={`vx-message is-${message.role}${streaming || typing ? ' is-live' : ''}`}>
      {waiting
        ? <TypingIndicator label={activityLabel(copy, message)} />
        : isAssistant
          ? (
            <div className="vx-md">
              <Markdown text={smooth} />
              {(streaming || typing) && <i className="vx-cursor" aria-hidden="true" />}
            </div>
          )
          : <p>{message.content}</p>}
      {isAssistant && streaming && smooth && message.activity && (
        <TypingIndicator label={activityLabel(copy, message)} />
      )}
      {widget && <MessageLineChart widget={widget} />}
      <footer>
        {isAssistant && !streaming && message.content && onSpeak && (
          playing
            ? (
              <button type="button" className="vx-msg-action is-playing" onClick={onStopSpeech} title={copy.stopSpeaking} aria-label={copy.stopSpeaking}>
                <Square size={10} fill="currentColor" />
              </button>
            )
            : (
              <button type="button" className="vx-msg-action" onClick={onSpeak} title={copy.playMessage} aria-label={copy.playMessage}>
                <Volume2 size={12} />
              </button>
            )
        )}
        {isAssistant && !streaming && message.content && <CopyButton copy={copy} text={message.content} />}
        {time && <time>{time}</time>}
      </footer>
    </div>
  );
}

interface ConversationProps {
  copy: VexaCopy;
  messages: ChatMessage[];
  /** No connection or an emergency stop: nothing can be sent at all. */
  disabled: boolean;
  /** A reply is in progress — the composer stays editable, but send turns into stop. */
  generating: boolean;
  onSend: (text: string) => boolean;
  onStop: () => void;
  onOpenFullChat: () => void;
  /** Vexa is talking (or a spoken reply is being prepared) — shows a stop-voice button. */
  speaking?: boolean;
  /** Index into `messages` of the reply read aloud on request. */
  playingIndex?: number | null;
  /** Reads `messages[index]` aloud; omitted when the host has no voice output. */
  onSpeak?: (text: string, index: number) => void;
  onStopSpeech?: () => void;
}

/** Distance from the bottom (px) that still counts as "following the conversation". */
const FOLLOW_THRESHOLD = 90;

export function ConversationCard({
  copy, messages, disabled, generating, onSend, onStop, onOpenFullChat,
  speaking = false, playingIndex = null, onSpeak, onStopSpeech,
}: ConversationProps) {
  const [draft, setDraft] = useState('');
  const [following, setFollowing] = useState(true);
  const listRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const followingRef = useRef(true);

  // Each entry keeps its index in `messages`: that is what the voice player tracks.
  const visible = useMemo(
    () => messages
      .map((message, index) => ({ message, index }))
      .filter(({ message }) => message.role === 'user' || message.role === 'assistant')
      .slice(-40),
    [messages],
  );

  const last = visible[visible.length - 1]?.message ?? null;
  // Between sending and the first stream frame there is no assistant bubble yet; show a
  // standalone "thinking" line so the reply visibly starts the moment Enter is pressed.
  const pendingReply = generating && !(last?.role === 'assistant' && last.streaming);

  const scrollToBottom = (smooth: boolean) => {
    const list = listRef.current;
    if (!list) return;
    list.scrollTo({ top: list.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  };

  // Streaming (and the smooth reveal) keeps growing the last bubble; follow it only while
  // the user is at the bottom, so reading an earlier reply is never yanked away.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return undefined;
    const observer = new ResizeObserver(() => {
      if (followingRef.current) list.scrollTop = list.scrollHeight;
    });
    Array.from(list.children).forEach(child => observer.observe(child));
    if (followingRef.current) list.scrollTop = list.scrollHeight;
    return () => observer.disconnect();
  }, [visible, pendingReply]);

  const onScroll = () => {
    const list = listRef.current;
    if (!list) return;
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < FOLLOW_THRESHOLD;
    followingRef.current = atBottom;
    if (atBottom !== following) setFollowing(atBottom);
  };

  // Auto-grow the composer up to ~6 lines, like a chat app, instead of a one-line input.
  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
  }, [draft]);

  const submit = () => {
    const text = draft.trim();
    if (!text || disabled || generating) return;
    if (onSend(text)) {
      setDraft('');
      // Your own message always brings you back to the live end of the conversation.
      followingRef.current = true;
      setFollowing(true);
      window.requestAnimationFrame(() => scrollToBottom(true));
    }
  };

  return (
    <section className="vx-panel is-flex is-grow vx-conversation">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.conversationTitle}</h2>
        {speaking && onStopSpeech && (
          <button type="button" className="vx-voice-stop" onClick={onStopSpeech} title={copy.stopSpeaking} aria-label={copy.stopSpeaking}>
            <span className="vx-voice-bars" aria-hidden="true"><i /><i /><i /></span>
            <VolumeX size={13} />
          </button>
        )}
        <button type="button" className="vx-icon-btn" onClick={onOpenFullChat} title={copy.openFullChat} aria-label={copy.openFullChat}>
          <MoreHorizontal size={13} />
        </button>
      </div>

      <div className="vx-messages-wrap">
        <div className="vx-messages" ref={listRef} onScroll={onScroll} aria-live="polite">
          {visible.length === 0 && !pendingReply && <p className="vx-empty">{copy.conversationEmpty}</p>}
          {visible.map(({ message, index }) => (
            <MessageBubble
              copy={copy}
              message={message}
              key={message.id ?? message.run_id ?? index}
              playing={playingIndex === index}
              onSpeak={onSpeak ? () => onSpeak(message.content, index) : undefined}
              onStopSpeech={onStopSpeech}
            />
          ))}
          {pendingReply && (
            <div className="vx-message is-assistant is-live">
              <TypingIndicator label={copy.vexaTyping} />
            </div>
          )}
        </div>
        {!following && (
          <button
            type="button"
            className="vx-jump-latest"
            onClick={() => { followingRef.current = true; setFollowing(true); scrollToBottom(true); }}
            title={copy.jumpToLatest}
            aria-label={copy.jumpToLatest}
          >
            <ArrowDown size={14} />
          </button>
        )}
      </div>

      <form
        className="vx-quick-ask"
        onSubmit={event => { event.preventDefault(); submit(); }}
      >
        <textarea
          ref={inputRef}
          rows={1}
          value={draft}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submit();
            }
          }}
          placeholder={copy.askFollowUp}
          aria-label={copy.askFollowUp}
          title={copy.composerHint}
          disabled={disabled}
        />
        {generating
          ? (
            <button type="button" className="is-stop" onClick={onStop} title={copy.stop} aria-label={copy.stop}>
              <Square size={11} fill="currentColor" />
            </button>
          )
          : (
            <button type="submit" disabled={disabled || !draft.trim()} title={copy.send} aria-label={`${copy.send} — ${copy.conversationTitle}`}>
              <ArrowUp size={15} />
            </button>
          )}
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
