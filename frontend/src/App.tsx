import { ErrorBoundary } from './ErrorBoundary';
import { uiCopy } from './uiCopy';
import { RunCursorTracker, reconnectDelayMs } from './wsResume';
import { notify, confirmAction } from './components/Notifications';
import React, { useState, useEffect, useRef, useCallback, useMemo, lazy, Suspense } from 'react';
import {
  Settings,
  Terminal, 
  Activity, 
  Globe,
  Cpu, 
  Database,
  Layers,
  BookOpen,
  Network,
  Server,
  Clock,
  Menu,
  X,
  UserCog,
  ChevronsLeft,
  ChevronsRight,
  ShieldCheck,
  AudioWaveform,
  KeyRound,
  Trello,
  Briefcase,
  Coins,
  Factory,
  Megaphone,
  Shuffle,
  ExternalLink,
  BarChart3,
} from 'lucide-react';

import type { AppSettings, ChatMessage, ChatSession, DecisionLog, ActivityLog, SystemConfig, AgentModel, Project } from './types';
import { styles } from './styles';
import { translate, normalizeLanguage, type Language } from './i18n';
import { 
  playBeep, 
  playAlarmSound, 
  stopAlarmSound, 
  initFetchInterceptor 
} from './utils';

// Import sub-components
import { ChatTab } from './components/ChatTab';
import { SimpleChatView } from './components/SimpleChatView';
import { loadSelection, saveSelection } from './components/simple/chatModels';
import type { ModelSelection } from './components/simple/chatModels';
const ConfigTab = lazy(() => import('./components/ConfigTab').then(module => ({ default: module.ConfigTab })));
const LogsTab = lazy(() => import('./components/LogsTab').then(module => ({ default: module.LogsTab })));
const ActivityTab = lazy(() => import('./components/ActivityTab').then(module => ({ default: module.ActivityTab })));
const MemoryTab = lazy(() => import('./components/MemoryTab').then(module => ({ default: module.MemoryTab })));
const ScheduleTab = lazy(() => import('./components/ScheduleTab').then(module => ({ default: module.ScheduleTab })));
const SubagentsTab = lazy(() => import('./components/SubagentsTab').then(module => ({ default: module.SubagentsTab })));
const ObsidianTab = lazy(() => import('./components/ObsidianTab').then(module => ({ default: module.ObsidianTab })));
const NetworkTab = lazy(() => import('./components/NetworkTab').then(module => ({ default: module.NetworkTab })));
const MCPTab = lazy(() => import('./components/MCPTab').then(module => ({ default: module.MCPTab })));
const AgentsAdminTab = lazy(() => import('./components/AgentsAdminTab').then(module => ({ default: module.AgentsAdminTab })));
const ApiKeysTab = lazy(() => import('./components/ApiKeysTab').then(module => ({ default: module.ApiKeysTab })));
import {
  SPEECH_CHUNK_MAX,
  SPEECH_FIRST_CHUNK_MAX,
  StreamingSpeechSplitter,
  cleanForSpeech,
  splitForSpeech,
  takeSpeechBatch,
} from './speechChunks';
const BrowserSessionsTab = lazy(() => import('./components/BrowserSessionsTab').then(module => ({ default: module.BrowserSessionsTab })));
const ProcessesTab = lazy(() => import('./components/ProcessesTab').then(module => ({ default: module.ProcessesTab })));
import { HermesMark } from './components/HermesMark';
import { VexaLogo } from './components/VexaLogo';
const MetricsTab = lazy(() => import('./components/MetricsTab').then(module => ({ default: module.MetricsTab })));
const ServerMetricsTab = lazy(() => import('./components/metrics/ServerMetricsTab').then(module => ({ default: module.ServerMetricsTab })));
import { FloatingWindow } from './components/FloatingWindow';
import { BrowserLiveView } from './components/BrowserLiveView';
import { VexaCommandCenter } from './components/VexaCommandCenter';
import type { DashboardRoute } from './components/vexa/vexaDashboardTypes';
const DevRunsTab = lazy(() => import('./components/DevRunsTab').then(module => ({ default: module.DevRunsTab })));
const KanbanTab = lazy(() => import('./components/KanbanTab').then(module => ({ default: module.KanbanTab })));
const ClientsTab = lazy(() => import('./components/ClientsTab').then(module => ({ default: module.ClientsTab })));
import { INBOX_UPDATE_EVENT, OPEN_INBOX_EVENT, useInboxUnread } from './components/smm/dialogs/inboxApi';
const CurrencySettingsTab = lazy(() => import('./components/CurrencySettingsTab').then(module => ({ default: module.CurrencySettingsTab })));
import { AppHeader } from './components/AppHeader';
import type { DevRunEvent } from './types';
const ContentFactoryTab = lazy(() => import('./components/ContentFactoryTab').then(module => ({ default: module.ContentFactoryTab })));
const SMMFactoryTab = lazy(() => import('./components/SMMFactoryTab').then(module => ({ default: module.SMMFactoryTab })));
import type { CFEvent } from './components/content-factory/types';
import { authEndpointFetch, authErrorMessage } from './accessSession';
import { composeWithAttachment } from './components/simple/chatApi';

// Initialize global fetch interceptor
initFetchInterceptor();

// Static BCP-47 locale map — defined at module level so hooks don't need it as a dep
const langToLocale: Record<string, string> = {
  ru: 'ru-RU', en: 'en-US', he: 'he-IL', de: 'de-DE', es: 'es-ES', fr: 'fr-FR'
};

// Hard ceiling on how long a reply may stay flagged as "speech on its way". XTTS on the
// GPU needs ~3-8s for a normal answer, but nginx would let a stuck /api/voice/synthesize
// hang for up to 120s — and Vexa's dialog mode refuses to reopen the microphone while the
// flag is set. Without this ceiling one hung request would freeze the conversation loop.
const TTS_PENDING_TIMEOUT_MS = 25000;

/**
 * One spoken reply in progress. Its text may keep arriving after playback has started —
 * a streamed answer is spoken sentence by sentence while the model is still writing.
 */
interface SpeechSession {
  requestId: number;
  /** Speakable pieces not yet taken for synthesis, in order. */
  queue: string[];
  /** No more pieces will come; the reply ends once the queue has played out. */
  closed: boolean;
  /** Playback caught up with a reply that is still being written. */
  paused: boolean;
  /** The playback loop, parked until more text arrives or the reply closes. */
  waiters: Array<() => void>;
}

function wakeSpeech(session: SpeechSession) {
  session.waiters.splice(0).forEach(resolve => resolve());
}

/** A streamed reply being fed to speech as it arrives, keyed by run id. */
interface SpeechStream {
  splitter: StreamingSpeechSplitter;
  session: SpeechSession | null;
  /** Speech for this reply was switched off or stopped — it must not resume. */
  muted: boolean;
  /**
   * A reconnect replays this run from further back than what was already fed in, so its
   * deltas would repeat spoken text. Stop feeding them; the final answer is lined up
   * against what was spoken instead.
   */
  frozen: boolean;
}

const tabs = ['vexa', 'processes', 'devruns', 'kanban', 'clients', 'agents', 'schedule', 'contentfactory', 'smm', 'settings', 'network', 'metrics'] as const;
type AppTab = typeof tabs[number];
// «Входящие» used to be a top-level tab; it is SMM Factory → Диалоги now, so a
// bookmarked #tab=inbox (or a saved tab from an older build) lands there.
export const normalizeTab = (value: string | null): AppTab => {
  if (value === 'inbox') return 'smm';
  return tabs.includes(value as AppTab) ? value as AppTab : 'vexa';
};

