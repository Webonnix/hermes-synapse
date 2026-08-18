import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Ban,
  CircleDot,
  Download,
  ExternalLink,
  GitCommitHorizontal,
  Hammer,
  LayoutGrid,
  ListChecks,
  ListTree,
  MoreVertical,
  Pause,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
  Trash2,
  Wrench,
} from 'lucide-react';
import type { DevRun, DevRunEvent } from '../types';
import { DiffViewer, looksLikeUnifiedDiff } from './DiffViewer';

type Props = {
  language: 'ru' | 'en';
  lastEvent?: DevRunEvent | null;
  giteaBaseUrl?: string;
};

const COPY = {
  ru: {
    title: 'Dev Runs — Mission Control', subtitle: 'Автономные прогоны разработки в песочнице dev-repo',
    newRun: 'Новый прогон', goalPlaceholder: 'Цель прогона, например: добавь эндпоинт /health в dev-repo',
    start: 'Запустить', refresh: 'Обновить', empty: 'Прогонов пока нет. Запустите первый, указав цель.',
    steps: 'Лента шагов', selectRun: 'Выберите прогон, чтобы увидеть живую ленту шагов.',
    pause: 'Пауза', resume: 'Продолжить', cancel: 'Отменить', confirmCancel: 'Отменить этот прогон?',
    iterations: 'Итерации', cost: 'Стоимость', time: 'Время', noDeadline: 'без дедлайна',
    report: 'Финальный отчёт', changedFiles: 'Изменения', tests: 'Тесты', commit: 'Коммит',
    openGitea: 'Открыть в Gitea', reason: 'Причина', goal: 'Цель',
    noSteps: 'Шагов ещё нет.', failedLoad: 'Не удалось загрузить dev-runs.',
    viewRuns: 'Прогоны', viewShowcase: 'Витрина демо', openDemo: 'Открыть демо',
    noDemos: 'Опубликованных демо пока нет — агент вызывает dev_publish_demo, когда результат готов к показу.',
    published: 'опубликовано',
    cardMenu: 'Действия', refine: 'Доработки', download: 'Скачать', deleteRun: 'Удалить',
    confirmDeleteRun: 'Удалить прогон и его демо? Это необратимо.',
  },
  en: {
    title: 'Dev Runs — Mission Control', subtitle: 'Autonomous development runs in the dev-repo sandbox',
    newRun: 'New run', goalPlaceholder: 'Run goal, e.g.: add a /health endpoint to the dev-repo',
    start: 'Start', refresh: 'Refresh', empty: 'No runs yet. Start the first one by stating a goal.',
    steps: 'Step feed', selectRun: 'Select a run to see its live step feed.',
    pause: 'Pause', resume: 'Resume', cancel: 'Cancel', confirmCancel: 'Cancel this run?',
    iterations: 'Iterations', cost: 'Cost', time: 'Time', noDeadline: 'no deadline',
    report: 'Final report', changedFiles: 'Changes', tests: 'Tests', commit: 'Commit',
    openGitea: 'Open in Gitea', reason: 'Reason', goal: 'Goal',
    noSteps: 'No steps yet.', failedLoad: 'Could not load dev-runs.',
    viewRuns: 'Runs', viewShowcase: 'Demo showcase', openDemo: 'Open demo',
    noDemos: 'No published demos yet — the agent calls dev_publish_demo once a result is ready to show.',
    published: 'published',
    cardMenu: 'Actions', refine: 'Refine', download: 'Download', deleteRun: 'Delete',
    confirmDeleteRun: 'Delete this run and its demo? This cannot be undone.',
  },
} as const;

const STATUS_LABEL: Record<string, [string, string]> = {
  planned: ['Запланирован', 'Planned'], running: ['Выполняется', 'Running'], paused: ['Пауза', 'Paused'],
  awaiting_approval: ['Ждёт approval', 'Awaiting approval'], verifying: ['Проверка', 'Verifying'],
  done: ['Готово', 'Done'], failed: ['Ошибка', 'Failed'], cancelled: ['Отменён', 'Cancelled'],
};

function shortTime(value?: string | null) {
  if (!value) return '—';
  const ts = Date.parse(value);
  if (Number.isNaN(ts)) return value;
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' }).format(ts);
}

