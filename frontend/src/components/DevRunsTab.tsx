import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Ban,
  CircleDot,
  ExternalLink,
  GitCommitHorizontal,
  Hammer,
  ListChecks,
  Pause,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
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
    </div>
  );
}