export default function App() {
  const legacySettingsTabs = ['config', 'logs', 'activity', 'memory', 'subagents', 'obsidian', 'mcp'];
  const [activeTab, setActiveTab] = useState<'vexa' | 'processes' | 'devruns' | 'kanban' | 'clients' | 'agents' | 'schedule' | 'contentfactory' | 'smm' | 'settings' | 'network' | 'metrics'>(() => {
    const saved = localStorage.getItem('vexa_active_tab');
    const requested = new URLSearchParams(window.location.hash.slice(1)).get('tab') ?? saved;
    if (requested === 'inbox') localStorage.setItem('smm_factory_view', 'dialogs');
    if (!window.location.hash && saved === 'chat') return 'vexa';
    // Settings → «Сервер и мониторинг» was folded into «Метрики».
    const requestedSection = new URLSearchParams(window.location.hash.slice(1)).get('section') ?? localStorage.getItem('vexa_settings_section');
    if (requested === 'tools' || (requested === 'settings' && requestedSection === 'tools')) return 'metrics';
    if (!window.location.hash && (saved === 'settings' || (saved && legacySettingsTabs.includes(saved)))) return 'settings';
    return normalizeTab(new URLSearchParams(window.location.hash.slice(1)).get('tab') ?? saved);
  });
  // 'channels' is deliberately absent: the messenger/social channels are now the
  // SMM Factory section, and a saved 'channels' from before that move falls back
  // to the first settings page instead of rendering an empty one.
  const [settingsSection, setSettingsSection] = useState<'config' | 'currency' | 'subagents' | 'mcp' | 'obsidian' | 'memory' | 'logs' | 'activity' | 'keys' | 'browser'>(() => {
    const savedSection = new URLSearchParams(window.location.hash.slice(1)).get('section') ?? localStorage.getItem('vexa_settings_section');
    if (savedSection && [...legacySettingsTabs, 'keys', 'currency', 'browser'].includes(savedSection)) return savedSection as any;
    const savedTab = localStorage.getItem('vexa_active_tab');
    return (savedTab && legacySettingsTabs.includes(savedTab) ? savedTab : 'config') as any;
  });
  /** Overdue-task count for the Клиенты nav badge — polled independently
   *  of ClientsTab (same reason AppHeader polls its own approvals count):
   *  the badge has to stay live while the user is on an entirely different
   *  tab, where ClientsTab itself is unmounted. */
  const [overdueTaskCount, setOverdueTaskCount] = useState(0);
  const inboxUnread = useInboxUnread();
  // The client card (and anything else) can ask to open one conversation; SMM
  // Factory → Диалоги picks the id up from storage when it mounts, or from the
  // event if it already is.
  useEffect(() => {
    const openInbox = () => setActiveTab('smm');
    window.addEventListener(OPEN_INBOX_EVENT, openInbox);
    return () => window.removeEventListener(OPEN_INBOX_EVENT, openInbox);
  }, []);
  const [vexaTranscriptOpen, setVexaTranscriptOpen] = useState(false);
  /** Which bottom-nav destination is open as a floating window over the Vexa dashboard. */
  const [vexaFloatingPanel, setVexaFloatingPanel] = useState<Exclude<DashboardRoute, 'terminal'> | null>(null);
  const [browserViewOpen, setBrowserViewOpen] = useState(false);
  const [vexaUiMode, setVexaUiMode] = useState<'immersive' | 'simple'>(
    () => (localStorage.getItem('hermes_vexa_ui_mode') === 'simple' ? 'simple' : 'immersive')
  );
  useEffect(() => {
    localStorage.setItem('hermes_vexa_ui_mode', vexaUiMode);
  }, [vexaUiMode]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  /**
   * Which model the open conversation is pinned to — null means "whatever the
   * backend is configured with". Held here rather than inside the chat view
   * because sendChatText has to put it on every outgoing frame; the ref is what
   * that (memoised) callback reads, so picking a model never has to rebuild it.
   */
  const [modelSelection, setModelSelection] = useState<ModelSelection | null>(null);
  const modelSelectionRef = useRef<ModelSelection | null>(null);
  useEffect(() => { modelSelectionRef.current = modelSelection; }, [modelSelection]);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState<boolean>(() => localStorage.getItem('hermes_sidebar_collapsed') === '1');
  /**
   * Public dashboard URL of the OmniRoute sidecar, so the sidebar can offer a
   * one-click jump into it. Comes from the backend rather than being hardcoded
   * because the URL differs per deployment (localhost:20128 in dev, the public
   * tunnel host in prod) — see ROUTER_PUBLIC_BASE_URL. Null until loaded or
   * when the sidecar isn't configured, in which case the entry stays hidden.
   */
  const [routerDashboardUrl, setRouterDashboardUrl] = useState<string | null>(null);
  // Normalised, never cast: a stale or JSON-encoded value here used to reach
  // translate() and take the whole dashboard down (see normalizeLanguage).
  const [language, setLanguageState] = useState<Language>(
    () => normalizeLanguage(localStorage.getItem('hermes_language')),
  );
  const [appSettings, setAppSettings] = useState<AppSettings>({ language });
  const appSettingsRef = useRef<AppSettings>({ language });
  const copy = useCallback((text: string) => uiCopy(language, text), [language]);
  const t = useCallback((key: string) => translate(language, key), [language]);
  const toggleSidebar = useCallback(() => {
    setIsSidebarCollapsed(prev => {
      const next = !prev;
      localStorage.setItem('hermes_sidebar_collapsed', next ? '1' : '0');
      return next;
    });
  }, []);
  const setLanguage = useCallback((nextLanguage: Language) => {
    localStorage.setItem('hermes_language', nextLanguage);
    setLanguageState(nextLanguage);
    setAppSettings({ language: nextLanguage });
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: nextLanguage }),
    }).catch(() => undefined);
  }, []);

  const [chatSessions, setChatSessions] = useState<ChatSession[]>([
    { id: 'dashboard', title: 'Main Terminal', agent_id: 'vexa' },
  ]);
  const [isConnected, setIsConnected] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(!!localStorage.getItem('vexa_auth_token'));
  const [otpCode, setOtpCode] = useState('');
  const [otpSent, setOtpSent] = useState(false);
  const lastAttemptedOtp = useRef('');
  const [authStatus, setAuthStatus] = useState<'idle' | 'sending' | 'sent' | 'verifying' | 'error' | 'success'>('idle');
  const [authError, setAuthError] = useState('');
  const [loginMode, setLoginMode] = useState<'telegram' | 'password'>('telegram');
  const [pwUsername, setPwUsername] = useState('');
  const [pwPassword, setPwPassword] = useState('');
  const [pwStatus, setPwStatus] = useState<'idle' | 'submitting' | 'error'>('idle');
  const [pwError, setPwError] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: 'assistant', content: 'Greetings, Albert. Connection to the Hermes network is complete. Awaiting your instructions.' }
  ]);
  const [logs, setLogs] = useState<DecisionLog[]>([]);
  const [lastDevRunEvent, setLastDevRunEvent] = useState<DevRunEvent | null>(null);
  // Content Factory pushes every pipeline/node transition over the same
  // socket with a `cf.` prefix (§72); the panel refreshes from these instead
  // of polling.
  const [lastCfEvent, setLastCfEvent] = useState<CFEvent | null>(null);
  const [devRunNotificationsEnabled, setDevRunNotificationsEnabled] = useState<boolean>(
    () => localStorage.getItem('hermes_devrun_notifications') === '1'
  );
  const devRunNotificationsRef = useRef(devRunNotificationsEnabled);
  useEffect(() => {
    devRunNotificationsRef.current = devRunNotificationsEnabled;
  }, [devRunNotificationsEnabled]);
  const [activityLogs, setActivityLogs] = useState<ActivityLog[]>([]);
  const [metrics, setMetrics] = useState<any>(null);
  const [isMetricsLoading, setIsMetricsLoading] = useState(false);
  const [config, setConfig] = useState<SystemConfig>({
    system_prompt: '',
    model: 'qwen3.8-27b',
    provider: 'llamacpp',
    api_base: 'http://127.0.0.1:8080',
    local_base_url: 'http://127.0.0.1:8080',
    openai_api_base: 'https://openrouter.ai/api/v1',
    local_num_ctx: 32768,
    local_think: false,
    fast_mode: false,
    max_history_len: 6,
    condenser_enabled: false,
    condense_trigger_extra: 10,
    max_tokens: 2048,
    tool_max_tokens: 2048,
    temperature: 0.7,
    auto_rag: false,
    memory_enabled: true,
    memory_auto_save: true,
    memory_max_items: 4
  });
  
  // Document / Memory States
  const [documents, setDocuments] = useState<{ id: string; title: string }[]>([]);
  const [noteTitle, setNoteTitle] = useState('');
  const [noteContent, setNoteContent] = useState('');
  const [isIndexing, setIsIndexing] = useState(false);
  const [memorySearchQuery, setMemorySearchQuery] = useState('');
  const [memorySearchResults, setMemorySearchResults] = useState<{ title: string; content: string; score: number }[] | null>(null);
  const [isSearchingMemory, setIsSearchingMemory] = useState(false);

  // Tools and system stats states
  const [timers, setTimers] = useState<{ id: string; label: string; duration?: number; time_left: number; status: string; created_at: string; type?: string; target_time?: string; interval_hours?: number; fire_count?: number; agent_id?: string; prompt?: string }[]>([]);

  const [isUploading, setIsUploading] = useState(false);
  // File attached to the current chat message (text context, not dataset upload)
  const [attachedFile, setAttachedFile] = useState<{ name: string; content: string; type?: string; pages?: number; truncated?: boolean } | null>(null);
  // chat id → run id of the turn still generating there. Per chat, not one global
  // flag: a long run in one chat used to lock the composer and model picker of
  // every other chat, including a brand-new empty one.
  const [generatingRuns, setGeneratingRuns] = useState<Record<string, string>>({});
  const [isTTSEnabled, setIsTTSEnabled] = useState(true);
  const [isSpeaking, setIsSpeaking] = useState(false);
  // True from the moment a spoken reply starts being fetched until it actually begins
  // playing (or fails). `isSpeaking` only flips on the audio element's `play` event, so
  // between the answer arriving and the synthesised wav coming back there are several
  // seconds where speech is very much on its way while `isSpeaking` is still false.
  const [isTtsPending, setIsTtsPending] = useState(false);
  const [playingMsgIndex, setPlayingMsgIndex] = useState<number | null>(null);
  const [micEnabled, setMicEnabled] = useState(false);
  const [micState, setMicState] = useState<'off' | 'listening' | 'capturing' | 'transcribing' | 'error'>('off');
  const [micErrorMessage, setMicErrorMessage] = useState('');
  // Bumped every time a listening turn ends without any recognised speech, so
  // Vexa's dialog mode can restart the microphone — before this, a turn the STT
  // heard nothing in simply ended the conversation loop with no visible reason.
  const [voiceIdleTick, setVoiceIdleTick] = useState(0);

  const [inputValue, setInputValue] = useState('');
  const [selectedLog, setSelectedLog] = useState<DecisionLog | null>(null);
  const [isSavingConfig, setIsSavingConfig] = useState(false);
  // Prompt edit states
  const [editedPrompt, setEditedPrompt] = useState('');
  const [editedModel, setEditedModel] = useState('');
  const [editedRuntimeConfig, setEditedRuntimeConfig] = useState<Partial<SystemConfig>>({});

  const wsRef = useRef<WebSocket | null>(null);
  // Per-run stream cursors: what we have already applied, and which runs a
  // reconnect still needs replayed. Survives socket churn, so it lives in a ref.
  const runCursorsRef = useRef(new RunCursorTracker());
  const mainChatEndRef = useRef<HTMLDivElement | null>(null);
  const subagentChatEndRef = useRef<HTMLDivElement | null>(null);
  const lastSentTimeRef = useRef<number>(0);
  const ttsEnabledRef = useRef(true);       // ref so WS handler always sees current value
  const generatingRunsRef = useRef<Record<string, string>>({}); // ref so send guard sees current value
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const voiceStreamRef = useRef<MediaStream | null>(null);
  const voiceChunksRef = useRef<Blob[]>([]);
  const vadCleanupRef = useRef<(() => void) | null>(null);
  // Set only when the VAD gives up on a recording it never heard speech in: that
  // one is dropped without an STT round-trip and reported as an idle turn so
  // dialog mode can re-arm. A manually stopped recording is always submitted,
  // however quiet it was — the server gets the final say on it.
  const voiceNoSpeechRef = useRef(false);
  const micStateRef = useRef<'off' | 'listening' | 'capturing' | 'transcribing' | 'error'>('off');
  const ttsAudioRef = useRef<HTMLAudioElement | null>(null);
  const ttsAudioUrlRef = useRef('');
  const ttsRequestRef = useRef(0);
  const speechSessionRef = useRef<SpeechSession | null>(null);
  const speechStreamsRef = useRef(new Map<string, SpeechStream>());
  const ttsPendingTimerRef = useRef<number | null>(null);
  const serverTtsAvailableRef = useRef<boolean | null>(null);
  // Last user message per session, used by the "Retry" action (P0 UX).
  const lastUserMessageRef = useRef<Record<string, string>>({});

  const [subagents, setSubagents] = useState<AgentModel[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  // Depth of each project in the folder tree, used only to indent options in
  // flat <select> project pickers (a <select> can't render a real tree).
  const projectDepth = useMemo(() => {
    const byId = new Map(projects.map(p => [p.id, p]));
    const depth = new Map<string, number>();
    const depthOf = (id: string): number => {
      if (depth.has(id)) return depth.get(id)!;
      const p = byId.get(id);
      const d = p?.parent_id && byId.has(p.parent_id) ? depthOf(p.parent_id) + 1 : 0;
      depth.set(id, d);
      return d;
    };
    projects.forEach(p => depthOf(p.id));
    return depth;
  }, [projects]);
  const [currentChatId, setCurrentChatId] = useState<string>(() => {
    return localStorage.getItem('vexa_current_chat_id') || 'dashboard';
  });
  const currentChatIdRef = useRef(localStorage.getItem('vexa_current_chat_id') || 'dashboard');
  
  const [newAgentId, setNewAgentId] = useState('');
  const [newAgentName, setNewAgentName] = useState('');
  const [newAgentPrompt, setNewAgentPrompt] = useState('');
  // Empty, not a model name: the hardcoded 'qwen3:8b' this used to hold has
  // not been served by this deployment for a long time, so the create form
  // offered a model the local server does not serve. Filled from the live list below.
  const [newAgentModel, setNewAgentModel] = useState('');
  const [newAgentSkills, setNewAgentSkills] = useState('');
  const [newAgentTemperature, setNewAgentTemperature] = useState(0.7);
  const [isCreatingAgent, setIsCreatingAgent] = useState(false);

  // Connection channel session states
  const [showNewSessionModal, setShowNewSessionModal] = useState(false);
  const [newSessionNameInput, setNewSessionNameInput] = useState('');
  const [newSessionAgentInput, setNewSessionAgentInput] = useState('vexa');
  const [newSessionProjectInput, setNewSessionProjectInput] = useState('');

  const [editingAgentId, setEditingAgentId] = useState('');
  const [editAgentName, setEditAgentName] = useState('');
  const [editAgentPrompt, setEditAgentPrompt] = useState('');
  const [editAgentModel, setEditAgentModel] = useState('');
  const [editAgentSkills, setEditAgentSkills] = useState('');
  const [editAgentTemperature, setEditAgentTemperature] = useState(0.7);
  const [isUpdatingAgent, setIsUpdatingAgent] = useState(false);
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);

  // A create form that has not been touched takes the first model the box
  // actually serves. Never overwrites a choice already made, and never touches
  // the edit form, which is filled from the agent being edited.
  useEffect(() => {
    if (models.length === 0) return;
    setNewAgentModel(current => current || models[0].id);
  }, [models]);

  useEffect(() => {
    // Protected endpoint — never poll it from the login screen.
    if (!isAuthenticated) return;
    const authToken = localStorage.getItem('vexa_auth_token');
    fetch('/api/router/stats', {
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
    })
      .then(response => (response.ok ? response.json() : null))
      .then(data => setRouterDashboardUrl(data?.dashboard_url || null))
      .catch(() => setRouterDashboardUrl(null));
  }, [isAuthenticated]);

  const navStyle = useCallback((tab: typeof activeTab) => ({
    ...styles.navBtn,
    ...(isSidebarCollapsed ? styles.navBtnCollapsed : {}),
    ...(activeTab === tab ? styles.navBtnActive : {})
  }), [activeTab, isSidebarCollapsed]);

  useEffect(() => {
    currentChatIdRef.current = currentChatId;
    localStorage.setItem('vexa_current_chat_id', currentChatId);
    // The server only delivers chat traffic to sockets that asked for it, so a
    // chat switch has to re-declare interest. Subscriptions are additive: the
    // previous chat stays subscribed, which is what keeps a background run's
    // completion visible after switching away and back.
    const socket = wsRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'subscribe', chat_ids: [currentChatId] }));
    }
  }, [currentChatId]);

  useEffect(() => {
    localStorage.setItem('vexa_active_tab', activeTab);
  }, [activeTab]);

  useEffect(() => {
    localStorage.setItem('vexa_settings_section', settingsSection);
  }, [settingsSection]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    params.set('tab', activeTab);
    if (activeTab === 'settings') params.set('section', settingsSection);
    else params.delete('section');
    const hash = `#${params}`;
    if (window.location.hash !== hash) window.history.pushState(null, '', hash);
  }, [activeTab, settingsSection]);

  useEffect(() => {
    const restoreRoute = () => {
      const params = new URLSearchParams(window.location.hash.slice(1));
      setActiveTab(normalizeTab(params.get('tab')));
      const section = params.get('section');
      setSettingsSection(section && ['config', 'currency', 'subagents', 'mcp', 'obsidian', 'memory', 'logs', 'activity', 'keys'].includes(section) ? section as any : 'config');
    };
    window.addEventListener('popstate', restoreRoute);
    window.addEventListener('hashchange', restoreRoute);
    return () => { window.removeEventListener('popstate', restoreRoute); window.removeEventListener('hashchange', restoreRoute); };
  }, []);

  // Keep ttsEnabledRef in sync with its state
  useEffect(() => { ttsEnabledRef.current = isTTSEnabled; }, [isTTSEnabled]);
  useEffect(() => { appSettingsRef.current = appSettings; }, [appSettings]);

  const endTtsPending = useCallback(() => {
    if (ttsPendingTimerRef.current !== null) {
      window.clearTimeout(ttsPendingTimerRef.current);
      ttsPendingTimerRef.current = null;
    }
    setIsTtsPending(false);
  }, []);

  const beginTtsPending = useCallback(() => {
    if (ttsPendingTimerRef.current !== null) window.clearTimeout(ttsPendingTimerRef.current);
    setIsTtsPending(true);
    ttsPendingTimerRef.current = window.setTimeout(() => {
      ttsPendingTimerRef.current = null;
      setIsTtsPending(false);
    }, TTS_PENDING_TIMEOUT_MS);
  }, []);

  const stopSpeech = useCallback(() => {
    ttsRequestRef.current += 1;
    // A queue parked waiting for more streamed text must wake to see it was superseded,
    // or its loop would wait forever.
    const session = speechSessionRef.current;
    speechSessionRef.current = null;
    if (session) {
      session.closed = true;
      session.queue.length = 0;
      wakeSpeech(session);
    }
    window.speechSynthesis?.cancel();
    if (ttsAudioRef.current) {
      ttsAudioRef.current.pause();
      ttsAudioRef.current.src = '';
      ttsAudioRef.current = null;
    }
    if (ttsAudioUrlRef.current) {
      URL.revokeObjectURL(ttsAudioUrlRef.current);
      ttsAudioUrlRef.current = '';
    }
    endTtsPending();
    setIsSpeaking(false);
    setPlayingMsgIndex(null);
  }, [endTtsPending]);

  // Shared "voice pipeline failed" indicator — same inline pill used by both the mic
  // (STT) and speech (TTS) paths, auto-clearing so it never lingers as a stale state.
  const showVoiceError = useCallback((message: string) => {
    // Every TTS failure path funnels through here, so this is also where a reply that
    // will never be spoken stops counting as "speech on its way".
    endTtsPending();
    setMicState('error');
    setMicErrorMessage(message);
    setTimeout(() => {
      setMicState(current => (current === 'error' ? 'off' : current));
      setMicErrorMessage('');
    }, 3500);
  }, [endTtsPending]);

  // ── TTS helper ─────────────────────────────────────────────────────────────
  /**
   * Starts a spoken reply whose text can keep arriving after playback has begun: pieces
   * are appended with enqueueSpeech, and the reply ends once closeSpeech was called and
   * everything queued has played. Only one reply speaks at a time — opening another, or
   * stopSpeech(), supersedes this one.
   */
  const openSpeech = useCallback((msgIndex?: number): SpeechSession => {
    stopSpeech();
    const requestId = ttsRequestRef.current;
    const session: SpeechSession = { requestId, queue: [], closed: false, paused: false, waiters: [] };
    speechSessionRef.current = session;
    // stopSpeech() just cleared the previous reply's flag; from here until playback
    // actually starts, this reply owns it. Vexa's dialog mode reads it to know a spoken
    // answer is still coming — otherwise it reopens the microphone 1.1s after the text
    // lands, and the stopSpeech() inside startVoiceRecording() aborts this very fetch.
    beginTtsPending();
    if (msgIndex !== undefined) setPlayingMsgIndex(msgIndex);

    const alive = () => requestId === ttsRequestRef.current;
    // The loop ended on a failure it already reported: stop taking text without
    // cancelling anything, so later sentences of a streamed reply are not queued here.
    const retire = () => {
      if (speechSessionRef.current === session) speechSessionRef.current = null;
      setIsSpeaking(false);
      setPlayingMsgIndex(null);
    };
    const resumed = () => {
      session.paused = false;
      endTtsPending();
      setIsSpeaking(true);
    };
    // Playback caught up with a reply that is still being written. Vexa is silent until
    // the next sentence arrives — not speaking, but with a spoken answer still on its
    // way, which is exactly what isTtsPending tells dialog mode.
    const pauseIfStarved = (starved: boolean) => {
      if (!starved || !alive()) return;
      session.paused = true;
      setIsSpeaking(false);
      beginTtsPending();
    };
    // Next text to synthesise, waiting for a streamed reply to produce it if need be.
    // null once the reply is closed and played out, superseded, or `giveUp` says so.
    const nextText = async (max: number, giveUp?: () => boolean): Promise<string | null> => {
      for (;;) {
        if (!alive() || giveUp?.()) return null;
        if (session.queue.length) return takeSpeechBatch(session.queue, max);
        if (session.closed) return null;
        await new Promise<void>(resolve => session.waiters.push(resolve));
      }
    };

    const speakWithBrowser = (text: string) => new Promise<boolean>(resolve => {
      if (!('speechSynthesis' in window)) {
        showVoiceError('Голосовой движок недоступен в этом браузере.');
        resolve(false);
        return;
      }
      const utter = new SpeechSynthesisUtterance(text);
      const locale = langToLocale[appSettingsRef.current.language] || 'ru-RU';
      utter.lang = locale;
      utter.rate = 1;
      utter.pitch = 1.03;

      const voices = window.speechSynthesis.getVoices();
      const langVoices = voices.filter(v => v.lang.startsWith(appSettingsRef.current.language));
      const femaleVoice = langVoices.find(v =>
        v.name.toLowerCase().includes('alena') ||
        v.name.toLowerCase().includes('milena') ||
        v.name.toLowerCase().includes('svetlana') ||
        v.name.toLowerCase().includes('tatyana') ||
        v.name.toLowerCase().includes('katya') ||
        v.name.toLowerCase().includes('irina') ||
        v.name.toLowerCase().includes('elena') ||
        v.name.toLowerCase().includes('xenia') ||
        v.name.toLowerCase().includes('baya') ||
        v.name.toLowerCase().includes('female')
      );
      if (femaleVoice) utter.voice = femaleVoice;
      else if (langVoices.length > 0) utter.voice = langVoices[0];

      // Chrome (and others) can silently drop speechSynthesis.speak() — no onstart, no
      // onerror, just nothing — when it's called from code the browser doesn't consider
      // a "real" user gesture (e.g. an auto-triggered reply from a WebSocket push after
      // the page has been idle). There's a proactive fix for this (the first-interaction
      // "unlock" effect below), but if it still happens, surface it instead of staying
      // silent forever with zero indication of why Vexa stopped talking.
      let started = false;
      utter.onstart = () => {
        started = true;
        resumed();
      };
      utter.onend = () => resolve(true);
      utter.onerror = () => {
        // stopSpeech()'s cancel() lands here too; that is not a failure.
        if (alive()) showVoiceError('Не удалось озвучить ответ.');
        resolve(false);
      };
      window.speechSynthesis.speak(utter);
      setTimeout(() => {
        if (!started && alive()) {
          showVoiceError('Браузер заблокировал голосовой ответ — кликните на странице и повторите.');
        }
      }, 1200);
    });

    // `first` is text already taken off the queue that the server voice failed to play,
    // so a mid-reply failure resumes where it stopped instead of repeating the answer.
    const speakQueueWithBrowser = async (first: string | null) => {
      let text = first ?? await nextText(SPEECH_CHUNK_MAX);
      while (text !== null) {
        if (!(await speakWithBrowser(text))) {
          if (alive()) retire();
          return;
        }
        if (!alive()) return;
        pauseIfStarved(!session.queue.length && !session.closed);
        text = await nextText(SPEECH_CHUNK_MAX);
      }
      if (alive()) stopSpeech();
    };

    void (async () => {
      // Pieces taken off the queue whose audio has not finished playing yet.
      const unplayed: string[] = [];
      let serverFailed = false;
      let taken = 0;
      // The chunk being synthesised ahead of playback. If we bail before awaiting it, its
      // rejection still has to be observed or the browser logs an unhandled rejection.
      let lookahead: Promise<Blob | null> | null = null;

      try {
        const authToken = localStorage.getItem('vexa_auth_token');
        const authHeaders: Record<string, string> = authToken ? { 'Authorization': `Bearer ${authToken}` } : {};
        if (serverTtsAvailableRef.current === null) {
          const statusResponse = await fetch('/api/voice/tts/status', { headers: authHeaders });
          const status = statusResponse.ok ? await statusResponse.json() : null;
          serverTtsAvailableRef.current = Boolean(status?.available);
        }
        if (!alive()) return;
        if (!serverTtsAvailableRef.current) {
          await speakQueueWithBrowser(null);
          return;
        }

        const synthesize = async (part: string): Promise<Blob> => {
          const response = await fetch('/api/voice/synthesize', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders },
            body: JSON.stringify({ text: part, rate: 1 }),
          });
          if (!response.ok) throw new Error('Local TTS provider is unavailable.');
          return response.blob();
        };

        // One <audio> element reused for every chunk: VexaAudioAnalyser may only wrap a
        // given element in a MediaElementAudioSourceNode once, so an element per chunk
        // would leak nodes and leave the core animation reading a dead source.
        const audio = new Audio();
        ttsAudioRef.current = audio;
        audio.onplay = resumed;

        const playBlob = (blob: Blob) => new Promise<void>((resolve, reject) => {
          const url = URL.createObjectURL(blob);
          if (ttsAudioUrlRef.current) URL.revokeObjectURL(ttsAudioUrlRef.current);
          ttsAudioUrlRef.current = url;
          audio.onended = () => resolve();
          audio.onerror = () => reject(new Error('TTS playback failed.'));
          audio.src = url;
          audio.play().catch(reject);
        });

        // Waits for the next text (a streamed reply may still be writing it) and starts
        // synthesising it at once. Called *before* the current chunk plays, so generation
        // overlaps playback. Synthesis runs ~2x faster than speech, so after the first
        // chunk the queue stays ahead of the speaker and the seams are inaudible.
        const fetchNext = async (): Promise<Blob | null> => {
          // Only the opening chunk is kept small: it alone decides the initial silence.
          const max = taken === 0 ? SPEECH_FIRST_CHUNK_MAX : SPEECH_CHUNK_MAX;
          // Once the server voice failed, the browser voice owns the queue — never race it.
          const text = await nextText(max, () => serverFailed);
          if (text === null) return null;
          taken += 1;
          unplayed.push(text);
          return synthesize(text);
        };

        lookahead = fetchNext();
        for (;;) {
          const pending = lookahead;
          lookahead = null;
          const blob = await pending;
          if (!alive()) return;
          if (!blob) break;
          lookahead = fetchNext();
          await playBlob(blob);
          if (!alive()) return;
          unplayed.shift();
          // The lookahead is parked waiting for text: the stream has not caught up yet.
          pauseIfStarved(session.waiters.length > 0);
        }
        if (alive()) stopSpeech();
      } catch {
        // A superseded request is not a failure: stopSpeech() clearing audio.src is what
        // rejected us. Falling through here would reset serverTtsAvailableRef and make
        // every following reply pay for a pointless /api/voice/tts/status probe.
        if (!alive()) return;
        serverFailed = true;
        lookahead?.catch(() => {});
        // Reset to null (not false) — a single failure shouldn't permanently downgrade
        // every future reply to the browser's robotic fallback voice for the rest of the
        // page session. null makes the next reply re-probe and retry.
        serverTtsAvailableRef.current = null;
        setIsSpeaking(false);
        await speakQueueWithBrowser(unplayed.length ? unplayed.splice(0).join(' ') : null);
      } finally {
        lookahead?.catch(() => {});
      }
    })();

    return session;
  }, [stopSpeech, showVoiceError, beginTtsPending, endTtsPending]);

  const enqueueSpeech = useCallback((session: SpeechSession, pieces: string[]) => {
    if (session !== speechSessionRef.current || session.closed || !pieces.length) return;
    session.queue.push(...pieces);
    // Speech had run dry, and its pending flag may have timed out while a tool ran:
    // re-assert it so dialog mode does not reopen the mic just before this plays.
    if (session.paused) beginTtsPending();
    wakeSpeech(session);
  }, [beginTtsPending]);

  const closeSpeech = useCallback((session: SpeechSession) => {
    session.closed = true;
    wakeSpeech(session);
  }, []);

  /** Speaks a complete text from the top, replacing whatever is being said. */
  const speakWhole = useCallback((rawText: string, msgIndex?: number) => {
    const clean = cleanForSpeech(rawText);
    if (!clean) return;
    const session = openSpeech(msgIndex);
    enqueueSpeech(session, splitForSpeech(clean));
    closeSpeech(session);
  }, [openSpeech, enqueueSpeech, closeSpeech]);

  const speakText = useCallback((rawText: string, msgIndex?: number) => {
    // If already playing this message — stop it
    if (msgIndex !== undefined && msgIndex === playingMsgIndex) {
      stopSpeech();
      return;
    }
    speakWhole(rawText, msgIndex);
  }, [playingMsgIndex, stopSpeech, speakWhole]);

  /**
   * Speaks a streamed reply's finished sentences while the model is still writing the
   * rest, so Vexa starts talking after the first sentence rather than the whole answer.
   * The caller only feeds deltas of the chat on screen.
   */
  const feedSpeechStream = useCallback((runId: string, delta: string) => {
    const streams = speechStreamsRef.current;
    let stream = streams.get(runId);
    if (!stream) {
      stream = { splitter: new StreamingSpeechSplitter(), session: null, muted: false, frozen: false };
      streams.set(runId, stream);
    }
    if (stream.muted || stream.frozen) return;
    if (!ttsEnabledRef.current) {
      stream.muted = true;
      return;
    }
    const pieces = stream.splitter.push(delta);
    if (!pieces.length) return;
    if (!stream.session) {
      stream.session = openSpeech();
    } else if (stream.session !== speechSessionRef.current) {
      // This reply's speech was stopped (mic opened, stop pressed, something else
      // started speaking): the next sentence must not start it again.
      stream.muted = true;
      return;
    }
    enqueueSpeech(stream.session, pieces);
  }, [openSpeech, enqueueSpeech]);

  /**
   * Ends a streamed reply's speech. With `speak`, says whatever part of the reply the
   * stream has not voiced yet — of `finalText` when given (all of it if nothing was
   * spoken while streaming); without, lets already-queued speech finish and adds nothing.
   */
  const finishSpeechStream = useCallback((runId: string, speak: boolean, finalText?: string) => {
    const streams = speechStreamsRef.current;
    const stream = streams.get(runId);
    streams.delete(runId);
    if (finalText === undefined) {
      // Ended without the final answer (stream end or a lost buffer). Should that answer
      // still arrive afterwards, this muted entry stops it being read out a second time.
      streams.set(runId, { splitter: new StreamingSpeechSplitter(), session: null, muted: true, frozen: false });
      if (streams.size > 32) streams.delete(streams.keys().next().value!);
    }
    if (!stream) {
      if (speak && finalText !== undefined) speakWhole(finalText);
      return;
    }
    let { session } = stream;
    // Its speech was stopped or replaced meanwhile: leave whatever is playing now alone.
    if (session && session !== speechSessionRef.current) return;
    if (speak && !stream.muted) {
      const pieces = stream.splitter.finish(finalText);
      if (!session && pieces.length) session = openSpeech();
      if (session) enqueueSpeech(session, pieces);
    }
    if (session) closeSpeech(session);
  }, [speakWhole, openSpeech, enqueueSpeech, closeSpeech]);

  /**
   * Silences a streamed reply for good: stops its speech if that is what is playing, and
   * keeps a muted entry so a straggling delta or final frame cannot start it again.
   */
  const cancelSpeechStream = useCallback((runId: string) => {
    const streams = speechStreamsRef.current;
    const stream = streams.get(runId);
    if (stream?.session && stream.session === speechSessionRef.current) stopSpeech();
    streams.set(runId, {
      splitter: stream?.splitter ?? new StreamingSpeechSplitter(),
      session: null,
      muted: true,
      frozen: false,
    });
    // A cancelled run gets no terminal frame to clear its entry; keep the map bounded.
    if (streams.size > 32) streams.delete(streams.keys().next().value!);
  }, [stopSpeech]);

  // ── Voice command helpers ───────────────────────────────────────────────────
  useEffect(() => {
    micStateRef.current = micState;
  }, [micState]);

  // Draft persistence: restore the unsent draft when switching sessions (P0 UX).
  useEffect(() => {
    try {
      const draft = localStorage.getItem(`hermes.draft.${currentChatId}`) || '';
      setInputValue(draft);
    } catch { /* localStorage unavailable — non-fatal */ }
  }, [currentChatId]);

  // A conversation resumes on the model it was last held against, falling back
  // to the user's pinned default and then to the backend's configured one.
  useEffect(() => {
    setModelSelection(loadSelection(currentChatId));
  }, [currentChatId]);

  // null clears the pick: the chat falls back to the backend's configured
  // model, which is what happens when a remembered model no longer exists.
  const handleModelSelect = useCallback((selection: ModelSelection | null) => {
    setModelSelection(selection);
    saveSelection(currentChatIdRef.current, selection);
  }, []);

  useEffect(() => {
    try {
      if (inputValue) {
        localStorage.setItem(`hermes.draft.${currentChatId}`, inputValue);
      } else {
        localStorage.removeItem(`hermes.draft.${currentChatId}`);
      }
    } catch { /* localStorage unavailable — non-fatal */ }
  }, [inputValue, currentChatId]);

  const isGenerating = Boolean(generatingRuns[currentChatId]);
  const isAnyGenerating = Object.keys(generatingRuns).length > 0;

  const markGenerating = useCallback((chatId: string, runId: string) => {
    const next = { ...generatingRunsRef.current, [chatId]: runId };
    generatingRunsRef.current = next;
    setGeneratingRuns(next);
  }, []);

  /** Ends a run by its id and/or its chat. A frame without a run_id (legacy
   *  server) clears its chat; one with a run_id never clears a newer run. */
  const clearGenerating = useCallback((chatId?: string, runId?: string) => {
    const next: Record<string, string> = {};
    let changed = false;
    Object.entries(generatingRunsRef.current).forEach(([chat, run]) => {
      const ends = (runId && run === runId) || (!runId && chatId !== undefined && chat === chatId);
      if (ends) changed = true;
      else next[chat] = run;
    });
    if (!changed) return;
    generatingRunsRef.current = next;
    setGeneratingRuns(next);
  }, []);

  /** `agentId` routes this one turn to another agent inside the same chat —
   *  the simple chat's @mention (see VexaAgent.respond's agent_override). */
  const sendChatText = useCallback((text: string, targetChatId = currentChatIdRef.current, options?: { agentId?: string | null }) => {
    const command = text.trim();
    if (!command || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;

    // Block concurrent sends while this chat's response is generating (P0 UX).
    if (generatingRunsRef.current[targetChatId]) {
      console.warn('Response in progress; ignoring new submission.');
      return false;
    }

    const now = Date.now();
    if (now - lastSentTimeRef.current < 300) {
      console.warn("Prevented duplicate message submission");
      return false;
    }
    lastSentTimeRef.current = now;
    lastUserMessageRef.current[targetChatId] = command;

    stopSpeech();
    const runId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    // The picked model travels with the message, not as a global setting, so
    // two chats can be held against two different models at once. An id the
    // server no longer knows resolves back to the configured default there
    // rather than failing the turn — see backend/chat_models.py.
    const picked = modelSelectionRef.current;
    wsRef.current.send(JSON.stringify({
      type: 'chat_message',
      content: command,
      chat_id: targetChatId,
      run_id: runId,
      ...(picked ? { model: picked.id, model_source: picked.source } : {}),
      ...(options?.agentId ? { agent_id: options.agentId } : {}),
    }));
    markGenerating(targetChatId, runId);
    return true;
  }, [stopSpeech, markGenerating]);

  const resetVoiceRecorder = useCallback(() => {
    vadCleanupRef.current?.();
    vadCleanupRef.current = null;
    voiceStreamRef.current?.getTracks().forEach(track => track.stop());
    voiceStreamRef.current = null;
    mediaRecorderRef.current = null;
    voiceChunksRef.current = [];
  }, []);

  const submitVoiceBlob = useCallback(async (blob: Blob) => {
    if (!blob.size) {
      setMicEnabled(false);
      setMicState('off');
      return;
    }

    setMicEnabled(true);
    setMicState('transcribing');
    setMicErrorMessage('');
    const formData = new FormData();
    formData.append('file', blob, `vexa-voice-${Date.now()}.webm`);

    try {
      const res = await fetch('/api/voice/transcribe', {
        method: 'POST',
        body: formData,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || 'Voice transcription failed.');
      }

      const text = String(data.text || '').trim();
      if (!text) {
        // Recognised nothing (background noise, a cough, a false trigger). Not an
        // error worth an alarm pill — end the turn quietly and let dialog mode
        // take another one rather than dropping out of the conversation.
        setMicEnabled(false);
        setMicState('off');
        setVoiceIdleTick(tick => tick + 1);
        return;
      }

      // Show what was recognized before it's auto-sent, so the user can actually see
      // their dictated text rather than it silently vanishing straight into the chat.
      // Long enough to register as "Vexa heard this", short enough not to be felt as
      // lag — in dialog mode this sits on the critical path of every single turn.
      setInputValue(text);
      await new Promise(resolve => setTimeout(resolve, 200));

      const vexaMode = localStorage.getItem('vexa_active_tab') === 'vexa';
      if (vexaMode && currentChatIdRef.current !== 'dashboard') {
        currentChatIdRef.current = 'dashboard';
        setCurrentChatId('dashboard');
        localStorage.setItem('vexa_current_chat_id', 'dashboard');
      }
      const sent = sendChatText(text, vexaMode ? 'dashboard' : currentChatIdRef.current);
      setInputValue(sent ? '' : text);
      if (sent) playBeep(1040, 0.12);
      setMicEnabled(false);
      setMicState('off');
    } catch (err) {
      // A transient "didn't catch that" is routine (short utterance, pause, background
      // noise) — surface it inline through the same mic-status pill used for
      // listening/capturing/transcribing instead of a blocking native notify().
      console.error('Voice transcription error:', err);
      setMicEnabled(false);
      showVoiceError(err instanceof Error ? err.message : 'Voice transcription failed.');
      // Same reasoning as the "recognised nothing" branch above: this turn produced no
      // answer, and dialog mode only re-arms on a new answer or an idle tick. Without
      // this, a single transcription failure (e.g. the server's own VAD found no speech
      // even though the client thought it heard some, or a transient STT error) would
      // silently end the conversation loop instead of retrying like a real idle turn.
      setVoiceIdleTick(tick => tick + 1);
    }
  }, [sendChatText, showVoiceError]);

  const stopVoiceRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== 'inactive') {
      recorder.stop();
    }
  }, []);

  const startVoiceRecording = useCallback(async () => {
    if (!window.isSecureContext) {
      notify(
        appSettingsRef.current.language === 'ru'
          ? 'Браузер блокирует микрофон на обычном HTTP. Откройте Vexa через HTTPS или через http://localhost:9119 по SSH-туннелю.'
          : 'The browser blocks microphone access over plain HTTP. Open Vexa over HTTPS or through http://localhost:9119 using an SSH tunnel.'
      );
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      notify(copy('Voice recording is not supported by this browser.'));
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          // Chrome-only: actively cancel out Vexa's own TTS reply bleeding back
          // through the speakers into this same recording — without it, dialog
          // mode re-arming the mic shortly after a reply finishes can pick up
          // the tail of its own voice as noise/echo, which both sounds like
          // distortion and can confuse the STT's speech/silence detection on
          // the next turn. Unsupported browsers just ignore the unknown key.
          suppressLocalAudioPlayback: true,
        } as MediaTrackConstraints
      });

      const mimeCandidates = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/ogg',
      ];
      const mimeType = mimeCandidates.find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);

      voiceStreamRef.current = stream;
      voiceChunksRef.current = [];
      voiceNoSpeechRef.current = false;
      mediaRecorderRef.current = recorder;

      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) {
          voiceChunksRef.current.push(event.data);
        }
      };

      recorder.onstop = () => {
        const recordedType = recorder.mimeType || mimeType || 'audio/webm';
        const blob = new Blob(voiceChunksRef.current, { type: recordedType });
        const heardNothing = voiceNoSpeechRef.current;
        resetVoiceRecorder();
        if (heardNothing) {
          // Silence: skip the upload (whisper's own VAD would strip it to an
          // empty transcript anyway) and report an idle turn.
          setMicEnabled(false);
          setMicState('off');
          setVoiceIdleTick(tick => tick + 1);
          return;
        }
        submitVoiceBlob(blob);
      };

      recorder.onerror = (event) => {
        console.error('Voice recorder error:', event);
        resetVoiceRecorder();
        setMicEnabled(false);
        setMicState('off');
        notify(copy('Voice recorder failed.'));
      };

      // Auto-stop on end-of-speech (simple client-side VAD) so the user doesn't have to
      // click the mic again to submit — speak, pause, done. Tuned generously (1.0s of
      // sustained quiet after real speech was heard) to avoid cutting off mid-sentence
      // pauses; a hard 60s cap guards against a stuck/silent stream never triggering it.
      try {
        const AudioContextCtor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (AudioContextCtor) {
          const vadCtx = new AudioContextCtor();
          const source = vadCtx.createMediaStreamSource(stream);
          const analyser = vadCtx.createAnalyser();
          analyser.fftSize = 512;
          source.connect(analyser);
          const dataArray = new Uint8Array(analyser.frequencyBinCount);

          // A fixed 0.025 floor assumed a hot microphone. On a quieter input the
          // RMS never crossed it, so no speech was ever "started", the recording
          // ran to its 60s cap and arrived at the STT as pure silence — the user
          // talks and Vexa simply never answers. The threshold is now derived
          // from the room: sample the ambient level during ARM_DELAY_MS and sit a
          // fixed multiple above it, clamped so neither a dead-silent room nor a
          // noisy one produces a useless threshold.
          const MIN_RMS_THRESHOLD = 0.012;
          const MAX_RMS_THRESHOLD = 0.05;
          const NOISE_FLOOR_MULTIPLIER = 2.5;
          let speechThreshold = MIN_RMS_THRESHOLD;
          let noiseFloorSum = 0;
          let noiseFloorSamples = 0;
          const SILENCE_MS = 1000;
          // A single instant above threshold used to count as "speech started" — the
          // confirmation beep (played the moment recording starts) or any stray click
          // bleeding into the mic could trip that, and since SILENCE_MS alone always
          // exceeds this, the old MIN_SPEECH_MS check never actually required sustained
          // voice. Recordings ended up ~1.5s of pure noise with zero real speech, which
          // the STT then correctly reported as "no speech detected". Now the RMS must
          // stay above threshold continuously for MIN_SUSTAINED_SPEECH_MS before we
          // treat it as real speech at all, and ARM_DELAY_MS ignores the recording's
          // first moments outright (covers the beep's own ~120ms tail).
          const MIN_SUSTAINED_SPEECH_MS = 200;
          const ARM_DELAY_MS = 350;
          const MAX_RECORDING_MS = 60000;
          // If nothing resembling speech turns up in this window, stop and let the
          // caller re-arm rather than recording silence until MAX_RECORDING_MS —
          // a minute of dead air costs a pointless STT round-trip and leaves
          // dialog mode looking frozen.
          const NO_SPEECH_TIMEOUT_MS = 12000;
          const startedAt = performance.now();
          let candidateSpeechAt: number | null = null;
          let speechStartedAt: number | null = null;
          let silenceStartedAt: number | null = null;
          let stopped = false;
          let rafId = 0;

          const tick = () => {
            if (stopped) return;
            analyser.getByteTimeDomainData(dataArray);
            let sumSquares = 0;
            for (let i = 0; i < dataArray.length; i += 1) {
              const normalized = (dataArray[i] - 128) / 128;
              sumSquares += normalized * normalized;
            }
            const rms = Math.sqrt(sumSquares / dataArray.length);
            const now = performance.now();

            if (now - startedAt < ARM_DELAY_MS) {
              // Sample the room only past the confirmation beep's ~120ms tail —
              // averaging the beep itself in would inflate the floor and push the
              // threshold right back above normal speech.
              if (now - startedAt > 200) {
                noiseFloorSum += rms;
                noiseFloorSamples += 1;
              }
              rafId = requestAnimationFrame(tick);
              return;
            }

            if (noiseFloorSamples > 0) {
              const noiseFloor = noiseFloorSum / noiseFloorSamples;
              speechThreshold = Math.min(
                MAX_RMS_THRESHOLD,
                Math.max(MIN_RMS_THRESHOLD, noiseFloor * NOISE_FLOOR_MULTIPLIER)
              );
              noiseFloorSamples = 0;
            }

            if (rms > speechThreshold) {
              if (candidateSpeechAt === null) candidateSpeechAt = now;
              if (speechStartedAt === null && now - candidateSpeechAt >= MIN_SUSTAINED_SPEECH_MS) {
                speechStartedAt = candidateSpeechAt;
              }
              silenceStartedAt = null;
            } else {
              candidateSpeechAt = null;
              if (speechStartedAt !== null) {
                if (silenceStartedAt === null) silenceStartedAt = now;
                else if (now - silenceStartedAt > SILENCE_MS) {
                  stopped = true;
                  stopVoiceRecording();
                  return;
                }
              }
            }

            if (speechStartedAt === null && now - startedAt > NO_SPEECH_TIMEOUT_MS) {
              stopped = true;
              voiceNoSpeechRef.current = true;
              stopVoiceRecording();
              return;
            }

            if (now - startedAt > MAX_RECORDING_MS) {
              stopped = true;
              stopVoiceRecording();
              return;
            }
            rafId = requestAnimationFrame(tick);
          };
          rafId = requestAnimationFrame(tick);

          vadCleanupRef.current = () => {
            stopped = true;
            cancelAnimationFrame(rafId);
            source.disconnect();
            analyser.disconnect();
            vadCtx.close().catch(() => {});
          };
        }
      } catch (vadErr) {
        console.error('Voice activity detection setup failed (manual stop still works):', vadErr);
      }

      recorder.start();
      setMicEnabled(true);
      setMicState('capturing');
      stopSpeech();
      stopAlarmSound();
      playBeep(880, 0.12);
    } catch (err) {
      console.error('Microphone permission error:', err);
      resetVoiceRecorder();
      setMicEnabled(false);
      setMicState('off');
      notify(copy('Microphone access was denied or is unavailable.'));
    }
  }, [resetVoiceRecorder, stopSpeech, stopVoiceRecording, submitVoiceBlob, copy]);

  const handleVoiceToggle = useCallback(() => {
    if (micStateRef.current === 'capturing') {
      playBeep(560, 0.10);
      stopVoiceRecording();
      return;
    }
    if (micStateRef.current === 'transcribing') return;
    startVoiceRecording();
  }, [startVoiceRecording, stopVoiceRecording]);

  useEffect(() => {
    return () => {
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== 'inactive') {
        try { recorder.stop(); } catch (_) {}
      }
      resetVoiceRecorder();
    };
  }, [resetVoiceRecorder]);

  // Request browser notification permission once on mount
  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
    if ('speechSynthesis' in window) {
      window.speechSynthesis.getVoices();
      window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
    }
  }, []);

  // Unlock audio/speech autoplay on the first real user interaction. Vexa's spoken
  // replies are triggered from an async WebSocket push, not a click — browsers (Chrome
  // especially) can block both <audio>.play() and speechSynthesis.speak() from
  // auto-triggered code until the page has seen at least one genuine gesture, and the
  // failure is completely silent (no error event fires either way). A single click or
  // keypress anywhere registers that gesture for the rest of the page session.
  useEffect(() => {
    const unlock = () => {
      try {
        const silent = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=');
        silent.volume = 0;
        void silent.play().catch(() => {});
      } catch { /* best-effort */ }
      if ('speechSynthesis' in window) {
        try {
          const primer = new SpeechSynthesisUtterance('');
          primer.volume = 0;
          window.speechSynthesis.speak(primer);
        } catch { /* best-effort */ }
      }
    };
    document.addEventListener('pointerdown', unlock, { once: true });
    document.addEventListener('keydown', unlock, { once: true });
    return () => {
      document.removeEventListener('pointerdown', unlock);
      document.removeEventListener('keydown', unlock);
    };
  }, []);

  // Listen for unauthorized events to clear auth state
  useEffect(() => {
    const handleUnauthorized = () => {
      setIsAuthenticated(false);
    };
    window.addEventListener('vexa-unauthorized', handleUnauthorized);
    return () => {
      window.removeEventListener('vexa-unauthorized', handleUnauthorized);
    };
  }, []);

  const fetchWithAuth = useCallback((url: string, options: RequestInit = {}) => {
    const token = localStorage.getItem('vexa_auth_token');
    const headers = new Headers(options.headers);
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return fetch(url, { ...options, headers }).then(res => {
      if (res.status === 401) {
        localStorage.removeItem('vexa_auth_token');
        setIsAuthenticated(false);
      }
      return res;
    });
  }, []);

  useEffect(() => {
    if (!isAuthenticated) return undefined;
    let cancelled = false;
    const load = () => {
      fetchWithAuth('/api/client-tasks/stats')
        .then(res => (res.ok ? res.json() : null))
        .then(data => { if (!cancelled && data) setOverdueTaskCount(data.overdue || 0); })
        .catch(() => {});
    };
    load();
    const interval = setInterval(load, 60000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [isAuthenticated, fetchWithAuth]);

  const handleRequestOtp = async () => {
    setOtpSent(false);
    setOtpCode('');
    lastAttemptedOtp.current = '';
    setAuthStatus('sending');
    setAuthError('');
    try {
      const res = await authEndpointFetch('/api/auth/request-code', {
        method: 'POST'
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        setOtpSent(true);
        setAuthStatus('sent');
      } else {
        setAuthStatus('error');
        setAuthError(data.message || copy('Error sending code.'));
      }
    } catch (err) {
      setAuthStatus('error');
      setAuthError(copy(authErrorMessage(err, 'Error connecting to backend.')));
    }
  };

  const verifyOtpCode = useCallback(async (code: string) => {
    if (!/^\d{6}$/.test(code) || authStatus === 'verifying') return;
    lastAttemptedOtp.current = code;
    setAuthStatus('verifying');
    setAuthError('');
    try {
      const res = await authEndpointFetch('/api/auth/verify-code', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
      });
      if (res.ok) {
        const data = await res.json();
        localStorage.setItem('vexa_auth_token', data.token);
        setIsAuthenticated(true);
        setAuthStatus('success');
        setOtpCode('');
      } else {
        const data = await res.json();
        setAuthStatus('error');
        setAuthError(data.detail || copy('Invalid access code.'));
      }
    } catch (err) {
      setAuthStatus('error');
      setAuthError(copy(authErrorMessage(err, 'Error verifying code.')));
    }
  }, [authStatus, copy]);

  const handleVerifyOtp = async (e: React.FormEvent) => {
    e.preventDefault();
    verifyOtpCode(otpCode);
  };

  const handlePasswordLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!pwUsername.trim() || !pwPassword) return;
    setPwStatus('submitting');
    setPwError('');
    try {
      const res = await authEndpointFetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: pwUsername.trim(), password: pwPassword })
      });
      const data = await res.json();
      if (res.ok && data.token) {
        localStorage.setItem('vexa_auth_token', data.token);
        setIsAuthenticated(true);
        setPwPassword('');
        setPwStatus('idle');
      } else {
        setPwStatus('error');
        setPwError(data.detail || copy('Invalid username or password.'));
      }
    } catch (err) {
      setPwStatus('error');
      setPwError(copy(authErrorMessage(err, 'Error connecting to backend.')));
    }
  };

  useEffect(() => {
    if (otpSent && otpCode.length === 6 && otpCode !== lastAttemptedOtp.current && authStatus !== 'verifying') {
      verifyOtpCode(otpCode);
    }
  }, [otpCode, authStatus, otpSent, verifyOtpCode]);

  // Initialize and maintain WebSocket connection
  useEffect(() => {
    if (!isAuthenticated) return;
    
    let reconnectTimeoutId: any = null;
    let isCleanedUp = false;
    // Reset on every successful open, so a long-lived connection that drops once
    // retries immediately instead of inheriting an old backoff.
    let reconnectAttempt = 0;

    const connectWS = () => {
      if (isCleanedUp) return;

      // Guard against overlapping reconnect attempts (e.g. onclose firing more
      // than once for the same disconnect): cancel any pending retry and force-
      // close any still-live previous socket before opening a new one. Without
      // this, a stale socket can keep receiving server broadcasts alongside the
      // new one, causing every chat/TTS message to be processed twice.
      if (reconnectTimeoutId) {
        clearTimeout(reconnectTimeoutId);
        reconnectTimeoutId = null;
      }
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.onmessage = null;
        wsRef.current.close();
        wsRef.current = null;
      }

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const token = localStorage.getItem('vexa_auth_token') || '';
      const wsUrl = `${protocol}//${window.location.host}/api/ws`;

      console.log('Connecting to authenticated WebSocket.');
      // hermes-v2 = addressed delivery + stream resume. Older servers ignore it
      // and answer hermes-v1, which still works (they just fan everything out).
      const ws = new WebSocket(wsUrl, ['hermes-v2', 'hermes-v1', `hermes-auth.${token}`]);
      wsRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
        reconnectAttempt = 0;
        console.log('WebSocket connected.');
        const tracker = runCursorsRef.current;
        // 'inbox' carries only "conversation X changed" frames; the server drops it
        // from the subscription for a role without inbox access.
        const chatIds = Array.from(new Set([currentChatIdRef.current, ...tracker.activeChatIds(), 'inbox']))
          .filter(Boolean);
        ws.send(JSON.stringify({ type: 'subscribe', chat_ids: chatIds }));
        // Ask for the tail of everything that was still running when the socket
        // died. Without this the tokens streamed during the gap — and the final
        // answer itself — were lost until a full page reload.
        const runs = tracker.resumeRequest();
        if (runs.length > 0) {
          ws.send(JSON.stringify({ type: 'resume', runs }));
        }
      };

      ws.onclose = () => {
        setIsConnected(false);
        if (!isCleanedUp && wsRef.current === ws) {
          const delay = reconnectDelayMs(reconnectAttempt);
          reconnectAttempt += 1;
          console.log(`WebSocket disconnected. Reconnecting in ${delay}ms...`);
          reconnectTimeoutId = setTimeout(connectWS, delay);
        }
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          console.log('WS Event received:', data);

          if (data.type === 'resume_begin') {
            // 'gap': the replay starts further back than our cursor, so the
            // partial text we have for this run is not a prefix of it. Drop the
            // partial and let the replay rebuild it.
            if (data.status === 'gap') {
              runCursorsRef.current.reset(data.run_id);
              const speechStream = speechStreamsRef.current.get(String(data.run_id));
              if (speechStream) speechStream.frozen = true;
              setMessages(prev => prev.map(message => message.run_id === data.run_id
                ? { ...message, content: '', thinking: '' }
                : message));
            } else if (data.status === 'unknown') {
              // The buffer is gone (expired or never existed). The DB is the only
              // remaining source of truth for this run.
              runCursorsRef.current.forget(data.run_id);
              finishSpeechStream(String(data.run_id), false);
              // No terminal frame will ever come for it — don't leave its chat
              // stuck on "generating".
              clearGenerating(undefined, data.run_id);
              setMessages(prev => prev.map(message => message.run_id === data.run_id
                ? { ...message, streaming: false }
                : message));
              const chatId = String(data.chat_id || currentChatIdRef.current);
              fetch(`/api/history/${chatId}`)
                .then(res => res.json())
                .then(history => {
                  // Guard the shape: an error payload is an object, and storing
                  // it where a list belongs is what has blanked the dashboard
                  // before.
                  if (Array.isArray(history) && chatId === currentChatIdRef.current) {
                    setMessages(history);
                  }
                })
                .catch(err => console.error('Resume history reload failed:', err));
            }
            return;
          }
          if (data.type === 'resume_end' || data.type === 'subscribed') {
            return;
          }
          // A replayed frame the socket already delivered must not be applied
          // twice: stream chunks are deltas, so a double-apply duplicates text.
          if (!runCursorsRef.current.accept(data)) {
            return;
          }

          if (data.type === 'init') {
            setConfig(data.config);
            setEditedPrompt(data.config.system_prompt);
            setEditedModel(data.config.model);
            setEditedRuntimeConfig(data.config);
            if (data.logs) {
              setLogs(data.logs);
            }
            // init carries the dashboard's history. Applying it to whatever chat
            // is open replaced that chat's messages on every reconnect (and
            // auto-titled a fresh chat from the dashboard's first message).
            if (data.history && data.history.length > 0 && currentChatIdRef.current === 'dashboard') {
              setMessages(data.history);
            }
            if (data.activity_logs) {
              setActivityLogs(data.activity_logs);
            }
          } else if (data.type === 'chat_stream_start') {
            const msgChatId = data.chat_id || 'dashboard';
            if (msgChatId === currentChatIdRef.current) {
              setMessages(prev => prev.some(message => message.run_id === data.run_id)
                ? prev
                : [...prev, {
                    role: 'assistant',
                    content: '',
                    thinking: '',
                    chat_id: msgChatId,
                    run_id: data.run_id,
                    streaming: true,
                    meta: { model: data.model, provider: data.provider, status: 'streaming' },
                  }]);
            }
          } else if (data.type === 'chat_stream_chunk') {
            const msgChatId = data.chat_id || 'dashboard';
            if (msgChatId === currentChatIdRef.current) {
              // A chunk with tool calls means the model is about to run a tool; the name
              // stays as the message's activity until the model starts writing again.
              const toolName = Array.isArray(data.tool_calls)
                ? (data.tool_calls as Array<{ function?: { name?: string }; name?: string }>)
                    .map(call => call?.function?.name || call?.name || '')
                    .find(Boolean)
                : undefined;
              setMessages(prev => prev.map(message => message.run_id === data.run_id
                ? {
                    ...message,
                    content: `${message.content || ''}${data.content || ''}`,
                    thinking: `${message.thinking || ''}${data.thinking || ''}`,
                    activity: toolName || (data.content ? undefined : message.activity),
                    streaming: true,
                  }
                : message));
              if (data.content && data.run_id) feedSpeechStream(String(data.run_id), String(data.content));
            }
          } else if (data.type === 'chat_stream_end') {
            const msgChatId = data.chat_id || 'dashboard';
            // Normally chat_message already finished this reply's speech; this covers a
            // stream that ends without one.
            if (data.run_id) {
              finishSpeechStream(
                String(data.run_id),
                ttsEnabledRef.current && msgChatId === currentChatIdRef.current,
              );
            }
            if (msgChatId === currentChatIdRef.current) {
              setMessages(prev => prev.map(message => message.run_id === data.run_id
                ? { ...message, streaming: false, id: data.message_id || message.id, meta: data.meta || message.meta }
                : message));
            }
          } else if (data.type === 'chat_message') {
            const msgChatId = data.chat_id || 'dashboard';
            if (msgChatId === currentChatIdRef.current) {
              const incoming: ChatMessage = {
                id: data.id,
                role: data.role,
                content: data.content,
                chat_id: msgChatId,
                cost_usd: data.cost_usd,
                meta: data.meta,
                run_id: data.run_id,
                streaming: false,
              };
              setMessages(prev => data.role === 'assistant' && data.run_id && prev.some(message => message.run_id === data.run_id)
                ? prev.map(message => message.run_id === data.run_id ? { ...message, ...incoming } : message)
                : [...prev, incoming]);
            }
            if (data.role === 'assistant') {
              clearGenerating(msgChatId, data.run_id || undefined);
              const runId = data.run_id ? String(data.run_id) : '';
              if (data.suppress_tts) {
                if (runId) cancelSpeechStream(runId);
                stopSpeech();
              } else if (ttsEnabledRef.current && msgChatId === currentChatIdRef.current) {
                if (!runId || data.meta?.status === 'error') {
                  // An error replaces whatever the stream was saying — say it whole.
                  if (runId) cancelSpeechStream(runId);
                  speakWhole(data.content as string);
                } else {
                  // Most of a streamed reply has usually been spoken already; say the rest.
                  finishSpeechStream(runId, true, data.content as string);
                }
              } else if (runId) {
                finishSpeechStream(runId, false);
              }
              if (
                'Notification' in window &&
                Notification.permission === 'granted' &&
                document.visibilityState !== 'visible'
              ) {
                const preview = (data.content as string)
                  .replace(/\*\*|__|\*|_|`/g, '')
                  .trim()
                  .slice(0, 80);
                new Notification('VEXA', {
                  body: preview || 'New Vexa response',
                  icon: '/favicon.svg',
                  tag: 'vexa-reply',
                  silent: false,
                });
              }
            }
          } else if (data.type === 'chat_cancelled') {
            const cancelledChatId = data.chat_id || currentChatIdRef.current;
            const cancelledRunId = String(data.run_id || generatingRunsRef.current[cancelledChatId] || '');
            if (cancelledChatId === currentChatIdRef.current) {
              setMessages(prev => prev.map(message =>
                message.run_id === cancelledRunId
                  ? {
                      ...message,
                      streaming: false,
                      meta: { ...(message.meta || {}), status: 'cancelled' },
                    }
                  : message
              ));
            }
            clearGenerating(cancelledChatId, cancelledRunId || undefined);
            if (cancelledRunId) cancelSpeechStream(cancelledRunId);
          } else if (data.type === 'user_message_id_update') {
            const msgChatId = data.chat_id || 'dashboard';
            if (msgChatId === currentChatIdRef.current) {
              setMessages((prev) => prev.map(m => 
                m.role === 'user' && m.content === data.content && !m.id
                  ? { ...m, id: data.id }
                  : m
              ));
            }
          } else if (data.type === 'session_title_update') {
            setChatSessions((prev) =>
              prev.map((s) => (s.id === data.chat_id ? { ...s, title: data.title } : s))
            );
          } else if (data.type === 'logs_update') {
            setLogs(data.logs);
          } else if (data.type === 'activity_log') {
            setActivityLogs((prev) => {
              const updated = [data.log, ...prev];
              return updated.slice(0, 200);
            });
          } else if (data.type === 'config_update') {
            setConfig(data);
            setEditedPrompt(data.system_prompt);
            setEditedModel(data.model);
            setEditedRuntimeConfig(data);
          } else if (data.type === 'timer_completed') {
            setTimers((prev) => {
              const exists = prev.some(t => t.id === data.timer.id);
              if (exists) {
                return prev.map(t => t.id === data.timer.id ? { ...t, status: 'completed', time_left: 0 } : t);
              }
              return [...prev, { ...data.timer, time_left: 0, status: 'completed' }];
            });
            playAlarmSound();
            speakText(`Albert, the timer "${data.timer.label}" is complete.`);
          } else if (data.type === 'alarm_fired') {
            setTimers((prev) => {
              const exists = prev.some(t => t.id === data.alarm.id);
              if (exists) {
                return prev.map(t => t.id === data.alarm.id ? { ...t, status: 'completed', time_left: 0 } : t);
              }
              return [...prev, { ...data.alarm, time_left: 0, status: 'completed' }];
            });
            playAlarmSound();
            speakText(`Albert, the alarm "${data.alarm.label}" has gone off.`);
          } else if (typeof data.type === 'string' && data.type.startsWith('cf.')) {
            setLastCfEvent(data as CFEvent);
          } else if (data.type === 'inbox_update') {
            window.dispatchEvent(new CustomEvent(INBOX_UPDATE_EVENT, {
              detail: { conversation_id: data.conversation_id, kind: data.kind },
            }));
          } else if (data.type === 'dev_run_event') {
            const runEvent = data as DevRunEvent;
            setLastDevRunEvent(runEvent);
            if (
              devRunNotificationsRef.current &&
              'Notification' in window &&
              Notification.permission === 'granted' &&
              ['awaiting_approval', 'done', 'failed'].includes(runEvent.event)
            ) {
              new Notification(`Dev-run ${runEvent.run_id}`, {
                body: `${runEvent.event}: ${runEvent.summary}`.slice(0, 120),
                icon: '/favicon.svg',
                tag: `dev-run-${runEvent.run_id}`,
              });
            }
          } else if (data.type === 'trace_update') {
            if (data.trace.agent !== 'Router') {
              setMessages((prev) => [...prev, {
                role: 'system',
                content: `⚙️ [${data.trace.agent}] ${data.trace.action}: ${data.trace.message.split('\n')[0]}`
              }]);
            }
          }
        } catch (err) {
          console.error('Error parsing WS frame:', err);
        }
      };
    };

    connectWS();

    return () => {
      isCleanedUp = true;
      if (reconnectTimeoutId) {
        clearTimeout(reconnectTimeoutId);
      }
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // A failed request (401 after the session lapses, or any error payload) answers
  // with an object like {detail: "..."}, not a list. Storing that unguarded is
  // what blanked the whole dashboard: the next render called .map on it, React
  // 19 unmounted the tree on the uncaught error, and all the user saw was the
  // body's CSS gradient. A non-list response leaves the previous value alone.
  const fetchDocuments = () => {
    fetch('/api/documents')
      .then(res => res.json())
      .then(data => { if (Array.isArray(data)) setDocuments(data); })
      .catch(err => console.log('Error fetching documents:', err));
  };

  const fetchMetrics = useCallback(() => {
    setIsMetricsLoading(true);
    fetchWithAuth('/api/metrics')
      .then(res => res.json())
      .then(data => {
        setMetrics(data);
        setIsMetricsLoading(false);
      })
      .catch(err => {
        console.log('Error fetching metrics:', err);
        setIsMetricsLoading(false);
      });
  }, [fetchWithAuth]);

  const fetchChatSessions = () => {
    fetch('/api/history/sessions')
      .then(res => res.json())
      .then(data => {
        if (Array.isArray(data)) {
          setChatSessions(data);
        }
      })
      .catch(err => console.log('Error fetching sessions:', err));
  };

  const getSessionLabel = (id: string) => {
    if (id === 'dashboard') return 'Main Terminal';
    const found = chatSessions.find(s => s.id === id);
    if (found && found.title) return found.title;
    if (id.startsWith('chat_')) {
      const parts = id.split('_');
      if (parts.length >= 3) {
        const namePart = parts.slice(1, -1).join('_');
        const decoded = namePart.replace(/_/g, ' ');
        return decoded.charAt(0).toUpperCase() + decoded.slice(1);
      } else if (parts.length === 2) {
        const ts = parseInt(parts[1], 10);
        if (!isNaN(ts)) {
          return new Date(ts).toLocaleString('ru-RU', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        }
      }
    }
    return id;
  };

  /** Creates the session, then names it, assigns its agent and its folder.
   *
   *  Those three calls used to be addressed to the backend's own host and port
   *  as seen from inside the server. In a browser that is the
   *  *user's own* machine, where nothing answers, so all three failed silently
   *  (they are each wrapped in a try/catch that only logs). The visible result
   *  was a chat that kept the name only because `getSessionLabel` reconstructs
   *  it from the generated id, and an agent choice that never stuck. Everything
   *  here goes through a relative path, which nginx proxies, like the rest of
   *  the app already did. */
  const handleCreateNewSessionConfirm = async (name: string, agentId: string = 'vexa', projectId: string = '') => {
    let sessionId = '';
    const trimmed = name.trim();
    if (trimmed) {
      const sanitized = trimmed.toLowerCase().replace(/[^a-z0-9а-яё_-]/g, '_');
      sessionId = `chat_${sanitized}_${Date.now().toString().slice(-4)}`;
    } else {
      sessionId = `chat_${Date.now()}`;
    }

    try {
      await fetchWithAuth(`/api/history/${sessionId}/agent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: agentId })
      });
    } catch (e) {
      console.error('Error setting session agent on creation:', e);
    }

    if (projectId) {
      try {
        await fetchWithAuth(`/api/history/${sessionId}/project`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id: projectId })
        });
      } catch (e) {
        console.error('Error setting session project on creation:', e);
      }
    }

    if (trimmed) {
      try {
        await fetchWithAuth(`/api/history/${sessionId}/rename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: trimmed })
        });
      } catch (e) {
        console.error('Error renaming session on creation:', e);
      }
    }
    
    fetchChatSessions();
    setTimeout(() => selectChat(sessionId), 100);
  };

  const handleCreateNewSession = () => {
    setNewSessionNameInput('');
    setNewSessionAgentInput('vexa');
    setNewSessionProjectInput('');
    setShowNewSessionModal(true);
  };

  const handleSetSessionAgent = async (sessionId: string, agentId: string) => {
    try {
      const res = await fetchWithAuth(`/api/history/${sessionId}/agent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: agentId })
      });
      if (res.ok) {
        setChatSessions(prev =>
          prev.map(s => s.id === sessionId ? { ...s, agent_id: agentId } : s)
        );
      }
    } catch (e) {
      console.error('Error updating session agent:', e);
    }
  };

  const handleSetSessionProject = async (sessionId: string, projectId: string) => {
    try {
      const res = await fetchWithAuth(`/api/history/${sessionId}/project`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId || null })
      });
      if (res.ok) {
        setChatSessions(prev =>
          prev.map(s => s.id === sessionId ? { ...s, project_id: projectId || null } : s)
        );
      }
    } catch (e) {
      console.error('Error updating session project:', e);
    }
  };

  /** "+" in the plain chat view: a chat exists the moment you click, with no
   *  name-and-agent modal in the way — the conversation names itself from its
   *  first message (see SimpleChatView's auto-title effect). */
  const handleQuickNewChat = useCallback(() => {
    const sessionId = `chat_${Date.now()}`;
    setCurrentChatId(sessionId);
    currentChatIdRef.current = sessionId;
    localStorage.setItem('vexa_current_chat_id', sessionId);
    setMessages([]);
    void fetchWithAuth(`/api/history/${sessionId}/agent`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: 'vexa' }),
    }).then(fetchChatSessions).catch(err => console.error('Error creating chat:', err));
  }, [fetchWithAuth]);

  const handleRenameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      const res = await fetchWithAuth(`/api/history/${sessionId}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      if (res.ok) {
        setChatSessions(prev => prev.map(s => (s.id === sessionId ? { ...s, title } : s)));
      }
    } catch (e) {
      console.error('Error renaming session:', e);
    }
  }, [fetchWithAuth]);

  /** Drops one message from the open conversation, both on screen and on disk. */
  const handleDeleteMessage = useCallback(async (messageId: number) => {
    const chatId = currentChatIdRef.current;
    try {
      const res = await fetchWithAuth(`/api/history/${chatId}/messages/${messageId}`, { method: 'DELETE' });
      if (res.ok) setMessages(prev => prev.filter(m => m.id !== messageId));
    } catch (e) {
      console.error('Error deleting message:', e);
    }
  }, [fetchWithAuth]);

  const fetchSubagents = () => {
    fetch('/api/subagents')
      .then(res => res.json())
      .then(data => { if (Array.isArray(data)) setSubagents(data); })
      .catch(err => console.log('Error fetching subagents:', err));
  };

  const fetchProjects = () => {
    fetchWithAuth('/api/projects')
      .then(res => res.json())
      .then(data => { if (Array.isArray(data)) setProjects(data); })
      .catch(err => console.log('Error fetching projects:', err));
  };

  const selectChat = (chatId: string, currentSubagentsList?: any[]) => {
    const listToSearch = currentSubagentsList || subagents;
    setCurrentChatId(chatId);
    setMessages([]); // clear temporarily
    fetch(`/api/history/${chatId}`)
      .then(res => res.json())
      .then(data => {
        if (data && data.length > 0) {
          setMessages(data);
        } else {
          if (chatId === 'dashboard') {
            setMessages([{ role: 'assistant', content: 'Greetings, Albert. Connection to the Hermes network is complete. Awaiting your instructions.' }]);
          } else {
            const agent = listToSearch.find((a: any) => a.id === chatId);
            if (chatId.startsWith('chat_')) {
              setMessages([{ role: 'assistant', content: 'Conversation initialized, Albert. How can I assist you today?' }]);
            } else {
              setMessages([{ role: 'assistant', content: `Sub-agent session "${agent?.name || chatId}" initialized, Albert. Ready for work.` }]);
            }
          }
        }
      })
      .catch(err => console.error('Error fetching history:', err));
  };

  const handleCreateSubagent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAgentId.trim() || !newAgentName.trim() || !newAgentPrompt.trim()) {
      notify(copy('Please fill in all fields.'));
      return;
    }
    setIsCreatingAgent(true);
    try {
      const cleanId = newAgentId.replace(/[^a-zA-Z0-9_-]/g, '').toLowerCase();
      const res = await fetch('/api/subagents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: cleanId,
          name: newAgentName,
          system_prompt: newAgentPrompt,
          model: newAgentModel,
          skills: newAgentSkills,
          temperature: newAgentTemperature,
        })
      });
      if (res.ok) {
        setNewAgentId('');
        setNewAgentName('');
        setNewAgentPrompt('');
        setNewAgentSkills('');
        setNewAgentTemperature(0.7);
        notify(copy('Sub-agent successfully created.'));
        
        fetch('/api/subagents')
          .then(r => r.json())
          .then(data => {
            setSubagents(data);
            selectChat(cleanId, data);
          });
      } else {
        notify(copy('Failed to create sub-agent.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Error creating sub-agent.'));
    } finally {
      setIsCreatingAgent(false);
    }
  };

  const handleDeleteSubagent = async (id: string) => {
    if (!await confirmAction(language === 'ru' ? 'Удалить агента вместе с историей его чата? Это действие нельзя отменить.' : 'Delete this agent and its chat history? This cannot be undone.')) {
      return;
    }
    try {
      const res = await fetch(`/api/subagents/${id}`, {
        method: 'DELETE'
      });
      if (res.ok) {
        fetchSubagents();
        if (currentChatId === id) {
          selectChat('dashboard');
        }
        notify(copy('Sub-agent deleted.'));
      } else {
        notify(copy('Failed to delete sub-agent.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Error during deletion.'));
    }
  };

  const handleUpdateSubagent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingAgentId || !editAgentName.trim() || !editAgentPrompt.trim()) {
      notify(copy('Please fill in all fields.'));
      return;
    }
    setIsUpdatingAgent(true);
    try {
      const res = await fetch('/api/subagents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: editingAgentId,
          name: editAgentName,
          system_prompt: editAgentPrompt,
          model: editAgentModel,
          skills: editAgentSkills,
          temperature: editAgentTemperature,
        })
      });
      if (res.ok) {
        notify(copy('Sub-agent successfully updated.'));
        fetch('/api/subagents')
          .then(r => r.json())
          .then(data => {
            setSubagents(data);
            selectChat(editingAgentId, data);
          });
      } else {
        notify(copy('Failed to update sub-agent.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Error updating sub-agent.'));
    } finally {
      setIsUpdatingAgent(false);
    }
  };

  const handleCancelTimer = (id: string) => {
    fetch(`/api/timers/${id}`, {
      method: 'DELETE',
    })
      .then(res => res.json())
      .then(data => {
        if (data.status === 'cancelled') {
          setTimers(prev => prev.filter(t => t.id !== id));
        }
      })
      .catch(err => console.error('Error cancelling timer:', err));
  };

  const handleClearActivityLogs = () => {
    fetch('/api/activity/logs', {
      method: 'DELETE'
    })
      .then(res => res.json())
      .then(data => {
        if (data.status === 'success') {
          setActivityLogs([]);
        }
      })
      .catch(err => console.log('Error clearing activity logs:', err));
  };

  const fetchModels = () => {
    const token = localStorage.getItem('vexa_auth_token');
    fetch('/api/models', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then(res => res.json())
      .then(data => {
        if (Array.isArray(data)) {
          setModels(data);
        }
      })
      .catch(err => console.error('Error fetching models:', err));
  };

  // Fetch initial logs & config from REST API as fallback
  useEffect(() => {
    if (!isAuthenticated) return;

    fetch('/api/config')
      .then(res => res.json())
      .then(data => {
        if (data && data.system_prompt !== undefined) {
          setConfig(data);
          setEditedPrompt(data.system_prompt);
          setEditedModel(data.model);
          setEditedRuntimeConfig(data);
        }
      })
      .catch(() => console.log('REST config fetch skipped/failed (using WS instead)'));

    fetch('/api/logs')
      .then(res => res.json())
      .then(data => {
        if (Array.isArray(data)) {
          setLogs(data);
        }
      })
      .catch(() => console.log('REST logs fetch skipped/failed'));
      
    fetchMetrics();
      
    fetchDocuments();
    fetchSubagents();
    fetchProjects();
    fetchChatSessions();
    fetchModels();

    fetchWithAuth('/api/settings')
      .then(res => res.json())
      .then(data => {
        if (data?.language) {
          const nextLanguage = normalizeLanguage(data.language);
          setAppSettings({ language: nextLanguage });
          setLanguageState(nextLanguage);
          // Storing the normalised value, not the raw one, is what stops a bad
          // server response from poisoning this browser for every later visit.
          localStorage.setItem('hermes_language', nextLanguage);
        }
      })
      .catch(() => {});
    
    if (isAuthenticated) {
      const savedChatId = localStorage.getItem('vexa_current_chat_id') || 'dashboard';
      selectChat(savedChatId);
    }
    // These fetch helpers and selectChat are intentionally initialized once per auth session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // Local smooth countdown for timers in state
  useEffect(() => {
    const localTicker = setInterval(() => {
      setTimers(prevTimers =>
        (Array.isArray(prevTimers) ? prevTimers : []).map(timer => {
          if (timer.status === 'running' && timer.time_left > 0) {
            return { ...timer, time_left: timer.time_left - 1 };
          }
          return timer;
        })
      );
    }, 1000);

    return () => clearInterval(localTicker);
  }, []);

  const handleIndexNote = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!noteTitle.trim() || !noteContent.trim()) return;
    setIsIndexing(true);
    try {
      const res = await fetch('/api/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: noteTitle, content: noteContent })
      });
      if (res.ok) {
        setNoteTitle('');
        setNoteContent('');
        notify(copy('Document indexed, Albert.'));
        fetchDocuments();
      } else {
        notify(copy('Index error.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Connection error.'));
    } finally {
      setIsIndexing(false);
    }
  };

  const handleDeleteDocument = async (docId: string) => {
    if (!await confirmAction(language === 'ru' ? 'Удалить документ из долговременной памяти?' : 'Delete document from long-term memory?')) return;
    try {
      const res = await fetch(`/api/documents/${docId}`, {
        method: 'DELETE'
      });
      if (res.ok) {
        fetchDocuments();
        notify(copy('Document deleted.'));
      } else {
        notify(copy('Deletion error.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Connection error.'));
    }
  };

  const handleSearchMemory = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!memorySearchQuery.trim()) return;
    setIsSearchingMemory(true);
    try {
      const res = await fetch(`/api/documents/search?q=${encodeURIComponent(memorySearchQuery)}`);
      if (res.ok) {
        const data = await res.json();
        setMemorySearchResults(data);
      } else {
        notify(copy('Search query error.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Backend connection error.'));
    } finally {
      setIsSearchingMemory(false);
    }
  };

  const handleClearMemorySearch = () => {
    setMemorySearchQuery('');
    setMemorySearchResults(null);
  };

  // Auto-scroll chat to bottom
  useEffect(() => {
    const timer = setTimeout(() => {
      if (mainChatEndRef.current) {
        mainChatEndRef.current.scrollIntoView({ behavior: 'auto' });
      }
      if (subagentChatEndRef.current) {
        subagentChatEndRef.current.scrollIntoView({ behavior: 'auto' });
      }
    }, 50);
    return () => clearTimeout(timer);
  }, [messages, currentChatId, activeTab]);

  // Handle attaching a file to the current chat message.
  // – PDF: sent to backend /api/parse-pdf for server-side text extraction (limit 500 KB text).
  // – Other text formats: read client-side via FileReader (limit 150 KB).
  const handleChatFileAttach = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = ''; // reset so the same file can be reselected after removal
    await attachFile(file);
  };

  /** Shared by the file picker, paste and drag & drop. */
  const attachFile = async (file: File) => {

    const isPdf = file.name.toLowerCase().endsWith('.pdf');

    if (isPdf) {
      // ── PDF path: server-side extraction ─────────────────────────────────────
      const MAX_PDF_MB = 25; // raw file size guard before sending to server
      if (file.size > MAX_PDF_MB * 1024 * 1024) {
        notify(`PDF "${file.name}" exceeds ${MAX_PDF_MB} MB. Please use a smaller file.`);
        return;
      }
      setIsUploading(true);
      try {
        const form = new FormData();
        form.append('file', file);
        const res = await fetchWithAuth('/api/parse-pdf', {
          method: 'POST',
          body: form,
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
          notify(`PDF parse failed: ${err.detail}`);
          return;
        }
        const data = await res.json();
        setAttachedFile({
          name: file.name,
          content: data.text,
          type: 'pdf',
          pages: data.pages,
          truncated: data.truncated,
        });
      } catch (err) {
        console.error('PDF parse error:', err);
        notify(copy('Could not connect to backend to parse PDF.'));
      } finally {
        setIsUploading(false);
      }
    } else {
      // ── Text path: client-side FileReader ────────────────────────────────────
      const MAX_TEXT_BYTES = 150 * 1024; // 150 KB
      if (file.size > MAX_TEXT_BYTES) {
        notify(`File "${file.name}" is too large for inline chat context (max 150 KB). Use the Memory tab to index it into the knowledge base instead.`);
        return;
      }
      const reader = new FileReader();
      reader.onload = (evt) => {
        const content = evt.target?.result as string;
        setAttachedFile({ name: file.name, content, type: 'text' });
      };
      reader.onerror = () => {
        notify(`Failed to read file "${file.name}".`);
      };
      reader.readAsText(file, 'utf-8');
    }
  };

  // Send message through WebSocket
  const handleSendMessage = (e: React.FormEvent, options?: { agentId?: string | null; text?: string }) => {
    e.preventDefault();
    // The attachment used to be shown in the composer and then dropped: only
    // the typed text was ever sent. It now travels in the same shape the
    // messenger bots use (backend/document_extract.build_document_prompt).
    const typed = options?.text ?? inputValue;
    const text = attachedFile ? composeWithAttachment(typed, attachedFile) : typed;
    if (sendChatText(text, undefined, { agentId: options?.agentId })) {
      setInputValue('');
      setAttachedFile(null);
    }
  };

  const handleVexaCommand = useCallback((text: string) => {
    if (currentChatIdRef.current !== 'dashboard') {
      currentChatIdRef.current = 'dashboard';
      setCurrentChatId('dashboard');
      localStorage.setItem('vexa_current_chat_id', 'dashboard');
    }
    return sendChatText(text, 'dashboard');
  }, [sendChatText]);

  const handleStopGeneration = useCallback(() => {
    const chatId = currentChatIdRef.current;
    const runId = generatingRunsRef.current[chatId];
    if (runId) {
      const token = localStorage.getItem('vexa_auth_token');
      void fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
    }
    clearGenerating(chatId);
    stopSpeech();
  }, [stopSpeech, clearGenerating]);

  // Re-send the last user message for the current session (P0 UX "Retry").
  const handleRetryLast = useCallback(() => {
    const last = lastUserMessageRef.current[currentChatIdRef.current];
    if (last) {
      sendChatText(last);
    }
  }, [sendChatText]);

  const handleClearChat = async () => {
    if (!await confirmAction(language === 'ru' ? 'Удалить всю историю этого чата? Это действие нельзя отменить.' : 'Delete all messages in this chat? This cannot be undone.')) return;
    
    try {
      const res = await fetch(`/api/history/${currentChatId}`, {
        method: 'DELETE'
      });
      if (res.ok) {
        fetchChatSessions();
        if (currentChatId === 'dashboard') {
          setMessages([{ role: 'assistant', content: 'Greetings, Albert. Connection to the Hermes network is complete. Awaiting your instructions.' }]);
        } else {
          const agent = subagents.find((a: any) => a.id === currentChatId);
          setMessages([{ role: 'assistant', content: `Sub-agent session "${agent?.name || currentChatId}" cleared, Albert. Ready for work.` }]);
        }
      }
    } catch(e) {
      notify(copy('Connection error.'));
      console.error('Error clearing history:', e);
    }
  };

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingConfig(true);
    try {
      const response = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          system_prompt: editedPrompt,
          model: editedModel,
          provider: editedRuntimeConfig.provider,
          api_base: editedRuntimeConfig.api_base,
          local_base_url: editedRuntimeConfig.local_base_url,
          openai_api_base: editedRuntimeConfig.openai_api_base,
          local_num_ctx: editedRuntimeConfig.local_num_ctx,
          local_think: editedRuntimeConfig.local_think,
          fast_mode: editedRuntimeConfig.fast_mode,
          max_history_len: editedRuntimeConfig.max_history_len,
          condenser_enabled: editedRuntimeConfig.condenser_enabled,
          condense_trigger_extra: editedRuntimeConfig.condense_trigger_extra,
          max_tokens: editedRuntimeConfig.max_tokens,
          tool_max_tokens: editedRuntimeConfig.tool_max_tokens,
          temperature: editedRuntimeConfig.temperature,
          auto_rag: editedRuntimeConfig.auto_rag,
          memory_enabled: editedRuntimeConfig.memory_enabled,
          memory_auto_save: editedRuntimeConfig.memory_auto_save,
          memory_max_items: editedRuntimeConfig.memory_max_items,
          telegram_reply_mode: editedRuntimeConfig.telegram_reply_mode
        })
      });
      if (response.ok) {
        const data = await response.json();
        setConfig(data.config);
        setEditedRuntimeConfig(data.config);
        setEditedModel(data.config.model);
        fetchModels();
        notify(copy('System configuration updated, Albert.'));
      } else {
        notify(copy('Error updating configuration.'));
      }
    } catch (err) {
      console.error(err);
      notify(copy('Connection error with backend server.'));
    } finally {
      setIsSavingConfig(false);
    }
  };

  if (!isAuthenticated) {
    return (
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        minHeight: '100vh',
        width: '100vw',
        background: 'radial-gradient(circle at center, #141820 0%, #06070b 100%)',
        color: '#e4e8ed',
        fontFamily: 'Inter, sans-serif',
        padding: '20px',
        boxSizing: 'border-box'
      }} className="scanlines">
        <div style={{
          width: '100%',
          maxWidth: '400px',
          padding: '40px 30px',
          borderRadius: '16px',
          boxShadow: '0 0 40px rgba(54, 207, 251, 0.075)',
          border: '1px solid rgba(157, 166, 181, 0.14)',
          textAlign: 'center'
        }} className="glass-panel">
          
          <div className="hermes-auth-brand" style={{ marginBottom: '30px' }}>
            <HermesMark className="hermes-auth-mark" />
            <h1 className="glow-text-cyan" style={{ fontSize: '2.5rem', fontWeight: 800, letterSpacing: 0, margin: 0 }}>HERMES</h1>
            <p style={{ color: '#36cffb', fontSize: '0.9rem', letterSpacing: 0, margin: '4px 0 0', textTransform: 'uppercase' }}>{copy("Secure Access Link")}</p>
          </div>

          <p style={{ color: '#99a2b1', fontSize: '0.95rem', lineHeight: '1.6', marginBottom: '20px' }}>
            {copy("Albert, identity confirmation is required to access the management console.")}
          </p>

          {loginMode === 'password' ? (
            <form onSubmit={handlePasswordLogin} style={{ display: 'flex', flexDirection: 'column', gap: '14px', marginBottom: '10px' }}>
              <input
                type="text"
                value={pwUsername}
                onChange={(e) => setPwUsername(e.target.value)}
                placeholder={copy("Username")}
                aria-label={copy("Username")}
                autoComplete="username"
                style={{
                  width: '100%', padding: '14px', borderRadius: '8px',
                  border: '1px solid rgba(157, 166, 181, 0.21)', background: 'rgba(20, 24, 32, 0.6)',
                  color: '#fff', fontSize: '1rem', outline: 'none', boxSizing: 'border-box'
                }}
                disabled={pwStatus === 'submitting'}
                autoFocus
              />
              <input
                type="password"
                value={pwPassword}
                onChange={(e) => setPwPassword(e.target.value)}
                placeholder={copy("Password")}
                aria-label={copy("Password")}
                autoComplete="current-password"
                style={{
                  width: '100%', padding: '14px', borderRadius: '8px',
                  border: '1px solid rgba(157, 166, 181, 0.21)', background: 'rgba(20, 24, 32, 0.6)',
                  color: '#fff', fontSize: '1rem', outline: 'none', boxSizing: 'border-box'
                }}
                disabled={pwStatus === 'submitting'}
              />
              {pwError && (
                <p style={{ color: '#f05d75', fontSize: '0.85rem', margin: 0 }}>⚠️ {pwError}</p>
              )}
              <button
                type="submit"
                disabled={pwStatus === 'submitting'}
                style={{
                  width: '100%', padding: '14px', borderRadius: '8px', border: '1px solid #9da6b5b2',
                  background: 'rgba(54, 207, 251, 0.1)', color: '#36cffb', fontWeight: 600,
                  fontSize: '1rem', cursor: pwStatus === 'submitting' ? 'default' : 'pointer', transition: 'all 0.2s',
                }}
                className="glow-btn-cyan"
              >
                {pwStatus === 'submitting' ? copy('Signing in...') : copy('Sign in')}
              </button>
              <button
                type="button"
                onClick={() => { setLoginMode('telegram'); setPwError(''); setPwStatus('idle'); }}
                style={{ background: 'none', border: 'none', color: '#6c7381', fontSize: '0.85rem', cursor: 'pointer', textDecoration: 'underline', marginTop: '4px' }}
              >
                {copy("Use Telegram code instead")}
              </button>
            </form>
          ) : (authStatus === 'idle' || (authStatus === 'error' && !otpSent)) && (
            <>
              <button
                onClick={handleRequestOtp}
                style={{
                  width: '100%',
                  padding: '14px',
                  borderRadius: '8px',
                  border: '1px solid #9da6b5b2',
                  background: 'rgba(54, 207, 251, 0.1)',
                  color: '#36cffb',
                  fontWeight: 600,
                  fontSize: '1rem',
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
                className="glow-btn-cyan"
              >
                {copy("Request code in Telegram")}
              </button>
              <button
                type="button"
                onClick={() => { setLoginMode('password'); setAuthError(''); }}
                style={{ background: 'none', border: 'none', color: '#6c7381', fontSize: '0.85rem', cursor: 'pointer', textDecoration: 'underline', marginTop: '14px' }}
              >
                {copy("Sign in with username & password")}
              </button>
            </>
          )}

          {loginMode === 'telegram' && !otpSent && authError && <p role="alert" style={{ color: '#f05d75' }}>{authError}</p>}
          {loginMode === 'telegram' && authStatus === 'sending' && (
            <p style={{ color: '#36cffb', fontSize: '0.95rem' }}>{copy("Initializing session and sending code...")}</p>
          )}

          {loginMode === 'telegram' && otpSent && (authStatus === 'sent' || authStatus === 'verifying' || authStatus === 'error') && (
            <form onSubmit={handleVerifyOtp} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <p style={{ color: '#35d49a', fontSize: '0.85rem', margin: '0 0 10px' }}>
                {copy("\u2713 Authorization code sent to your trusted Telegram chat.")}
              </p>
              
              <input
                type="text"
                maxLength={6}
                aria-label={language === 'ru' ? 'Код из Telegram' : 'Telegram code'}
                inputMode="numeric"
                autoComplete="one-time-code"
                value={otpCode}
                onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ''))}
                placeholder="0 0 0 0 0 0"
                style={{
                  width: '100%',
                  padding: '16px',
                  borderRadius: '8px',
                  border: '1px solid rgba(157, 166, 181, 0.21)',
                  background: 'rgba(20, 24, 32, 0.6)',
                  color: '#fff',
                  fontSize: '1.5rem',
                  letterSpacing: '12px',
                  textAlign: 'center',
                  outline: 'none',
                  boxSizing: 'border-box'
                }}
                disabled={authStatus === 'verifying'}
                autoFocus
              />

              {authError && (
                <p style={{ color: '#f05d75', fontSize: '0.85rem', margin: 0 }}>
                  ⚠️ {authError}
                </p>
              )}

              {authStatus === 'verifying' && (
                <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', padding: '10px 0' }}>
                  <span className="pulse-dot" style={{ width: 8, height: 8, background: '#36cffb', boxShadow: '0 0 6px #36cffb80' }} />
                  <span style={{ color: '#36cffb', fontSize: '0.9rem', fontFamily: 'var(--font-mono)' }}>{copy("VERIFYING CODE...")}</span>
                </div>
              )}

              <button
                type="button"
                onClick={handleRequestOtp}
                disabled={authStatus === 'verifying'}
                style={{
                  background: 'none',
                  border: 'none',
                  color: '#6c7381',
                  fontSize: '0.85rem',
                  cursor: 'pointer',
                  textDecoration: 'underline',
                  marginTop: '10px'
                }}
              >
                {copy("Resend code")}
              </button>
            </form>
          )}
        </div>
      </div>
    );
  }

  const vexaImmersive = activeTab === 'vexa' && vexaUiMode === 'immersive';

  return (
    <div className={`app-container scanlines${activeTab === 'vexa' ? (vexaUiMode === 'immersive' ? ' is-vexa-mode' : ' is-vexa-simple-mode') : ''}`}>
      {/* Mobile Menu Toggle Button */}
      <button 
        aria-label={language === 'ru' ? 'Меню разделов' : 'Navigation menu'}
        aria-expanded={sidebarOpen}
        aria-controls="main-sidebar"
        onClick={() => setSidebarOpen(!sidebarOpen)}
        className="mobile-menu-btn"
        style={{
          position: 'fixed',
          top: '16px',
          left: '16px',
          zIndex: 1100,
          background: 'rgba(15, 18, 24, 0.8)',
          border: '1px solid rgba(157, 166, 181, 0.21)',
          color: 'var(--accent-cyan)',
          padding: '8px',
          borderRadius: '8px',
          cursor: 'pointer',
          display: 'none',
          alignItems: 'center',
          justifyContent: 'center',
          boxShadow: '0 0 10px rgba(94, 225, 255, 0.05)',
          backdropFilter: 'blur(4px)'
        }}
      >
        {sidebarOpen ? <X size={20} /> : <Menu size={20} />}
      </button>

      {/* Sidebar Overlay for Mobile */}
      {sidebarOpen && (
        <div 
          onClick={() => setSidebarOpen(false)}
          className="sidebar-overlay"
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            width: '100vw',
            height: '100vh',
            backgroundColor: 'rgba(8, 9, 14, 0.6)',
            backdropFilter: 'blur(4px)',
            zIndex: 999,
            display: 'none'
          }}
        />
      )}

      {/* 1. Left Sidebar — always in flow, including on the Vexa dashboard. The dashboard
          grid sizes itself from the space that is left over (container queries in
          vexa.css), so collapsing the rail simply gives the core more room. */}
      <aside
        id="main-sidebar"
        style={{ ...styles.sidebar, ...(isSidebarCollapsed ? styles.sidebarCollapsed : {}) }}
        className={`glass-panel sidebar ${sidebarOpen ? 'sidebar-open' : ''} ${isSidebarCollapsed ? 'sidebar-collapsed' : ''}`}
      >
        <button
          type="button"
          onClick={toggleSidebar}
          style={styles.sidebarToggle}
          title={isSidebarCollapsed ? 'Развернуть меню' : 'Свернуть меню до иконок'}
          aria-label={isSidebarCollapsed ? 'Развернуть меню' : 'Свернуть меню до иконок'}
        >
          {isSidebarCollapsed ? <ChevronsRight size={17} /> : <ChevronsLeft size={17} />}
        </button>
        <div style={styles.logoArea}>
          <VexaLogo className="hermes-logo-mark" />
          <h1 className="glow-text-cyan sidebar-title" style={styles.logoTitle}>VEXA</h1>
        </div>
        <p className="sidebar-subtitle" style={styles.logoSubtitle}>{t('appSubtitle')}</p>
        
        <nav style={{ ...styles.navMenu, ...(isSidebarCollapsed ? styles.navMenuCollapsed : {}) }}>
          <button
            style={navStyle('vexa')}
            onClick={() => {
              selectChat('dashboard');
              setActiveTab('vexa');
              setSidebarOpen(false);
            }}
            title={t('navVexa')}
          >
            <AudioWaveform size={18} />
            <span>{t('navVexa')}</span>
          </button>

          <button
            style={navStyle('processes')}
            onClick={() => { setActiveTab('processes'); setSidebarOpen(false); }}
            title={t('navProcesses')}
          >
            <ShieldCheck size={18} />
            <span>{t('navProcesses')}</span>
          </button>

          <button
            style={navStyle('metrics')}
            onClick={() => { setActiveTab('metrics'); setSidebarOpen(false); }}
            title={t('navMetrics')}
          >
            <BarChart3 size={18} />
            <span>{t('navMetrics')}</span>
          </button>

          <button
            style={navStyle('devruns')}
            onClick={() => { setActiveTab('devruns'); setSidebarOpen(false); }}
            title="Dev Runs"
          >
            <Terminal size={18} />
            <span>Dev Runs</span>
          </button>

          <button
            style={navStyle('kanban')}
            onClick={() => { setActiveTab('kanban'); setSidebarOpen(false); }}
            title={t('navKanban')}
          >
            <Trello size={18} />
            <span>{t('navKanban')}</span>
          </button>

          <button
            style={navStyle('clients')}
            onClick={() => { setActiveTab('clients'); setSidebarOpen(false); }}
            title={t('navClients')}
          >
            <Briefcase size={18} />
            <span>{t('navClients')}</span>
            {overdueTaskCount > 0 && !isSidebarCollapsed && (
              <span style={{
                marginLeft: 'auto', minWidth: 16, padding: '1px 5px', borderRadius: 9,
                background: 'rgba(246, 184, 74, 0.22)', color: '#f6b84a',
                font: '700 0.62rem var(--font-mono)', textAlign: 'center',
              }}>{overdueTaskCount}</span>
            )}
          </button>

          <button
            style={navStyle('agents')}
            onClick={() => { setActiveTab('agents'); setSidebarOpen(false); }}
            title={t('navAgents')}
          >
            <UserCog size={18} />
            <span>{t('navAgents')}</span>
          </button>

          <button
            style={navStyle('schedule')}
            onClick={() => { setActiveTab('schedule'); setSidebarOpen(false); }}
            title="Schedules & Automation"
          >
            <Clock size={18} />
            <span>Schedules & Automation</span>
          </button>

          <button
            style={navStyle('contentfactory')}
            onClick={() => { setActiveTab('contentfactory'); setSidebarOpen(false); }}
            title="Content Factory"
          >
            <Factory size={18} />
            <span>Content Factory</span>
            {!isSidebarCollapsed && (
              <span style={{
                marginLeft: 'auto', fontSize: 9, fontWeight: 700, letterSpacing: 0.6,
                padding: '2px 6px', borderRadius: 4, color: '#141820', background: '#36cffb',
              }}>NEW</span>
            )}
          </button>

          <button
            style={navStyle('smm')}
            onClick={() => { setActiveTab('smm'); setSidebarOpen(false); }}
            title={t('navSmmFactory')}
          >
            <Megaphone size={18} />
            <span>{t('navSmmFactory')}</span>
            {inboxUnread > 0 && !isSidebarCollapsed && (
              <span style={{
                marginLeft: 'auto', minWidth: 16, padding: '1px 5px', borderRadius: 9,
                background: 'rgba(124, 92, 252, 0.28)', color: '#B197FF',
                font: '700 0.62rem var(--font-mono)', textAlign: 'center',
              }} aria-label={`${inboxUnread} диалогов с непрочитанными`}>{inboxUnread}</span>
            )}
          </button>

          <button
            style={navStyle('network')}
            onClick={() => { setActiveTab('network'); setSidebarOpen(false); }}
            title={t('navArchitecture')}
          >
            <Network size={18} />
            <span>{t('navArchitecture')}</span>
          </button>
          
          {routerDashboardUrl && (
            <a
              style={{ ...styles.navBtn, ...(isSidebarCollapsed ? styles.navBtnCollapsed : {}), textDecoration: 'none' }}
              href={routerDashboardUrl}
              target="_blank"
              rel="noreferrer"
              onClick={() => setSidebarOpen(false)}
              title="OmniRoute"
            >
              <Shuffle size={18} />
              <span>OmniRoute</span>
              {!isSidebarCollapsed && <ExternalLink size={12} style={{ marginLeft: 'auto', opacity: 0.6 }} />}
            </a>
          )}

          <button
            style={navStyle('settings')}
            onClick={() => { setActiveTab('settings'); setSidebarOpen(false); }}
            title={t('navSettings')}
          >
            <Settings size={18} />
            <span>{t('navSettings')}</span>
          </button>
        </nav>

        {/* Sidebar Status Info */}
        <div style={styles.statusBox} className="glass-panel sidebar-status">
          <div style={styles.statusRow}>
            <span style={styles.statusLabel}>{t('network')}:</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className={`pulse-dot ${isConnected ? '' : 'danger'}`} />
              <span style={{ fontSize: '0.85rem', color: isConnected ? 'var(--success)' : 'var(--danger)' }}>
                {isConnected ? t('connected') : t('disconnected')}
              </span>
            </div>
          </div>
          
          <div style={styles.statusRow}>
            <span style={styles.statusLabel}>{t('llmCore')}:</span>
            <div style={styles.modelTag}>
              <Cpu size={12} style={{ color: 'var(--accent-cyan)' }} />
              <span style={styles.modelName}>{config.model.split('/').pop()}</span>
            </div>
          </div>
          
          <div style={styles.statusRow}>
            <span style={styles.statusLabel}>{t('callLogs')}:</span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.85rem', color: 'var(--accent-cyan)' }}>
              {logs.length} logs
            </span>
          </div>
        </div>
      </aside>

      {/* 2. Main Workspace */}
      <main
        style={styles.mainContent}
        className={
          activeTab === 'vexa'
            ? (vexaUiMode === 'immersive' ? 'vexa-main' : 'simple-chat-page')
            : undefined
        }
      >
        <ErrorBoundary key={`${activeTab}:${settingsSection}`} compact>
        <Suspense fallback={<div role="status" className="page-loading">{language === 'ru' ? 'Загрузка раздела…' : 'Loading section…'}</div>}>
        {activeTab === 'vexa' && (
          <>
            {vexaUiMode === 'simple' ? (
              <SimpleChatView
                language={language}
                messages={messages}
                inputValue={inputValue}
                setInputValue={setInputValue}
                isGenerating={isGenerating}
                onStopGeneration={handleStopGeneration}
                handleSendMessage={handleSendMessage}
                isConnected={isConnected}
                micState={micState}
                onVoiceToggle={handleVoiceToggle}
                chatSessions={chatSessions}
                currentChatId={currentChatId}
                selectChat={selectChat}
                handleCreateNewSession={handleCreateNewSession}
                getSessionLabel={getSessionLabel}
                fetchChatSessions={fetchChatSessions}
                mainChatEndRef={mainChatEndRef}
                attachedFile={attachedFile}
                setAttachedFile={setAttachedFile}
                handleChatFileAttach={handleChatFileAttach}
                isUploading={isUploading}
                onSwitchToImmersive={() => setVexaUiMode('immersive')}
                isTTSEnabled={isTTSEnabled}
                setIsTTSEnabled={setIsTTSEnabled}
                isSpeaking={isSpeaking}
                setIsSpeaking={setIsSpeaking}
                projects={projects}
                fetchProjects={fetchProjects}
                handleSetSessionProject={handleSetSessionProject}
                modelSelection={modelSelection}
                onModelSelect={handleModelSelect}
                onSendText={(text, options) => sendChatText(text, undefined, options)}
                onSetSessionAgent={handleSetSessionAgent}
                onAttachFile={attachFile}
                onRegenerate={handleRetryLast}
                onDeleteMessage={handleDeleteMessage}
                onRenameSession={handleRenameSession}
                onQuickNewChat={handleQuickNewChat}
                speakText={speakText}
                playingMsgIndex={playingMsgIndex}
              />
            ) : (
              <>
                <VexaCommandCenter
                  agents={subagents}
                  messages={messages}
                  isConnected={isConnected}
                  isGenerating={isGenerating}
                  isSpeaking={isSpeaking}
                  isTtsPending={isTtsPending}
                  playingMsgIndex={playingMsgIndex}
                  onSpeakMessage={speakText}
                  onStopSpeech={stopSpeech}
                  micState={micState}
                  micErrorMessage={micErrorMessage}
                  voiceIdleTick={voiceIdleTick}
                  onVoiceToggle={handleVoiceToggle}
                  onCommand={handleVexaCommand}
                  onStop={handleStopGeneration}
                  language={language}
                  micStreamRef={voiceStreamRef}
                  ttsAudioElRef={ttsAudioRef}
                  onOpenAgentChat={(agentId) => {
                    if (agentId) selectChat(agentId);
                    setVexaTranscriptOpen(true);
                  }}
                  chatSessions={chatSessions}
                  currentChatId={currentChatId}
                  getSessionLabel={getSessionLabel}
                  onCreateSession={handleCreateNewSession}
                  onSwitchToSimpleMode={() => setVexaUiMode('simple')}
                  fetchAgents={fetchSubagents}
                  onNavigate={(route) => {
                    // The dashboard's bottom navigation and quick-action icons open their
                    // destination as a floating window over the dashboard, rather than
                    // navigating away from it — "terminal" just closes whichever is open.
                    setVexaFloatingPanel(route === 'terminal' ? null : route);
                  }}
                />
                {vexaTranscriptOpen && (
                  <FloatingWindow
                    title={t('navVexa')}
                    subtitle={getSessionLabel(currentChatId)}
                    storageKey="hermes_vexa_channel_window"
                    onClose={() => setVexaTranscriptOpen(false)}
                    labels={{
                      minimize: t('vexaWindowMinimize'),
                      restore: t('vexaWindowRestore'),
                      fullscreen: t('vexaWindowFullscreen'),
                      exitFullscreen: t('vexaWindowExitFullscreen'),
                      close: t('vexaWindowClose'),
                    }}
                  >
                    <ChatTab
                      currentChatId={currentChatId}
                      chatSessions={chatSessions}
                      messages={messages}
                      inputValue={inputValue}
                      setInputValue={setInputValue}
                      isSpeaking={isSpeaking}
                      setIsSpeaking={setIsSpeaking}
                      micState={micState}
                      micErrorMessage={micErrorMessage}
                      micEnabled={micEnabled}
                      onVoiceToggle={handleVoiceToggle}
                      isTTSEnabled={isTTSEnabled}
                      setIsTTSEnabled={setIsTTSEnabled}
                      isGenerating={isGenerating}
                      playingMsgIndex={playingMsgIndex}
                      setPlayingMsgIndex={setPlayingMsgIndex}
                      config={config}
                      isConnected={isConnected}
                      isUploading={isUploading}
                      attachedFile={attachedFile}
                      setAttachedFile={setAttachedFile}
                      speakText={speakText}
                      handleClearChat={handleClearChat}
                      handleSendMessage={handleSendMessage}
                      handleChatFileAttach={handleChatFileAttach}
                      selectChat={selectChat}
                      handleCreateNewSession={handleCreateNewSession}
                      fetchChatSessions={fetchChatSessions}
                      getSessionLabel={getSessionLabel}
                      mainChatEndRef={mainChatEndRef}
                      t={t}
                      onStopGeneration={handleStopGeneration}
                      onRetryLast={handleRetryLast}
                      hasLastUserMessage={messages.some(message => message.role === 'user')}
                      onChangeModel={() => { setActiveTab('settings'); setSettingsSection('config'); }}
                      subagents={subagents}
                      handleSetSessionAgent={handleSetSessionAgent}
                      projects={projects}
                      handleSetSessionProject={handleSetSessionProject}
                      fetchProjects={fetchProjects}
                      activeDevRun={lastDevRunEvent}
                      onOpenDevRuns={() => { setVexaTranscriptOpen(false); setActiveTab('devruns'); }}
                    />
                  </FloatingWindow>
                )}
                {vexaFloatingPanel && vexaFloatingPanel !== 'settings' && (
                  <FloatingWindow
                    title={
                      vexaFloatingPanel === 'analytics' ? t('navAnalytics')
                        : vexaFloatingPanel === 'agents' ? t('navAgents') : t('navProcesses')
                    }
                    storageKey={`hermes_vexa_panel_${vexaFloatingPanel}_window`}
                    onClose={() => setVexaFloatingPanel(null)}
                    labels={{
                      minimize: t('vexaWindowMinimize'),
                      restore: t('vexaWindowRestore'),
                      fullscreen: t('vexaWindowFullscreen'),
                      exitFullscreen: t('vexaWindowExitFullscreen'),
                      close: t('vexaWindowCloseGeneric'),
                    }}
                  >
                    {vexaFloatingPanel === 'analytics' && (
                      <MetricsTab metrics={metrics} isLoading={isMetricsLoading} onRefresh={fetchMetrics} />
                    )}
                    {vexaFloatingPanel === 'agents' && (
                      <AgentsAdminTab agents={subagents} models={models} fetchAgents={fetchSubagents} t={t} />
                    )}
                    {vexaFloatingPanel === 'protocols' && <ProcessesTab language={language} />}
                  </FloatingWindow>
                )}
              </>
            )}
          </>
        )}

        {/* The Vexa dashboard carries its own approvals badge and emergency stop in its
            header, so the floating one would duplicate them there. */}
        {!vexaImmersive && (
          <AppHeader
            language={language}
            onOpenProcesses={() => setActiveTab('processes')}
            onOpenBrowserView={() => setBrowserViewOpen(true)}
          />
        )}

        {browserViewOpen && (
          <FloatingWindow
            title={t('browserViewTitle')}
            storageKey="hermes_browser_view_window"
            onClose={() => setBrowserViewOpen(false)}
            labels={{
              minimize: t('vexaWindowMinimize'),
              restore: t('vexaWindowRestore'),
              fullscreen: t('vexaWindowFullscreen'),
              exitFullscreen: t('vexaWindowExitFullscreen'),
              close: t('vexaWindowCloseGeneric'),
            }}
          >
            <BrowserLiveView language={language} />
          </FloatingWindow>
        )}

        {activeTab === 'processes' && <ProcessesTab language={language} />}

        {activeTab === 'devruns' && (
          <DevRunsTab language={language} lastEvent={lastDevRunEvent} />
        )}

        {activeTab === 'kanban' && (
          <KanbanTab language={language} lastEvent={lastDevRunEvent} agents={subagents} />
        )}

        {activeTab === 'clients' && (
          <ClientsTab onOpenAgent={() => { setActiveTab('agents'); }} />
        )}

        {activeTab === 'agents' && (
          <AgentsAdminTab
            agents={subagents}
            models={models}
            fetchAgents={fetchSubagents}
            t={t}
          />
        )}

        {/* LLM run/cost metrics stay in the Vexa analytics panel; this tab is the GPU server. */}
        {activeTab === 'metrics' && <ServerMetricsTab language={language} />}

        {activeTab === 'schedule' && (
          <ScheduleTab
            timers={timers}
            subagents={subagents}
            handleCancelTimer={handleCancelTimer}
          />
        )}

        {activeTab === 'contentfactory' && (
          <ContentFactoryTab language={language} lastEvent={lastCfEvent} onOpenAgents={() => setActiveTab('agents')} />
        )}

        {activeTab === 'smm' && (
          <SMMFactoryTab agents={subagents} unreadDialogs={inboxUnread} onOpenClient={() => setActiveTab('clients')} />
        )}

        {activeTab === 'network' && (
          <NetworkTab subagents={subagents} setSubagents={setSubagents} fetchSubagents={fetchSubagents} models={models} />
        )}

        {(activeTab === 'settings' || vexaFloatingPanel === 'settings') && (() => {
          const settingsBody = (
          <div style={styles.tabWrapper}>
            <nav className="admin-subnav">
              <button type="button" className={settingsSection === 'config' ? 'is-active' : ''} onClick={() => setSettingsSection('config')}>
                <Settings size={15} />
                <span>{t('navConfig')}</span>
              </button>
              <button type="button" className={settingsSection === 'currency' ? 'is-active' : ''} onClick={() => setSettingsSection('currency')}>
                <Coins size={15} />
                <span>{t('navCurrency')}</span>
              </button>
              <button type="button" className={settingsSection === 'keys' ? 'is-active' : ''} onClick={() => setSettingsSection('keys')}>
                <KeyRound size={15} />
                <span>{t('navApiKeys')}</span>
              </button>
              <button type="button" className={settingsSection === 'subagents' ? 'is-active' : ''} onClick={() => { setSettingsSection('subagents'); selectChat(currentChatId === 'dashboard' ? 'dashboard' : currentChatId); }}>
                <Layers size={15} />
                <span>{t('navSubagents')}</span>
              </button>
              <button type="button" className={settingsSection === 'mcp' ? 'is-active' : ''} onClick={() => setSettingsSection('mcp')}>
                <Server size={15} />
                <span>{t('navMcp')}</span>
              </button>
              <button type="button" className={settingsSection === 'browser' ? 'is-active' : ''} onClick={() => setSettingsSection('browser')}>
                <Globe size={15} />
                <span>{language === 'ru' ? 'Браузер' : 'Browser'}</span>
              </button>
              <button type="button" className={settingsSection === 'obsidian' ? 'is-active' : ''} onClick={() => setSettingsSection('obsidian')}>
                <BookOpen size={15} />
                <span>{t('navObsidian')}</span>
              </button>
              <button type="button" className={settingsSection === 'memory' ? 'is-active' : ''} onClick={() => setSettingsSection('memory')}>
                <Database size={15} />
                <span>{t('navMemory')}</span>
              </button>
              <button type="button" className={settingsSection === 'logs' ? 'is-active' : ''} onClick={() => setSettingsSection('logs')}>
                <Terminal size={15} />
                <span>{t('navLogs')}</span>
              </button>
              <button type="button" className={settingsSection === 'activity' ? 'is-active' : ''} onClick={() => setSettingsSection('activity')}>
                <Activity size={15} />
                <span>{t('navActivity')}</span>
              </button>
            </nav>

            {settingsSection === 'config' && (
              <ConfigTab
                editedModel={editedModel}
                setEditedModel={setEditedModel}
                editedPrompt={editedPrompt}
                setEditedPrompt={setEditedPrompt}
                isSavingConfig={isSavingConfig}
                handleSaveConfig={handleSaveConfig}
                models={models}
                runtimeConfig={editedRuntimeConfig}
                setRuntimeConfig={setEditedRuntimeConfig}
                language={language}
                onLanguageChange={(nextLanguage) => setLanguage(nextLanguage as Language)}
                devRunNotificationsEnabled={devRunNotificationsEnabled}
                onDevRunNotificationsChange={(enabled) => {
                  localStorage.setItem('hermes_devrun_notifications', enabled ? '1' : '0');
                  setDevRunNotificationsEnabled(enabled);
                }}
                activeModel={config.model}
                onModelActivated={(nextConfig) => {
                  setConfig(prev => ({ ...prev, ...nextConfig }));
                  setEditedRuntimeConfig(prev => ({ ...prev, ...nextConfig }));
                  if (nextConfig.model) setEditedModel(nextConfig.model);
                }}
              />
            )}

            {settingsSection === 'currency' && <CurrencySettingsTab />}

            {settingsSection === 'browser' && <BrowserSessionsTab language={language === 'ru' ? 'ru' : 'en'} />}

            {settingsSection === 'keys' && <ApiKeysTab />}

            {settingsSection === 'logs' && (
              <LogsTab
                logs={logs}
                selectedLog={selectedLog}
                setSelectedLog={setSelectedLog}
              />
            )}

            {settingsSection === 'activity' && (
              <ActivityTab
                isGenerating={isAnyGenerating}
                activityLogs={activityLogs}
                handleClearActivityLogs={handleClearActivityLogs}
              />
            )}

            {settingsSection === 'memory' && (
              <MemoryTab
                noteTitle={noteTitle}
                setNoteTitle={setNoteTitle}
                noteContent={noteContent}
                setNoteContent={setNoteContent}
                isIndexing={isIndexing}
                documents={documents}
                memorySearchQuery={memorySearchQuery}
                setMemorySearchQuery={setMemorySearchQuery}
                isSearchingMemory={isSearchingMemory}
                memorySearchResults={memorySearchResults}
                handleIndexNote={handleIndexNote}
                handleSearchMemory={handleSearchMemory}
                handleClearMemorySearch={handleClearMemorySearch}
                handleDeleteDocument={handleDeleteDocument}
              />
            )}

            {settingsSection === 'subagents' && (
              <SubagentsTab
                currentChatId={currentChatId}
                subagents={subagents}
                messages={messages}
                inputValue={inputValue}
                setInputValue={setInputValue}
                isSpeaking={isSpeaking}
                setIsSpeaking={setIsSpeaking}
                isGenerating={isGenerating}
                playingMsgIndex={playingMsgIndex}
                setPlayingMsgIndex={setPlayingMsgIndex}
                config={config}
                isConnected={isConnected}
                newAgentId={newAgentId}
                setNewAgentId={setNewAgentId}
                newAgentName={newAgentName}
                setNewAgentName={setNewAgentName}
                newAgentPrompt={newAgentPrompt}
                setNewAgentPrompt={setNewAgentPrompt}
                newAgentModel={newAgentModel}
                setNewAgentModel={setNewAgentModel}
                newAgentSkills={newAgentSkills}
                setNewAgentSkills={setNewAgentSkills}
                newAgentTemperature={newAgentTemperature}
                setNewAgentTemperature={setNewAgentTemperature}
                isCreatingAgent={isCreatingAgent}
                editingAgentId={editingAgentId}
                setEditingAgentId={setEditingAgentId}
                editAgentName={editAgentName}
                setEditAgentName={setEditAgentName}
                editAgentPrompt={editAgentPrompt}
                setEditAgentPrompt={setEditAgentPrompt}
                editAgentModel={editAgentModel}
                setEditAgentModel={setEditAgentModel}
                editAgentSkills={editAgentSkills}
                setEditAgentSkills={setEditAgentSkills}
                editAgentTemperature={editAgentTemperature}
                setEditAgentTemperature={setEditAgentTemperature}
                isUpdatingAgent={isUpdatingAgent}
                speakText={speakText}
                handleSendMessage={handleSendMessage}
                selectChat={selectChat}
                handleCreateSubagent={handleCreateSubagent}
                handleUpdateSubagent={handleUpdateSubagent}
                handleDeleteSubagent={handleDeleteSubagent}
                setCurrentChatId={setCurrentChatId}
                subagentChatEndRef={subagentChatEndRef}
                models={models}
              />
            )}

            {settingsSection === 'obsidian' && (
              <ObsidianTab authToken={localStorage.getItem('vexa_auth_token')} />
            )}

            {settingsSection === 'mcp' && (
              <MCPTab />
            )}
          </div>
          );
          return vexaFloatingPanel === 'settings' ? (
            <FloatingWindow
              title={t('navSettings')}
              storageKey="hermes_vexa_panel_settings_window"
              onClose={() => setVexaFloatingPanel(null)}
              labels={{
                minimize: t('vexaWindowMinimize'),
                restore: t('vexaWindowRestore'),
                fullscreen: t('vexaWindowFullscreen'),
                exitFullscreen: t('vexaWindowExitFullscreen'),
                close: t('vexaWindowCloseGeneric'),
              }}
            >
              {settingsBody}
            </FloatingWindow>
          ) : (
            activeTab === 'settings' && settingsBody
          );
        })()}

        </Suspense>
        </ErrorBoundary>
      </main>

      {/* New Session Custom Modal */}
      {showNewSessionModal && (
        <div style={{
          position: 'fixed',
          top: 0,
          left: 0,
          width: '100vw',
          height: '100vh',
          backgroundColor: 'rgba(8, 9, 14, 0.85)',
          backdropFilter: 'blur(8px)',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          zIndex: 1000
        }}>
          <div style={{
            width: '400px',
            padding: '24px',
            backgroundColor: 'rgba(21, 25, 33, 0.96)',
            border: '1px solid rgba(146, 119, 255, 0.34)',
            borderRadius: '8px',
            boxShadow: '0 22px 60px rgba(0, 0, 0, 0.4), 0 0 28px rgba(146, 119, 255, 0.1)',
            display: 'flex',
            flexDirection: 'column',
            gap: '16px'
          }}>
            <div>
              <h3 style={{ fontSize: '1.2rem', color: '#fff', fontWeight: 600, marginBottom: '4px', letterSpacing: '0.5px' }} className="glow-text-cyan">
                CREATE NEW CHAT
              </h3>
              <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                Enter name for the new chat session
              </p>
            </div>
            
            <input 
              type="text"
              value={newSessionNameInput}
              onChange={(e) => setNewSessionNameInput(e.target.value)}
              placeholder="e.g. Oil Market Analysis"
              style={{
                width: '100%',
                padding: '10px 12px',
                borderRadius: '8px',
                border: '1px solid rgba(255,255,255,0.1)',
                backgroundColor: 'rgba(0,0,0,0.3)',
                color: '#fff',
                fontSize: '0.9rem',
                outline: 'none',
                transition: 'border-color 0.2s'
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  handleCreateNewSessionConfirm(newSessionNameInput, newSessionAgentInput, newSessionProjectInput);
                  setShowNewSessionModal(false);
                }
              }}
              autoFocus
            />

            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <label style={{ fontSize: '0.78rem', color: 'var(--text-muted)', fontWeight: 600 }}>
                PROJECT
              </label>
              <select
                value={newSessionProjectInput}
                onChange={(e) => {
                  const nextProjectId = e.target.value;
                  setNewSessionProjectInput(nextProjectId);
                  // Switching project narrows which agent makes sense — reset
                  // to the default (Vexa) only when the picked agent belongs to
                  // a *different* project. An agent with no project of its own
                  // stays valid everywhere, same as the list below.
                  const picked = subagents.find(a => a.id === newSessionAgentInput);
                  if (nextProjectId && picked && picked.project_id && picked.project_id !== nextProjectId) {
                    setNewSessionAgentInput('vexa');
                  }
                }}
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  borderRadius: '8px',
                  border: '1px solid rgba(255,255,255,0.1)',
                  backgroundColor: 'rgba(20, 24, 32, 0.95)',
                  color: '#fff',
                  fontSize: '0.9rem',
                  outline: 'none',
                  cursor: 'pointer'
                }}
              >
                <option value="">— No project —</option>
                {projects.map(p => (
                  <option key={p.id} value={p.id}>{'  '.repeat(projectDepth.get(p.id) || 0)}{p.name}</option>
                ))}
              </select>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <label style={{ fontSize: '0.78rem', color: 'var(--text-muted)', fontWeight: 600 }}>
                ORCHESTRATOR / AGENT
              </label>
              <select
                value={newSessionAgentInput}
                onChange={(e) => setNewSessionAgentInput(e.target.value)}
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  borderRadius: '8px',
                  border: '1px solid rgba(255,255,255,0.1)',
                  backgroundColor: 'rgba(20, 24, 32, 0.95)',
                  color: '#fff',
                  fontSize: '0.9rem',
                  outline: 'none',
                  cursor: 'pointer'
                }}
              >
                <option value="vexa">👑 Vexa (Main)</option>
                {subagents
                  // A project narrows the list, but must never hide agents
                  // nobody has assigned to any project yet — those stay
                  // available everywhere until someone scopes them.
                  .filter(a => !newSessionProjectInput || a.project_id === newSessionProjectInput || !a.project_id)
                  .map(a => {
                    const isOrch = a.agent_type === 'orchestrator' || a.agent_type === 'sub-orchestrator';
                    const icon = isOrch ? '🧠' : '🤖';
                    return (
                      <option key={a.id} value={a.id}>
                        {icon} {a.name}
                      </option>
                    );
                  })}
              </select>
            </div>

            <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end', marginTop: '8px' }}>
              <button
                onClick={() => setShowNewSessionModal(false)}
                style={{
                  padding: '8px 16px',
                  borderRadius: '6px',
                  border: '1px solid rgba(255,255,255,0.1)',
                  backgroundColor: 'transparent',
                  color: 'var(--text-dim)',
                  cursor: 'pointer',
                  fontSize: '0.8rem',
                  fontWeight: 600
                }}
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  handleCreateNewSessionConfirm(newSessionNameInput, newSessionAgentInput, newSessionProjectInput);
                  setShowNewSessionModal(false);
                }}
                style={{
                  padding: '8px 16px',
                  borderRadius: '6px',
                  border: '1px solid rgba(146, 119, 255, 0.45)',
                  backgroundColor: 'rgba(146, 119, 255, 0.14)',
                  color: '#fff',
                  cursor: 'pointer',
                  fontSize: '0.8rem',
                  fontWeight: 600
                }}
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