function ProgressBar({ label, used, budget, format }: {
  label: string; used: number; budget: number | null; format?: (v: number) => string;
}) {
  const show = format || ((v: number) => String(v));
  const ratio = budget && budget > 0 ? Math.min(1, used / budget) : 0;
  const level = ratio >= 0.95 ? 'is-critical' : ratio >= 0.8 ? 'is-warning' : '';
  return (
    <div className="devrun-progress">
      <span className="devrun-progress-label">{label}</span>
      <div className="devrun-progress-track" role="progressbar" aria-valuenow={Math.round(ratio * 100)} aria-valuemin={0} aria-valuemax={100}>
        <div className={`devrun-progress-fill ${level}`} style={{ width: `${ratio * 100}%` }} />
      </div>
      <span className="devrun-progress-value">{show(used)}{budget ? ` / ${show(budget)}` : ''}</span>
    </div>
  );
}

export function DevRunsTab({ language, lastEvent, giteaBaseUrl }: Props) {
  const copy = COPY[language];
  const [runs, setRuns] = useState<DevRun[]>([]);
  const [selected, setSelected] = useState<DevRun | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [goal, setGoal] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<'runs' | 'showcase'>('runs');
  const [openCardMenu, setOpenCardMenu] = useState('');
  const cardMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!openCardMenu) return;
    const onDocClick = (event: MouseEvent) => {
      if (cardMenuRef.current && !cardMenuRef.current.contains(event.target as Node)) setOpenCardMenu('');
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [openCardMenu]);

  const loadRuns = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await fetch('/api/dev-runs?limit=100');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setRuns(await response.json() as DevRun[]);
      setError('');
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : copy.failedLoad);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [copy.failedLoad]);

  const loadSelected = useCallback(async (runId: string) => {
    if (!runId) return;
    try {
      const response = await fetch(`/api/dev-runs/${encodeURIComponent(runId)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setSelected(await response.json() as DevRun);
    } catch {
      /* transient — the poller retries */
    }
  }, []);

  useEffect(() => {
    void loadRuns();
    const interval = window.setInterval(() => void loadRuns(true), 7000);
    return () => window.clearInterval(interval);
  }, [loadRuns]);

  useEffect(() => {
    if (selectedId) void loadSelected(selectedId);
  }, [selectedId, loadSelected]);

  // Live updates: any dev_run_event refreshes the list and, when it concerns
  // the opened run, its step feed.
  useEffect(() => {
    if (!lastEvent) return;
    void loadRuns(true);
    if (lastEvent.run_id === selectedId) void loadSelected(selectedId);
  }, [lastEvent, selectedId, loadRuns, loadSelected]);

  const post = async (path: string) => {
    setBusy(path);
    try {
      const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      await loadRuns(true);
      if (selectedId) await loadSelected(selectedId);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusy('');
    }
  };

  const createRun = async () => {
    if (!goal.trim()) return;
    setBusy('create');
    try {
      const response = await fetch('/api/dev-runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: goal.trim() }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const run = await response.json() as DevRun;
      setGoal('');
      setSelectedId(run.id);
      await loadRuns(true);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusy('');
    }
  };

  const statusOf = (run: DevRun) => STATUS_LABEL[run.status]?.[language === 'ru' ? 0 : 1] || run.status;

  // "Refine" reuses the existing Runs-tab inspector (resume/pause/cancel,
  // step feed) rather than duplicating those controls in the showcase card —
  // it just navigates the owner to that run.
  const refineRun = (run: DevRun) => {
    setOpenCardMenu('');
    setView('runs');
    setSelectedId(run.id);
  };

  const downloadDemo = async (run: DevRun) => {
    setOpenCardMenu('');
    // A plain window.open() bypasses the app's fetch-based auth interceptor
    // (window.fetch is monkey-patched to attach the Bearer token — see
    // utils.tsx's initFetchInterceptor — but raw navigations never go through
    // it), so this fetches the zip itself and saves it via a blob URL.
    try {
      const response = await fetch(`/api/dev-runs/${run.id}/download`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${run.id}-demo.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    }
  };

  const deleteRun = async (run: DevRun) => {
    setOpenCardMenu('');
    if (!window.confirm(copy.confirmDeleteRun)) return;
    setBusy(`delete-${run.id}`);
    try {
      const response = await fetch(`/api/dev-runs/${run.id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (selectedId === run.id) { setSelected(null); setSelectedId(''); }
      await loadRuns(true);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusy('');
    }
  };

  // Every run that has ever called dev_publish_demo — a run can be cancelled/
  // failed afterward and its demo stays valid (the /demo/<id>/ files on disk
  // aren't touched by status changes), so this deliberately isn't filtered by
  // status the way the workspace's active-run list implicitly is.
  const demos = useMemo(() => runs.filter(run => run.demo_url), [runs]);

  const report = useMemo(() => {
    if (!selected?.steps || !['done', 'failed', 'cancelled'].includes(selected.status)) return null;
    const steps = selected.steps;
    const commits = steps.filter(step => step.tool === 'git_commit' && step.status === 'done');
    const verify = [...steps].reverse().find(step => step.phase === 'verify');
    const diffStep = [...steps].reverse().find(step => looksLikeUnifiedDiff(step.summary));
    const final = [...steps].reverse().find(step => step.phase === 'observe');
    return { commits, verify, diffStep, final };
  }, [selected]);

  if (loading && !runs.length) {
    return <div className="control-loading"><RefreshCw className="spin-slow" size={20} />Dev Runs</div>;
  }

  return (
    <div className="devruns-page">
      <header className="control-header">
        <div>
          <span className="control-eyebrow"><Rocket size={14} />DEV RUNS</span>
          <h2>{copy.title}</h2>
          <p>{copy.subtitle}</p>
        </div>
        <div className="control-actions">
          <div className="admin-subnav" role="tablist">
            <button type="button" role="tab" aria-selected={view === 'runs'} className={view === 'runs' ? 'is-active' : ''} onClick={() => setView('runs')}>
              <ListTree size={14} />{copy.viewRuns}
            </button>
            <button type="button" role="tab" aria-selected={view === 'showcase'} className={view === 'showcase' ? 'is-active' : ''} onClick={() => setView('showcase')}>
              <LayoutGrid size={14} />{copy.viewShowcase}
              {demos.length > 0 && <em className="admin-subnav-count">{demos.length}</em>}
            </button>
          </div>
          <button type="button" className="icon-btn" onClick={() => void loadRuns()} title={copy.refresh} aria-label={copy.refresh}>
            <RefreshCw size={16} />
          </button>
        </div>
      </header>

      {error && <div className="control-alert"><ShieldAlert size={15} />{copy.failedLoad} {error}</div>}

      <section className="devrun-create">
        <Hammer size={16} />
        <input
          value={goal}
          onChange={event => setGoal(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter') void createRun(); }}
          placeholder={copy.goalPlaceholder}
          aria-label={copy.newRun}
        />
        <button type="button" disabled={!goal.trim() || busy === 'create'} onClick={() => void createRun()}>
          <Play size={14} />{copy.start}
        </button>
      </section>

      {view === 'showcase' && (
        <section className="devrun-showcase" aria-label={copy.viewShowcase}>
          {!demos.length && <p className="control-empty">{copy.noDemos}</p>}
          <div className="devrun-showcase-grid">
            {demos.map(run => (
              <div key={run.id} className="devrun-showcase-card">
                <a className="devrun-showcase-linkarea" href={run.demo_url!} target="_blank" rel="noreferrer">
                  <div className="devrun-showcase-frame">
                    <iframe src={run.demo_url!} title={run.goal} loading="lazy" sandbox="allow-scripts allow-same-origin" />
                  </div>
                  <div className="devrun-showcase-body">
                    <strong>{run.goal}</strong>
                    <div className="devrun-showcase-meta">
                      <span className={`task-status is-${run.status}`}><CircleDot size={11} />{statusOf(run)}</span>
                      <span>{copy.published} {shortTime(run.updated_at)}</span>
                    </div>
                  </div>
                  <span className="devrun-showcase-open"><ExternalLink size={13} />{copy.openDemo}</span>
                </a>
                <div className="devrun-showcase-menu" ref={openCardMenu === run.id ? cardMenuRef : undefined}>
                  <button
                    type="button"
                    className="devrun-showcase-menu-btn"
                    title={copy.cardMenu}
                    aria-label={copy.cardMenu}
                    onClick={() => setOpenCardMenu(openCardMenu === run.id ? '' : run.id)}
                  >
                    <MoreVertical size={14} />
                  </button>
                  {openCardMenu === run.id && (
                    <div className="devrun-showcase-menu-popover" role="menu">
                      <button type="button" onClick={() => refineRun(run)}><Wrench size={12} />{copy.refine}</button>
                      <button type="button" onClick={() => void downloadDemo(run)}><Download size={12} />{copy.download}</button>
                      <button
                        type="button"
                        className="is-danger"
                        disabled={busy === `delete-${run.id}`}
                        onClick={() => void deleteRun(run)}
                      >
                        <Trash2 size={12} />{copy.deleteRun}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {view === 'runs' && (
      <div className="devruns-workspace">
        <section className="devruns-list" aria-label={copy.title}>
          {!runs.length && <p className="control-empty">{copy.empty}</p>}
          {runs.map(run => (
            <button
              key={run.id}
              type="button"
              className={`devrun-row${selectedId === run.id ? ' is-selected' : ''}`}
              onClick={() => setSelectedId(run.id)}
            >
              <span className={`task-status is-${run.status}`}><CircleDot size={12} />{statusOf(run)}</span>
              <span className="devrun-row-main">
                <strong>{run.goal}</strong>
                <small>{run.id} · {shortTime(run.created_at)}</small>
              </span>
              <span className="devrun-row-progress">
                <ProgressBar label="" used={run.iter_used} budget={run.iter_budget} />
              </span>
            </button>
          ))}
        </section>

        <aside className="devrun-inspector">
          {!selected && <p className="control-empty">{copy.selectRun}</p>}
          {selected && (
            <>
              <div className="devrun-inspector-head">
                <div>
                  <span className={`task-status is-${selected.status}`}>{statusOf(selected)}</span>
                  <h3>{selected.goal}</h3>
                  <code>{selected.id}</code>
                </div>
                <div className="devrun-commands">
                  {selected.demo_url && (
                    <a className="devrun-demo-link" href={selected.demo_url} target="_blank" rel="noreferrer">
                      <ExternalLink size={14} />{copy.openDemo}
                    </a>
                  )}
                  {['running', 'planned', 'verifying'].includes(selected.status) && (
                    <button type="button" disabled={Boolean(busy)} onClick={() => void post(`/api/dev-runs/${selected.id}/pause`)}>
                      <Pause size={14} />{copy.pause}
                    </button>
                  )}
                  {['paused', 'awaiting_approval'].includes(selected.status) && (
                    <button type="button" disabled={Boolean(busy)} onClick={() => void post(`/api/dev-runs/${selected.id}/resume`)}>
                      <Play size={14} />{copy.resume}
                    </button>
                  )}
                  {!['done', 'failed', 'cancelled'].includes(selected.status) && (
                    <button
                      type="button"
                      className="is-danger"
                      disabled={Boolean(busy)}
                      onClick={() => { if (window.confirm(copy.confirmCancel)) void post(`/api/dev-runs/${selected.id}/cancel`); }}
                    >
                      <Ban size={14} />{copy.cancel}
                    </button>
                  )}
                </div>
              </div>

              {selected.status_reason && (
                <p className="devrun-reason"><strong>{copy.reason}:</strong> {selected.status_reason}</p>
              )}

              <div className="devrun-budgets">
                <ProgressBar label={copy.iterations} used={selected.iter_used} budget={selected.iter_budget} />
                <ProgressBar
                  label={copy.cost}
                  used={selected.cost_used}
                  budget={selected.cost_budget}
                  format={value => `$${value.toFixed(3)}`}
                />
                <div className="devrun-progress">
                  <span className="devrun-progress-label">{copy.time}</span>
                  <span className="devrun-progress-value">
                    {shortTime(selected.created_at)} → {selected.wall_deadline ? shortTime(selected.wall_deadline) : copy.noDeadline}
                  </span>
                </div>
              </div>

              <section className="devrun-steps" aria-label={copy.steps}>
                <div className="control-section-head"><div><ListChecks size={16} /><h4>{copy.steps}</h4></div><span>{selected.steps?.length || 0}</span></div>
                {!selected.steps?.length && <p className="control-empty">{copy.noSteps}</p>}
                <ol className="devrun-step-list">
                  {selected.steps?.map(step => (
                    <li key={step.id} className={`devrun-step is-${step.status}`}>
                      <span className="devrun-step-seq">{step.seq}</span>
                      <span className="devrun-step-body">
                        <strong>{step.phase}{step.tool ? ` · ${step.tool}` : ''}</strong>
                        {looksLikeUnifiedDiff(step.summary)
                          ? <DiffViewer diff={step.summary} />
                          : <small>{step.summary}</small>}
                      </span>
                      <time>{shortTime(step.created_at)}</time>
                    </li>
                  ))}
                </ol>
              </section>

              {report && (
                <section className="devrun-report" aria-label={copy.report}>
                  <h4>{copy.report}</h4>
                  {report.final && <p>{report.final.summary}</p>}
                  {report.verify && (
                    <p><strong>{copy.tests}:</strong> {report.verify.summary}</p>
                  )}
                  {report.commits.map(commit => (
                    <p key={commit.id} className="devrun-commit">
                      <GitCommitHorizontal size={14} />
                      <span>{copy.commit}: {commit.summary}</span>
                      {giteaBaseUrl && (
                        <a href={giteaBaseUrl} target="_blank" rel="noreferrer">
                          <ExternalLink size={13} />{copy.openGitea}
                        </a>
                      )}
                    </p>
                  ))}
                  {report.diffStep && (
                    <>
                      <strong>{copy.changedFiles}</strong>
                      <DiffViewer diff={report.diffStep.summary} />
                    </>
                  )}
                </section>
              )}
            </>
          )}
        </aside>
      </div>
      )}
    </div>
  );
}
