import { useCallback, useEffect, useState } from 'react';
import { BookOpen, Check, Pin, PinOff, Play, RefreshCw, ShieldAlert, X } from 'lucide-react';

// Agent self-development (backend/agent_learning_api.py). The routes are
// owner-only (agents.learning.*), so a 403 is an expected answer for every
// other role and renders as a quiet note, not an error banner.

export interface LearningTrack {
  id: string;
  agent_id: string;
  title: string;
  enabled: boolean;
  topics: string[];
  feeds: { kind?: string; repo?: string }[];
  cadence_hours: number;
  max_sources: number;
  max_items: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
}

export interface LearningRun {
  id: string;
  track_id: string;
  agent_id: string;
  trigger: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  sources_count: number;
  items_new: number;
  items_merged: number;
  items_quarantined: number;
  items_dropped: number;
  model: string;
  cost_usd: number;
  note_path: string;
  note_pending: number;
  error: string;
}

export interface LearningItem {
  id: string;
  agent_id: string;
  track_id: string;
  title: string;
  summary: string;
  why_it_matters: string;
  how_to_apply: string;
  tags: string[];
  confidence: string;
  source_urls: string[];
  status: string;
  quarantine_reason: string;
  pinned: number;
  created_at: string;
  expires_at: string | null;
}

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function authHeaders(): Record<string, string> {

  return { 'Content-Type': 'application/json',  };
}

async function api(path: string, init?: RequestInit): Promise<unknown> {
  const res = await fetch(path, { headers: authHeaders(), ...init });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(typeof data?.detail === 'string' ? data.detail : `Запрос не удался (${res.status})`, res.status);
  }
  return res.json();
}

// An error payload is an object; mapping over it would take the whole panel
// down (docs/CONTRACTS.md, «Фронтенд»). Rows without an id are dropped too —
// they cannot be keyed or acted on.
function rowsOf<T extends { id: string }>(data: unknown): T[] {
  if (!Array.isArray(data)) return [];
  return data.filter((row): row is T => !!row && typeof row === 'object' && typeof (row as T).id === 'string');
}

function isRow<T extends { id: string }>(data: unknown): data is T {
  return !!data && typeof data === 'object' && typeof (data as T).id === 'string';
}

const CADENCE_OPTIONS: { hours: number; label: string }[] = [
  { hours: 24, label: 'Каждый день' },
  { hours: 72, label: 'Раз в 3 дня' },
  { hours: 168, label: 'Раз в неделю' },
  { hours: 336, label: 'Раз в 2 недели' },
  { hours: 720, label: 'Раз в месяц' },
];

const RUN_STATUS: Record<string, { label: string; chip: string }> = {
  running: { label: 'идёт', chip: 'is-pending' },
  ok: { label: 'готово', chip: 'is-active' },
  skipped: { label: 'пропущен', chip: 'is-inactive' },
  failed: { label: 'ошибка', chip: 'is-revoked' },
  interrupted: { label: 'прерван', chip: 'is-revoked' },
};

const ITEM_STATUS_FILTERS: { value: string; label: string }[] = [
  { value: 'active', label: 'Действующие' },
  { value: 'rejected', label: 'Отклонённые' },
  { value: 'expired', label: 'Устаревшие' },
  { value: '', label: 'Все' },
];

const CONFIDENCE: Record<string, string> = { high: 'высокая', medium: 'средняя', low: 'низкая' };

function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}

function cadenceLabel(hours: number): string {
  return CADENCE_OPTIONS.find(option => option.hours === hours)?.label || `Каждые ${hours} ч`;
}

