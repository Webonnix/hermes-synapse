import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ExternalLink, GitBranch, History, Plus, RefreshCw, Rocket, ShieldAlert, User, X } from 'lucide-react';
import type { AgentModel, DevRun, DevRunEvent, DevRunRevision, DevRunStatus } from '../types';

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
    refine: 'Доработать', versions: 'Версии', cancelRefine: 'Отменить доработку',
    refineOf: 'Доработка:', refinePlaceholder: 'Что изменить или добавить в этой версии...',
    revisionsTitle: 'Версии продукта', close: 'Закрыть',
    live: 'текущая', makeLive: 'Сделать текущей', openRevision: 'Открыть',
    noSnapshot: 'Сборка удалена', loadingRevisions: 'Загружаем версии…',
    noRevisions: 'У этой задачи ещё нет версий.',
    liveUrlHint: 'Постоянная ссылка на продукт — всегда показывает текущую версию.',
    revisionShort: 'v', waitingFor: 'Ждёт завершения',
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
    refine: 'Refine', versions: 'Versions', cancelRefine: 'Cancel refinement',
    refineOf: 'Refining:', refinePlaceholder: 'What to change or add in this revision...',
    revisionsTitle: 'Product revisions', close: 'Close',
    live: 'live', makeLive: 'Make it live', openRevision: 'Open',
    noSnapshot: 'Build deleted', loadingRevisions: 'Loading revisions…',
    noRevisions: 'This task has no revisions yet.',
    liveUrlHint: 'Permanent product link — always serves the current revision.',
    revisionShort: 'v', waitingFor: 'Waiting for',
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

