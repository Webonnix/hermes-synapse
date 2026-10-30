import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  AlertTriangle, Ban, CheckCircle2, ClipboardCheck, Cpu, Download, FileCode2, History, Inbox, Play, Power,
  RefreshCw, Search, Send, Settings2, ShieldCheck, Wallet, X,
} from 'lucide-react';
import {
  ACTION_LABEL, APPROVAL_STATUS, SOURCE_HOSTS, STATUS_LABEL, confirmApproval, downloadArtifact, earnerApi, formatDate, safeHttpsUrl,
  type Approval, type EarnerOverview, type EarnerSource, type EarnerEvent, type ExternalModels, type IncomeResponse,
  type OpportunityDetail, type OpportunityRow, type Proposal, type RepoPhase, type SpendHistory, type Submission,
} from './earnerApi';
import './earner.css';

/**
 * «Autonomous Earner»: find small programming tasks, prepare and check a
 * solution — while every stage that spends, opens the network or runs code
 * waits for the operator on a server-side approval (backend/earner).
 *
 * Text from sources, model output and generated code is untrusted: it is
 * rendered as plain text only (never as HTML), and only collector-provided
 * https URLs become links.
 */

type View = 'overview' | 'opportunities' | 'approvals' | 'income' | 'history' | 'settings';
const IN_FLIGHT = new Set(['evaluating', 'queued_build', 'building', 'reviewing', 'queued_execution', 'executing']);

function StatusChip({ status }: { status: string }) {
  const meta = STATUS_LABEL[status] ?? { label: status, tone: 'muted' as const };
  return <span className={`ea-chip is-${meta.tone}`}>{meta.label}</span>;
}

function Meter({ label, entry, hint }: { label: string; entry: { used: number; limit: number; remaining: number; actual?: number }; hint?: string }) {
  const ratio = entry.limit > 0 ? Math.min(1, entry.used / entry.limit) : 1;
  return (
    <div className="ea-meter" title={hint}>
      <div className="ea-meter-head"><span>{label}</span><span>{entry.used} / {entry.limit}</span></div>
      <div className="ea-meter-bar"><div className={ratio >= 1 ? 'is-full' : ''} style={{ width: `${ratio * 100}%` }} /></div>
      <div className="ea-meter-foot">осталось {entry.remaining}{entry.actual !== undefined ? ` · фактически ${entry.actual}` : ''}</div>
    </div>
  );
}

function UntrustedText({ text, className = '' }: { text: string; className?: string }) {
  // Plain text node: React escapes it, so markup in a task never renders.
  return <div className={`ea-untrusted ${className}`}>{text || '—'}</div>;
}

function ExternalLink({ url }: { url: string }) {
  const safe = safeHttpsUrl(url);
  if (!safe) return <span className="ea-mono">{url}</span>;
  return <a href={safe} target="_blank" rel="noopener noreferrer nofollow" className="ea-link">{url}</a>;
}

function List({ items }: { items: string[] | undefined }) {
  if (!items || items.length === 0) return <span className="ea-dim">—</span>;
  return <ul className="ea-list">{items.map((item, index) => <li key={index}>{item}</li>)}</ul>;
}

function DiffView({ diff }: { diff: string }) {
  // Each line is a text node with a class by its first character: a diff
  // written by a model is shown, never interpreted.
  return (
    <pre className="ea-code ea-diff">
      {diff.split('\n').map((line, index) => {
        const tone = line.startsWith('+++') || line.startsWith('---') ? 'is-file'
          : line.startsWith('@@') ? 'is-hunk' : line.startsWith('+') ? 'is-add' : line.startsWith('-') ? 'is-del' : '';
        return <span key={index} className={tone}>{line}{'\n'}</span>;
      })}
    </pre>
  );
}

function PhaseLine({ label, phase }: { label: string; phase: RepoPhase | null | undefined }) {
  if (!phase) return null;
  const tests = phase.tests;
  return (
    <>
      <span>{label}</span>
      <span>
        {phase.timed_out ? 'превышено время' : `код ${phase.exit_code ?? '—'}`}
        {tests ? ` · тестов ${tests.total}, упало ${tests.failed_count}, пропущено ${tests.skipped}` : ''}
        {phase.addopts_cleared ? ' · addopts репозитория отключены (нет плагина в образе)' : ''}
        {phase.duration_s !== undefined ? ` · ${phase.duration_s} с` : ''}
      </span>
    </>
  );
}

// ── approvals ────────────────────────────────────────────────────────────────

function ApprovalCard({ approval, onDone }: { approval: Approval; onDone: (message: string) => void }) {
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const expires = new Date(approval.expires_at).getTime();
  const expired = Number.isFinite(expires) && expires <= Date.now();

  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      await confirmApproval(approval);
      onDone(`${ACTION_LABEL[approval.action]}: подтверждено, работа поставлена в очередь`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
      onDone('');
    } finally {
      setBusy(false);
    }
  };
  const decline = async () => {
    setBusy(true);
    try {
      await earnerApi(`/approvals/${encodeURIComponent(approval.id)}/decline`, { method: 'POST', body: JSON.stringify({ reason: 'Отклонено оператором' }) });
      onDone('Отклонено');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ea-card ea-approval">
      <div className="ea-approval-head">
        <span className={`ea-chip is-wait`}>{ACTION_LABEL[approval.action] ?? approval.action}</span>
        <strong>{approval.summary.title}</strong>
      </div>
      <div className="ea-approval-grid">
        <section><h4>Что будет сделано</h4><List items={approval.summary.will_do} /></section>
        <section><h4>Какие данные используются</h4><List items={approval.summary.data} /></section>
        <section><h4>Лимиты</h4><List items={approval.summary.limits} /></section>
        <section><h4>Не входит в это разрешение</h4><List items={approval.summary.not_included} /></section>
      </div>
      {approval.summary.diff && (
        <section><h4>Точный diff (входит в хеш подтверждения)</h4><DiffView diff={approval.summary.diff} /></section>
      )}
      <div className="ea-approval-meta">
        <span>Действует до {formatDate(approval.expires_at)}</span>
        <span>Хеш содержимого: <code>{approval.content_hash.slice(0, 16)}…</code></span>
        <span>Запросил: {approval.requested_by}</span>
        <span>Одноразовое; аннулируется при изменении содержимого</span>
      </div>
      {error && <div className="ea-error">{error}</div>}
      {expired ? (
        <div className="ea-dim">Срок истёк — запросите подтверждение заново.</div>
      ) : (
        <div className="ea-approval-actions">
          <label className="ea-check">
            <input type="checkbox" checked={checked} onChange={event => setChecked(event.target.checked)} />
            Я проверил(а), что именно будет сделано
          </label>
          <button className="ea-btn is-primary" disabled={!checked || busy} onClick={confirm}>
            <ShieldCheck size={16} /> Подтвердить
          </button>
          <button className="ea-btn" disabled={busy} onClick={decline}><X size={16} /> Отклонить</button>
        </div>
      )}
    </div>
  );
}

// ── submission ───────────────────────────────────────────────────────────────

const EXECUTION_LABEL: Record<string, string> = {
  passed: 'тесты прошли', no_regressions: 'нет новых падений (часть тестов падает и без патча)', failed: 'тесты не прошли',
  timeout: 'превышено время', killed: 'контейнер убит', inconclusive: 'результат не получен',
};

const SUBMISSION_STATUS: Record<string, string> = {
  draft: 'черновик', queued: 'в очереди', sending: 'отправляется', sent: 'опубликовано', failed: 'не отправлено', superseded: 'заменён',
};

