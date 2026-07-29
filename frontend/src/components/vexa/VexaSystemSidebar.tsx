import {
  Activity, Check, ChevronDown, Clock, Cpu, HardDrive, MessageSquare,
  Mic2, Network, Plus, Users, Wifi, X as XIcon,
} from 'lucide-react';
import type { ChatSession, ProviderBinding } from '../../types';
import type { VexaCopy } from './vexaCopy';
import type { SystemOverview } from './vexaDashboardTypes';
import { DensityGraph } from './VexaVisuals';
import { resourceBand } from './useVexaTelemetry';

/** Left system column: status, test model, chat history, neural density. */

function percentText(value: number | null, fallback: string) {
  return value === null ? fallback : `${Math.round(value)}%`;
}

function percentTone(value: number | null) {
  if (value === null) return 'is-muted';
  const band = resourceBand(value);
  return band === 'critical' ? 'is-red' : band === 'high' ? 'is-amber' : '';
}

function uptimeText(seconds: number | null, fallback: string) {
  if (seconds === null || !Number.isFinite(seconds)) return fallback;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export function SystemStatusCard({ copy, overview }: { copy: VexaCopy; overview: SystemOverview }) {
  const circuitLabel = overview.circuit.status === 'online' ? 'ONLINE'
    : overview.circuit.status === 'degraded' ? 'DEGRADED' : 'OFFLINE';
  const circuitTone = overview.circuit.status === 'online' ? 'is-green'
    : overview.circuit.status === 'degraded' ? 'is-amber' : 'is-red';
  const voiceText = `${overview.voice.mode === 'local' ? copy.local : copy.browser} · ${overview.voice.voice || overview.voice.engine}`;

  return (
    <section className="vx-panel">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.systemStatus}</h2>
        <span className={`vx-panel-aside ${circuitTone === 'is-green' ? 'is-green' : circuitTone === 'is-amber' ? 'is-amber' : 'is-red'}`}>
          {circuitLabel}
        </span>
      </div>
      <div className="vx-panel-body">
        <div className="vx-stat-row" title={copy.circuit}>
          <Wifi size={14} /><span>{copy.circuit}</span><strong className={circuitTone}>{circuitLabel}</strong>
        </div>
        <div className="vx-stat-row">
          <Users size={14} /><span>{copy.agents}</span><strong>{overview.agentsTotal}</strong>
        </div>
        <div className="vx-stat-row">
          <Activity size={14} /><span>{copy.active}</span><strong>{overview.agentsActive}</strong>
        </div>
        <div className="vx-stat-row" title={voiceText}>
          <Mic2 size={14} /><span>{copy.voice}</span><strong className="is-truncate">{voiceText}</strong>
        </div>
        <div className="vx-stat-row">
          <Cpu size={14} /><span>{copy.cpuLoad}</span>
          <strong className={percentTone(overview.resources.cpu)}>{percentText(overview.resources.cpu, copy.noData)}</strong>
        </div>
        <div className="vx-stat-row">
          <HardDrive size={14} /><span>{copy.memory}</span>
          <strong className={percentTone(overview.resources.memory)}>{percentText(overview.resources.memory, copy.noData)}</strong>
        </div>
        <div className="vx-stat-row">
          <Network size={14} /><span>{copy.network}</span>
          <strong className={overview.resources.network === null ? 'is-muted' : ''}>{percentText(overview.resources.network, copy.noData)}</strong>
        </div>
        <div className="vx-stat-row">
          <Clock size={14} /><span>{copy.uptime}</span>
          <strong className={overview.uptimeSeconds === null ? 'is-muted' : ''}>{uptimeText(overview.uptimeSeconds, copy.noData)}</strong>
        </div>
      </div>
    </section>
  );
}

interface ModelCardProps {
  copy: VexaCopy;
  providers: ProviderBinding[];
  selectedProviderId: string;
  pendingProviderId: string | null;
  pendingModelName: string;
  saving: boolean;
  activeProviderName?: string;
  onSelectProvider: (providerId: string) => void;
  onPendingModelChange: (value: string) => void;
  onApply: () => void;
  onCancel: () => void;
}

