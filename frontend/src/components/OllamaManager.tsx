import { AlertTriangle, Box, CheckCircle2, Cpu, Download, HardDrive, Loader2, Play, RefreshCw, Server, Trash2, Unplug, Zap } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { OllamaModel, OllamaStatus, SystemConfig } from '../types';

interface OllamaManagerProps {
  selectedModel: string;
  onSelectModel: (model: string) => void;
  /** The model actually in use by the live agent right now (from GET /api/config), as opposed to selectedModel which may just be a pending, unsaved choice in the form below. */
  activeModel?: string;
  /** Called after a model is activated in-place, with the backend's fresh config, so callers can sync it into their own state without a full page reload. */
  onActivated?: (config: Partial<SystemConfig>) => void;
}

function formatBytes(value?: number) {
  if (!value || value < 1) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** unit).toFixed(unit > 2 ? 1 : 0)} ${units[unit]}`;
}

/** 262144 -> "256K". The context a model was actually loaded with is the number
 *  that matters operationally, so it is shown the way people quote it. */
function formatContext(tokens?: number) {
  if (!tokens || tokens < 1) return '';
  if (tokens >= 1024 * 1024) return `${(tokens / (1024 * 1024)).toFixed(tokens % (1024 * 1024) ? 1 : 0)}M ctx`;
  if (tokens >= 1024) return `${Math.round(tokens / 1024)}K ctx`;
  return `${tokens} ctx`;
}

/** Ollama reports "name" and "name:latest" interchangeably depending on the
 *  endpoint, so compare tags the way Ollama resolves them rather than literally. */
function sameModel(a?: string, b?: string) {
  if (!a || !b) return false;
  const norm = (value: string) => (value.includes(':') ? value : `${value}:latest`);
  return norm(a) === norm(b);
}

/** What a resident model is really costing right now, straight from /api/ps. */
function residencySummary(entry?: OllamaModel) {
  if (!entry) return '';
  const parts = [formatContext(entry.context_length)];
  if (entry.size_vram) {
    parts.push(`${formatBytes(entry.size_vram)} VRAM`);
    if (entry.size) {
      const onGpu = Math.round((entry.size_vram / entry.size) * 100);
      parts.push(onGpu >= 100 ? '100% GPU' : `${onGpu}% GPU · CPU offload`);
    }
  }
  return parts.filter(Boolean).join(' · ');
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

export function OllamaManager({ selectedModel, onSelectModel, activeModel, onActivated }: OllamaManagerProps) {
  const [status, setStatus] = useState<OllamaStatus | null>(null);
  const [models, setModels] = useState<OllamaModel[]>([]);
  const [running, setRunning] = useState<OllamaModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pullName, setPullName] = useState('');
  const [pulling, setPulling] = useState(false);
  const [pullStatus, setPullStatus] = useState('');
  const [pullProgress, setPullProgress] = useState(0);
  const [busyModel, setBusyModel] = useState('');
  const [activatingModel, setActivatingModel] = useState('');

  /** Keyed by name so each row can show its real residency, not just a yes/no. */
  const runningByName = useMemo(() => {
    const map = new Map<string, OllamaModel>();
    for (const model of running) map.set(model.name || model.model || '', model);
    return map;
  }, [running]);

  /** The configured model is what will serve the next request; whether it is
   *  resident is a separate fact, and the two disagreeing is worth surfacing. */
  const activeResidency = running.find(model => sameModel(model.name || model.model, activeModel));
  const strayLoaded = running
    .map(model => model.name || model.model || '')
    .filter(name => name && !sameModel(name, activeModel));

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const nextStatus = await apiJson<OllamaStatus>('/api/ollama/status');
      setStatus(nextStatus);
      if (!nextStatus.available) {
        setModels([]);
        setRunning([]);
        setError(nextStatus.error || 'Ollama is unavailable.');
        return;
      }
      const [modelData, runningData] = await Promise.all([
        apiJson<{ models: OllamaModel[] }>('/api/ollama/models'),
        apiJson<{ models: OllamaModel[] }>('/api/ollama/running'),
      ]);
      setModels(modelData.models || []);
      setRunning(runningData.models || []);
      setError('');
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : 'Could not connect to Ollama.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const pullModel = async () => {
    const model = pullName.trim();
    if (!model || pulling) return;
    setPulling(true);
    setPullStatus('Connecting to registry…');
    setPullProgress(0);
    setError('');
    try {
      const response = await fetch('/api/ollama/models/pull', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      if (!response.ok || !response.body) throw new Error(`Pull failed: HTTP ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (event.error) throw new Error(event.error);
          setPullStatus(event.status || 'Downloading…');
          if (event.total && event.completed != null) setPullProgress(Math.min(100, Math.round(event.completed / event.total * 100)));
        }
        if (done) break;
      }
      setPullStatus('Model installed');
      setPullProgress(100);
      setPullName('');
      onSelectModel(model);
      await refresh();
    } catch (pullError) {
      setError(pullError instanceof Error ? pullError.message : 'Model pull failed.');
      setPullStatus('');
    } finally {
      setPulling(false);
    }
  };

  const unloadModel = async (model: string) => {
    setBusyModel(model);
    try {
      await apiJson('/api/ollama/models/unload', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model }) });
      await refresh();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : 'Could not unload model.');
    } finally {
      setBusyModel('');
    }
  };

  const deleteModel = async (model: string) => {
    if (!window.confirm(`Delete local model “${model}”? This removes its files from Ollama.`)) return;
    setBusyModel(model);
    try {
      await apiJson(`/api/ollama/models/${encodeURIComponent(model)}`, { method: 'DELETE' });
      if (selectedModel === model) onSelectModel('');
      await refresh();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : 'Could not delete model.');
    } finally {
      setBusyModel('');
    }
  };

  /** Switches the live agent to this model immediately — no need to touch the rest of the config form below. */
  const activateModel = async (model: string) => {
    if (activatingModel || sameModel(model, activeModel)) return;
    setActivatingModel(model);
    setError('');
    try {
      const result = await apiJson<{ status: string; config: Partial<SystemConfig> }>('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      onSelectModel(model);
      onActivated?.(result.config);
      await refresh();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : `Could not activate “${model}”.`);
    } finally {
      setActivatingModel('');
    }
  };

  return (
    <section className="ollama-manager" aria-labelledby="ollama-manager-title">
      <header>
        <div className="ollama-manager-title"><span><Server size={18} /></span><div><h3 id="ollama-manager-title">Ollama Runtime</h3><p>{status?.base_url || 'Native local model provider'}</p></div></div>
        <div className={`ollama-health ${status?.available ? 'is-online' : 'is-offline'}`} role="status">
          {loading ? <Loader2 size={14} className="spin-slow" /> : status?.available ? <CheckCircle2 size={14} /> : <Unplug size={14} />}
          <span>{loading ? 'Checking…' : status?.available ? `Online · v${status.version || '?'}` : 'Offline'}</span>
        </div>
        <button type="button" className="ollama-refresh" onClick={() => void refresh()} disabled={loading} aria-label="Refresh Ollama"><RefreshCw size={15} /></button>
      </header>

      {error && <div className="ollama-error" role="alert"><AlertTriangle size={15} /><span>{error}</span></div>}

      <div className="ollama-runtime-stats">
        <div><HardDrive size={15} /><span>Installed</span><strong>{status?.models_count ?? models.length}</strong></div>
        <div><Cpu size={15} /><span>Loaded</span><strong>{status?.running_count ?? running.length}</strong></div>
        <div className="ollama-active-stat">
          <Zap size={15} /><span>Active now</span>
          <strong title={activeModel}>{activeModel || 'None'}</strong>
          {activeModel && (
            <em className={activeResidency ? 'is-resident' : ''}>
              {activeResidency ? `in VRAM · ${residencySummary(activeResidency)}` : 'not loaded — loads on first request'}
            </em>
          )}
        </div>
        {selectedModel && !sameModel(selectedModel, activeModel) && (
          <div><Box size={15} /><span>Pending in form below</span><strong title={selectedModel}>{selectedModel}</strong></div>
        )}
      </div>

      {strayLoaded.length > 0 && (
        <div className="ollama-warning" role="status">
          <AlertTriangle size={15} />
          <span>Loaded in VRAM but not the active model: <strong>{strayLoaded.join(', ')}</strong>. It still occupies GPU memory until it expires or you unload it.</span>
        </div>
      )}

      <div className="ollama-pull">
        <label><Download size={16} /><input value={pullName} onChange={event => setPullName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); void pullModel(); } }} placeholder="Model name, e.g. qwen3:8b" disabled={pulling || !status?.available} aria-label="Model to pull" /></label>
        <button type="button" onClick={() => void pullModel()} disabled={!pullName.trim() || pulling || !status?.available}>{pulling ? <Loader2 size={15} className="spin-slow" /> : <Download size={15} />}{pulling ? 'Pulling' : 'Pull model'}</button>
      </div>
      {pullStatus && <div className="ollama-pull-progress" role="status"><span>{pullStatus}<strong>{pullProgress}%</strong></span><i><b style={{ width: `${pullProgress}%` }} /></i></div>}

      <div className="ollama-model-list" role="list" aria-label="Installed Ollama models">
        {!loading && status?.available && !models.length && <div className="ollama-no-models">No local models installed. Pull a model above.</div>}
        {models.map(model => {
          const name = model.name || model.model || '';
          const residency = runningByName.get(name);
          const isRunning = Boolean(residency);
          const isActive = sameModel(name, activeModel);
          const busy = busyModel === name;
          const activating = activatingModel === name;
          return (
            <article key={name} role="listitem" className={`${selectedModel === name ? 'is-selected' : ''} ${isActive ? 'is-active' : ''}`}>
              <button type="button" className="ollama-model-select" onClick={() => onSelectModel(name)} aria-pressed={selectedModel === name}>
                <span className="ollama-model-icon"><Box size={17} /></span>
                <span className="ollama-model-info"><strong title={name}>{name}</strong><small>{residency ? residencySummary(residency) : `${model.details?.parameter_size || 'Local model'} · ${model.details?.quantization_level || model.details?.family || 'Ollama'} · ${formatBytes(model.size)}`}</small></span>
                <span className="ollama-model-badges">
                  {isActive && <span className="ollama-active-badge" title="This is the model the live agent uses right now"><Zap size={11} fill="currentColor" />Active</span>}
                  <span className={`ollama-running ${isRunning ? 'is-running' : ''}`} title={residency ? `Resident in GPU memory — ${residencySummary(residency)}` : 'Not in memory'}>{isRunning ? <><Play size={11} fill="currentColor" />Loaded</> : 'Idle'}</span>
                </span>
              </button>
              <div className="ollama-model-actions">
                {!isActive && (
                  <button type="button" className="primary" onClick={() => void activateModel(name)} disabled={activating || Boolean(activatingModel)} title="Switch the live agent to this model now">
                    {activating ? <Loader2 size={14} className="spin-slow" /> : <Zap size={14} />}
                    {activating ? 'Activating…' : 'Activate'}
                  </button>
                )}
                {isRunning && <button type="button" onClick={() => void unloadModel(name)} disabled={busy} title="Unload from memory"><Unplug size={14} /></button>}
                <button type="button" className="danger" onClick={() => void deleteModel(name)} disabled={busy} title="Delete local model">{busy ? <Loader2 size={14} className="spin-slow" /> : <Trash2 size={14} />}</button>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