function TrackEditor({ track, onSaved, onRun, running }: {
  track: LearningTrack;
  onSaved: (track: LearningTrack) => void;
  onRun: (track: LearningTrack) => void;
  running: boolean;
}) {
  const [topicsText, setTopicsText] = useState(track.topics.join('\n'));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const save = async (fields: Partial<Pick<LearningTrack, 'enabled' | 'cadence_hours' | 'topics'>>) => {
    setSaving(true);
    setError('');
    try {
      const data = await api(`/api/agent-learning/tracks/${encodeURIComponent(track.id)}`, {
        method: 'PUT',
        body: JSON.stringify(fields),
      });
      if (!isRow<LearningTrack>(data)) throw new Error('Неожиданный ответ сервера');
      onSaved(data);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const topics = topicsText.split('\n').map(line => line.trim()).filter(Boolean);
  const topicsChanged = topics.join('\n') !== track.topics.join('\n');
  const cadenceOptions = CADENCE_OPTIONS.some(option => option.hours === track.cadence_hours)
    ? CADENCE_OPTIONS
    : [...CADENCE_OPTIONS, { hours: track.cadence_hours, label: cadenceLabel(track.cadence_hours) }];

  return (
    <div className="admin-item-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 10 }} data-testid={`learning-track-${track.id}`}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div className="admin-item-main">
          <div className="admin-item-name">{track.title || track.id}</div>
          <div className="admin-item-meta">
            Последний прогон: {formatDate(track.last_run_at)}{track.last_status ? ` (${RUN_STATUS[track.last_status]?.label || track.last_status})` : ''}
            {' · '}следующий: {track.enabled ? formatDate(track.next_run_at) : 'выключен'}
          </div>
        </div>
        <div className="admin-item-actions">
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.82rem' }}>
            <input type="checkbox" checked={track.enabled} disabled={saving}
              onChange={e => save({ enabled: e.target.checked })} />
            Включён
          </label>
          <select className="form-input" aria-label="Частота" value={track.cadence_hours} disabled={saving}
            onChange={e => save({ cadence_hours: Number(e.target.value) })} style={{ width: 'auto' }}>
            {cadenceOptions.map(option => <option key={option.hours} value={option.hours}>{option.label}</option>)}
          </select>
          <button type="button" className="btn-primary" disabled={running} onClick={() => onRun(track)}>
            <Play size={14} />
            <span>Запустить сейчас</span>
          </button>
        </div>
      </div>
      <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: '0.78rem', color: 'var(--text-dim)' }}>
        Темы (по одной на строку)
        <textarea className="form-input" rows={Math.min(6, Math.max(2, topics.length + 1))} value={topicsText}
          onChange={e => setTopicsText(e.target.value)} />
      </label>
      {track.feeds.length > 0 && (
        <div className="admin-item-meta">
          Ленты: {track.feeds.map(feed => feed.repo || feed.kind || '?').join(', ')}
        </div>
      )}
      {topicsChanged && (
        <button type="button" className="btn-ghost" style={{ alignSelf: 'flex-start' }} disabled={saving}
          onClick={() => save({ topics })}>
          {saving ? '...' : 'Сохранить темы'}
        </button>
      )}
      {error && <div style={{ color: 'var(--danger)', fontSize: '0.82rem' }}>{error}</div>}
    </div>
  );
}

function NotePreview({ path }: { path: string }) {
  const [content, setContent] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');

  const toggle = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (content !== null) return;
    try {
      const data = await api(`/api/obsidian/note?path=${encodeURIComponent(path)}`);
      const text = (data as { content?: unknown })?.content;
      if (typeof text !== 'string') throw new Error('Неожиданный ответ сервера');
      setContent(text);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div>
      <button type="button" className="admin-status-chip clickable" onClick={toggle} title={path}>
        <BookOpen size={12} /> {open ? 'Скрыть заметку' : 'Заметка Obsidian'}
      </button>
      {open && (
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: '0.75rem', maxHeight: 280, overflowY: 'auto', marginTop: 6, color: 'var(--text-muted)' }}>
          {error || (content ?? 'Загрузка...')}
        </pre>
      )}
    </div>
  );
}