function SubmissionSection({ detail, onChanged }: { detail: OpportunityDetail; onChanged: () => void }) {
  const draft = (detail.submissions || []).find(item => item.status === 'draft');
  const history = (detail.submissions || []).filter(item => item.status !== 'draft');
  const [body, setBody] = useState(draft?.body ?? '');
  const [title, setTitle] = useState(draft?.title ?? '');
  const [error, setError] = useState('');
  useEffect(() => { setBody(draft?.body ?? ''); setTitle(draft?.title ?? ''); }, [draft?.id, draft?.body, draft?.title]);
  const isPr = draft?.kind === 'github_pr';
  const repoPatch = detail.artifact?.mode === 'repo';
  const edited = !!draft && (body !== draft.body || (isPr && title !== (draft.title ?? '')));

  const call = async (path: string, init: RequestInit) => {
    setError('');
    try {
      await earnerApi(path, init);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };
  const pendingSubmit = (detail.approvals || []).some(a => a.action === 'submit' && a.status === 'pending');

  return (
    <section className="ea-section">
      <h4><Send size={15} /> Отправка заказчику (в один клик)</h4>
      <p className="ea-dim">
        {repoPatch
          ? 'Earner готовит pull request из вашего форка: один коммит ровно с проверенным diff, текст с пометкой об ИИ. '
          : 'Earner сам готовит комментарий к GitHub-задаче с решением и пометкой об ИИ. '}
        Опубликуется он только после того, как вы подтвердите конкретный текст{repoPatch ? ' и diff' : ''}; пометку об ИИ
        удалить нельзя. Нужен classic-токен EARNER_GITHUB_TOKEN (scope public_repo) в «Настройки → API-ключи».
      </p>
      {error && <div className="ea-error">{error}</div>}
      {draft ? (
        <>
          {isPr && (
            <label className="ea-label">Заголовок PR
              <input className="ea-input" value={title} onChange={event => setTitle(event.target.value)} />
            </label>
          )}
          {isPr && draft.payload?.head_branch && (
            <div className="ea-dim">Ветка в форке: {draft.payload.head_branch} → {draft.payload.base?.owner}/{draft.payload.base?.repo}:{draft.payload.base?.branch}</div>
          )}
          <textarea className="ea-input ea-mono" rows={12} value={body} onChange={event => setBody(event.target.value)} />
          <div className="ea-row">
            <button className="ea-btn" disabled={!edited} onClick={() => call(`/submissions/${draft.id}`, { method: 'PUT', body: JSON.stringify(isPr ? { body, title } : { body }) })}>Сохранить текст</button>
            <button className="ea-btn is-primary" disabled={edited || pendingSubmit} onClick={() => call('/approvals', { method: 'POST', body: JSON.stringify({ action: 'submit', target_id: draft.id }) })}>
              <ClipboardCheck size={15} /> Подготовить подтверждение отправки
            </button>
            {pendingSubmit && <span className="ea-dim">Подтверждение ждёт выше</span>}
          </div>
        </>
      ) : (
        detail.status === 'checks_passed' && !detail.is_demo && (
          <div className="ea-row">
            {repoPatch && (
              <button className="ea-btn is-primary" onClick={() => call(`/opportunities/${detail.id}/submission`, { method: 'POST', body: JSON.stringify({ kind: 'pr' }) })}><Send size={15} /> Подготовить pull request</button>
            )}
            <button className="ea-btn" onClick={() => call(`/opportunities/${detail.id}/submission`, { method: 'POST', body: JSON.stringify({ kind: 'comment' }) })}><Send size={15} /> Подготовить комментарий</button>
          </div>
        )
      )}
      {history.map((item: Submission) => (
        <div key={item.id} className="ea-dim">
          {formatDate(item.updated_at)} · {SUBMISSION_STATUS[item.status] ?? item.status}
          {item.response_url && <> · <ExternalLink url={item.response_url} /></>}
          {item.error && <span className="ea-bad"> · {item.error}</span>}
        </div>
      ))}
    </section>
  );
}

// ── opportunity detail ───────────────────────────────────────────────────────

function OpportunityPanel({ id, onClose, onChanged, sandboxReason }: {
  id: string; onClose: () => void; onChanged: (message: string) => void; sandboxReason: string;
}) {
  const [detail, setDetail] = useState<OpportunityDetail | null>(null);
  const [error, setError] = useState('');
  const [file, setFile] = useState(0);
  const [note, setNote] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await earnerApi<OpportunityDetail>(`/opportunities/${encodeURIComponent(id)}`);
      if (data && typeof data.id === 'string') setDetail(data);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  }, [id]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!detail || !IN_FLIGHT.has(detail.status)) return;
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [detail, load]);

  if (!detail) {
    return <div className="ea-panel"><button className="ea-btn" onClick={onClose}><X size={16} /></button>{error || 'Загрузка…'}</div>;
  }
  const proposal = detail.proposal as Proposal;
  const files = detail.artifact?.files ?? [];
  const pending = (detail.approvals || []).filter(a => a.status === 'pending');

  const act = async (path: string, body: unknown, message: string) => {
    try {
      await earnerApi(path, { method: 'POST', body: JSON.stringify(body) });
      onChanged(message);
      setNote('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };

  return (
    <div className="ea-panel">
      <div className="ea-panel-head">
        <div>
          <div className="ea-eyebrow">{detail.is_demo ? 'Демонстрационная задача' : `Источник: ${detail.source_id}`}</div>
          <h3>{detail.title}</h3>
          <div className="ea-row"><StatusChip status={detail.status} />{detail.stale ? <span className="ea-chip is-bad">источник: не открыта</span> : null}</div>
        </div>
        <button className="ea-btn" onClick={onClose} aria-label="Закрыть"><X size={16} /></button>
      </div>
      {error && <div className="ea-error">{error}</div>}
      {detail.error && <div className="ea-error">{detail.error}</div>}
      {detail.reject_reason && <div className="ea-warn">Причина: {detail.reject_reason}</div>}

      {pending.map(approval => (
        <ApprovalCard key={approval.id} approval={approval} onDone={message => { if (message) onChanged(message); void load(); }} />
      ))}
      {detail.status === 'proposed' && pending.length === 0 && (
        <button className="ea-btn is-primary" onClick={() => act('/approvals', { action: 'generate', target_id: detail.id }, 'Экран подтверждения подготовлен')}>
          <ClipboardCheck size={16} /> Подготовить подтверждение генерации
        </button>
      )}
      {detail.status === 'awaiting_execution' && pending.length === 0 && (
        sandboxReason
          ? <div className="ea-warn"><Ban size={14} /> Исполнение выключено: {sandboxReason}</div>
          : <button className="ea-btn is-primary" onClick={() => act('/approvals', { action: 'execute', target_id: detail.id }, 'Экран подтверждения подготовлен')}>
              <ClipboardCheck size={16} /> Подготовить подтверждение исполнения
            </button>
      )}

      <section className="ea-section">
        <h4>Источник (собрано Scout, недоверенные данные)</h4>
        <div className="ea-kv">
          <span>URL</span><ExternalLink url={detail.url} />
          <span>Получено</span><span>{formatDate(detail.fetched_at)} · последний раз видели {formatDate(detail.last_seen_at)}</span>
          <span>Состояние у источника</span><span>{detail.source_state || '—'}</span>
          <span>Метки</span><span>{(detail.labels || []).join(', ') || '—'}</span>
          <span>Награда</span><span>{detail.claimed_reward ? `заявлено в тексте: ${detail.claimed_reward}` : 'не указана'} · оплата не подтверждена</span>
        </div>
        <UntrustedText text={detail.excerpt} className="is-scroll" />
      </section>

      {proposal && proposal.decision && (
        <section className="ea-section">
          <h4>Предложение Evaluator</h4>
          <div className="ea-kv">
            <span>Результат</span><span>{proposal.deliverable || '—'}</span>
            <span>Трудозатраты</span><span>{proposal.effort_hours} ч · неопределённость: {proposal.uncertainty}</span>
            <span>Почему подходит</span><span>{proposal.fit_reason || '—'}</span>
            <span>Зависимости</span><span>{(proposal.dependencies || []).join(', ') || '—'}</span>
            <span>Условия оплаты</span><span>{proposal.payment_terms || '—'}</span>
            <span>Подтверждение оплаты</span><span>{proposal.payment_evidence || 'нет'} — система не считает оплату подтверждённой</span>
          </div>
          <h5>Критерии приёмки</h5>
          <List items={proposal.acceptance_criteria} />
          <h5>Риски</h5>
          <List items={proposal.risks} />
          <h5>Спецификация для Builder</h5>
          <UntrustedText text={proposal.builder_spec} />
        </section>
      )}

      {files.length > 0 && (
        <section className="ea-section">
          <div className="ea-row ea-between">
            <h4><FileCode2 size={15} /> Артефакт · хеш {detail.artifact_hash.slice(0, 12)}… · {detail.artifact.provider === 'external' ? `внешняя модель ${detail.artifact.model}` : 'локальная модель'}{detail.artifact.attempt && detail.artifact.attempt > 1 ? `, попытка ${detail.artifact.attempt}` : ''}</h4>
            <button className="ea-btn" onClick={() => downloadArtifact(detail.id).catch(err => setError(err.message))}><Download size={15} /> ZIP</button>
          </div>
          {detail.artifact.mode === 'repo' && detail.artifact.repo && (
            <>
              <div className="ea-kv">
                <span>Репозиторий</span><span className="ea-mono">{detail.artifact.repo.owner}/{detail.artifact.repo.repo}@{detail.artifact.repo.commit.slice(0, 12)} ({detail.artifact.repo.default_branch}{detail.artifact.repo.license ? `, ${detail.artifact.repo.license}` : ''})</span>
                <span>Изменения</span><span>{detail.artifact.static?.files_changed} файл(ов), +{detail.artifact.static?.added}/−{detail.artifact.static?.removed}; тесты: {(detail.artifact.static?.tests_touched || []).join(', ') || 'не затронуты'}</span>
                <span>Тесты</span><span>{detail.artifact.test_plan?.runner} · объявленные зависимости: {(detail.artifact.test_plan?.dependencies || []).join(', ') || 'нет'}</span>
                <span>Правила об ИИ</span><span>{detail.artifact.static?.ai_policy?.status === 'disclosure' ? 'репозиторий просит указывать использование ИИ' : detail.artifact.static?.ai_policy?.status === 'forbidden' ? 'запрещено' : 'запретов не найдено'}</span>
                <span>Модели показаны</span><span className="ea-mono">{(detail.artifact.static?.context || []).join(', ')}</span>
              </div>
              {detail.artifact.summary && <UntrustedText text={detail.artifact.summary} />}
              <DiffView diff={detail.artifact.diff ?? ''} />
              <h5>Файлы после правки</h5>
            </>
          )}
          <div className="ea-tabs">
            {files.map((item, index) => (
              <button key={item.path} className={`ea-tab ${index === file ? 'is-active' : ''}`} onClick={() => setFile(index)}>{item.path}</button>
            ))}
          </div>
          <pre className="ea-code">{files[Math.min(file, files.length - 1)]?.content}</pre>
          {detail.artifact.static && (
            <div className="ea-dim">
              {detail.artifact.mode !== 'repo' && <>Импорты: {(detail.artifact.static.imports || []).join(', ') || 'нет'} · тестов: {detail.artifact.static.test_count} · {detail.artifact.static.bytes} байт</>}
              {detail.artifact.static.risks.length > 0 && <><h5>На что посмотреть</h5><List items={detail.artifact.static.risks} /></>}
            </div>
          )}
        </section>
      )}

      {detail.review && detail.review.verdict && (
        <section className="ea-section">
          <h4>Заключение Reviewer: {detail.review.accepted ? 'одобрено' : 'отклонено'}</h4>
          <div className="ea-dim">{detail.review.note}</div>
          <table className="ea-table">
            <tbody>
              {(detail.review.criteria || []).map((criterion, index) => (
                <tr key={index}><td>{criterion.met ? '✓' : '✗'}</td><td>{criterion.criterion}</td><td className="ea-dim">{criterion.note}</td></tr>
              ))}
            </tbody>
          </table>
          <p>{detail.review.summary}</p>
          {(detail.review.quality_issues || []).length > 0 && <><h5>Замечания</h5><List items={detail.review.quality_issues} /></>}
          {(detail.review.risks || []).length > 0 && <><h5>Риски</h5><List items={detail.review.risks} /></>}
        </section>
      )}

      {detail.execution && detail.execution.status && (
        <section className="ea-section">
          <h4>Sandbox: {EXECUTION_LABEL[detail.execution.status] ?? detail.execution.status} · {detail.execution.runtime} · {detail.execution.duration_s ?? '—'} с</h4>
          {detail.execution.mode === 'repo' && (
            <div className="ea-kv">
              <PhaseLine label="Исходный коммит" phase={detail.execution.baseline} />
              <PhaseLine label="С патчем" phase={detail.execution.patched} />
              <span>Новые падения</span><span>{(detail.execution.new_failures || []).join(', ') || 'нет'}</span>
              <span>Исправлено</span><span>{(detail.execution.fixed || []).join(', ') || '—'}</span>
              <span>Тесты патча прошли</span><span>{(detail.execution.touched_tests_passed || []).join(', ') || 'нет'}</span>
              <span>Нет в образе</span><span>{(detail.execution.deps_missing || []).join(', ') || '—'}</span>
            </div>
          )}
          {detail.execution.detail && <div className="ea-warn">{detail.execution.detail}</div>}
          <pre className="ea-code is-short">{detail.execution.stdout || detail.execution.stderr || ''}{detail.execution.stdout && detail.execution.stderr ? `\n${detail.execution.stderr}` : ''}</pre>
        </section>
      )}

      {(detail.status === 'checks_passed' || (detail.submissions || []).length > 0) && (
        <SubmissionSection detail={detail} onChanged={() => { onChanged('Отправка обновлена'); void load(); }} />
      )}

      {detail.artifact_hash && (
        <section className="ea-section">
          <h4>Ручные действия</h4>
          <p className="ea-dim">
            Earner ничего не отправляет заказчику и не принимает оплату. Передайте результат сами,
            затем отметьте здесь факт передачи и, после поступления денег, факт оплаты.
          </p>
          <div className="ea-kv">
            <span>Передача</span><span>{detail.delivered_at ? `${formatDate(detail.delivered_at)} — ${detail.delivered_note}` : 'не отмечена'}</span>
            <span>Оплата</span><span>{detail.paid_at ? `${formatDate(detail.paid_at)} — ${detail.paid_note}` : 'не подтверждена (сопоставляется с поступлениями на кошелёк)'}</span>
          </div>
          <div className="ea-row">
            <input className="ea-input" value={note} onChange={event => setNote(event.target.value)} placeholder="Комментарий: кому, как, сумма" />
            {!detail.delivered_at && <button className="ea-btn" disabled={note.trim().length < 3} onClick={() => act(`/opportunities/${detail.id}/outcome`, { kind: 'delivered', note }, 'Передача отмечена')}>Отметить передачу</button>}
            {detail.delivered_at && !detail.paid_at && <button className="ea-btn" disabled={note.trim().length < 3} onClick={() => act(`/opportunities/${detail.id}/outcome`, { kind: 'paid', note }, 'Оплата отмечена')}>Подтвердить оплату</button>}
          </div>
        </section>
      )}

      <section className="ea-section">
        <h4><History size={15} /> История</h4>
        <EventList events={detail.events} />
        {(detail.approvals || []).length > 0 && (
          <>
            <h5>Подтверждения</h5>
            <table className="ea-table"><tbody>
              {detail.approvals.map(approval => (
                <tr key={approval.id}>
                  <td>{ACTION_LABEL[approval.action]}</td><td>{APPROVAL_STATUS[approval.status] ?? approval.status}</td>
                  <td className="ea-dim">{formatDate(approval.decided_at || approval.created_at)} {approval.decided_by} {approval.status_reason}</td>
                </tr>
              ))}
            </tbody></table>
          </>
        )}
      </section>
      {!IN_FLIGHT.has(detail.status) && !detail.status.startsWith('queued') && detail.status !== 'dismissed' && (
        <button className="ea-btn is-danger" onClick={() => act(`/opportunities/${detail.id}/dismiss`, { reason: 'Скрыто оператором' }, 'Задача скрыта')}>Скрыть задачу</button>
      )}
    </div>
  );
}

function EventList({ events }: { events: EarnerEvent[] }) {
  if (!events || events.length === 0) return <div className="ea-dim">Событий нет</div>;
  return (
    <ul className="ea-events">
      {events.map(event => (
        <li key={event.id}><span className="ea-dim">{formatDate(event.created_at)}</span> {event.message}{event.actor ? <span className="ea-dim"> · {event.actor}</span> : null}</li>
      ))}
    </ul>
  );
}

// ── settings ─────────────────────────────────────────────────────────────────

const LIMIT_LABELS: Record<string, string> = {
  tasks_per_day: 'Задач в сутки', llm_calls_per_day: 'Вызовов модели в сутки', tokens_per_day: 'Токенов (резерв) в сутки',
  fetches_per_day: 'Запросов к источникам в сутки', sandbox_runs_per_day: 'Запусков sandbox в сутки',
  max_candidates_per_run: 'Кандидатов за исследование', max_evaluations_per_run: 'Оценок за исследование',
  max_fetches_per_run: 'Запросов за исследование', submissions_per_day: 'Отправок заказчикам в сутки',
};

function ExternalSection({ overview, external, setExternal }: {
  overview: EarnerOverview;
  external: { external_enabled: boolean; external_model: string; external_month_usd: number };
  setExternal: (value: { external_enabled: boolean; external_model: string; external_month_usd: number }) => void;
}) {
  const [catalog, setCatalog] = useState<ExternalModels | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    earnerApi<ExternalModels>('/external').then(data => setCatalog(data && Array.isArray(data.models) ? data : null)).catch(err => setError(err instanceof Error ? err.message : 'Ошибка'));
  }, []);
  const usage = overview.budget.external;
  const selected = catalog?.models.find(model => model.id === external.external_model);
  return (
    <div className="ea-card">
      <h4>Внешние модели (OmniRoute)</h4>
      <p className="ea-dim">
        Локальная модель — по умолчанию. Внешняя подключается для сложных задач, для одного повтора после отказа Reviewer
        и как резерв, если локальная недоступна. Доступны только модели с опубликованной ценой; деньги резервируются до вызова,
        при исчерпании лимита внешние вызовы прекращаются.
      </p>
      {error && <div className="ea-error">{error}</div>}
      <label className="ea-check"><input type="checkbox" checked={external.external_enabled} onChange={e => setExternal({ ...external, external_enabled: e.target.checked })} /> Разрешить внешние модели</label>
      <div className="ea-limits">
        <label className="ea-label">Модель
          <select className="ea-input" value={external.external_model} onChange={e => setExternal({ ...external, external_model: e.target.value })}>
            {!selected && <option value={external.external_model}>{external.external_model} (цена неизвестна)</option>}
            {(catalog?.models || []).map(model => (
              <option key={model.id} value={model.id}>{model.id} — ${model.input}/${model.output} за 1M токенов</option>
            ))}
          </select>
        </label>
        <label className="ea-label">Лимит в месяц, $
          <input className="ea-input" type="number" min={0} max={200} step={1} value={external.external_month_usd} onChange={e => setExternal({ ...external, external_month_usd: Number(e.target.value) })} />
        </label>
      </div>
      <div className="ea-dim">
        {usage.month}: потрачено ${usage.used_usd.toFixed(4)}, в резерве ${usage.reserved_usd.toFixed(4)} из ${usage.limit_usd.toFixed(2)}
        {catalog && !catalog.info.available && catalog.info.enabled ? ` · недоступно: ${catalog.info.reason}` : ''}
      </div>
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

function SettingsView({ overview, onSaved }: { overview: EarnerOverview; onSaved: (message: string) => void }) {
  const settings = overview.settings;
  const [directions, setDirections] = useState(settings.directions.join('\n'));
  const [constraints, setConstraints] = useState(settings.constraints);
  const [hosts, setHosts] = useState(settings.allowed_hosts.join('\n'));
  const [sources, setSources] = useState<EarnerSource[]>(settings.sources);
  const [limits, setLimits] = useState<Record<string, number>>(settings.limits);
  const [error, setError] = useState('');
  const [schedule, setSchedule] = useState({ interval_hours: 24, days: 7, max_runs: 7 });
  const [autopilot, setAutopilot] = useState({ interval_hours: 12, days: 14, max_runs: 28, auto_generate: true, auto_execute: true });
  const [external, setExternal] = useState({ external_enabled: settings.external_enabled, external_model: settings.external_model, external_month_usd: settings.external_month_usd });
  const [walletAddress, setWalletAddress] = useState(settings.wallet.address);
  const [walletNetworks, setWalletNetworks] = useState<string[]>(settings.wallet.networks.length ? settings.wallet.networks : ['base', 'polygon']);

  const addSource = (source: EarnerSource) => {
    if (sources.some(item => item.id === source.id)) return;
    setSources([...sources, source]);
    const needed = [SOURCE_HOSTS[source.kind], source.kind === 'rss' ? hostOf(source.url ?? '') : '',
      source.kind === 'opire' ? 'api.github.com' : ''].filter(Boolean);
    const current = hosts.split('\n').map(h => h.trim()).filter(Boolean);
    const missing = needed.filter(host => !current.includes(host));
    if (missing.length) setHosts([...current, ...missing].join('\n'));
  };

  const save = async () => {
    setError('');
    try {
      await earnerApi('/settings', {
        method: 'PUT',
        body: JSON.stringify({
          directions: directions.split('\n').map(s => s.trim()).filter(Boolean),
          constraints,
          allowed_hosts: hosts.split('\n').map(s => s.trim()).filter(Boolean),
          sources,
          limits,
          ...external,
          wallet: { address: walletAddress, networks: walletNetworks },
        }),
      });
      onSaved('Настройки сохранены. Ожидающие подтверждения, привязанные к прежним настройкам, аннулированы.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };

  const requestAutopilot = async () => {
    try {
      await earnerApi('/approvals', { method: 'POST', body: JSON.stringify({ action: 'autopilot', params: autopilot }) });
      onSaved('Экран подтверждения автопилота подготовлен — откройте «Подтверждения»');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };

  const requestSchedule = async () => {
    try {
      await earnerApi('/approvals', { method: 'POST', body: JSON.stringify({ action: 'schedule', params: schedule }) });
      onSaved('Экран подтверждения автопоиска подготовлен — откройте «Подтверждения»');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };

  return (
    <div className="ea-settings">
      {error && <div className="ea-error">{error}</div>}
      <div className="ea-card">
        <h4>Направления и ограничения</h4>
        <label className="ea-label">Направления (по одному в строке)
          <textarea className="ea-input" rows={3} value={directions} onChange={e => setDirections(e.target.value)} placeholder="парсеры и утилиты на Python" />
        </label>
        <label className="ea-label">Ограничения для Evaluator
          <textarea className="ea-input" rows={2} value={constraints} onChange={e => setConstraints(e.target.value)} placeholder="только задачи до 4 часов; без работы с персональными данными" />
        </label>
      </div>
      <div className="ea-card">
        <h4>Интернет и источники</h4>
        <p className="ea-dim">По умолчанию интернет выключен. Разрешены только точные имена хостов по HTTPS; переходы по редиректам не выполняются. Платные поисковые сервисы не подключены.</p>
        <label className="ea-label">Разрешённые хосты (точные DNS-имена, по одному в строке)
          <textarea className="ea-input ea-mono" rows={3} value={hosts} onChange={e => setHosts(e.target.value)} placeholder="api.github.com" />
        </label>
        <div className="ea-sources">
          {sources.map((source, index) => (
            <div key={source.id} className="ea-source">
              <label className="ea-check"><input type="checkbox" checked={source.enabled} onChange={e => setSources(sources.map((s, i) => i === index ? { ...s, enabled: e.target.checked } : s))} /> {source.id}</label>
              <span className="ea-chip is-info">{source.kind}</span>
              {(source.kind === 'github_issues' || source.kind === 'freelancer_com') && (
                <input className="ea-input ea-mono" value={source.query ?? ''} onChange={e => setSources(sources.map((s, i) => i === index ? { ...s, query: e.target.value } : s))} />
              )}
              {source.kind === 'rss' && (
                <input className="ea-input ea-mono" value={source.url ?? ''} onChange={e => setSources(sources.map((s, i) => i === index ? { ...s, url: e.target.value } : s))} />
              )}
              {source.kind === 'demo' && <span className="ea-dim">встроенная учебная задача, без сети</span>}
              {source.kind === 'freelancehunt' && (
                <input className="ea-input ea-mono" title="id навыков Freelancehunt через запятую: 22 Python, 169 парсинг, 180 боты, 178 обработка данных"
                  value={(source.skill_ids ?? []).join(',')}
                  onChange={e => setSources(sources.map((s, i) => i === index ? { ...s, skill_ids: e.target.value.split(',').map(v => Number(v.trim())).filter(v => Number.isInteger(v) && v > 0) } : s))} />
              )}
              {source.kind === 'opire' && <span className="ea-dim">награды за GitHub-issues; описание читается через api.github.com</span>}
              {source.kind === 'superteam' && <span className="ea-dim">Agent API: только листинги, открытые для ИИ-агентов; нужен ключ EARNER_SUPERTEAM_API_KEY; выплаты в USDC (обычно Solana)</span>}
              <button className="ea-btn" onClick={() => setSources(sources.filter((_, i) => i !== index))}><X size={14} /></button>
            </div>
          ))}
        </div>
        <div className="ea-row">
          <button className="ea-btn" onClick={() => addSource({ id: 'demo', kind: 'demo', enabled: true })}>+ Демо-источник</button>
          <button className="ea-btn" onClick={() => addSource({ id: 'gh-bounty-python', kind: 'github_issues', enabled: true, query: 'label:bounty language:python state:open' })}>+ GitHub: bounty, Python</button>
          <button className="ea-btn" onClick={() => addSource({ id: 'fl-programming', kind: 'rss', enabled: true, url: 'https://www.fl.ru/rss/all.xml?category=5' })}>+ FL.ru: Программирование</button>
          <button className="ea-btn" onClick={() => addSource({ id: 'freelancer-python', kind: 'freelancer_com', enabled: true, query: 'python script' })}>+ Freelancer.com: Python</button>
          <button className="ea-btn" onClick={() => addSource({ id: 'freelancehunt-python', kind: 'freelancehunt', enabled: true, skill_ids: [22, 169, 180] })}>+ Freelancehunt: Python, парсинг, боты</button>
          <button className="ea-btn" onClick={() => addSource({ id: 'opire', kind: 'opire', enabled: true })}>+ Opire: награды за issues</button>
          <button className="ea-btn" onClick={() => addSource({ id: 'superteam', kind: 'superteam', enabled: true, types: ['bounty'] })}>+ Superteam Earn: баунти для агентов</button>
          <button className="ea-btn" onClick={() => addSource({ id: `rss-${sources.length + 1}`, kind: 'rss', enabled: true, url: 'https://' })}>+ RSS/Atom</button>
        </div>
      </div>
      <ExternalSection overview={overview} external={external} setExternal={setExternal} />
      <div className="ea-card">
        <h4><Wallet size={15} /> Кошелёк для оплаты (только приём)</h4>
        <p className="ea-dim">
          Только публичный адрес 0x… — без ключа и seed-фразы. Earner раз в 5 минут читает переводы USDC/USDT
          на этот адрес в выбранных сетях и привязывает поступление к переданной задаче, если сумма совпадает.
          Отправлять что-либо с кошелька Earner не умеет.
        </p>
        <label className="ea-label">Публичный адрес
          <input className="ea-input ea-mono" value={walletAddress} onChange={e => setWalletAddress(e.target.value.trim())} placeholder="0x…" />
        </label>
        <div className="ea-row">
          {['base', 'polygon', 'ethereum'].map(net => (
            <label key={net} className="ea-check">
              <input type="checkbox" checked={walletNetworks.includes(net)} onChange={e => setWalletNetworks(e.target.checked ? [...walletNetworks, net] : walletNetworks.filter(n => n !== net))} /> {net}
            </label>
          ))}
        </div>
      </div>
      <div className="ea-card">
        <h4>Бюджет и лимиты</h4>
        <div className="ea-limits">
          {Object.entries(LIMIT_LABELS).map(([key, label]) => (
            <label key={key} className="ea-label">{label}
              <input className="ea-input" type="number" min={0} value={limits[key] ?? 0} onChange={e => setLimits({ ...limits, [key]: Number(e.target.value) })} />
            </label>
          ))}
        </div>
        <p className="ea-dim">Sandbox — всегда не больше одного одновременно. Сутки считаются по UTC.</p>
      </div>
      <button className="ea-btn is-primary" onClick={save}><Settings2 size={16} /> Сохранить настройки</button>

      <div className="ea-card">
        <h4>Автопилот</h4>
        <p className="ea-dim">
          Одно разрешение на срок: Earner сам ищет задачи по расписанию и сам подтверждает генерацию и исполнение
          в gVisor-песочнице — с теми же проверками и в пределах дневных лимитов. Отправка заказчикам остаётся
          вашим кликом по каждому тексту. Любое изменение настроек, «Остановить Earner» или «Остановить автопилот»
          прекращают его.
        </p>
        {overview.schedule && overview.schedule.active && overview.schedule.auto_generate !== undefined && (overview.schedule.auto_generate || overview.schedule.auto_execute) ? (
          <div className="ea-row">
            <span>Включён до {formatDate(overview.schedule.valid_until)}: поиск каждые {overview.schedule.interval_hours} ч ({overview.schedule.runs_done}/{overview.schedule.max_runs}),
              автоподтверждение: {[overview.schedule.auto_generate ? 'генерация' : '', overview.schedule.auto_execute ? 'исполнение' : ''].filter(Boolean).join(', ')}</span>
            <button className="ea-btn is-danger" onClick={() => earnerApi('/schedule/revoke', { method: 'POST' }).then(() => onSaved('Автопилот остановлен'))}>Остановить автопилот</button>
          </div>
        ) : (
          <>
            <div className="ea-row">
              <label className="ea-check"><input type="checkbox" checked={autopilot.auto_generate} onChange={e => setAutopilot({ ...autopilot, auto_generate: e.target.checked })} /> сам подтверждать генерацию</label>
              <label className="ea-check"><input type="checkbox" checked={autopilot.auto_execute} onChange={e => setAutopilot({ ...autopilot, auto_execute: e.target.checked })} /> сам подтверждать исполнение в sandbox</label>
            </div>
            <div className="ea-row">
              <label className="ea-label">Поиск каждые, ч<input className="ea-input" type="number" min={2} max={168} value={autopilot.interval_hours} onChange={e => setAutopilot({ ...autopilot, interval_hours: Number(e.target.value) })} /></label>
              <label className="ea-label">Срок, дней<input className="ea-input" type="number" min={1} max={30} value={autopilot.days} onChange={e => setAutopilot({ ...autopilot, days: Number(e.target.value) })} /></label>
              <label className="ea-label">Поисков максимум<input className="ea-input" type="number" min={1} max={120} value={autopilot.max_runs} onChange={e => setAutopilot({ ...autopilot, max_runs: Number(e.target.value) })} /></label>
              <button className="ea-btn is-primary" onClick={requestAutopilot}><ClipboardCheck size={16} /> Запросить разрешение автопилота</button>
            </div>
          </>
        )}
      </div>

      <div className="ea-card">
        <h4>Автопоиск по расписанию</h4>
        <p className="ea-dim">Выключен, пока вы отдельно не подтвердите его. Разрешение ограничено текущими источниками, сроком и числом запусков и не распространяется на генерацию, исполнение и внешние действия.</p>
        {overview.schedule && overview.schedule.active ? (
          <div className="ea-row">
            <span>Каждые {overview.schedule.interval_hours} ч до {formatDate(overview.schedule.valid_until)}, запусков {overview.schedule.runs_done}/{overview.schedule.max_runs}, следующий {formatDate(overview.schedule.next_run_at)}</span>
            <button className="ea-btn is-danger" onClick={() => earnerApi('/schedule/revoke', { method: 'POST' }).then(() => onSaved('Автопоиск остановлен'))}>Остановить</button>
          </div>
        ) : (
          <div className="ea-row">
            <label className="ea-label">Интервал, ч<input className="ea-input" type="number" min={6} max={168} value={schedule.interval_hours} onChange={e => setSchedule({ ...schedule, interval_hours: Number(e.target.value) })} /></label>
            <label className="ea-label">Срок, дней<input className="ea-input" type="number" min={1} max={14} value={schedule.days} onChange={e => setSchedule({ ...schedule, days: Number(e.target.value) })} /></label>
            <label className="ea-label">Запусков максимум<input className="ea-input" type="number" min={1} max={60} value={schedule.max_runs} onChange={e => setSchedule({ ...schedule, max_runs: Number(e.target.value) })} /></label>
            <button className="ea-btn" onClick={requestSchedule}><ClipboardCheck size={16} /> Запросить разрешение</button>
          </div>
        )}
        {overview.schedule && !overview.schedule.active && overview.schedule.status_reason && <div className="ea-dim">Последнее расписание: {overview.schedule.status_reason}</div>}
      </div>
    </div>
  );
}

// ── income ───────────────────────────────────────────────────────────────────

function IncomeView({ opportunities, onChanged }: { opportunities: OpportunityRow[]; onChanged: (message: string) => void }) {
  const [data, setData] = useState<IncomeResponse | null>(null);
  const [error, setError] = useState('');
  const [choice, setChoice] = useState<Record<string, string>>({});
  const load = useCallback(() => {
    earnerApi<IncomeResponse>('/income').then(value => setData(value && Array.isArray(value.income) ? value : null)).catch(err => setError(err instanceof Error ? err.message : 'Ошибка'));
  }, []);
  useEffect(() => { load(); }, [load]);
  const candidates = opportunities.filter(row => row.delivered_at && !row.paid_at);
  const link = async (incomeId: string) => {
    try {
      await earnerApi(`/income/${encodeURIComponent(incomeId)}/link`, { method: 'POST', body: JSON.stringify({ opportunity_id: choice[incomeId] }) });
      onChanged('Поступление привязано к задаче');
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };
  if (!data) return <div className="ea-card">{error || 'Загрузка…'}</div>;
  return (
    <div className="ea-stack">
      {error && <div className="ea-error">{error}</div>}
      <div className="ea-card">
        <h4><Wallet size={15} /> Поступления на кошелёк</h4>
        {data.wallet.address ? (
          <div className="ea-kv">
            <span>Адрес</span><span className="ea-mono">{data.wallet.address}</span>
            <span>Сети</span><span>{data.wallet.networks.join(', ') || '—'}</span>
            <span>Всего</span><span>${data.total_usd.toFixed(2)} · привязано к задачам ${data.linked_usd.toFixed(2)}</span>
            <span>Проверено</span><span>{data.cursors.map(c => `${c.network}: блок ${c.last_block}, ${formatDate(c.checked_at)}${c.error ? ` — ошибка: ${c.error}` : ''}`).join(' · ') || 'ещё нет'}</span>
          </div>
        ) : <div className="ea-dim">Кошелёк не указан. Добавьте публичный адрес в «Настройках».</div>}
        <p className="ea-dim">{data.note}</p>
      </div>
      <div className="ea-card">
        {data.income.length === 0 ? <div className="ea-dim">Поступлений пока нет.</div> : (
          <table className="ea-table">
            <thead><tr><th>Сумма</th><th>Сеть</th><th>От</th><th>Транзакция</th><th>Задача</th></tr></thead>
            <tbody>
              {data.income.map(row => (
                <tr key={row.id}>
                  <td>{row.amount} {row.token}</td><td>{row.network}</td><td className="ea-mono">{row.from_address.slice(0, 10)}…</td>
                  <td><ExternalLink url={row.explorer_url} /></td>
                  <td>
                    {row.opportunity_id ? `${row.opportunity_id} (${row.link_kind === 'auto' ? 'авто' : 'вручную'})` : (
                      <div className="ea-row">
                        <select className="ea-input ea-select" value={choice[row.id] ?? ''} onChange={e => setChoice({ ...choice, [row.id]: e.target.value })}>
                          <option value="">— выберите переданную задачу —</option>
                          {candidates.map(opp => <option key={opp.id} value={opp.id}>{opp.title.slice(0, 60)} ({opp.claimed_reward || 'без суммы'})</option>)}
                        </select>
                        <button className="ea-btn" disabled={!choice[row.id]} onClick={() => link(row.id)}>Привязать</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── main ─────────────────────────────────────────────────────────────────────

export function EarnerTab() {
  const [view, setView] = useState<View>('overview');
  const [overview, setOverview] = useState<EarnerOverview | null>(null);
  const [opportunities, setOpportunities] = useState<OpportunityRow[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [history, setHistory] = useState<{ events: EarnerEvent[]; spend: SpendHistory | null }>({ events: [], spend: null });
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ov, opps, aprs] = await Promise.all([
        earnerApi<EarnerOverview>('/overview'),
        earnerApi<OpportunityRow[]>('/opportunities'),
        earnerApi<Approval[]>('/approvals'),
      ]);
      if (ov && typeof ov === 'object' && 'settings' in ov) setOverview(ov);
      setOpportunities(Array.isArray(opps) ? opps.filter(row => row && typeof row.id === 'string') : []);
      setApprovals(Array.isArray(aprs) ? aprs.filter(row => row && typeof row.id === 'string') : []);
      setError('');
    } catch (err) {
      if (err && typeof err === 'object' && 'status' in err && (err as { status: number }).status === 403) setForbidden(true);
      else setError(err instanceof Error ? err.message : 'Ошибка загрузки');
    }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const [events, spend] = await Promise.all([earnerApi<EarnerEvent[]>('/events'), earnerApi<SpendHistory>('/spend')]);
      setHistory({ events: Array.isArray(events) ? events : [], spend: spend && Array.isArray(spend.days) ? spend : null });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка загрузки');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (view === 'history') void loadHistory(); }, [view, loadHistory]);
  const busy = opportunities.some(row => IN_FLIGHT.has(row.status)) || (overview?.runs || []).some(run => run.status === 'queued' || run.status === 'running');
  useEffect(() => {
    const timer = window.setInterval(() => { void load(); }, busy ? 5000 : 20000);
    return () => window.clearInterval(timer);
  }, [busy, load]);

  const done = (message: string) => {
    if (message) setNotice(message);
    void load();
  };

  const requestResearch = async () => {
    try {
      await earnerApi('/approvals', { method: 'POST', body: JSON.stringify({ action: 'research' }) });
      setNotice('Экран подтверждения исследования подготовлен');
      setView('approvals');
      void load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };

  const toggleEnabled = async () => {
    if (!overview) return;
    try {
      await earnerApi('/settings', { method: 'PUT', body: JSON.stringify({ enabled: !overview.settings.enabled }) });
      done(overview.settings.enabled ? 'Earner остановлен: новые стадии не запускаются' : 'Earner включён');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка');
    }
  };

  const pending = useMemo(() => approvals.filter(a => a.status === 'pending' && new Date(a.expires_at).getTime() > Date.now()), [approvals]);
  const visible = useMemo(() => opportunities.filter(row => !filter || row.status === filter), [opportunities, filter]);

  if (forbidden) return <div className="ea-root"><div className="ea-card">Раздел доступен только роли с правом earner.view.</div></div>;
  if (!overview) return <div className="ea-root"><div className="ea-card">{error || 'Загрузка…'}</div></div>;

  const sandboxReason = overview.sandbox.available ? '' : overview.sandbox.reason;
  const views: { key: View; label: string; icon: ReactNode; badge?: number }[] = [
    { key: 'overview', label: 'Обзор', icon: <Cpu size={15} /> },
    { key: 'opportunities', label: 'Возможности', icon: <Search size={15} />, badge: opportunities.filter(o => o.status !== 'dismissed').length },
    { key: 'approvals', label: 'Подтверждения', icon: <ShieldCheck size={15} />, badge: pending.length },
    { key: 'income', label: 'Доход', icon: <Wallet size={15} /> },
    { key: 'history', label: 'История и расходы', icon: <History size={15} /> },
    { key: 'settings', label: 'Настройки', icon: <Settings2 size={15} /> },
  ];

  return (
    <div className="ea-root">
      <header className="ea-header">
        <div>
          <div className="ea-eyebrow">Hermes · человек подтверждает каждую стадию</div>
          <h2 className="ea-title">Autonomous Earner</h2>
          <p className="ea-subtitle">Поиск небольших программных задач, подготовка и изолированная проверка решения. Передача заказчику и оплата — только вручную.</p>
        </div>
        <div className="ea-row">
          <button className="ea-btn" onClick={() => void load()}><RefreshCw size={15} /> Обновить</button>
          <button className={`ea-btn ${overview.settings.enabled ? 'is-danger' : 'is-primary'}`} onClick={toggleEnabled}>
            <Power size={15} /> {overview.settings.enabled ? 'Остановить Earner' : 'Включить Earner'}
          </button>
        </div>
      </header>

      <nav className="ea-nav">
        {views.map(item => (
          <button key={item.key} className={`ea-nav-btn ${view === item.key ? 'is-active' : ''}`} onClick={() => setView(item.key)}>
            {item.icon}<span>{item.label}</span>{item.badge ? <span className="ea-badge">{item.badge}</span> : null}
          </button>
        ))}
      </nav>

      {notice && <div className="ea-notice" onClick={() => setNotice('')}><CheckCircle2 size={15} /> {notice}</div>}
      {error && <div className="ea-error">{error}</div>}
      {!overview.settings.enabled && <div className="ea-warn"><Power size={14} /> Earner остановлен: воркер не запускает новые стадии.</div>}

      {view === 'overview' && (
        <div className="ea-grid">
          <div className="ea-card">
            <h4>Состояние</h4>
            <div className="ea-kv">
              <span>Модель</span>
              <span>{overview.llm.local ? `${overview.llm.model} · llama.cpp (${overview.llm.host}) · только локально, без внешнего fallback` : <span className="ea-bad">{overview.llm.reason}</span>}</span>
              <span>Исполнение</span>
              <span>{overview.sandbox.available ? `доступно: ${overview.sandbox.runtime}, без сети` : <span className="ea-bad">выключено — {overview.sandbox.reason}</span>}</span>
              <span>Интернет</span>
              <span>{overview.settings.allowed_hosts.length ? `только ${overview.settings.allowed_hosts.join(', ')}` : 'выключен (нет разрешённых хостов)'}</span>
              <span>Автопилот / автопоиск</span>
              <span>{overview.schedule && overview.schedule.active
                ? `${overview.schedule.auto_generate || overview.schedule.auto_execute ? 'автопилот' : 'только поиск'}: каждые ${overview.schedule.interval_hours} ч до ${formatDate(overview.schedule.valid_until)}`
                : 'выключен'}</span>
              <span>Внешние модели</span>
              <span>{overview.budget.external.enabled ? `${overview.budget.external.model}, осталось $${overview.budget.external.remaining_usd.toFixed(2)} из $${overview.budget.external.limit_usd.toFixed(2)}` : 'выключены'}</span>
              <span>Ожидают решения</span>
              <span>{overview.pending_approvals}</span>
            </div>
            <div className="ea-row">
              <button className="ea-btn is-primary" disabled={!overview.settings.enabled} onClick={requestResearch}><Play size={15} /> Запросить исследование</button>
              {overview.settings.sources.length === 0 && <span className="ea-dim">Сначала добавьте источник в «Настройках»</span>}
            </div>
          </div>
          <div className="ea-card">
            <h4>Бюджет на {overview.budget.day} ({overview.budget.timezone})</h4>
            <div className="ea-meters">
              <Meter label="Задачи" entry={overview.budget.tasks} />
              <Meter label="Вызовы модели" entry={overview.budget.llm_calls} />
              <Meter label="Токены (резерв)" entry={overview.budget.tokens} hint="Резервируется до вызова: оценка промпта + max_tokens; после ответа уменьшается до факта" />
              <Meter label="Запросы к источникам" entry={overview.budget.fetches} />
              <Meter label="Запуски sandbox" entry={overview.budget.sandbox_runs} />
              <Meter label="Отправки" entry={overview.budget.submissions} />
              {overview.budget.external.enabled && (
                <Meter label={`Внешние модели, $ (${overview.budget.external.month})`} entry={{ used: Number(overview.budget.external.reserved_usd.toFixed(2)), limit: overview.budget.external.limit_usd, remaining: Number(overview.budget.external.remaining_usd.toFixed(2)), actual: Number(overview.budget.external.used_usd.toFixed(4)) }} />
              )}
            </div>
            <div className="ea-dim">Оценка затрат на электричество: ${overview.budget.est_cost_usd.toFixed(4)} — {overview.budget.est_cost_note}</div>
          </div>
          <div className="ea-card ea-span">
            <h4>Отчёт</h4>
            <div className="ea-report">
              <div><strong>{overview.report.potential}</strong><span>потенциальные возможности</span></div>
              <div><strong>{overview.report.prepared}</strong><span>подготовленные результаты</span></div>
              <div><strong>{overview.report.delivered_manual}</strong><span>передано (вручную подтверждено)</span></div>
              <div><strong>{overview.report.paid_manual}</strong><span>оплачено ({overview.report.paid_onchain} подтверждено блокчейном)</span></div>
            </div>
            {overview.income.wallet.address
              ? <div className="ea-dim"><Wallet size={13} /> Поступления на {overview.income.wallet.address.slice(0, 8)}…: ${overview.income.total_usd.toFixed(2)}, из них привязано к задачам ${overview.income.linked_usd.toFixed(2)}</div>
              : <div className="ea-dim"><Wallet size={13} /> Кошелёк не указан — добавьте публичный адрес в «Настройках».</div>}
            <div className="ea-warn"><AlertTriangle size={14} /> {overview.report.note}{overview.report.demo ? ` Демо-задач в списке: ${overview.report.demo}.` : ''}</div>
          </div>
          <div className="ea-card ea-span">
            <h4>Последние исследования</h4>
            {overview.runs.length === 0 ? <div className="ea-dim">Ещё не запускались</div> : (
              <table className="ea-table">
                <thead><tr><th>Создано</th><th>Запуск</th><th>Статус</th><th>Новых / повтор</th><th>Оценено / предложено</th><th>Источники</th></tr></thead>
                <tbody>
                  {overview.runs.map(run => (
                    <tr key={run.id}>
                      <td>{formatDate(run.created_at)}</td><td>{run.trigger === 'schedule' ? 'по расписанию' : 'вручную'}</td>
                      <td>{run.status}{run.error ? <div className="ea-dim">{run.error}</div> : null}</td>
                      <td>{run.candidates_new} / {run.candidates_seen}</td><td>{run.evaluated} / {run.proposed}</td>
                      <td>{(run.sources_report || []).map(s => <div key={s.source_id} className={s.ok ? '' : 'ea-bad'}>{s.source_id}: {s.ok ? `${s.found} шт.` : s.error}</div>)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {view === 'opportunities' && (
        <div className="ea-split">
          <div className="ea-card ea-list-card">
            <div className="ea-row ea-between">
              <h4><Inbox size={15} /> Найденные возможности</h4>
              <select className="ea-input ea-select" value={filter} onChange={e => setFilter(e.target.value)}>
                <option value="">Все статусы</option>
                {Object.entries(STATUS_LABEL).map(([key, meta]) => <option key={key} value={key}>{meta.label}</option>)}
              </select>
            </div>
            {visible.length === 0 ? <div className="ea-dim">Пусто. Запустите исследование из «Обзора».</div> : (
              <table className="ea-table is-clickable">
                <thead><tr><th>Задача</th><th>Статус</th><th>Награда (заявлено)</th><th>Оценка</th></tr></thead>
                <tbody>
                  {visible.map(row => (
                    <tr key={row.id} className={selected === row.id ? 'is-selected' : ''} onClick={() => setSelected(row.id)}>
                      <td>{row.is_demo ? <span className="ea-chip is-info">демо</span> : null} {row.title}<div className="ea-dim">{row.source_id} · {formatDate(row.fetched_at)}</div></td>
                      <td><StatusChip status={row.status} /></td>
                      <td>{row.claimed_reward || '—'}</td>
                      <td>{row.effort_hours ? `${row.effort_hours} ч, ${row.uncertainty}` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {selected && <OpportunityPanel key={selected} id={selected} onClose={() => setSelected(null)} onChanged={done} sandboxReason={sandboxReason} />}
        </div>
      )}

      {view === 'approvals' && (
        <div className="ea-stack">
          {pending.length === 0 && <div className="ea-card ea-dim">Нет ожидающих подтверждений.</div>}
          {pending.map(approval => <ApprovalCard key={approval.id} approval={approval} onDone={done} />)}
          <div className="ea-card">
            <h4>История разрешений</h4>
            <table className="ea-table">
              <thead><tr><th>Создано</th><th>Действие</th><th>Что</th><th>Статус</th><th>Кем / почему</th></tr></thead>
              <tbody>
                {approvals.filter(a => !pending.includes(a)).map(approval => (
                  <tr key={approval.id}>
                    <td>{formatDate(approval.created_at)}</td><td>{ACTION_LABEL[approval.action]}</td><td>{approval.summary.title}</td>
                    <td>{APPROVAL_STATUS[approval.status] ?? approval.status}</td>
                    <td className="ea-dim">{approval.decided_by} {approval.status_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {view === 'income' && <IncomeView opportunities={opportunities} onChanged={done} />}

      {view === 'history' && (
        <div className="ea-grid">
          <div className="ea-card">
            <h4>Расход по дням (UTC)</h4>
            <table className="ea-table">
              <thead><tr><th>День</th><th>Задачи</th><th>Вызовы</th><th>Токены резерв / факт</th><th>Запросы</th><th>Sandbox</th><th>Оценка, $</th></tr></thead>
              <tbody>
                {(history.spend?.days || []).map(day => (
                  <tr key={day.day}><td>{day.day}</td><td>{day.tasks}</td><td>{day.llm_calls}</td><td>{day.tokens_reserved} / {day.tokens_used}</td><td>{day.fetches}</td><td>{day.sandbox_runs}</td><td>{day.est_cost_usd.toFixed(4)}</td></tr>
                ))}
              </tbody>
            </table>
            <h4>Вызовы модели</h4>
            <table className="ea-table">
              <thead><tr><th>Время</th><th>Стадия</th><th>Статус</th><th>Токены</th><th>Длительность</th></tr></thead>
              <tbody>
                {(history.spend?.calls || []).map(call => (
                  <tr key={call.id}><td>{formatDate(call.created_at)}</td><td>{call.stage}</td><td>{call.status}{call.error ? ` (${call.error})` : ''}</td><td>{call.prompt_tokens}+{call.completion_tokens} (резерв {call.reserved_tokens})</td><td>{(call.duration_ms / 1000).toFixed(1)} с</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="ea-card">
            <h4>Журнал событий</h4>
            <EventList events={history.events} />
          </div>
        </div>
      )}

      {view === 'settings' && <SettingsView key={overview.settings.revision} overview={overview} onSaved={done} />}
    </div>
  );
}