export function ModelSelectorCard({
  copy, providers, selectedProviderId, pendingProviderId, pendingModelName,
  saving, activeProviderName, onSelectProvider, onPendingModelChange, onApply, onCancel,
}: ModelCardProps) {
  return (
    <section className="vx-panel">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.testModel}</h2>
      </div>
      <div className="vx-panel-body">
        <div className="vx-select-wrap">
          <select
            className="vx-select"
            value={pendingProviderId ?? selectedProviderId}
            disabled={saving}
            aria-label={copy.testModel}
            onChange={event => onSelectProvider(event.target.value)}
          >
            <option value="ollama">{copy.localModel}</option>
            {providers.map(provider => (
              <option key={provider.id} value={provider.id}>{provider.name}</option>
            ))}
          </select>
          <ChevronDown size={14} />
        </div>

        {pendingProviderId && (
          <div className="vx-inline-confirm">
            <input
              value={pendingModelName}
              onChange={event => onPendingModelChange(event.target.value)}
              placeholder={copy.externalModelPlaceholder}
              aria-label={copy.externalModelPlaceholder}
              autoFocus
            />
            <button type="button" title={copy.applySwitch} aria-label={copy.applySwitch} disabled={!pendingModelName.trim() || saving} onClick={onApply}>
              <Check size={13} />
            </button>
            <button type="button" title={copy.cancelSwitch} aria-label={copy.cancelSwitch} onClick={onCancel}>
              <XIcon size={13} />
            </button>
          </div>
        )}

        <p className="vx-hint">
          {selectedProviderId !== 'ollama' ? `${copy.testingWith} ${activeProviderName ?? selectedProviderId}` : copy.usingLocalModel}
          {' '}{copy.modelSwitchCaveat}
        </p>
      </div>
    </section>
  );
}

interface HistoryCardProps {
  copy: VexaCopy;
  sessions: ChatSession[];
  currentChatId: string;
  getSessionLabel: (id: string) => string;
  onCreate: () => void;
  onSelect: (id: string) => void;
  onSeeAll: () => void;
}

export function ChatHistoryCard({ copy, sessions, currentChatId, getSessionLabel, onCreate, onSelect, onSeeAll }: HistoryCardProps) {
  const visible = sessions.slice(0, 14);
  return (
    <section className="vx-panel is-flex is-grow">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.history}</h2>
        <button type="button" className="vx-icon-btn" onClick={onCreate} title={copy.newChat} aria-label={copy.newChat}>
          <Plus size={13} />
        </button>
      </div>
      <div className="vx-panel-body is-scroll">
        <div className="vx-history-list">
          {visible.length === 0 && <p className="vx-empty">{copy.noHistory}</p>}
          {visible.map(session => (
            <button
              type="button"
              key={session.id}
              className={`vx-history-item${currentChatId === session.id ? ' is-active' : ''}`}
              onClick={() => onSelect(session.id)}
              title={session.title || getSessionLabel(session.id)}
            >
              <MessageSquare size={12} />
              <span>{session.title || getSessionLabel(session.id)}</span>
            </button>
          ))}
        </div>
        {sessions.length > visible.length && (
          <button type="button" className="vx-link" onClick={onSeeAll}>{copy.seeAll}</button>
        )}
      </div>
    </section>
  );
}

export function NeuralDensityCard({ copy, density }: { copy: VexaCopy; density: number | null }) {
  const label = density === null ? copy.noData
    : density >= 85 ? copy.densityHigh
      : density >= 60 ? copy.densityMedium : copy.densityLow;

  return (
    <section className="vx-panel">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.density}</h2>
      </div>
      <div className="vx-panel-body">
        <div className="vx-density">
          <DensityGraph intensity={(density ?? 0) / 100} />
          <div>
            <div className="vx-density-value">{density === null ? '—' : `${density.toFixed(1)}%`}</div>
            <small>{label}</small>
          </div>
        </div>
      </div>
    </section>
  );
}
