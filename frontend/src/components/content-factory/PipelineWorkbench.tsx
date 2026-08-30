import { useMemo, useState } from 'react';
import {
  Activity, Bot, Check, Clock3, FileText, Globe2, Image as ImageIcon, Mic2,
  Pause, Play, Plus, Rocket, Scissors, Upload, Video,
} from 'lucide-react';
import type {
  CFActivityItem, CFAgentDetail, CFAgentRow, CFAsset, CFChannel, CFEdge, CFNode, CFPipeline, CFQueue, CFRun,
  CFPipelineCreateRequest, CFProviderRow, CFSettings, CFStats, CFTemplate, CFTraceEntry, CFValidation,
} from './types';
import { PipelineWizard } from './PipelineWizard';
import './pipeline-workbench.css';
import './pipeline-workbench-activity.css';

type Props = {
  language: 'ru' | 'en';
  pipelines: CFPipeline[];
  pipeline: CFPipeline | null;
  nodes: CFNode[];
  edges: CFEdge[];
  runs: CFRun[];
  run: CFRun | null;
  trace: CFTraceEntry[];
  validation: CFValidation | null;
  agents: CFAgentRow[];
  agentDetail: CFAgentDetail | null;
  templates: CFTemplate[];
  // Control-centre panels, each measured by the backend rather than guessed
  // from whatever happens to be loaded on screen (dashboard.py).
  queue: CFQueue | null;
  activity: CFActivityItem[];
  stats: CFStats | null;
  channels: CFChannel[];
  providers: CFProviderRow[];
  settings: CFSettings | null;
  assets: CFAsset[];
  logs: { at: string | null; nodeId: string; text: string; level?: string }[];
  busy: boolean;
  onPipeline: (id: string) => void;
  onRun: (id: string) => void;
  onStart: (dryRun: boolean, topic: string) => void;
  onPause: () => void;
  onActivate: () => void;
  onTemplate: (template: CFTemplate, request: CFPipelineCreateRequest) => Promise<void>;
  onAgent: (agentId: string) => void;
  onAsset: (assetId: string) => void;
  onOpenAgents?: () => void;
  onConfigureChannel: (channelId: string) => void;
};

const ICONS = [Globe2, FileText, Mic2, ImageIcon, Scissors, Upload];

function statusClass(status?: string) {
  if (status === 'COMPLETED') return 'is-complete';
  if (['RUNNING', 'WAITING_EXTERNAL'].includes(status ?? '')) return 'is-running';
  if (['FAILED', 'CANCELLED'].includes(status ?? '')) return 'is-failed';
  if (['WAITING_APPROVAL', 'RETRYING'].includes(status ?? '')) return 'is-waiting';
  return 'is-queued';
}

function compact(value: number) {
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value || 0);
}

function MiniChart({ stats, empty }: { stats: CFStats | null; empty: string }) {
  const points = stats?.series ?? [];
  // Produced content, not views: on a channel that has published nothing the
  // views series is flat zero and would draw a straight line that looks like
  // data. This one moves as soon as the factory does anything at all.
  const values = points.map((point) => point.created + point.published);
  const max = Math.max(...values, 1);
  const hasData = values.some((value) => value > 0);
  const line = values.length > 1
    ? values.map((value, index) => `${(index / (values.length - 1)) * 100},${38 - (value / max) * 32}`).join(' ')
    : '';
  return (
    <>
      <svg className="cfw-chart" viewBox="0 0 100 42" preserveAspectRatio="none" aria-label="Analytics chart">
        {[8, 18, 28, 38].map(y => <line key={y} x1="0" x2="100" y1={y} y2={y} />)}
        {hasData && line && <polyline points={line} />}
      </svg>
      {!hasData && <p className="cfw-empty-small">{empty}</p>}
    </>
  );
}

