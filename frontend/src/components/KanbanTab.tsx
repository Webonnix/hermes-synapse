import { useCallback, useEffect, useMemo, useState } from 'react';
import { ExternalLink, Plus, RefreshCw, Rocket, ShieldAlert, User } from 'lucide-react';
import type { AgentModel, DevRun, DevRunEvent, DevRunStatus } from '../types';

type Props = {
  language: 'ru' | 'en';
  lastEvent?: DevRunEvent | null;
  agents: AgentModel[];
};

const COPY = {
  ru: {
    title: 'Kanban — задачи агентов', subtitle: 'Создавайте задачи, назначайте агентам, следите за прогрессом и демо',
    newTask: 'Новая задача', goalPlaceholder: 'Что нужно сделать, например: собери лендинг для...',
    assignee: 'Исполнитель', auto: 'Авто', toBacklog: 'В бэклог', startNow: 'Начать сразу',
    refresh: 'Обновить', demo: 'Демо', unassigned: 'не назначен',
    failedLoad: 'Не удалось загрузить задачи.', empty: 'Пусто',
    columns: {
      backlog: 'Бэклог', planned: 'To Do', running: 'В работе',
      review: 'Проверка', paused: 'Заблокировано', done: 'Готово', failed: 'Ошибка',
    },
    dropHintStart: 'Перетащите сюда карточку из Бэклога, чтобы запустить её',
    dropHintResume: 'Перетащите сюда карточку из Проверки/Заблокировано, чтобы продолжить',
  },
  en: {
    title: 'Kanban — agent tasks', subtitle: 'Create tasks, assign agents, track progress and demos',
    newTask: 'New task', goalPlaceholder: 'What needs to be done, e.g.: build a landing page for...',
    assignee: 'Assignee', auto: 'Auto', toBacklog: 'To backlog', startNow: 'Start now',
    refresh: 'Refresh', demo: 'Demo', unassigned: 'unassigned',
    failedLoad: 'Could not load tasks.', empty: 'Empty',
    columns: {
      backlog: 'Backlog', planned: 'To Do', running: 'In Progress',
      review: 'Review', paused: 'Blocked', done: 'Done', failed: 'Failed',
    },
    dropHintStart: 'Drop a Backlog card here to start it',
    dropHintResume: 'Drop a Review/Blocked card here to resume it',
  },
} as const;

type ColumnKey = 'backlog' | 'planned' | 'running' | 'review' | 'paused' | 'done' | 'failed';

const COLUMN_STATUSES: Record<ColumnKey, DevRunStatus[]> = {
  backlog: ['backlog'],
  planned: ['planned'],
  running: ['running'],
  review: ['verifying', 'awaiting_approval'],
  paused: ['paused'],
  done: ['done'],
  failed: ['failed', 'cancelled'],
};

const COLUMN_ORDER: ColumnKey[] = ['backlog', 'planned', 'running', 'review', 'paused', 'done', 'failed'];

function shortTime(value?: string | null) {
  if (!value) return '—';
  const ts = Date.parse(value);
  if (Number.isNaN(ts)) return value;
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' }).format(ts);
}