// A continuation clones its parent's working tree, so the parent must have
// stopped moving — refining a card that is still executing would fork a tree
// mid-write. Terminal statuses only; a paused card is resumed, not forked.
const REFINABLE_STATUSES: DevRunStatus[] = ['done', 'failed', 'cancelled'];

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
  const [parent, setParent] = useState<DevRun | null>(null);
  const [revisionsFor, setRevisionsFor] = useState<DevRun | null>(null);
  const [revisions, setRevisions] = useState<DevRunRevision[]>([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);

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

  // How many cards each chain has, so a badge only appears where it means
  // something (a product with more than one revision).
  const chainSize = useMemo(() => {
    const sizes = new Map<string, number>();
    for (const run of runs) {
      const root = run.root_run_id || run.id;
      sizes.set(root, (sizes.get(root) || 0) + 1);
    }
    return sizes;
  }, [runs]);

  const runsById = useMemo(() => new Map(runs.map(run => [run.id, run])), [runs]);

  /** Mirrors dev_runs.waits_for_parent: a queued continuation is not stuck,
   *  it is waiting for the revision it continues to stop writing its tree. */
  const waitingForParent = useCallback((run: DevRun): DevRun | null => {
    if (run.status !== 'planned' || !run.parent_run_id) return null;
    const parentRun = runsById.get(run.parent_run_id);
    if (!parentRun) return null;
    const busy: DevRunStatus[] = ['planned', 'running', 'verifying', 'paused', 'awaiting_approval'];
    return busy.includes(parentRun.status) ? parentRun : null;
  }, [runsById]);

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
      return true;
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
      return false;
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
        body: JSON.stringify({
          goal: goal.trim(),
          assignee_agent_id: assignee || null,
          start,
          parent_run_id: parent?.id || null,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      setGoal('');
      setAssignee('');
      setParent(null);
      setShowNew(false);
      await loadRuns(true);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusy('');
    }
  };

  const startRefine = (run: DevRun) => {
    setParent(run);
    setGoal('');
    // The continuation stays with the agent that already knows this product;
    // the backend applies the same default, so "Auto" here means "inherit".
    setAssignee('');
    setShowNew(true);
    setRevisionsFor(null);
  };

  const loadRevisions = useCallback(async (run: DevRun) => {
    setRevisionsFor(run);
    setRevisionsLoading(true);
    try {
      const response = await fetch(`/api/dev-runs/${run.id}/lineage`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setRevisions(await response.json() as DevRunRevision[]);
      setError('');
    } catch (nextError) {
      setRevisions([]);
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setRevisionsLoading(false);
    }
  }, []);

  const promote = async (revision: DevRunRevision) => {
    const ok = await post(`/api/dev-runs/${revision.id}/promote`);
    if (ok && revisionsFor) await loadRevisions(revisionsFor);
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
          <button type="button" className="icon-btn" onClick={() => { setParent(null); setShowNew(open => !open); }} title={copy.newTask} aria-label={copy.newTask}>
            <Plus size={16} />
          </button>
        </div>
      </header>

      {error && <div className="control-alert"><ShieldAlert size={15} />{error}</div>}

      {showNew && (
        <section className="kanban-create">
          {parent && (
            <p className="kanban-refine-of">
              <GitBranch size={12} />
              <span>{copy.refineOf} {parent.goal}</span>
              <button type="button" onClick={() => setParent(null)} aria-label={copy.cancelRefine} title={copy.cancelRefine}>
                <X size={12} />
              </button>
            </p>
          )}
          <input
            value={goal}
            onChange={event => setGoal(event.target.value)}
            placeholder={parent ? copy.refinePlaceholder : copy.goalPlaceholder}
            aria-label={parent ? copy.refine : copy.newTask}
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
              {byColumn[column].map(run => {
                const revision = run.revision || 1;
                const inChain = revision > 1 || (chainSize.get(run.root_run_id || run.id) || 1) > 1;
                const canRefine = REFINABLE_STATUSES.includes(run.status);
                const blockedBy = waitingForParent(run);
                return (
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
                    {blockedBy && (
                      <p className="kanban-card-waiting">
                        <History size={11} />
                        {copy.waitingFor} {copy.revisionShort}{blockedBy.revision || 1}
                      </p>
                    )}
                    {run.status_reason && <p className="kanban-card-reason">{run.status_reason}</p>}
                    <div className="kanban-card-actions">
                      {inChain && (
                        <span className="kanban-revision" title={copy.versions}>
                          {copy.revisionShort}{revision}
                        </span>
                      )}
                      {run.demo_url && (
                        <a className="kanban-card-demo" href={run.demo_url} target="_blank" rel="noreferrer" title={copy.liveUrlHint}>
                          <ExternalLink size={12} />{copy.demo}
                        </a>
                      )}
                      {canRefine && (
                        <button type="button" className="kanban-card-action" onClick={() => startRefine(run)}>
                          <GitBranch size={12} />{copy.refine}
                        </button>
                      )}
                      {(inChain || run.demo_url) && (
                        <button type="button" className="kanban-card-action" onClick={() => void loadRevisions(run)}>
                          <History size={12} />{copy.versions}
                        </button>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        ))}
      </div>

      {revisionsFor && (
        <aside className="kanban-drawer" role="dialog" aria-label={copy.revisionsTitle}>
          <header>
            <span><History size={14} />{copy.revisionsTitle}</span>
            <button type="button" className="icon-btn" onClick={() => setRevisionsFor(null)} aria-label={copy.close} title={copy.close}>
              <X size={15} />
            </button>
          </header>
          <div className="kanban-drawer-body">
            {revisionsLoading && <p className="kanban-empty">{copy.loadingRevisions}</p>}
            {!revisionsLoading && !revisions.length && <p className="kanban-empty">{copy.noRevisions}</p>}
            {!revisionsLoading && revisions.map(revision => (
              <article key={revision.id} className={`kanban-revision-row${revision.is_live ? ' is-live' : ''}`}>
                <div className="kanban-revision-head">
                  <span className="kanban-revision">{copy.revisionShort}{revision.revision || 1}</span>
                  {revision.is_live && <span className="kanban-revision-live"><Check size={11} />{copy.live}</span>}
                  <span className="kanban-card-time">{shortTime(revision.created_at)}</span>
                </div>
                <p className="kanban-card-goal">{revision.goal}</p>
                <div className="kanban-card-actions">
                  {revision.has_snapshot ? (
                    <a className="kanban-card-demo" href={revision.demo_snapshot_url || `/demo/${revision.id}/`} target="_blank" rel="noreferrer">
                      <ExternalLink size={12} />{copy.openRevision}
                    </a>
                  ) : (
                    <span className="kanban-revision-gone">{copy.noSnapshot}</span>
                  )}
                  {revision.has_snapshot && !revision.is_live && (
                    <button
                      type="button"
                      className="kanban-card-action"
                      disabled={busy === `/api/dev-runs/${revision.id}/promote`}
                      onClick={() => void promote(revision)}
                    >
                      <Check size={12} />{copy.makeLive}
                    </button>
                  )}
                  {REFINABLE_STATUSES.includes(revision.status) && (
                    <button type="button" className="kanban-card-action" onClick={() => startRefine(revision)}>
                      <GitBranch size={12} />{copy.refine}
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        </aside>
      )}
    </div>
  );
}