export function PipelineWorkbench(props: Props) {
  const {
    language, pipelines, pipeline, nodes, run, trace, validation, agents, agentDetail,
    templates, queue, activity, stats, channels, providers, settings, assets, logs: runLogs, busy,
    onPipeline, onRun, onStart, onPause, onActivate, onTemplate, onAgent, onAsset,
    onOpenAgents, onConfigureChannel,
  } = props;
  const ru = language === 'ru';
  const [agentId, setAgentId] = useState('');
  const [topic, setTopic] = useState(ru ? 'Новости искусственного интеллекта' : 'Artificial intelligence news');
  const [selectedNodeId, setSelectedNodeId] = useState('');
  const [agentTab, setAgentTab] = useState<'overview' | 'settings' | 'models' | 'metrics'>('overview');
  const [channelId, setChannelId] = useState('');
  const [showAllAssets, setShowAllAssets] = useState(false);
  const selectedAgent = agents.find(agent => agent.id === agentId) ?? agents[0];
  // The inspector shows measured numbers for the agent that is actually
  // selected; until its detail arrives it shows the roster row, never a
  // placeholder metric.
  const detail = agentDetail?.id === selectedAgent?.id ? agentDetail : null;
  const nodeRun = useMemo(
    () => new Map((run?.nodes ?? []).map(item => [item.node_id, item])),
    [run],
  );
  const automaticNode = (run?.nodes ?? []).find(item => ['RUNNING', 'WAITING_EXTERNAL', 'RETRYING'].includes(item.status))
    ?? (run?.nodes ?? []).find(item => item.status === 'FAILED')
    ?? (run?.nodes ?? []).at(-1);
  const activeNode = (run?.nodes ?? []).find(item => item.node_id === selectedNodeId) ?? automaticNode;
  const selectedNode = nodes.find(node => node.id === selectedNodeId);
  const nodeLogs = trace.find(entry => entry.nodeId === activeNode?.node_id)?.logs ?? [];

  return (
    <div className="cfw-shell">
      <div className="cfw-primary">
        <section className="cfw-panel cfw-pipeline">
          <header className="cfw-pipeline-head">
            <div className="cfw-title-group">
              <div className="cfw-mark"><Rocket size={14} /></div>
              <select
                aria-label={ru ? 'Выбор пайплайна' : 'Select pipeline'}
                value={pipeline?.id ?? ''}
                onChange={event => onPipeline(event.target.value)}
              >
                <option value="" disabled>{ru ? 'Выберите пайплайн' : 'Select a pipeline'}</option>
                {pipelines.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
              {pipeline && <span className={`cfw-chip ${pipeline.status === 'ACTIVE' ? 'green' : ''}`}>{pipeline.status}</span>}
              {pipeline && <span className="cfw-schedule"><Clock3 size={11} /> {ru ? 'По расписанию' : 'Scheduled'}</span>}
            </div>
            <div className="cfw-actions">
              <input className="cfw-launch-input" aria-label={ru ? 'Тема запуска' : 'Run topic'} value={topic}
                onChange={event => setTopic(event.target.value)} placeholder={ru ? 'Тема ролика' : 'Video topic'} />
              {pipeline?.status === 'ACTIVE' ? (
                <>
                  <button disabled={busy || !topic.trim()} onClick={() => onStart(false, topic.trim())}><Play size={12} />{ru ? 'Запустить' : 'Run'}</button>
                  <button disabled={busy} onClick={onPause}><Pause size={12} />{ru ? 'Пауза' : 'Pause'}</button>
                </>
              ) : pipeline ? <button disabled={busy || !!validation?.errors?.length} onClick={onActivate}><Check size={12} />{ru ? 'Активировать' : 'Activate'}</button> : null}
              <button className="accent" disabled={busy || !pipeline || !topic.trim()} onClick={() => onStart(true, topic.trim())}><Plus size={12} />{ru ? 'Тестовый прогон' : 'Test run'}</button>
            </div>
          </header>

          {!pipeline ? (
            <div className="cfw-empty">{ru ? 'Создайте pipeline из шаблона в нижней панели.' : 'Create a pipeline from a template below.'}</div>
          ) : (
            <>
              <div className="cfw-stages" aria-label={ru ? 'Этапы пайплайна' : 'Pipeline stages'}>
                {nodes.map((node, index) => {
                  const state = nodeRun.get(node.id);
                  const Icon = ICONS[index % ICONS.length];
                  return (
                    <button type="button" className={`cfw-stage ${statusClass(state?.status)} ${selectedNodeId === node.id ? 'is-selected' : ''}`}
                      key={node.id} onClick={() => setSelectedNodeId(node.id)} aria-pressed={selectedNodeId === node.id}>
                      {index > 0 && <span className="cfw-connector" />}
                      <div className="cfw-stage-icon"><Icon size={22} /></div>
                      <strong>{index + 1}. {(node.name || node.type).toUpperCase()}</strong>
                      <span>{node.type.replaceAll('_', ' ')}</span>
                      <p>{state?.provider || (ru ? 'Ожидает запуска' : 'Waiting to start')}</p>
                      <footer>
                        <b>{state?.status ?? 'PENDING'}</b>
                        <time>{state?.duration_ms ? `${(state.duration_ms / 1000).toFixed(1)}s` : ''}</time>
                      </footer>
                      {state?.status === 'RUNNING' && <i style={{ width: `${state.progress}%` }} />}
                    </button>
                  );
                })}
              </div>

              <div className="cfw-current">
                <div className="cfw-current-status">
                  <label>{ru ? 'Текущий этап' : 'Current stage'}</label>
                  <h3>{activeNode ? `${activeNode.name || activeNode.node_id}` : selectedNode ? (selectedNode.name || selectedNode.id) : (ru ? 'Выберите этап' : 'Select a stage')}</h3>
                  <p>{activeNode?.error_message || (ru ? 'Pipeline готов к выполнению.' : 'Pipeline is ready to run.')}</p>
                  <div className="cfw-progress"><i style={{ width: `${activeNode?.progress ?? run?.progress ?? 0}%` }} /></div>
                  <dl>
                    <div><dt>{ru ? 'Провайдер' : 'Provider'}</dt><dd>{activeNode?.provider || '—'}</dd></div>
                    <div><dt>{ru ? 'Модель' : 'Model'}</dt><dd>{activeNode?.model || '—'}</dd></div>
                    <div><dt>{ru ? 'Попытка' : 'Attempt'}</dt><dd>{activeNode?.attempt ?? '—'}</dd></div>
                    <div><dt>{ru ? 'Прогресс' : 'Progress'}</dt><dd>{activeNode?.progress ?? run?.progress ?? 0}%</dd></div>
                  </dl>
                </div>
                <div className="cfw-output">
                  <label>{ru ? 'Выходные данные' : 'Output data'}</label>
                  <div className="cfw-output-grid">
                    {(showAllAssets ? assets : assets.slice(0, 3)).map(asset => (
                      <button className="cfw-preview" key={asset.id} onClick={() => onAsset(asset.id)}
                        title={`${asset.type} · ${asset.provider || '—'}`}>
                        <Video size={21} />
                        <span>{asset.type}{asset.duration ? ` · ${asset.duration.toFixed(0)}s` : ''}</span>
                      </button>
                    ))}
                    {!assets.length && (
                      <div className="cfw-preview cfw-preview-empty">
                        <Video size={21} />
                        <span>{ru ? 'Материалов ещё нет' : 'No assets yet'}</span>
                      </div>
                    )}
                  </div>
                  {assets.length > 3 && (
                    <button onClick={() => setShowAllAssets(value => !value)}>
                      {showAllAssets ? (ru ? 'Свернуть' : 'Collapse') : (ru ? `Показать все (${assets.length})` : `Show all (${assets.length})`)}
                    </button>
                  )}
                </div>
                <div className="cfw-logs">
                  <label>{ru ? 'Логи агента' : 'Agent logs'}</label>
                  {runLogs.slice(-8).map((entry, index) => (
                    <p key={`${entry.nodeId}-${index}`} className={entry.level === 'error' ? 'error' : undefined}>
                      <time>{entry.at
                        ? new Intl.DateTimeFormat(language, { hour: '2-digit', minute: '2-digit' }).format(new Date(entry.at))
                        : '--:--'}</time>
                      {entry.nodeId}: {entry.text}
                    </p>
                  ))}
                  {!runLogs.length && nodeLogs.map((log, index) => (
                    <p key={`node-${index}`}><time>{String(index + 1).padStart(2, '0')}</time>{log}</p>
                  ))}
                  {!runLogs.length && !nodeLogs.length && (
                    <p className="muted">{ru ? 'Логи появятся после запуска' : 'Logs appear after a run'}</p>
                  )}
                </div>
              </div>
            </>
          )}
        </section>

        <section className="cfw-bottom">
          <div className="cfw-panel cfw-library">
            <header><span>{ru ? 'Библиотека агентов' : 'Agent library'}</span><button onClick={onOpenAgents}><Plus size={11} />{ru ? 'Создать' : 'Create'}</button></header>
            {agents.map(agent => (
              <button key={agent.id} aria-label={agent.name}
                className={selectedAgent?.id === agent.id ? 'selected' : ''}
                onClick={() => { setAgentId(agent.id); onAgent(agent.id); }}>
                <Bot size={15} /><span><strong>{agent.name}</strong><small>{agent.role}</small></span><i />
              </button>
            ))}
          </div>

          <div className="cfw-panel cfw-agent">
            <header><Bot size={15} />{selectedAgent?.name ?? (ru ? 'Агент' : 'Agent')}</header>
            <div className="cfw-agent-tabs">
              <button aria-label={ru ? 'Обзор выбранного агента' : 'Agent overview'} className={agentTab === 'overview' ? 'active' : ''} onClick={() => setAgentTab('overview')}>{ru ? 'Обзор' : 'Overview'}</button>
              <button aria-label={ru ? 'Настройки выбранного агента' : 'Agent settings'} className={agentTab === 'settings' ? 'active' : ''} onClick={() => setAgentTab('settings')}>{ru ? 'Настройки' : 'Settings'}</button>
              <button aria-label={ru ? 'Модели выбранного агента' : 'Agent models'} className={agentTab === 'models' ? 'active' : ''} onClick={() => setAgentTab('models')}>{ru ? 'Модели' : 'Models'}</button>
              <button aria-label={ru ? 'Метрики выбранного агента' : 'Agent metrics'} className={agentTab === 'metrics' ? 'active' : ''} onClick={() => setAgentTab('metrics')}>{ru ? 'Метрики' : 'Metrics'}</button>
            </div>
            <div className="cfw-agent-grid">
              {(agentTab === 'overview' || agentTab === 'settings') && <article><label>{agentTab === 'settings' ? (ru ? 'Настройки агента' : 'Agent settings') : (ru ? 'Описание' : 'Description')}</label><h3>{selectedAgent?.name ?? '—'}</h3><p>{selectedAgent?.role ?? '—'}</p><dl><div><dt>{ru ? 'Статус' : 'Status'}</dt><dd>{selectedAgent?.status ?? '—'}</dd></div><div><dt>{ru ? 'Модель' : 'Model'}</dt><dd>{selectedAgent?.model ?? '—'}</dd></div><div><dt>{ru ? 'Бюджет' : 'Budget'}</dt><dd>${Number(selectedAgent?.budgetUsdLimit ?? 0).toFixed(2)}</dd></div></dl>{agentTab === 'settings' && <button className="cfw-inline-action" onClick={onOpenAgents}>{ru ? 'Открыть админку агента' : 'Open agent admin'}</button>}</article>}
              {(agentTab === 'overview' || agentTab === 'models') && <article><label>{ru ? 'Используемые модели' : 'Models in use'}</label>{(selectedAgent?.providerChain ?? []).map(provider => <p className="cfw-model" key={provider}><Bot size={13} /><span>{provider}</span><b>LLM</b></p>)}{agentTab === 'models' && <button className="cfw-inline-action" onClick={onOpenAgents}>{ru ? 'Изменить модели' : 'Change models'}</button>}</article>}
              {(agentTab === 'overview' || agentTab === 'metrics') && <article>
                <label>{ru ? 'Производительность' : 'Performance'}</label>
                <dl>
                  <div><dt>{ru ? 'Успешность' : 'Success'}</dt>
                    <dd>{detail?.metrics?.successRate != null ? `${detail.metrics.successRate}%` : '—'}</dd></div>
                  <div><dt>{ru ? 'Выполнено' : 'Completed'}</dt>
                    <dd>{detail?.metrics?.completed ?? '—'}</dd></div>
                  <div><dt>{ru ? 'Ошибок' : 'Failed'}</dt>
                    <dd>{detail?.metrics?.failed ?? '—'}</dd></div>
                  <div><dt>{ru ? 'Среднее время' : 'Average time'}</dt>
                    <dd>{detail?.metrics?.avgDurationMs ? `${(detail.metrics.avgDurationMs / 1000).toFixed(1)}s` : '—'}</dd></div>
                  <div><dt>{ru ? 'Стоимость' : 'Cost'}</dt>
                    <dd>${Number(detail?.metrics?.cost ?? 0).toFixed(2)}</dd></div>
                </dl>
                <p className="cfw-empty-small">{detail
                  ? (ru ? `За ${detail.windowDays ?? 7} дней` : `Over ${detail.windowDays ?? 7} days`)
                  : (ru ? 'Метрики загружаются' : 'Loading metrics')}</p>
              </article>}
            </div>
          </div>

          <div className="cfw-panel cfw-create cfw-create-wizard">
            <PipelineWizard language={language} templates={templates} channels={channels}
              providers={providers} settings={settings} busy={busy} onCreate={onTemplate} />
          </div>
        </section>
      </div>

      <aside className="cfw-rail">
        <section className="cfw-panel">
          <header>
            <span>{ru ? 'Очередь pipelines' : 'Pipeline queue'}</span>
            <b>{queue ? `${queue.active}/${queue.capacity}` : '—'}</b>
          </header>
          {(queue?.items ?? []).slice(0, 5).map(item => (
            <button key={`${item.kind}-${item.id}`}
              onClick={() => item.kind === 'RUN' && onRun(item.id)}>
              <Rocket size={13} />
              <span>
                <strong>{item.name}</strong>
                <small>{item.kind === 'SCHEDULE'
                  ? `${item.expression ?? ''} · ${item.status}`
                  : `${item.status}${item.at ? ` · ${new Intl.DateTimeFormat(language, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(item.at))}` : ''}`}</small>
              </span>
              <b>{item.kind === 'RUN' ? `${item.progress ?? 0}%` : 'SCHED'}</b>
            </button>
          ))}
          {!queue?.items?.length && <p className="cfw-empty-small">{ru ? 'Очередь пуста' : 'Queue is empty'}</p>}
        </section>
        <section className="cfw-panel">
          <header><span>{ru ? 'Последняя активность' : 'Recent activity'}</span></header>
          {activity.slice(0, 6).map((item, index) => (
            <div className="cfw-activity" key={`${item.type}-${item.refId}-${index}`}>
              <Activity size={12} />
              <span>
                <strong>{item.title}</strong>
                <small>{item.type === 'publication'
                  ? `${ru ? 'публикация' : 'publication'} · ${item.detail}`
                  : `${ru ? 'прогон' : 'run'} · ${item.detail}`}</small>
                {item.at && <em>{new Intl.DateTimeFormat(language, { hour: '2-digit', minute: '2-digit' }).format(new Date(item.at))}</em>}
                {item.status === 'FAILED' && <i>{item.detail}</i>}
              </span>
            </div>
          ))}
          {!activity.length && <p className="cfw-empty-small">{ru ? 'Событий пока нет' : 'No events yet'}</p>}
        </section>
        <section className="cfw-panel cfw-stats">
          <header>
            <span>{ru ? 'Статистика' : 'Statistics'}</span>
            <b>{stats ? `${stats.series.length}D` : '—'}</b>
          </header>
          <div>
            <span><small>{ru ? 'Создано' : 'Created'}</small><strong>{stats?.today.created ?? 0}</strong></span>
            <span><small>{ru ? 'Просмотры' : 'Views'}</small><strong>{compact(stats?.totals.views ?? 0)}</strong></span>
            <span><small>Eng Rate</small><strong>{Number(stats?.totals.engagementRate ?? 0).toFixed(1)}%</strong></span>
          </div>
          <MiniChart stats={stats} empty={ru ? 'Данных пока нет' : 'No data yet'} />
        </section>
        {validation && (validation.errors.length > 0 || validation.warnings.length > 0) && <section className="cfw-panel cfw-validation"><header>{ru ? 'Проверка' : 'Validation'}</header>{validation.errors.map(item => <p className="error" key={`${item.code}-${item.message}`}>{item.code}: {item.message}</p>)}{validation.warnings.map(item => <p key={`${item.code}-${item.message}`}>{item.code}: {item.message}</p>)}{validation.errors.some(item => item.code === 'NO_CHANNEL') && <div className="cfw-fix"><select value={channelId} onChange={event => setChannelId(event.target.value)}><option value="">{ru ? 'Выберите аккаунт' : 'Choose account'}</option>{channels.map(channel => <option key={channel.id} value={channel.id}>{channel.platform} · {channel.account_name}</option>)}</select><button disabled={busy || !channelId} onClick={() => onConfigureChannel(channelId)}>{ru ? 'Сохранить канал' : 'Save channel'}</button></div>}{validation.warnings.some(item => item.code === 'NO_FALLBACK') && <button className="cfw-inline-action" onClick={onOpenAgents}>{ru ? 'Настроить модели и fallback' : 'Configure models and fallback'}</button>}</section>}
      </aside>
    </div>
  );
}