export function KanbanTab({ language, lastEvent, agents }: Props) {
  const copy = COPY[language];
  const [runs, setRuns] = useState<DevRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [showNew, setShowNew] = useState(false);
  const [goal, setGoal] = useState('');
  const [assignee, setAssignee] = useState('');
  const [dragId, setDragId] = useState('');

  const agentName = useCallback((id?: string | null) => {
    if (!id) return copy.unassigned;
    return agents.find(a => a.id === id)?.name || id;
  }, [agents, copy.unassigned]);

  const loadRuns = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await fetch('/api/dev-runs?limit=200');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setRuns(await response.json() as DevRun[]);
      setError('');
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : copy.failedLoad);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [copy.failedLoad]);

  useEffect(() => {
    void loadRuns();
    const interval = window.setInterval(() => void loadRuns(true), 6000);
    return () => window.clearInterval(interval);
  }, [loadRuns]);

  useEffect(() => {
    if (lastEvent) void loadRuns(true);
  }, [lastEvent, loadRuns]);

  const byColumn = useMemo(() => {
    const grouped: Record<ColumnKey, DevRun[]> = { backlog: [], planned: [], running: [], review: [], paused: [], done: [], failed: [] };
    for (const run of runs) {
      const column = COLUMN_ORDER.find(key => COLUMN_STATUSES[key].includes(run.status));
      if (column) grouped[column].push(run);
    }
    return grouped;
  }, [runs]);

  const post = async (path: string, body?: Record<string, unknown>) => {
    setBusy(path);
    try {
      const response = await fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      await loadRuns(true);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusy('');
    }
  };

  const createTask = async (start: boolean) => {
    if (!goal.trim()) return;
    setBusy('create');
    try {
      const response = await fetch('/api/dev-runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: goal.trim(), assignee_agent_id: assignee || null, start }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      setGoal('');
      setAssignee('');
      setShowNew(false);
      await loadRuns(true);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusy('');
    }
  };

  const handleDrop = (column: ColumnKey) => {
    if (!dragId) return;
    const run = runs.find(r => r.id === dragId);
    setDragId('');
    if (!run) return;
    if (column === 'planned' && run.status === 'backlog') {
      void post(`/api/dev-runs/${run.id}/start`);
    } else if (column === 'running' && ['paused', 'awaiting_approval'].includes(run.status)) {
      void post(`/api/dev-runs/${run.id}/resume`);
    }
  };

  if (loading && !runs.length) {
    return <div className="control-loading"><RefreshCw className="spin-slow" size={20} />Kanban</div>;
  }

  return (
    <div className="kanban-page">
      <header className="control-header">
        <div>
          <span className="control-eyebrow"><Rocket size={14} />KANBAN</span>
          <h2>{copy.title}</h2>
          <p>{copy.subtitle}</p>
        </div>
        <div className="control-actions">
          <button type="button" className="icon-btn" onClick={() => void loadRuns()} title={copy.refresh} aria-label={copy.refresh}>
            <RefreshCw size={16} />
          </button>
          <button type="button" className="icon-btn" onClick={() => setShowNew(open => !open)} title={copy.newTask} aria-label={copy.newTask}>
            <Plus size={16} />
          </button>
        </div>
      </header>

      {error && <div className="control-alert"><ShieldAlert size={15} />{error}</div>}

      {showNew && (
        <section className="kanban-create">
          <input
            value={goal}
            onChange={event => setGoal(event.target.value)}
            placeholder={copy.goalPlaceholder}
            aria-label={copy.newTask}
          />
          <select value={assignee} onChange={event => setAssignee(event.target.value)} aria-label={copy.assignee}>
            <option value="">{copy.auto}</option>
            {agents.filter(a => a.is_enabled !== false).map(a => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
          <button type="button" disabled={!goal.trim() || busy === 'create'} onClick={() => void createTask(false)}>
            {copy.toBacklog}
          </button>
          <button type="button" disabled={!goal.trim() || busy === 'create'} onClick={() => void createTask(true)}>
            {copy.startNow}
          </button>
        </section>
      )}

      <div className="kanban-board">
        {COLUMN_ORDER.map(column => (
          <section
            key={column}
            className="kanban-column"
            onDragOver={event => event.preventDefault()}
            onDrop={() => handleDrop(column)}
          >
            <header className="kanban-column-head">
              <span>{copy.columns[column]}</span>
              <span className="kanban-count">{byColumn[column].length}</span>
            </header>
            <div className="kanban-column-body">
              {!byColumn[column].length && (
                <p className="kanban-empty">
                  {column === 'planned' ? copy.dropHintStart : column === 'running' ? copy.dropHintResume : copy.empty}
                </p>
              )}
              {byColumn[column].map(run => (
                <article
                  key={run.id}
                  className="kanban-card"
                  draggable
                  onDragStart={() => setDragId(run.id)}
                  onDragEnd={() => setDragId('')}
                >
                  <p className="kanban-card-goal">{run.goal}</p>
                  <div className="kanban-card-meta">
                    <span className="kanban-card-assignee"><User size={11} />{agentName(run.assignee_agent_id)}</span>
                    <span className="kanban-card-time">{shortTime(run.created_at)}</span>
                  </div>
                  {run.status_reason && <p className="kanban-card-reason">{run.status_reason}</p>}
                  {run.demo_url && (
                    <a className="kanban-card-demo" href={run.demo_url} target="_blank" rel="noreferrer">
                      <ExternalLink size={12} />{copy.demo}
                    </a>
                  )}
                </article>
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