function ItemCard({ item, onAction, busy }: {
  item: LearningItem;
  onAction: (item: LearningItem, action: 'approve' | 'reject' | 'pin' | 'unpin') => void;
  busy: boolean;
}) {
  return (
    <div className="admin-item-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }} data-testid={`learning-item-${item.id}`}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div className="admin-item-name">
          {item.pinned ? <Pin size={12} style={{ marginRight: 4 }} /> : null}
          {item.title}
        </div>
        <div className="admin-item-actions">
          {item.status !== 'active' && (
            <button type="button" className="btn-ghost" disabled={busy} onClick={() => onAction(item, 'approve')}>
              <Check size={14} /><span>Одобрить</span>
            </button>
          )}
          {item.status !== 'rejected' && (
            <button type="button" className="btn-ghost" disabled={busy} onClick={() => onAction(item, 'reject')}>
              <X size={14} /><span>Отклонить</span>
            </button>
          )}
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => onAction(item, item.pinned ? 'unpin' : 'pin')}>
            {item.pinned ? <PinOff size={14} /> : <Pin size={14} />}
            <span>{item.pinned ? 'Открепить' : 'Закрепить'}</span>
          </button>
        </div>
      </div>
      {item.status === 'quarantined' && item.quarantine_reason && (
        <div style={{ color: 'var(--warning, #f6b84a)', fontSize: '0.8rem' }}>Причина карантина: {item.quarantine_reason}</div>
      )}
      <div style={{ fontSize: '0.85rem' }}>{item.summary}</div>
      {item.how_to_apply && <div className="admin-item-meta">Как применять: {item.how_to_apply}</div>}
      <div className="admin-item-meta">
        Уверенность: {CONFIDENCE[item.confidence] || item.confidence} · {formatDate(item.created_at)}
        {item.expires_at && !item.pinned ? ` · до ${formatDate(item.expires_at)}` : ''}
        {item.tags.length > 0 ? ` · ${item.tags.join(', ')}` : ''}
      </div>
      {item.source_urls.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: '0.75rem' }}>
          {item.source_urls.map(url => (
            <a key={url} href={url} target="_blank" rel="noopener noreferrer nofollow" style={{ color: 'var(--text-dim)', wordBreak: 'break-all' }}>{url}</a>
          ))}
        </div>
      )}
    </div>
  );
}

