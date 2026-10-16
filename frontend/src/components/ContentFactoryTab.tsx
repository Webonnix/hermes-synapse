/**
 * CONTENT FACTORY (§4–8, §37–42).
 *
 * The control centre for autonomous content production: what is running, what
 * needs a human, what it cost, and what actually happened inside a run.
 *
 * Two rules this panel follows that are easy to lose sight of:
 *
 * - **Never claim more than the backend said.** A run is "published" only when
 *   a publication row says so; a node is "done" only when its status says so.
 *   Optimistic UI here would mean telling the owner a video went out when it
 *   did not.
 * - **Errors show their cause.** §103: provider, reason code, attempt number
 *   and what can be done about it — never a bare "something went wrong".
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  AlertTriangle, BadgeCheck, BarChart3, Clock, Coins, Eye, Factory, FileVideo, Heart, Layers,
  MessageCircle, Network, Play, Plug, Plus, RefreshCw, Share2, ShieldCheck, Wallet, WandSparkles, X,
} from 'lucide-react';
import { PipelineWorkbench } from './content-factory/PipelineWorkbench';
import { PipelineWizard } from './content-factory/PipelineWizard';
import { IntegrationsPanel } from './content-factory/IntegrationsPanel';
import { SettingsPanel } from './content-factory/SettingsPanel';
import { StoragePanel } from './content-factory/StoragePanel';
import type {
  CFActivityItem, CFAgentDetail, CFAgentRow, CFAnalyticsOverview, CFAsset, CFChannel, CFContentItem,
  CFEdge, CFEvent, CFIntegrations, CFLLMEndpoints, CFLiveRun, CFMediaProvider, CFNode, CFOverview,
  CFPipeline, CFPipelineCreateRequest, CFProviderRow, CFQueue, CFRun, CFSettings, CFStats,
  CFStorageConfig, CFStorageUsage,
  CFTemplate, CFTraceEntry, CFValidation,
} from './content-factory/types';

type Props = {
  language: 'ru' | 'en';
  lastEvent?: CFEvent | null;
  onOpenAgents?: () => void;
};

type View = 'pipelines' | 'wizard' | 'queue' | 'analytics' | 'storage' | 'channels' | 'integrations' | 'settings';

const COPY = {
  ru: {
    title: 'CONTENT FACTORY', subtitle: 'Автономное производство и публикация контента',
    overview: 'Обзор', pipelines: 'Пайплайны', wizard: 'Мастер pipeline',
    queue: 'Очередь', analytics: 'Аналитика', channels: 'Аккаунты',
    agents: 'Агенты и модели', refresh: 'Обновить',
    activePipelines: 'Активных пайплайнов', processing: 'В работе', review: 'Ждут проверки',
    scheduled: 'Запланировано', publishedToday: 'Опубликовано сегодня', failed: 'Ошибок',
    costToday: 'Затраты сегодня', costMonth: 'За месяц',
    liveActivity: 'Активные прогоны', noLiveRuns: 'Сейчас ничего не выполняется.',
    createFromTemplate: 'Создать из шаблона', templates: 'Шаблоны',
    noPipelines: 'Пайплайнов пока нет. Начните с шаблона — он создаст черновик, который можно править.',
    activate: 'Активировать', pause: 'Пауза', run: 'Запустить', testRun: 'Тестовый прогон',
    validation: 'Проверка', errors: 'Ошибки', warnings: 'Предупреждения', noProblems: 'Проблем не найдено.',
    version: 'Версия', status: 'Статус', mode: 'Режим публикации', cost: 'Стоимость',
    runs: 'Прогоны', trace: 'Хроника выполнения', node: 'Узел', provider: 'Провайдер',
    attempt: 'Попытка', duration: 'Длительность', cancel: 'Отменить прогон', retry: 'Повторить узел',
    regenerate: 'Сгенерировать заново', approve: 'Одобрить', reject: 'Отклонить',
    awaitingApproval: 'Прогон ждёт вашего решения',
    noRuns: 'Прогонов ещё не было.', selectPipeline: 'Выберите пайплайн слева.',
    dryRun: 'тестовый', budget: 'Бюджет',
    contentEmpty: 'Контента пока нет — он появится после первого успешного прогона.',
    title_: 'Название', createdAt: 'Создан', quality: 'Качество',
    channelsEmpty: 'Каналы не подключены. Без канала публикация невозможна.',
    addChannel: 'Добавить канал', platform: 'Платформа', account: 'Аккаунт',
    connectAccount: 'Подключить аккаунт', editConnection: 'Подключение', proxy: 'Прокси',
    proxyHint: 'http://user:password@host:port', refreshToken: 'Refresh token',
    clientId: 'Client ID / Key', clientSecret: 'Client secret', save: 'Сохранить',
    statistics: 'Статистика', views: 'Просмотры', likes: 'Лайки', comments: 'Комментарии',
    shares: 'Репосты', engagement: 'Вовлечённость', syncStats: 'Получить статистику',
    noStats: 'Статистика появится после синхронизации опубликованных видео.',
    agentsTitle: 'Агенты фабрики', providersTitle: 'Провайдеры', storage: 'Хранилище',
    model: 'Модель', chain: 'Цепочка провайдеров', budgetLimit: 'Лимит бюджета',
    free: 'бесплатный', asyncJob: 'асинхронный', concurrency: 'параллельно',
    loadFailed: 'Не удалось загрузить данные Content Factory.',
    mockWarning: 'Включены mock-провайдеры: ни один платный API не вызывается.',
    seedAgents: 'Создать агентов',
    integrations: 'Интеграции',
    settingsTab: 'Настройки',
    kpis: 'Сводка',
    storageTitle: 'Хранилище ассетов',
    noAssets: 'Ассетов пока нет — они появятся после первого прогона.',
    settingsSaved: 'Настройки сохранены.',
    wizardSubtitle: 'Соберите новый pipeline из шаблона: контент, надёжность, публикация.',
    wizardTemplates: 'Доступно шаблонов', wizardChannels: 'Подключено аккаунтов',
    wizardPipelines: 'Уже создано pipelines',
    wizardNoTemplates: 'Шаблоны не загрузились — без них мастер не может создать pipeline.',
    wizardBackToPipelines: 'К пайплайнам',
  },
  en: {
    title: 'CONTENT FACTORY', subtitle: 'Autonomous content production and publishing',
    overview: 'Overview', pipelines: 'Pipelines', wizard: 'Pipeline wizard',
    queue: 'Queue', analytics: 'Analytics', channels: 'Accounts',
    agents: 'Agents & models', refresh: 'Refresh',
    activePipelines: 'Active pipelines', processing: 'Processing', review: 'Review required',
    scheduled: 'Scheduled', publishedToday: 'Published today', failed: 'Failed',
    costToday: 'Cost today', costMonth: 'This month',
    liveActivity: 'Live pipeline activity', noLiveRuns: 'Nothing is running right now.',
    createFromTemplate: 'Create from template', templates: 'Templates',
    noPipelines: 'No pipelines yet. Start from a template — it creates an editable draft.',
    activate: 'Activate', pause: 'Pause', run: 'Run', testRun: 'Test run',
    validation: 'Validation', errors: 'Errors', warnings: 'Warnings', noProblems: 'No problems found.',
    version: 'Version', status: 'Status', mode: 'Publishing mode', cost: 'Cost',
    runs: 'Runs', trace: 'Execution trace', node: 'Node', provider: 'Provider',
    attempt: 'Attempt', duration: 'Duration', cancel: 'Cancel run', retry: 'Retry node',
    regenerate: 'Regenerate', approve: 'Approve', reject: 'Reject',
    awaitingApproval: 'This run is waiting for your decision',
    noRuns: 'No runs yet.', selectPipeline: 'Select a pipeline on the left.',
    dryRun: 'test', budget: 'Budget',
    contentEmpty: 'No content yet — it appears after the first successful run.',
    title_: 'Title', createdAt: 'Created', quality: 'Quality',
    channelsEmpty: 'No channels connected. Publishing is impossible without one.',
    addChannel: 'Add channel', platform: 'Platform', account: 'Account',
    connectAccount: 'Connect account', editConnection: 'Connection', proxy: 'Proxy',
    proxyHint: 'http://user:password@host:port', refreshToken: 'Refresh token',
    clientId: 'Client ID / Key', clientSecret: 'Client secret', save: 'Save',
    statistics: 'Statistics', views: 'Views', likes: 'Likes', comments: 'Comments',
    shares: 'Shares', engagement: 'Engagement', syncStats: 'Fetch statistics',
    noStats: 'Statistics will appear after syncing published videos.',
    agentsTitle: 'Factory agents', providersTitle: 'Providers', storage: 'Storage',
    model: 'Model', chain: 'Provider chain', budgetLimit: 'Budget limit',
    free: 'free', asyncJob: 'async', concurrency: 'concurrent',
    loadFailed: 'Could not load Content Factory data.',
    mockWarning: 'Mock providers are enabled: no paid API is being called.',
    seedAgents: 'Create agents',
    integrations: 'Integrations',
    settingsTab: 'Settings',
    kpis: 'Summary',
    storageTitle: 'Asset storage',
    noAssets: 'No assets yet — they appear after the first run.',
    settingsSaved: 'Settings saved.',
    wizardSubtitle: 'Build a new pipeline from a template: content, reliability, publishing.',
    wizardTemplates: 'Templates available', wizardChannels: 'Accounts connected',
    wizardPipelines: 'Pipelines already created',
    wizardNoTemplates: 'No templates loaded — the wizard cannot create a pipeline without one.',
    wizardBackToPipelines: 'Back to pipelines',
  },
} as const;

const RUN_STATUS_COLOR: Record<string, string> = {
  QUEUED: '#99a2b1', RUNNING: '#36cffb', WAITING: '#36cffb', WAITING_APPROVAL: '#b197ff',
  COMPLETED: '#35d49a', PARTIALLY_COMPLETED: '#f6b84a', FAILED: '#f05d75', CANCELLED: '#6c7381',
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/content-factory${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // §76: keep the code, it is what the UI reacts to; the message is only text.
    const envelope = body?.detail?.error;
    throw Object.assign(new Error(envelope?.message ?? `HTTP ${response.status}`), {
      code: envelope?.code ?? 'HTTP_ERROR',
      retryable: envelope?.retryable ?? false,
      traceId: envelope?.traceId,
    });
  }
  return body as T;
}

function parseJson<T>(raw: unknown, fallback: T): T {
  if (raw == null) return fallback;
  if (typeof raw !== 'string') return raw as T;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function money(value: number | null | undefined) {
  return `$${Number(value ?? 0).toFixed(2)}`;
}

function shortTime(value?: string | null) {
  if (!value) return '—';
  const ts = Date.parse(value);
  if (Number.isNaN(ts)) return value;
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short',
  }).format(ts);
}


function Kpi({ icon, label, value, tone }: {
  icon: ReactNode; label: string; value: string | number; tone?: string;
}) {
  return (
    <div style={{
      flex: '1 1 150px', minWidth: 150, padding: '14px 16px', borderRadius: 12,
      background: 'rgba(20,24,32,0.62)', border: '1px solid rgba(153,162,177,0.16)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: '#99a2b1', fontSize: 11,
        textTransform: 'uppercase', letterSpacing: 0.6 }}>
        {icon}<span>{label}</span>
      </div>
      <div style={{ marginTop: 8, fontSize: 24, fontWeight: 700, color: tone ?? '#e4e8ed' }}>{value}</div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const color = RUN_STATUS_COLOR[status] ?? '#99a2b1';
  return (
    <span style={{
      display: 'inline-block', padding: '2px 8px', borderRadius: 999, fontSize: 10,
      fontWeight: 700, letterSpacing: 0.5, color, border: `1px solid ${color}55`,
      background: `${color}18`,
    }}>{status}</span>
  );
}

function compactNumber(value: number) {
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value || 0);
}

function AnalyticsPanel({ data, label, empty }: {
  data: CFAnalyticsOverview | null;
  label: { statistics: string; views: string; likes: string; comments: string; engagement: string };
  empty: string;
}) {
  const points = data?.series ?? [];
  const values = points.map((point) => point.views);
  const max = Math.max(...values, 1);
  const polyline = points.length > 1
    ? points.map((point, index) => `${(index / (points.length - 1)) * 100},${42 - (point.views / max) * 36}`).join(' ')
    : '';
  return (
    <aside style={{ padding: 14, borderRadius: 12, background: 'linear-gradient(145deg, rgba(20,24,32,.9), rgba(13,15,21,.78))',
      border: '1px solid rgba(157,166,181,0.14)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, color: '#dbdfe6', fontSize: 11,
        fontWeight: 700, letterSpacing: .7, textTransform: 'uppercase' }}>
        <BarChart3 size={13} />{label.statistics}<span style={{ flex: 1 }} />7D
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(90px, 1fr))', gap: 12, marginTop: 14 }}>
        {[[label.views, compactNumber(data?.views ?? 0), <Eye size={12} />],
          [label.likes, compactNumber(data?.likes ?? 0), <Heart size={12} />],
          [label.comments, compactNumber(data?.comments ?? 0), <MessageCircle size={12} />],
          [label.engagement, `${Number(data?.engagementRate ?? 0).toFixed(1)}%`, <Share2 size={12} />]
          ].map(([name, value, icon]) => (
          <div key={String(name)}>
            <div style={{ display: 'flex', gap: 5, alignItems: 'center', color: '#6c7381', fontSize: 10 }}>{icon}{name}</div>
            <div style={{ color: '#e4e8ed', fontSize: 20, fontWeight: 700, marginTop: 3 }}>{value}</div>
          </div>
        ))}
      </div>
      {polyline ? (
        <svg viewBox="0 0 100 46" preserveAspectRatio="none" style={{ width: '100%', height: 90, marginTop: 10 }}>
          <defs><linearGradient id="cf-chart" x1="0" y1="0" x2="1" y2="0"><stop stopColor="#7c5cfc"/><stop offset="1" stopColor="#36cffb"/></linearGradient></defs>
          {[10, 22, 34, 46].map((y) => <line key={y} x1="0" x2="100" y1={y} y2={y} stroke="rgba(153,162,177,.1)" strokeWidth=".5" />)}
          <polyline points={polyline} fill="none" stroke="url(#cf-chart)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
        </svg>
      ) : <p style={{ color: '#6c7381', fontSize: 11, lineHeight: 1.4, margin: '18px 0 4px' }}>{empty}</p>}
    </aside>
  );
}

export function ContentFactoryTab({ language, lastEvent, onOpenAgents }: Props) {
  const t = COPY[language];
  // The mission-control screen is the section, not a summary page in front of
  // it: the first thing an owner needs is what is running right now.
  const [view, setView] = useState<View>('pipelines');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [overview, setOverview] = useState<CFOverview | null>(null);
  const [pipelines, setPipelines] = useState<CFPipeline[]>([]);
  const [templates, setTemplates] = useState<CFTemplate[]>([]);
  const [selectedPipeline, setSelectedPipeline] = useState<CFPipeline | null>(null);
  const [validation, setValidation] = useState<CFValidation | null>(null);
  const [runs, setRuns] = useState<CFRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<CFRun | null>(null);
  const [trace, setTrace] = useState<CFTraceEntry[]>([]);
  const [content, setContent] = useState<CFContentItem[]>([]);
  const [channels, setChannels] = useState<CFChannel[]>([]);
  const [analytics, setAnalytics] = useState<CFAnalyticsOverview | null>(null);
  const [channelEditor, setChannelEditor] = useState<string | 'new' | null>(null);
  const [channelForm, setChannelForm] = useState({ platform: 'YOUTUBE', accountName: '',
    refreshToken: '', clientId: '', clientSecret: '', proxyUrl: '' });
  const [agents, setAgents] = useState<CFAgentRow[]>([]);
  const [providers, setProviders] = useState<CFProviderRow[]>([]);
  const [queue, setQueue] = useState<CFQueue | null>(null);
  const [activity, setActivity] = useState<CFActivityItem[]>([]);
  const [stats, setStats] = useState<CFStats | null>(null);
  const [live, setLive] = useState<CFLiveRun | null>(null);
  const [agentDetail, setAgentDetail] = useState<CFAgentDetail | null>(null);
  const [integrations, setIntegrations] = useState<CFIntegrations | null>(null);
  const [storageUsage, setStorageUsage] = useState<CFStorageUsage | null>(null);
  const [storageConfig, setStorageConfig] = useState<CFStorageConfig | null>(null);
  const [mediaProviders, setMediaProviders] = useState<CFMediaProvider[]>([]);
  const [llmEndpoints, setLlmEndpoints] = useState<CFLLMEndpoints | null>(null);
  const [settings, setSettings] = useState<CFSettings | null>(null);

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await api<CFOverview>('/overview'));
      setError(null);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  /** The four panels the control centre shows around the pipeline itself. */
  const loadControlCentre = useCallback(async () => {
    try {
      const [queueBody, activityBody, statsBody, liveBody] = await Promise.all([
        api<CFQueue>('/queue'),
        api<{ items: CFActivityItem[] }>('/activity'),
        api<CFStats>('/stats?days=7'),
        api<CFLiveRun>('/live'),
      ]);
      setQueue(queueBody);
      setActivity(activityBody.items);
      setStats(statsBody);
      setLive(liveBody);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadAgentDetail = useCallback(async (agentId: string) => {
    try {
      setAgentDetail(await api<CFAgentDetail>(`/agents/${encodeURIComponent(agentId)}`));
    } catch {
      // A missing agent detail must not blank the workbench: the roster row is
      // still on screen and the inspector simply shows dashes.
      setAgentDetail(null);
    }
  }, []);

  const openAsset = useCallback(async (assetId: string) => {
    try {
      const body = await api<{ url: string }>(`/assets/${encodeURIComponent(assetId)}/url`);
      if (body.url) window.open(body.url, '_blank', 'noopener');
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadIntegrations = useCallback(async () => {
    try {
      const [integrationBody, usageBody, configBody, providerBody, llmBody] = await Promise.all([
        api<CFIntegrations>('/integrations'),
        api<CFStorageUsage>('/storage/usage'),
        api<CFStorageConfig>('/storage/config'),
        api<{ items: CFMediaProvider[] }>('/integrations/providers'),
        api<CFLLMEndpoints>('/integrations/llm'),
      ]);
      setIntegrations(integrationBody);
      setStorageUsage(usageBody);
      setStorageConfig(configBody);
      setMediaProviders(providerBody.items);
      setLlmEndpoints(llmBody);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api<CFSettings>('/settings'));
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadPipelines = useCallback(async () => {
    try {
      const [list, templateList] = await Promise.all([
        api<{ items: CFPipeline[] }>('/pipelines'),
        api<{ items: CFTemplate[] }>('/templates'),
      ]);
      setPipelines(list.items);
      setTemplates(templateList.items);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const openPipeline = useCallback(async (pipelineId: string) => {
    try {
      const [pipeline, runList, check] = await Promise.all([
        api<CFPipeline>(`/pipelines/${pipelineId}`),
        api<{ items: CFRun[] }>(`/runs?pipeline_id=${encodeURIComponent(pipelineId)}&limit=20`),
        api<CFValidation>(`/pipelines/${pipelineId}/validate`, { method: 'POST' }),
      ]);
      setSelectedPipeline(pipeline);
      setRuns(runList.items);
      setValidation(check);
      setSelectedRun(null);
      setTrace([]);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const openRun = useCallback(async (runId: string) => {
    try {
      const [run, traceBody] = await Promise.all([
        api<CFRun>(`/runs/${runId}`),
        api<{ entries: CFTraceEntry[] }>(`/runs/${runId}/trace`),
      ]);
      setSelectedRun(run);
      setTrace(traceBody.entries);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadQueue = useCallback(async () => {
    try {
      setContent((await api<{ items: CFContentItem[] }>('/content?limit=100')).items);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadChannels = useCallback(async () => {
    try {
      setChannels((await api<{ items: CFChannel[] }>('/channels')).items);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  const loadAnalytics = useCallback(async () => {
    try { setAnalytics(await api<CFAnalyticsOverview>('/analytics/overview?days=7')); }
    catch (exc) { setError((exc as Error).message || t.loadFailed); }
  }, [t.loadFailed]);

  const loadAgents = useCallback(async () => {
    try {
      const [agentBody, providerBody] = await Promise.all([
        api<{ items: CFAgentRow[] }>('/agents'),
        api<{ items: CFProviderRow[]; storage: Record<string, unknown> }>('/providers'),
      ]);
      setAgents(agentBody.items);
      setProviders(providerBody.items);
    } catch (exc) {
      setError((exc as Error).message || t.loadFailed);
    }
  }, [t.loadFailed]);

  useEffect(() => {
    if (view === 'pipelines' || view === 'wizard') void loadPipelines();
    if (view === 'queue') void loadQueue();
    if (view === 'channels') void loadChannels();
    if (view === 'analytics') void loadAnalytics();
    if (view === 'storage' || view === 'integrations') void loadIntegrations();
    if (view === 'settings') void loadSettings();
  }, [view, loadPipelines, loadQueue, loadChannels, loadAnalytics, loadIntegrations, loadSettings]);

  useEffect(() => {
    if (view !== 'pipelines') return;
    void loadAgents();
    void loadOverview();
    void loadControlCentre();
    void loadChannels();
    void loadSettings();
  }, [view, loadAgents, loadOverview, loadControlCentre, loadChannels, loadSettings]);

  // The wizard needs its own inputs: templates come with loadPipelines above,
  // the provider chain from /providers, and accounts + defaults decide what it
  // is allowed to offer (activation without a connected channel is refused).
  useEffect(() => {
    if (view !== 'wizard') return;
    void loadAgents();
    void loadChannels();
    void loadSettings();
  }, [view, loadAgents, loadChannels, loadSettings]);

  useEffect(() => {
    if (view === 'pipelines' && !selectedPipeline && pipelines[0]) void openPipeline(pipelines[0].id);
  }, [view, pipelines, selectedPipeline, openPipeline]);

  useEffect(() => { void loadAnalytics(); }, [loadAnalytics]);

  // Live updates (§72): the backend already pushes every state change, so this
  // refreshes what is on screen instead of polling on a timer.
  useEffect(() => {
    if (!lastEvent?.type?.startsWith('cf.')) return;
    if (view === 'pipelines') {
      void loadOverview();
      void loadControlCentre();
    }
    if (selectedRun && lastEvent.pipelineRunId === selectedRun.id) void openRun(selectedRun.id);
    else if (view === 'pipelines' && selectedPipeline && lastEvent.type.startsWith('cf.pipeline')) {
      void openPipeline(selectedPipeline.id);
    }
  }, [lastEvent, view, selectedRun, selectedPipeline, loadOverview, loadControlCentre, openRun, openPipeline]);

  const act = useCallback(async (action: () => Promise<unknown>, after?: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      if (after) await after();
    } catch (exc) {
      const err = exc as Error & { code?: string };
      setError(err.code ? `${err.code}: ${err.message}` : err.message);
    } finally {
      setBusy(false);
    }
  }, []);

  /**
   * Pipeline creation (the wizard tab). A blocked activation is an error the
   * owner must see: the backend created the draft but refused to make it live,
   * and saying nothing would look like a successful launch.
   */
  const createPipeline = useCallback(async (template: CFTemplate, request: CFPipelineCreateRequest) => {
    await act(async () => {
      const result = await api<{
        pipeline: CFPipeline;
        activation?: { status: 'ACTIVE' | 'BLOCKED'; error?: { code: string; message: string } };
      }>(`/templates/${template.id}/instantiate`, {
        method: 'POST', body: JSON.stringify({
          name: request.name, options: request.options, activate: request.activate,
        }),
      });
      await loadPipelines();
      await openPipeline(result.pipeline.id);
      // The new pipeline is what the owner asked for, so hand them the
      // workbench that now has it selected.
      setView('pipelines');
      if (result.activation?.status === 'BLOCKED') {
        const blocked = new Error(result.activation.error?.message || 'Pipeline activation was blocked') as Error & { code?: string };
        blocked.code = result.activation.error?.code;
        throw blocked;
      }
    });
  }, [act, loadPipelines, openPipeline]);

  const graph = useMemo(() => {
    const version = selectedPipeline?.version;
    if (!version) return { nodes: [] as CFNode[], edges: [] as CFEdge[] };
    return {
      nodes: parseJson<CFNode[]>(version.nodes, []),
      edges: parseJson<CFEdge[]>(version.edges, []),
    };
  }, [selectedPipeline]);

  const mockActive = providers.some((provider) => provider.id.startsWith('mock-'));

  return (
    <div style={{ padding: '20px 24px', height: '100%', overflowY: 'auto' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 18 }}>
        <Factory size={22} color="#36cffb" />
        <div style={{ flex: 1 }}>
          <h2 style={{ margin: 0, fontSize: 18, letterSpacing: 1.2, color: '#e4e8ed' }}>{t.title}</h2>
          <p style={{ margin: '2px 0 0', fontSize: 12, color: '#6c7381' }}>{t.subtitle}</p>
        </div>
        <button
          onClick={() => act(async () => {
            if (view === 'pipelines') {
              await Promise.all([loadPipelines(), loadControlCentre(), loadOverview()]);
            }
            if (view === 'wizard') {
              await Promise.all([loadPipelines(), loadChannels(), loadAgents(), loadSettings()]);
            }
            if (view === 'queue') await loadQueue();
            if (view === 'channels') await loadChannels();
            if (view === 'analytics') await loadAnalytics();
            if (view === 'storage' || view === 'integrations') await loadIntegrations();
            if (view === 'settings') await loadSettings();
          })}
          disabled={busy}
          className="btn-ghost"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
        >
          <RefreshCw size={14} />{t.refresh}
        </button>
      </header>

      <nav aria-label={t.title} style={{ display: 'flex', gap: 6, marginBottom: 18, flexWrap: 'wrap' }}>
        {([
          ['pipelines', t.pipelines], ['wizard', t.wizard], ['queue', t.queue], ['analytics', t.analytics],
          ['storage', t.storage], ['channels', t.channels],
          ['integrations', t.integrations], ['settings', t.settingsTab],
        ] as [View, string][]).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setView(key)}
            style={{
              padding: '7px 14px', borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer',
              border: `1px solid ${view === key ? 'rgba(54,207,251,0.55)' : 'rgba(153,162,177,0.2)'}`,
              background: view === key ? 'rgba(54,207,251,0.12)' : 'rgba(20,24,32,0.5)',
              color: view === key ? '#5ee1ff' : '#99a2b1',
            }}
          >{label}</button>
        ))}
      </nav>

      {error && (
        <div role="alert" style={{
          marginBottom: 14, padding: '10px 14px', borderRadius: 10, fontSize: 12,
          background: 'rgba(69,17,17,0.6)', border: '1px solid rgba(240,93,117,0.4)', color: '#f05d75',
        }}>
          <AlertTriangle size={13} style={{ verticalAlign: -2, marginRight: 6 }} />{error}
        </div>
      )}

      {mockActive && (view === 'pipelines' || view === 'wizard' || view === 'integrations') && (
        <div style={{
          marginBottom: 14, padding: '10px 14px', borderRadius: 10, fontSize: 12,
          background: 'rgba(44,51,64,0.55)', border: '1px solid rgba(246,184,74,0.4)', color: '#f6b84a',
        }}>
          <ShieldCheck size={13} style={{ verticalAlign: -2, marginRight: 6 }} />{t.mockWarning}
        </div>
      )}

      {view === 'analytics' && (
        <section style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 300px), 1fr))', gap: 16 }}>
          <div style={{ padding: 18, borderRadius: 12, background: 'rgba(13,15,21,.7)', border: '1px solid rgba(153,162,177,.14)' }}>
            <h3 style={{ color: '#e4e8ed', fontSize: 15, margin: '0 0 6px' }}>{t.statistics}</h3>
            <p style={{ color: '#6c7381', fontSize: 11, margin: '0 0 18px' }}>{analytics?.publications ?? 0} publications · 7 days</p>
            <button className="btn-primary" disabled={busy} onClick={() => act(
              () => api('/analytics/refresh', { method: 'POST', body: '{}' }),
              async () => { await loadAnalytics(); await loadChannels(); },
            )} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <RefreshCw size={13} />{t.syncStats}
            </button>
          </div>
          <AnalyticsPanel data={analytics} label={t} empty={t.noStats} />
        </section>
      )}

      {view === 'pipelines' && (
        <>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
          <Kpi icon={<Layers size={13} />} label={t.activePipelines} value={overview?.activePipelines ?? 0} />
          <Kpi icon={<Play size={13} />} label={t.processing} value={overview?.processing ?? 0} tone="#5ee1ff" />
          <Kpi icon={<BadgeCheck size={13} />} label={t.review} value={overview?.reviewRequired ?? 0} tone="#b197ff" />
          <Kpi icon={<Clock size={13} />} label={t.scheduled} value={overview?.scheduled ?? 0} />
          <Kpi icon={<FileVideo size={13} />} label={t.publishedToday} value={overview?.publishedToday ?? 0}
            tone="#35d49a" />
          <Kpi icon={<AlertTriangle size={13} />} label={t.failed} value={overview?.failed ?? 0}
            tone={(overview?.failed ?? 0) > 0 ? '#f05d75' : undefined} />
          <Kpi icon={<Coins size={13} />} label={t.costToday} value={money(overview?.costToday)} />
          <Kpi icon={<Wallet size={13} />} label={t.costMonth} value={money(overview?.costMonth)} />
        </div>
        <PipelineWorkbench
          language={language}
          pipelines={pipelines}
          pipeline={selectedPipeline}
          nodes={graph.nodes}
          edges={graph.edges}
          runs={runs}
          run={selectedRun}
          trace={trace}
          validation={validation}
          agents={agents}
          agentDetail={agentDetail}
          queue={queue}
          activity={activity}
          stats={stats}
          channels={channels}
          assets={(live?.assets ?? []) as CFAsset[]}
          logs={live?.logs ?? []}
          busy={busy}
          onAgent={(agentId) => void loadAgentDetail(agentId)}
          onOpenAgents={onOpenAgents}
          onAsset={(assetId) => void openAsset(assetId)}
          onPipeline={(id) => void openPipeline(id)}
          onRun={(id) => void openRun(id)}
          onStart={(dryRun, topic) => {
            if (!selectedPipeline) return;
            void act(
              () => api(`/pipelines/${selectedPipeline.id}/runs`, {
                method: 'POST', body: JSON.stringify({ dryRun, triggerData: { topic } }),
              }),
              () => openPipeline(selectedPipeline.id),
            );
          }}
          onConfigureChannel={(channelId) => {
            if (!selectedPipeline?.version) return;
            const variables = parseJson<Record<string, unknown>>(selectedPipeline.version.variables, {});
            const configuredNodes = graph.nodes.map((node) => node.type === 'publish'
              ? { ...node, config: { ...(node.config ?? {}), channelId } }
              : node);
            void act(
              () => api(`/pipelines/${selectedPipeline.id}/draft`, {
                method: 'PUT', body: JSON.stringify({ nodes: configuredNodes, edges: graph.edges, variables }),
              }),
              () => openPipeline(selectedPipeline.id),
            );
          }}
          onPause={() => {
            if (!selectedPipeline) return;
            void act(
              () => api(`/pipelines/${selectedPipeline.id}/pause`, { method: 'POST' }),
              async () => { await loadPipelines(); await openPipeline(selectedPipeline.id); },
            );
          }}
          onActivate={() => {
            if (!selectedPipeline) return;
            void act(
              () => api(`/pipelines/${selectedPipeline.id}/activate`, { method: 'POST' }),
              async () => { await loadPipelines(); await openPipeline(selectedPipeline.id); },
            );
          }}
          onOpenWizard={() => setView('wizard')}
        />
        </>
      )}


      {view === 'wizard' && (
        <section className="cf-wizard-page">
          <div className="cf-wizard-intro">
            <div>
              <h3><WandSparkles size={15} />{t.wizard}</h3>
              <p>{t.wizardSubtitle}</p>
            </div>
            <button className="btn-ghost" onClick={() => setView('pipelines')}>{t.wizardBackToPipelines}</button>
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
            <Kpi icon={<Layers size={13} />} label={t.wizardTemplates} value={templates.length} />
            <Kpi icon={<Plug size={13} />} label={t.wizardChannels}
              value={channels.filter((channel) => channel.status === 'CONNECTED').length}
              tone={channels.some((channel) => channel.status === 'CONNECTED') ? '#35d49a' : undefined} />
            <Kpi icon={<Network size={13} />} label={t.wizardPipelines} value={pipelines.length} />
          </div>
          {templates.length === 0 ? (
            <p style={{ color: '#f6b84a', fontSize: 12 }}>{t.wizardNoTemplates}</p>
          ) : (
            <div className="cfw-panel cf-wizard-card">
              <PipelineWizard
                language={language}
                templates={templates}
                channels={channels}
                providers={providers}
                settings={settings}
                busy={busy}
                onCreate={createPipeline}
              />
            </div>
          )}
        </section>
      )}

      {view === 'queue' && (
        <section>
          {content.length === 0 && <p style={{ color: '#6c7381', fontSize: 12 }}>{t.contentEmpty}</p>}
          {content.length > 0 && (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ color: '#6c7381', textAlign: 'left' }}>
                  <th style={{ padding: '8px' }}>{t.title_}</th>
                  <th style={{ padding: '8px' }}>{t.status}</th>
                  <th style={{ padding: '8px' }}>{t.quality}</th>
                  <th style={{ padding: '8px' }}>{t.cost}</th>
                  <th style={{ padding: '8px' }}>{t.createdAt}</th>
                </tr>
              </thead>
              <tbody>
                {content.map((item) => (
                  <tr key={item.id} style={{ borderTop: '1px solid rgba(153,162,177,0.1)' }}>
                    <td style={{ padding: '9px 8px', color: '#e4e8ed' }}>{item.title || item.id}</td>
                    <td style={{ padding: '9px 8px' }}><StatusPill status={item.status} /></td>
                    <td style={{ padding: '9px 8px', color: '#99a2b1' }}>
                      {item.quality_score != null ? item.quality_score.toFixed(2) : '—'}
                    </td>
                    <td style={{ padding: '9px 8px', color: '#99a2b1' }}>{money(item.cost)}</td>
                    <td style={{ padding: '9px 8px', color: '#6c7381' }}>{shortTime(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {view === 'channels' && (
        <section>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
            <button className="btn-primary" onClick={() => { setChannelEditor('new'); setChannelForm({ platform: 'YOUTUBE', accountName: '', refreshToken: '', clientId: '', clientSecret: '', proxyUrl: '' }); }}
              style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Plus size={13} />{t.connectAccount}</button>
          </div>
          {channelEditor && (
            <div style={{ padding: 16, marginBottom: 14, borderRadius: 12, background: 'rgba(20,24,32,.85)', border: '1px solid rgba(157,166,181,0.196)' }}>
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}><strong style={{ color: '#e4e8ed', fontSize: 13 }}>{channelEditor === 'new' ? t.connectAccount : t.editConnection}</strong><span style={{ flex: 1 }} /><button className="btn-ghost" onClick={() => setChannelEditor(null)}><X size={13} /></button></div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 10 }}>
                <select value={channelForm.platform} disabled={channelEditor !== 'new'} onChange={(event) => setChannelForm({ ...channelForm, platform: event.target.value })} style={{ padding: 9, borderRadius: 7 }}><option>YOUTUBE</option><option>TIKTOK</option></select>
                <input aria-label={t.account} placeholder={t.account} value={channelForm.accountName} disabled={channelEditor !== 'new'} onChange={(event) => setChannelForm({ ...channelForm, accountName: event.target.value })} />
                <input type="password" aria-label={t.refreshToken} placeholder={t.refreshToken} value={channelForm.refreshToken} onChange={(event) => setChannelForm({ ...channelForm, refreshToken: event.target.value })} />
                <input aria-label={t.clientId} placeholder={t.clientId} value={channelForm.clientId} onChange={(event) => setChannelForm({ ...channelForm, clientId: event.target.value })} />
                <input type="password" aria-label={t.clientSecret} placeholder={t.clientSecret} value={channelForm.clientSecret} onChange={(event) => setChannelForm({ ...channelForm, clientSecret: event.target.value })} />
                <input aria-label={t.proxy} placeholder={t.proxyHint} value={channelForm.proxyUrl} onChange={(event) => setChannelForm({ ...channelForm, proxyUrl: event.target.value })} />
              </div>
              <button className="btn-primary" disabled={busy || (channelEditor === 'new' && !channelForm.accountName.trim())} onClick={() => act(async () => {
                const credentials = channelForm.platform === 'TIKTOK'
                  ? { refresh_token: channelForm.refreshToken, client_key: channelForm.clientId, client_secret: channelForm.clientSecret }
                  : { refresh_token: channelForm.refreshToken, client_id: channelForm.clientId, client_secret: channelForm.clientSecret };
                const path = channelEditor === 'new' ? '/channels' : `/channels/${channelEditor}/connection`;
                await api(path, { method: channelEditor === 'new' ? 'POST' : 'PATCH', body: JSON.stringify({ platform: channelForm.platform, accountName: channelForm.accountName, credentials, proxyUrl: channelForm.proxyUrl }) });
                setChannelEditor(null); await loadChannels();
              })} style={{ marginTop: 12 }}>{t.save}</button>
            </div>
          )}
          {channels.length === 0 && <p style={{ color: '#6c7381', fontSize: 12 }}>{t.channelsEmpty}</p>}
          {channels.map((channel) => (
            <div key={channel.id} style={{
              display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8, padding: '12px 14px',
              borderRadius: 10, background: 'rgba(20,24,32,0.6)', border: '1px solid rgba(153,162,177,0.16)',
            }}>
              <Plug size={15} color="#36cffb" />
              <div style={{ flex: 1 }}>
                <div style={{ color: '#e4e8ed', fontSize: 13 }}>{channel.account_name}</div>
                <div style={{ color: '#6c7381', fontSize: 11 }}>{channel.platform} · {channel.proxy_enabled ? `${t.proxy}: ${channel.proxy_host}` : `${t.proxy}: —`} · {t.statistics}: {shortTime(channel.analytics_updated_at)}</div>
              </div>
              <StatusPill status={channel.status} />
              <button className="btn-ghost" onClick={() => { setChannelEditor(channel.id); setChannelForm({ platform: channel.platform, accountName: channel.account_name, refreshToken: '', clientId: '', clientSecret: '', proxyUrl: '' }); }}><Network size={13} /> {t.editConnection}</button>
              {channel.last_error && (
                <span style={{ color: '#f05d75', fontSize: 11 }}>{channel.last_error}</span>
              )}
            </div>
          ))}
        </section>
      )}

      {view === 'storage' && (
        <StoragePanel
          config={storageConfig}
          usage={storageUsage}
          language={language}
          busy={busy}
          onSave={async (patch) => {
            await act(
              () => api('/storage/config', { method: 'PUT', body: JSON.stringify(patch) }),
              loadIntegrations,
            );
          }}
          onTest={(patch) => api<Record<string, unknown>>('/storage/test', {
            method: 'POST', body: JSON.stringify(patch),
          })}
          onClearSecret={async (field) => {
            // An explicit null is how the backend tells "delete it" apart from
            // "the box was left empty, keep what is stored".
            await act(
              () => api('/storage/config', { method: 'PUT', body: JSON.stringify({ [field]: null }) }),
              loadIntegrations,
            );
          }}
          onOneDriveLogin={() => api<Record<string, unknown>>('/storage/onedrive/login', { method: 'POST' })}
          onOneDrivePoll={async () => {
            const result = await api<Record<string, unknown>>('/storage/onedrive/poll', { method: 'POST' });
            // Only a finished sign-in changes anything worth reloading; the
            // "still pending" answer arrives every five seconds.
            if (result.status === 'connected') await loadIntegrations();
            return result;
          }}
          onOneDriveSignOut={async () => {
            await act(() => api('/storage/onedrive/sign-out', { method: 'POST' }), loadIntegrations);
          }}
        />
      )}

      {view === 'integrations' && (
        <IntegrationsPanel
          data={integrations}
          usage={storageUsage}
          providers={mediaProviders}
          llm={llmEndpoints}
          language={language}
          busy={busy}
          onRefresh={() => void loadIntegrations()}
          onSaveProvider={async (providerId, patch) => {
            await act(
              () => api(`/integrations/providers/${encodeURIComponent(providerId)}`, {
                method: 'PUT', body: JSON.stringify(patch),
              }),
              loadIntegrations,
            );
          }}
          onDeleteProvider={async (providerId) => {
            await act(
              () => api(`/integrations/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' }),
              loadIntegrations,
            );
          }}
          onTestProvider={(providerId) => api<Record<string, unknown>>(
            `/integrations/providers/${encodeURIComponent(providerId)}/test`, { method: 'POST' },
          )}
          onSetDefault={async (category, providerId) => {
            await act(
              () => api(`/integrations/defaults/${encodeURIComponent(category)}`, {
                method: 'PUT', body: JSON.stringify({ providerId }),
              }),
              loadIntegrations,
            );
          }}
          onCreateLlm={async (payload) => {
            const result = await api<Record<string, unknown>>('/integrations/llm', {
              method: 'POST', body: JSON.stringify(payload),
            });
            await loadIntegrations();
            return result;
          }}
          onDeleteLlm={async (bindingId) => {
            await act(
              () => api(`/integrations/llm/${encodeURIComponent(bindingId)}`, { method: 'DELETE' }),
              loadIntegrations,
            );
          }}
          onUpdateLlm={async (bindingId, defaultModel) => {
            await act(
              () => api(`/integrations/llm/${encodeURIComponent(bindingId)}`, {
                method: 'PUT', body: JSON.stringify({ defaultModel }),
              }),
              loadIntegrations,
            );
          }}
          onLoadLlmModels={async (bindingId) => {
            // Deliberately not routed through `act`: a picker that cannot list
            // its options must degrade to a text field, not raise a banner over
            // the settings screen.
            try {
              const result = await api<{ models?: string[] }>(
                `/integrations/llm/${encodeURIComponent(bindingId)}/models`,
              );
              return result.models ?? [];
            } catch {
              return [];
            }
          }}
        />
      )}

      {view === 'settings' && (
        <SettingsPanel
          settings={settings}
          language={language}
          busy={busy}
          onSave={async (patch) => {
            await act(
              () => api('/settings', { method: 'PUT', body: JSON.stringify(patch) }),
              loadSettings,
            );
          }}
        />
      )}
    </div>
  );
}

export default ContentFactoryTab;
