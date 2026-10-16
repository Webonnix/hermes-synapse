import { AlertTriangle, CheckCircle2, Cpu, HardDrive, Loader2, RefreshCw, Server, Unplug, Zap } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import type { LocalLlmStatus, SystemConfig } from '../types';

interface LocalModelPanelProps {
  onSelectModel: (model: string) => void;
  /** The model the live agent uses right now (GET /api/config). */
  activeModel?: string;
  /** Called after the agent is switched to the served model, with the backend's fresh config. */
  onActivated?: (config: Partial<SystemConfig>) => void;
}

function formatBytes(value?: number) {
  if (!value || value < 1) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** unit).toFixed(unit > 2 ? 1 : 0)} ${units[unit]}`;
}

/** 196608 -> "192K ctx", the way context windows are quoted. */
function formatContext(tokens?: number) {
  if (!tokens || tokens < 1) return '';
  if (tokens >= 1024) return `${Math.round(tokens / 1024)}K ctx`;
  return `${tokens} ctx`;
}

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(detail?.message || detail || data.error || `HTTP ${response.status}`);
  }
  return data as T;
}

/**
 * The local llama.cpp server. Unlike the Ollama panel it replaced there is no
 * pull/delete/unload: llama-server loads exactly one model at start-up and keeps
 * it resident, so the operational facts are whether it is up, what it serves,
 * and whether the agent is actually configured for that model.
 */
export function LocalModelPanel({ onSelectModel, activeModel, onActivated }: LocalModelPanelProps) {
  const [status, setStatus] = useState<LocalLlmStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [activating, setActivating] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await apiJson<LocalLlmStatus>('/api/llm/status');
      setStatus(next);
      setError(next.available ? '' : (next.error || 'The llama.cpp server is unavailable.'));
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : 'Could not reach the llama.cpp server.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const served = status?.model || '';
  const mismatch = Boolean(status?.available && served && activeModel && served !== activeModel);
  const isLoadingModel = status?.health === 'loading';

  const switchToServedModel = async () => {
    if (!served || activating) return;
    setActivating(true);
    setError('');
    try {
      const result = await apiJson<{ status: string; config: Partial<SystemConfig> }>('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: served }),
      });
      onSelectModel(served);
      onActivated?.(result.config);
      await refresh();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : `Could not switch to “${served}”.`);
    } finally {
      setActivating(false);
    }
  };

  const healthLabel = loading
    ? 'Checking…'
    : status?.available
      ? `Online${status.build ? ` · ${status.build}` : ''}`
      : isLoadingModel ? 'Loading model…' : 'Offline';

  return (
    <section className="localllm-manager" aria-labelledby="localllm-manager-title">
      <header>
        <div className="localllm-manager-title">
          <span><Server size={18} /></span>
          <div>
            <h3 id="localllm-manager-title">llama.cpp server</h3>
            <p>{status?.base_url || 'Local model server'}</p>
          </div>
        </div>
        <div className={`localllm-health ${status?.available ? 'is-online' : 'is-offline'}`} role="status">
          {loading ? <Loader2 size={14} className="spin-slow" /> : status?.available ? <CheckCircle2 size={14} /> : <Unplug size={14} />}
          <span>{healthLabel}</span>
        </div>
        <button type="button" className="localllm-refresh" onClick={() => void refresh()} disabled={loading} aria-label="Refresh llama.cpp status">
          <RefreshCw size={15} />
        </button>
      </header>

      {error && <div className="localllm-error" role="alert"><AlertTriangle size={15} /><span>{error}</span></div>}

      <div className="localllm-runtime-stats">
        <div>
          <HardDrive size={15} /><span>Served model</span>
          <strong title={status?.model_path || served}>{served || '—'}</strong>
        </div>
        <div>
          <Cpu size={15} /><span>Context</span>
          <strong>{formatContext(status?.n_ctx) || '—'}</strong>
        </div>
        <div className="localllm-active-stat">
          <Zap size={15} /><span>Active now</span>
          <strong title={activeModel}>{activeModel || 'None'}</strong>
          {status?.available && (
            <em className={mismatch ? '' : 'is-resident'}>
              {[
                mismatch ? 'not the served model' : 'resident',
                formatBytes(status.model_size),
                status.vision ? 'vision' : '',
                status.total_slots ? `${status.total_slots} slot${status.total_slots === 1 ? '' : 's'}` : '',
              ].filter(Boolean).join(' · ')}
            </em>
          )}
        </div>
      </div>

      {mismatch && (
        <div className="localllm-warning" role="status">
          <AlertTriangle size={15} />
          <span>
            The agent is configured for <strong>{activeModel}</strong>, but the server serves <strong>{served}</strong>.
          </span>
          <button type="button" onClick={() => void switchToServedModel()} disabled={activating}>
            {activating ? <Loader2 size={14} className="spin-slow" /> : <Zap size={14} />}
            {activating ? 'Switching…' : `Use ${served}`}
          </button>
        </div>
      )}
    </section>
  );
}