export function AgentLearningPanel({ agentId }: { agentId: string }) {
  const [tracks, setTracks] = useState<LearningTrack[]>([]);
  const [runs, setRuns] = useState<LearningRun[]>([]);
  const [items, setItems] = useState<LearningItem[]>([]);
  const [quarantine, setQuarantine] = useState<LearningItem[]>([]);
  const [statusFilter, setStatusFilter] = useState('active');
  const [forbidden, setForbidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busyItem, setBusyItem] = useState('');

  const agentQuery = `agent_id=${encodeURIComponent(agentId)}`;

  const loadRuns = useCallback(async (trackList: LearningTrack[]) => {
    const perTrack = await Promise.all(trackList.map(track =>
      api(`/api/agent-learning/runs?track_id=${encodeURIComponent(track.id)}&limit=20`).then(rowsOf<LearningRun>)));
    setRuns(perTrack.flat().sort((a, b) => String(b.started_at).localeCompare(String(a.started_at))));
  }, []);

  const loadItems = useCallback(async () => {
    const statusQuery = statusFilter ? `&status=${encodeURIComponent(statusFilter)}` : '';
    const [listed, quarantined] = await Promise.all([
      api(`/api/agent-learning/items?${agentQuery}${statusQuery}`),
      api(`/api/agent-learning/items?${agentQuery}&status=quarantined`),
    ]);
    // "Все" would list quarantined items twice — they have their own block.
    setItems(rowsOf<LearningItem>(listed).filter(item => item.status !== 'quarantined'));
    setQuarantine(rowsOf<LearningItem>(quarantined));
  }, [agentQuery, statusFilter]);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const trackList = rowsOf<LearningTrack>(await api(`/api/agent-learning/tracks?${agentQuery}`));
      setTracks(trackList);
      setForbidden(false);
      await Promise.all([loadRuns(trackList), loadItems()]);
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 403) setForbidden(true);
      else setError((exc as Error).message);
    } finally {
      setLoading(false);
    }
  }, [agentQuery, loadItems, loadRuns]);

  useEffect(() => { load(); }, [load]);

  // A run takes minutes; refresh while one is going so the history and the
  // knowledge list catch up without the owner reloading the page.
  const anyRunning = runs.some(run => run.status === 'running');
  useEffect(() => {
    if (!anyRunning) return undefined;
    const timer = window.setTimeout(() => { load(); }, 5000);
    return () => window.clearTimeout(timer);
  }, [anyRunning, runs, load]);

  const runNow = async (track: LearningTrack) => {
    setError('');
    setNotice('');
    try {
      await api(`/api/agent-learning/tracks/${encodeURIComponent(track.id)}/run`, { method: 'POST' });
      setNotice('Прогон запущен — займёт несколько минут.');
      await loadRuns(tracks);
    } catch (exc) {
      // 409 = another run is going (one per process); the backend's detail says so.
      setError((exc as Error).message);
    }
  };

  const itemAction = async (item: LearningItem, action: 'approve' | 'reject' | 'pin' | 'unpin') => {
    setBusyItem(item.id);
    setError('');
    try {
      const base = `/api/agent-learning/items/${encodeURIComponent(item.id)}`;
      const data = action === 'pin' || action === 'unpin'
        ? await api(`${base}/pin`, { method: 'POST', body: JSON.stringify({ pinned: action === 'pin' }) })
        : await api(`${base}/${action}`, { method: 'POST' });
      if (!isRow<LearningItem>(data)) throw new Error('Неожиданный ответ сервера');
      await loadItems();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusyItem('');
    }
  };

  if (forbidden) {
    return (
      <div className="admin-form-section">
        <h4>Саморазвитие</h4>
        <p className="admin-form-section-hint">Раздел доступен только владельцу.</p>
      </div>
    );
  }

  const trackTitle = (trackId: string) => tracks.find(track => track.id === trackId)?.title || trackId;

  return (
    <div className="admin-form-section" data-testid="agent-learning-panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
        <h4>Саморазвитие</h4>
        <button type="button" className="icon-btn" title="Обновить" onClick={() => load()} disabled={loading}>
          <RefreshCw size={14} />
        </button>
      </div>
      <p className="admin-form-section-hint">
        Агент по расписанию читает новые источники по своим темам и сохраняет выжимку в свои знания.
        Пункты с подозрительным содержимым попадают в карантин и не используются, пока вы их не одобрите.
      </p>
      {error && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginBottom: 8 }}>{error}</div>}
      {notice && <div style={{ color: 'var(--success)', fontSize: '0.85rem', marginBottom: 8 }}>{notice}</div>}

      <h5 style={{ margin: '12px 0 6px' }}>Треки</h5>
      {tracks.length === 0 && !loading && <div className="admin-empty">У агента нет треков обучения.</div>}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {tracks.map(track => (
          // Keyed on the topics too: a saved track remounts with the server's copy.
          <TrackEditor key={`${track.id}:${track.topics.join('\n')}`} track={track} running={anyRunning}
            onSaved={saved => setTracks(prev => prev.map(row => (row.id === saved.id ? saved : row)))}
            onRun={runNow} />
        ))}
      </div>

      <h5 style={{ margin: '16px 0 6px' }}>История прогонов</h5>
      {runs.length === 0 ? <div className="admin-empty">Прогонов ещё не было.</div> : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Начало</th><th>Трек</th><th>Статус</th><th>Источники</th><th>Новые</th>
                <th>Объединены</th><th>Карантин</th><th>Стоимость</th><th>Заметка</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(run => (
                <tr key={run.id} data-testid={`learning-run-${run.id}`}>
                  <td>{formatDate(run.started_at)}{run.trigger === 'manual' ? ' · вручную' : ''}</td>
                  <td>{trackTitle(run.track_id)}</td>
                  <td>
                    <span className={`admin-status-chip ${RUN_STATUS[run.status]?.chip || ''}`} title={run.error || undefined}>
                      <i className="dot" />{RUN_STATUS[run.status]?.label || run.status}
                    </span>
                  </td>
                  <td>{run.sources_count}</td>
                  <td>{run.items_new}</td>
                  <td>{run.items_merged}</td>
                  <td>{run.items_quarantined}</td>
                  <td>${Number(run.cost_usd || 0).toFixed(4)}</td>
                  <td>
                    {run.note_path ? <NotePreview path={run.note_path} />
                      : run.note_pending ? <span className="admin-item-meta">ждёт Obsidian</span> : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {quarantine.length > 0 && (
        <>
          <h5 style={{ margin: '16px 0 6px', display: 'flex', alignItems: 'center', gap: 6, color: 'var(--warning, #f6b84a)' }}>
            <ShieldAlert size={14} /> Карантин ({quarantine.length})
          </h5>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }} data-testid="learning-quarantine">
            {quarantine.map(item => (
              <ItemCard key={item.id} item={item} onAction={itemAction} busy={busyItem === item.id} />
            ))}
          </div>
        </>
      )}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '16px 0 6px', gap: 8 }}>
        <h5 style={{ margin: 0 }}>Знания</h5>
        <select className="form-input" aria-label="Статус знаний" value={statusFilter}
          onChange={e => setStatusFilter(e.target.value)} style={{ width: 'auto' }}>
          {ITEM_STATUS_FILTERS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </div>
      {items.length === 0 ? <div className="admin-empty">Пусто.</div> : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {items.map(item => (
            <ItemCard key={item.id} item={item} onAction={itemAction} busy={busyItem === item.id} />
          ))}
        </div>
      )}
    </div>
  );
}
