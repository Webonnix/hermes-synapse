import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowDown, Check, ChevronDown, ChevronRight, Copy, Edit2, Folder, FolderOpen, FolderPlus,
  Gauge, Menu, Mic, MicOff, Moon, MoreVertical, Paperclip, PanelLeft, Pencil, Plus, RefreshCw,
  Search, Send, Sparkles, Square, Sun, Trash2, Volume2, VolumeX, X as XIcon, Zap,
} from 'lucide-react';
import type { ChatMessage, ChatSession, Project } from '../types';
import { Markdown } from './simple/Markdown';
import { ModelPicker } from './simple/ModelPicker';
import {
  emptyCatalog, fetchChatModels, saveDefaultSelection, loadDefaultSelection, isSelectionStale,
} from './simple/chatModels';
import type { ChatModelCatalog, ModelSelection } from './simple/chatModels';
import './simple/owui.css';

/**
 * The plain chat view, rebuilt to Open WebUI's shape: chat sidebar with search
 * and date-grouped history, a navbar owning the model selector, centred
 * messages with per-message actions, and a rounded auto-growing composer.
 *
 * The one structural addition over Open WebUI is the model picker's two
 * sources — the local llama.cpp model this box serves and everything reachable
 * through the 9Router sidecar — which is the whole point of the picker here
 * (see components/simple/chatModels.ts and backend/chat_models.py).
 */

// While a reply streams the backend hasn't reported a token count yet, so the
// live figure is derived from the text so far. Qwen's BPE lands around 3-4
// characters per token on mixed Russian/English, which is close enough for a
// progress read-out — it is always shown with a "≈" and replaced by the exact
// number the moment the run finishes.
const CHARS_PER_TOKEN = 3.5;

const COLLAPSED_FOLDERS_KEY = 'vexa_simple_collapsed_folders';
const THEME_KEY = 'vexa_owui_theme';
const SIDEBAR_KEY = 'vexa_owui_sidebar';
const MENU_MAX_HEIGHT = 272;
/** Below this the reply is "close enough to the bottom" to keep auto-scrolling. */
const STICK_TO_BOTTOM_PX = 120;
/** Matches the media query in owui.css where the sidebar becomes an overlay. */
const NARROW_PX = 900;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('vexa_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

interface SimpleChatViewProps {
  language: 'ru' | 'en';
  messages: ChatMessage[];
  inputValue: string;
  setInputValue: (val: string) => void;
  isGenerating: boolean;
  onStopGeneration: () => void;
  handleSendMessage: (e: React.FormEvent) => void;
  isConnected: boolean;
  micState: 'off' | 'listening' | 'capturing' | 'transcribing' | 'error';
  onVoiceToggle: () => void;
  chatSessions: ChatSession[];
  currentChatId: string;
  selectChat: (chatId: string) => void;
  handleCreateNewSession: () => void;
  getSessionLabel: (sessionId: string) => string;
  fetchChatSessions: () => void;
  mainChatEndRef: React.RefObject<HTMLDivElement | null>;
  attachedFile: { name: string; content: string; type?: string; pages?: number; truncated?: boolean } | null;
  setAttachedFile: (file: { name: string; content: string; type?: string; pages?: number; truncated?: boolean } | null) => void;
  handleChatFileAttach: (e: React.ChangeEvent<HTMLInputElement>) => void;
  isUploading: boolean;
  onSwitchToImmersive: () => void;
  isTTSEnabled: boolean;
  setIsTTSEnabled: (val: boolean | ((prev: boolean) => boolean)) => void;
  isSpeaking: boolean;
  setIsSpeaking: (val: boolean) => void;
  /** Project folders (backend/projects.py) the sessions are grouped into. */
  projects?: Project[];
  fetchProjects?: () => void;
  handleSetSessionProject?: (sessionId: string, projectId: string) => void;

  // ── Open WebUI parity ─────────────────────────────────────────────────────
  /** The model this chat is pinned to; null = whatever the backend defaults to. */
  modelSelection?: ModelSelection | null;
  onModelSelect?: (selection: ModelSelection | null) => void;
  /** Sends text straight out, bypassing the composer — suggestions, edits, retry. */
  onSendText?: (text: string) => boolean;
  onRegenerate?: () => void;
  onDeleteMessage?: (messageId: number) => void;
  onRenameSession?: (sessionId: string, title: string) => void;
  /** Creates and opens a chat with no modal, the way Open WebUI's "+" does. */
  onQuickNewChat?: () => void;
  speakText?: (text: string, index: number) => void;
  playingMsgIndex?: number | null;
  userName?: string;
}

const COPY = {
  ru: {
    newChat: 'Новый чат',
    search: 'Поиск чатов',
    placeholder: 'Спросите что-нибудь у Vexa',
    empty: 'С чего начнём?',
    emptyHint: 'Выберите модель сверху — локальную или внешнюю через 9Router.',
    send: 'Отправить',
    stop: 'Остановить',
    delete: 'Удалить',
    confirmDelete: 'Точно?',
    immersive: 'Обычный режим',
    assistant: 'Vexa',
    voiceOn: 'Озвучивание включено',
    voiceOff: 'Озвучивание выключено',
    readAloud: 'Прочитать вслух',
    copy: 'Копировать',
    copied: 'Скопировано',
    attach: 'Прикрепить файл',
    edit: 'Изменить',
    regenerate: 'Сгенерировать заново',
    save: 'Отправить',
    cancel: 'Отмена',
    tokensPerSec: 'ток/с',
    tokens: 'токенов',
    generating: 'генерация',
    withTools: 'включая инструменты',
    secondsUnit: 'с',
    statDetails: 'Подробности генерации',
    statResponseRate: 'Скорость ответа',
    statPromptRate: 'Скорость чтения промпта',
    statOutTokens: 'Токенов в ответе',
    statInTokens: 'Токенов в промпте',
    statDecode: 'Время генерации',
    statPrompt: 'Обработка промпта',
    statModelWait: 'Ожидание модели',
    statTotal: 'Всего с инструментами',
    statToolCalls: 'Вызовов инструментов',
    statModel: 'Модель',
    folders: 'Папки',
    newFolder: 'Новая папка',
    folderName: 'Название папки…',
    newSubfolder: 'Новая подпапка',
    rename: 'Переименовать',
    deleteFolder: 'Удалить папку',
    folderOptions: 'Действия с папкой',
    chatOptions: 'Действия с чатом',
    moveToFolder: 'Переместить в папку',
    noFolder: 'Без папки',
    emptyFolder: 'Пусто',
    confirmDeleteFolder: 'Удалить папку «{name}»? Чаты и подпапки внутри поднимутся на уровень выше — ничего не удалится.',
    today: 'Сегодня',
    yesterday: 'Вчера',
    last7: 'Последние 7 дней',
    last30: 'Последние 30 дней',
    older: 'Ранее',
    nothingFound: 'Ничего не найдено',
    toggleSidebar: 'Показать/скрыть панель',
    theme: 'Сменить тему',
    scrollDown: 'Вниз',
    offline: 'Нет связи с сервером',
    enterToSend: 'Enter — отправить, Shift+Enter — новая строка',
    suggestions: [
      ['Что нового на сервере?', 'Проверь загрузку GPU и статус контейнеров'],
      ['Напиши план', 'Разложи задачу на шаги с оценкой времени'],
      ['Разбери код', 'Найди узкие места и предложи, что упростить'],
      ['Сделай выжимку', 'Кратко перескажи документ или переписку'],
    ] as [string, string][],
  },
  en: {
    newChat: 'New chat',
    search: 'Search chats',
    placeholder: 'Ask Vexa anything',
    empty: 'Where should we begin?',
    emptyHint: 'Pick a model above — local, or external through 9Router.',
    send: 'Send',
    stop: 'Stop',
    delete: 'Delete',
    confirmDelete: 'Sure?',
    immersive: 'Immersive mode',
    assistant: 'Vexa',
    voiceOn: 'Voice replies on',
    voiceOff: 'Voice replies off',
    readAloud: 'Read aloud',
    copy: 'Copy',
    copied: 'Copied',
    attach: 'Attach a file',
    edit: 'Edit',
    regenerate: 'Regenerate',
    save: 'Send',
    cancel: 'Cancel',
    tokensPerSec: 'tok/s',
    tokens: 'tokens',
    generating: 'generation',
    withTools: 'including tools',
    secondsUnit: 's',
    statDetails: 'Generation details',
    statResponseRate: 'Response rate',
    statPromptRate: 'Prompt read rate',
    statOutTokens: 'Response tokens',
    statInTokens: 'Prompt tokens',
    statDecode: 'Generation time',
    statPrompt: 'Prompt processing',
    statModelWait: 'Model wait',
    statTotal: 'Total with tools',
    statToolCalls: 'Tool calls',
    statModel: 'Model',
    folders: 'Folders',
    newFolder: 'New folder',
    folderName: 'Folder name…',
    newSubfolder: 'New sub-folder',
    rename: 'Rename',
    deleteFolder: 'Delete folder',
    folderOptions: 'Folder options',
    chatOptions: 'Chat options',
    moveToFolder: 'Move to folder',
    noFolder: 'No folder',
    emptyFolder: 'Empty',
    confirmDeleteFolder: 'Delete folder "{name}"? Chats and sub-folders inside move up one level — nothing is deleted.',
    today: 'Today',
    yesterday: 'Yesterday',
    last7: 'Previous 7 days',
    last30: 'Previous 30 days',
    older: 'Older',
    nothingFound: 'No results found',
    toggleSidebar: 'Toggle sidebar',
    theme: 'Toggle theme',
    scrollDown: 'Scroll to bottom',
    offline: 'Disconnected from the server',
    enterToSend: 'Enter to send, Shift+Enter for a new line',
    suggestions: [
      ['What is up with the server?', 'Check GPU load and container status'],
      ['Draft a plan', 'Break the task into steps with estimates'],
      ['Review this code', 'Find the bottlenecks and what to simplify'],
      ['Summarise it', 'Condense a document or a thread'],
    ] as [string, string][],
  },
} as const;

type Copy = { [K in keyof (typeof COPY)['ru']]: (typeof COPY)['ru'][K] extends readonly unknown[] ? [string, string][] : string };

const seconds = (ms: number, unit: string) =>
  ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} ${unit}` : `${Math.round(ms)} ms`;

/** Approximate rate while a reply streams; the measured numbers arrive with the final frame. */
function LiveRate({
  msg,
  copy,
  streamStartedAt,
}: {
  msg: ChatMessage;
  copy: Copy;
  streamStartedAt: React.MutableRefObject<Map<string, number>>;
}) {
  if (msg.role !== 'assistant' || !msg.streaming) return null;
  const key = msg.run_id || String(msg.id ?? '');
  const text = msg.content || '';
  if (!key || !text) return null;
  if (!streamStartedAt.current.has(key)) streamStartedAt.current.set(key, Date.now());
  const elapsed = (Date.now() - (streamStartedAt.current.get(key) as number)) / 1000;
  // Below a second the rate is mostly noise from the first chunk landing.
  if (elapsed < 1) return null;
  return (
    <div className="owui-stats is-live">
      <Zap size={11} />
      <span>≈ {(text.length / CHARS_PER_TOKEN / elapsed).toFixed(0)} {copy.tokensPerSec}</span>
    </div>
  );
}

/** Action-bar button that opens the reply's generation stats, like Open WebUI's info action. */
function StatsAction({ msg, copy, numberFormat }: { msg: ChatMessage; copy: Copy; numberFormat: string }) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!anchorRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const meta = msg.meta;
  if (!meta) return null;
  const outputTokens = meta.output_tokens ?? 0;
  const inputTokens = meta.input_tokens ?? 0;
  const decodeMs = meta.decode_ms ?? null;
  const promptMs = meta.prompt_ms ?? null;
  const genMs = meta.generation_ms ?? null;
  const totalMs = meta.latency_ms ?? null;
  // The rate is decode time only — dividing by generation_ms would fold in
  // prompt ingestion (seconds, on a long context) and report roughly half the
  // speed the model was really running at. generation_ms/latency_ms remain the
  // fallback for replies from a backend that doesn't report decode time.
  const rateBaseMs = decodeMs || genMs || totalMs;
  const waitMs = genMs || totalMs;
  if (!outputTokens || !rateBaseMs || !waitMs) return null;

  const rate = outputTokens / (rateBaseMs / 1000);
  const promptRate = inputTokens && promptMs ? inputTokens / (promptMs / 1000) : null;
  const toolMs = genMs != null && totalMs != null && totalMs - genMs > 500 ? totalMs : null;

  const rows: [string, string][] = [
    [copy.statResponseRate, `${rate.toFixed(2)} ${copy.tokensPerSec}`],
    ...(promptRate ? ([[copy.statPromptRate, `${promptRate.toFixed(0)} ${copy.tokensPerSec}`]] as [string, string][]) : []),
    [copy.statOutTokens, outputTokens.toLocaleString(numberFormat)],
    ...(inputTokens ? ([[copy.statInTokens, inputTokens.toLocaleString(numberFormat)]] as [string, string][]) : []),
    ...(decodeMs ? ([[copy.statDecode, seconds(decodeMs, copy.secondsUnit)]] as [string, string][]) : []),
    ...(promptMs ? ([[copy.statPrompt, seconds(promptMs, copy.secondsUnit)]] as [string, string][]) : []),
    [copy.statModelWait, seconds(waitMs, copy.secondsUnit)],
    ...(toolMs ? ([[copy.statTotal, seconds(toolMs, copy.secondsUnit)]] as [string, string][]) : []),
    ...(meta.tool_iterations ? ([[copy.statToolCalls, String(meta.tool_iterations)]] as [string, string][]) : []),
    ...(meta.model ? ([[copy.statModel, meta.model]] as [string, string][]) : []),
  ];

  return (
    <span className="owui-stats-anchor" ref={anchorRef}>
      <button
        type="button"
        className={`owui-action${open ? ' is-active' : ''}`}
        onClick={() => setOpen(v => !v)}
        title={`${copy.statDetails} · ${rate.toFixed(1)} ${copy.tokensPerSec}`}
        aria-expanded={open}
        aria-label={copy.statDetails}
      >
        <Gauge size={14} />
      </button>
      {open && (
        <div className="owui-stats-pop" role="dialog" aria-label={copy.statDetails}>
          <div className="owui-stats">
            <Zap size={11} />
            <span className="owui-stats-rate">{rate.toFixed(1)} {copy.tokensPerSec}</span>
            <span>{outputTokens.toLocaleString(numberFormat)} {copy.tokens}</span>
            <span>
              {seconds(waitMs, copy.secondsUnit)} {copy.generating}
              {toolMs && ` · ${seconds(toolMs, copy.secondsUnit)} ${copy.withTools}`}
            </span>
          </div>
          <dl className="owui-stats-details">
            {rows.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </span>
  );
}

/** Copy-to-clipboard action that reports back in place, like Open WebUI's. */
function CopyAction({ text, label, copiedLabel }: { text: string; label: string; copiedLabel: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="owui-action"
      title={done ? copiedLabel : label}
      aria-label={done ? copiedLabel : label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          window.setTimeout(() => setDone(false), 1600);
        } catch { /* clipboard blocked on an insecure origin — text stays selectable */ }
      }}
    >
      {done ? <Check size={14} /> : <Copy size={14} />}
    </button>
  );
}

// ─── Date grouping ──────────────────────────────────────────────────────────
// Open WebUI buckets the chat list by recency rather than showing raw dates;
// the labels below are the same set it uses.
type Bucket = 'today' | 'yesterday' | 'last7' | 'last30' | 'older';

function bucketOf(updatedAt?: string | null): Bucket {
  if (!updatedAt) return 'older';
  const then = new Date(updatedAt.includes('T') ? updatedAt : updatedAt.replace(' ', 'T') + 'Z');
  if (Number.isNaN(then.getTime())) return 'older';
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const days = Math.floor((startOfToday.getTime() - then.getTime()) / 86400000);
  if (days < 0) return 'today';
  if (days === 0) return 'yesterday';
  if (days < 7) return 'last7';
  if (days < 30) return 'last30';
  return 'older';
}

export function SimpleChatView({
  language,
  messages,
  inputValue,
  setInputValue,
  isGenerating,
  onStopGeneration,
  handleSendMessage,
  isConnected,
  micState,
  onVoiceToggle,
  chatSessions,
  currentChatId,
  selectChat,
  handleCreateNewSession,
  getSessionLabel,
  fetchChatSessions,
  mainChatEndRef,
  attachedFile,
  setAttachedFile,
  handleChatFileAttach,
  isUploading,
  onSwitchToImmersive,
  isTTSEnabled,
  setIsTTSEnabled,
  setIsSpeaking,
  projects = [],
  fetchProjects = () => undefined,
  handleSetSessionProject = () => undefined,
  modelSelection = null,
  onModelSelect = () => undefined,
  onSendText,
  onRegenerate,
  onDeleteMessage,
  onRenameSession,
  onQuickNewChat,
  speakText,
  playingMsgIndex = null,
  userName = 'Albert',
}: SimpleChatViewProps) {
  const copy = COPY[language] as unknown as Copy;
  const numberFormat = language === 'ru' ? 'ru-RU' : 'en-US';

  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem(THEME_KEY) as 'light' | 'dark') || 'dark',
  );
  // On a narrow screen the sidebar overlays the conversation, so it starts
  // closed there unless the user has said otherwise — open, it would hide the
  // very thing they came to read.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const stored = localStorage.getItem(SIDEBAR_KEY);
    if (stored) return stored === 'collapsed';
    return window.innerWidth < NARROW_PX;
  });
  const [search, setSearch] = useState('');
  const [hoveredSession, setHoveredSession] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const confirmTimerRef = useRef<number | null>(null);
  const visibleMessages = useMemo(() => messages.filter(m => m.role !== 'system'), [messages]);
  const streamStartedAtRef = useRef<Map<string, number>>(new Map());

  useEffect(() => { localStorage.setItem(THEME_KEY, theme); }, [theme]);
  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed ? 'collapsed' : 'open');
  }, [sidebarCollapsed]);

  // ─── Model catalogue ────────────────────────────────────────────────────
  const [catalog, setCatalog] = useState<ChatModelCatalog>(emptyCatalog);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [defaultSelection, setDefaultSelection] = useState<ModelSelection | null>(() => loadDefaultSelection());

  const loadModels = useCallback(async (refresh = false) => {
    setModelsLoading(true);
    try {
      setCatalog(await fetchChatModels(refresh));
    } catch {
      // A picker with no models is a degraded control, not a broken page —
      // messages still go to the backend's configured default model.
      setCatalog(emptyCatalog);
    } finally {
      setModelsLoading(false);
    }
  }, []);

  useEffect(() => { void loadModels(); }, [loadModels]);

  const pickDefault = (selection: ModelSelection | null) => {
    setDefaultSelection(selection);
    saveDefaultSelection(selection);
  };

  // Drop a remembered pick the server can no longer route to. Without this the
  // navbar keeps naming a deleted model while every turn quietly runs on the
  // configured default — the reply then looks like it came from the model in
  // the trigger, which is the one thing this control must never get wrong.
  useEffect(() => {
    if (isSelectionStale(catalog, modelSelection)) onModelSelect(null);
    if (isSelectionStale(catalog, defaultSelection)) pickDefault(null);
  }, [catalog]);  // eslint-disable-line react-hooks/exhaustive-deps

  // ─── Sessions ───────────────────────────────────────────────────────────
  const deleteSession = async (sessionId: string) => {
    const path = sessionId === 'dashboard' ? '/api/history/dashboard' : `/api/history/${sessionId}`;
    const res = await fetch(path, { method: 'DELETE', headers: authHeaders() });
    if (res.ok) {
      if (currentChatId === sessionId) selectChat('dashboard');
      fetchChatSessions();
    }
  };

  // Native window.confirm() gets silently suppressed by the browser after a few
  // rapid dialogs in a row (Chrome's "prevent this page from creating additional
  // dialogs"), which is exactly what happens when clearing out a long test-session
  // list — so this uses a two-click arm/confirm pattern entirely inside the app
  // instead, which can't be swallowed by the browser.
  const handleDeleteClick = (sessionId: string) => {
    if (confirmingDelete === sessionId) {
      if (confirmTimerRef.current) window.clearTimeout(confirmTimerRef.current);
      setConfirmingDelete(null);
      void deleteSession(sessionId);
      return;
    }
    setConfirmingDelete(sessionId);
    if (confirmTimerRef.current) window.clearTimeout(confirmTimerRef.current);
    confirmTimerRef.current = window.setTimeout(() => setConfirmingDelete(null), 3000);
  };

  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [renamingSessionTitle, setRenamingSessionTitle] = useState('');

  const submitRenameSession = (sessionId: string) => {
    const title = renamingSessionTitle.trim();
    setRenamingSessionId(null);
    if (!title || !onRenameSession) return;
    onRenameSession(sessionId, title);
  };

  // ─── Project folders ──────────────────────────────────────────────────
  // Folders start open — collapsing is the deliberate act, so a fresh visit
  // shows every chat it already had. Only the collapsed ids are remembered.
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(COLLAPSED_FOLDERS_KEY);
      return new Set<string>(raw ? JSON.parse(raw) : []);
    } catch { return new Set<string>(); }
  });
  const [folderMenuId, setFolderMenuId] = useState<string | null>(null);
  const [sessionMenuId, setSessionMenuId] = useState<string | null>(null);
  const [movingSessionId, setMovingSessionId] = useState<string | null>(null);
  // The sidebar list scrolls, so an absolutely positioned dropdown would be
  // clipped by it — menus are placed with position:fixed against the screen
  // instead, anchored to the button that opened them.
  const [menuPos, setMenuPos] = useState<{ top: number; right: number }>({ top: 0, right: 0 });
  // undefined = the inline "new folder" input is closed; null = creating a
  // top-level folder; a string = creating a sub-folder of that folder.
  const [creatingParent, setCreatingParent] = useState<string | null | undefined>(undefined);
  const [newFolderName, setNewFolderName] = useState('');
  const [renamingFolderId, setRenamingFolderId] = useState<string | null>(null);
  const [renamingFolderName, setRenamingFolderName] = useState('');

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSED_FOLDERS_KEY, JSON.stringify([...collapsedFolders]));
    } catch { /* storage unavailable — folders just reopen next visit */ }
  }, [collapsedFolders]);

  // Fixed-position menus can't rely on the row's mouseleave to close, so any
  // click outside them (or Escape) dismisses them.
  const menuOpen = folderMenuId !== null || movingSessionId !== null || sessionMenuId !== null;
  useEffect(() => {
    if (!menuOpen) return;
    const close = () => { setFolderMenuId(null); setMovingSessionId(null); setSessionMenuId(null); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    window.addEventListener('click', close);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  type FolderNode = Project & { children: FolderNode[] };

  const folderTree = useMemo<FolderNode[]>(() => {
    const byId = new Map<string, FolderNode>();
    projects.forEach(p => byId.set(p.id, { ...p, children: [] }));
    const roots: FolderNode[] = [];
    byId.forEach(node => {
      const parent = node.parent_id ? byId.get(node.parent_id) : undefined;
      if (parent) parent.children.push(node); else roots.push(node);
    });
    return roots;
  }, [projects]);

  // The tree flattened in display order, for the "move to folder" menu — a
  // flat list can't nest, so each entry carries its depth to indent by.
  const flatFolders = useMemo(() => {
    const out: { id: string; name: string; depth: number }[] = [];
    const walk = (nodes: FolderNode[], d: number) => nodes.forEach(n => { out.push({ id: n.id, name: n.name, depth: d }); walk(n.children, d + 1); });
    walk(folderTree, 0);
    return out;
  }, [folderTree]);

  const query = search.trim().toLowerCase();
  const matchesSearch = useCallback((session: ChatSession) => {
    if (!query) return true;
    return (session.title || getSessionLabel(session.id)).toLowerCase().includes(query);
  }, [query, getSessionLabel]);

  const sessionsByFolder = useMemo(() => {
    const map = new Map<string, ChatSession[]>();
    chatSessions.forEach(session => {
      if (session.id === 'dashboard') return;
      if (!matchesSearch(session)) return;
      // A session pointing at a folder that no longer exists is treated as
      // unfiled rather than disappearing from the list.
      const pid = session.project_id && projects.some(p => p.id === session.project_id) ? session.project_id : '';
      if (!map.has(pid)) map.set(pid, []);
      map.get(pid)!.push(session);
    });
    return map;
  }, [chatSessions, projects, matchesSearch]);

  const anchorMenu = (target: HTMLElement) => {
    const rect = target.getBoundingClientRect();
    setMenuPos({
      // Keep a menu opened near the bottom of the window on screen.
      top: Math.min(rect.bottom + 4, Math.max(8, window.innerHeight - MENU_MAX_HEIGHT - 8)),
      right: Math.max(8, window.innerWidth - rect.right),
    });
  };

  const toggleFolder = (folderId: string) => {
    setCollapsedFolders(prev => {
      const next = new Set(prev);
      if (next.has(folderId)) next.delete(folderId); else next.add(folderId);
      return next;
    });
  };

  const openNewFolder = (parentId: string | null) => {
    if (parentId) setCollapsedFolders(prev => { const next = new Set(prev); next.delete(parentId); return next; });
    setCreatingParent(parentId);
    setNewFolderName('');
  };

  const submitNewFolder = async () => {
    const name = newFolderName.trim();
    const parent = creatingParent;
    setCreatingParent(undefined);
    setNewFolderName('');
    if (!name || parent === undefined) return;
    const res = await fetch('/api/projects', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ name, parent_id: parent || null }),
    });
    if (res.ok) fetchProjects();
  };

  const submitRenameFolder = async (folderId: string) => {
    const name = renamingFolderName.trim();
    const folder = projects.find(p => p.id === folderId);
    setRenamingFolderId(null);
    if (!name || !folder || name === folder.name) return;
    const res = await fetch(`/api/projects/${folderId}`, {
      method: 'PUT',
      headers: authHeaders(),
      body: JSON.stringify({
        name,
        description: folder.description,
        is_active: folder.is_active,
        parent_id: folder.parent_id ?? null,
      }),
    });
    if (res.ok) fetchProjects();
  };

  const deleteFolder = async (folderId: string, name: string) => {
    if (!window.confirm(copy.confirmDeleteFolder.replace('{name}', name))) return;
    const res = await fetch(`/api/projects/${folderId}`, { method: 'DELETE', headers: authHeaders() });
    if (res.ok) {
      fetchProjects();
      fetchChatSessions();
    }
  };

  const moveSession = (sessionId: string, folderId: string) => {
    setMovingSessionId(null);
    handleSetSessionProject(sessionId, folderId);
  };

  const toggleTTS = () => {
    if (isTTSEnabled) {
      window.speechSynthesis?.cancel();
      setIsSpeaking(false);
    }
    setIsTTSEnabled(v => !v);
  };

  // ─── Composer ───────────────────────────────────────────────────────────
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  // Grow with the text up to the max-height the stylesheet caps it at, then
  // scroll — the same behaviour as Open WebUI's input.
  //
  // An empty composer clears the inline height instead of measuring, so it
  // falls back to rows=1. Measuring here would be wrong anyway on the very
  // first paint: the stylesheet is injected after mount in dev, and a textarea
  // measured before its own padding and line-height exist reports a scrollHeight
  // that pins the composer open at its 200px maximum for the rest of the session.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    if (!inputValue) { el.style.height = ''; return; }
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [inputValue]);

  const onComposerKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      formRef.current?.requestSubmit();
    }
  };

  // ─── Scroll handling ────────────────────────────────────────────────────
  const scrollRef = useRef<HTMLDivElement>(null);
  const [showScrollDown, setShowScrollDown] = useState(false);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setShowScrollDown(el.scrollHeight - el.scrollTop - el.clientHeight > STICK_TO_BOTTOM_PX);
  };

  const scrollToBottom = () => {
    mainChatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  // ─── Auto-title ─────────────────────────────────────────────────────────
  // A chat created from the "+" has no name, so Open WebUI names it from the
  // opening message. Done once per chat; the id is remembered so a user who
  // renames it afterwards is never overwritten.
  const autoTitledRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!onRenameSession || currentChatId === 'dashboard') return;
    if (autoTitledRef.current.has(currentChatId)) return;
    const session = chatSessions.find(s => s.id === currentChatId);
    if (!session || (session.title && session.title !== session.id)) return;
    const firstUser = visibleMessages.find(m => m.role === 'user');
    if (!firstUser?.content) return;
    autoTitledRef.current.add(currentChatId);
    const title = firstUser.content.replace(/\s+/g, ' ').trim().slice(0, 48);
    if (title) onRenameSession(currentChatId, title);
  }, [visibleMessages, currentChatId, chatSessions, onRenameSession]);

  // ─── Message editing ────────────────────────────────────────────────────
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingText, setEditingText] = useState('');

  const submitEdit = () => {
    const text = editingText.trim();
    setEditingIndex(null);
    if (text && onSendText) onSendText(text);
  };

  // ─── Rendering ──────────────────────────────────────────────────────────
  /** One chat row. Shared by the folder tree and the date-grouped list, so a
   * chat looks and behaves the same wherever it sits. */
  const renderSessionRow = (session: ChatSession, depth = 0): ReactNode => {
    const label = session.title && session.title !== session.id ? session.title : getSessionLabel(session.id);
    const isRenaming = renamingSessionId === session.id;
    const forced = movingSessionId === session.id || sessionMenuId === session.id || confirmingDelete === session.id;
    return (
      <div
        key={session.id}
        className={`owui-chat-item${currentChatId === session.id ? ' is-active' : ''}`}
        style={depth ? { paddingLeft: `${depth * 14 + 8}px` } : undefined}
        onClick={() => !isRenaming && openChat(session.id)}
        onMouseEnter={() => setHoveredSession(session.id)}
        onMouseLeave={() => setHoveredSession(current => (current === session.id ? null : current))}
      >
        {isRenaming ? (
          <input
            autoFocus
            value={renamingSessionTitle}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => setRenamingSessionTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitRenameSession(session.id);
              else if (e.key === 'Escape') setRenamingSessionId(null);
            }}
            onBlur={() => submitRenameSession(session.id)}
            aria-label={copy.rename}
          />
        ) : (
          <span className="owui-chat-item-title">{label}</span>
        )}
        <div className={`owui-chat-item-actions${forced ? ' is-forced' : ''}`}>
          {session.id !== 'dashboard' && (
            <div className="owui-menu-wrap">
              <button
                type="button"
                className="owui-chat-item-action"
                title={copy.chatOptions}
                aria-label={`${copy.chatOptions}: ${label}`}
                onClick={(e) => {
                  e.stopPropagation();
                  anchorMenu(e.currentTarget);
                  setFolderMenuId(null);
                  setMovingSessionId(null);
                  setSessionMenuId(sessionMenuId === session.id ? null : session.id);
                }}
              >
                <MoreVertical size={13} />
              </button>
              {sessionMenuId === session.id && (
                <div className="owui-menu" style={{ top: menuPos.top, right: menuPos.right }} onClick={(e) => e.stopPropagation()}>
                  <button
                    type="button"
                    onClick={() => {
                      setSessionMenuId(null);
                      setRenamingSessionId(session.id);
                      setRenamingSessionTitle(label);
                    }}
                  >
                    <Pencil size={12} />{copy.rename}
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      anchorMenu(e.currentTarget);
                      setSessionMenuId(null);
                      setMovingSessionId(session.id);
                    }}
                  >
                    <Folder size={12} />{copy.moveToFolder}
                  </button>
                  <div className="owui-menu-sep" />
                  <button type="button" className="is-danger" onClick={() => { setSessionMenuId(null); handleDeleteClick(session.id); }}>
                    <Trash2 size={12} />{copy.delete}
                  </button>
                </div>
              )}
            </div>
          )}
          {session.id !== 'dashboard' && (hoveredSession === session.id || movingSessionId === session.id) && (
            <div className="owui-menu-wrap">
              <button
                type="button"
                className="owui-chat-item-action"
                title={copy.moveToFolder}
                aria-label={`${copy.moveToFolder}: ${label}`}
                onClick={(e) => {
                  e.stopPropagation();
                  anchorMenu(e.currentTarget);
                  setFolderMenuId(null);
                  setSessionMenuId(null);
                  setMovingSessionId(movingSessionId === session.id ? null : session.id);
                }}
              >
                <Folder size={13} />
              </button>
              {movingSessionId === session.id && (
                <div className="owui-menu" style={{ top: menuPos.top, right: menuPos.right }} onClick={(e) => e.stopPropagation()}>
                  <button type="button" onClick={() => moveSession(session.id, '')}>{copy.noFolder}</button>
                  {flatFolders.map(folder => (
                    <button
                      key={folder.id}
                      type="button"
                      style={{ paddingLeft: `${folder.depth * 12 + 8}px` }}
                      onClick={() => moveSession(session.id, folder.id)}
                    >
                      <Folder size={12} />{folder.name}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          {(hoveredSession === session.id || confirmingDelete === session.id) && (
            <button
              type="button"
              className={`owui-chat-item-action${confirmingDelete === session.id ? ' is-confirming' : ''}`}
              title={confirmingDelete === session.id ? copy.confirmDelete : copy.delete}
              onClick={(e) => { e.stopPropagation(); handleDeleteClick(session.id); }}
            >
              {confirmingDelete === session.id ? copy.confirmDelete : <Trash2 size={13} />}
            </button>
          )}
        </div>
      </div>
    );
  };

  const renderNewFolderInput = (depth: number): ReactNode => (
    <div className="owui-folder-new" style={{ paddingLeft: `${depth * 14 + 8}px` }}>
      <FolderPlus size={13} />
      <input
        autoFocus
        value={newFolderName}
        onChange={(e) => setNewFolderName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void submitNewFolder();
          else if (e.key === 'Escape') { setCreatingParent(undefined); setNewFolderName(''); }
        }}
        onBlur={() => void submitNewFolder()}
        placeholder={copy.folderName}
        aria-label={copy.folderName}
      />
    </div>
  );

  /** One folder row plus, while it is open, its sub-folders and its chats. */
  const renderFolderNode = (node: FolderNode, depth: number): ReactNode => {
    const isOpen = !collapsedFolders.has(node.id);
    const ownSessions = sessionsByFolder.get(node.id) || [];
    const isRenaming = renamingFolderId === node.id;
    return (
      <Fragment key={node.id}>
        <div
          className="owui-folder"
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          onClick={() => toggleFolder(node.id)}
        >
          {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          {isOpen ? <FolderOpen size={14} /> : <Folder size={14} />}
          {isRenaming ? (
            <input
              autoFocus
              value={renamingFolderName}
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => setRenamingFolderName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void submitRenameFolder(node.id);
                else if (e.key === 'Escape') setRenamingFolderId(null);
              }}
              onBlur={() => void submitRenameFolder(node.id)}
              aria-label={copy.rename}
            />
          ) : (
            <span className="owui-folder-name">{node.name}</span>
          )}
          {!isOpen && ownSessions.length > 0 && <span className="owui-folder-count">{ownSessions.length}</span>}
          <div className="owui-menu-wrap">
            <button
              type="button"
              className="owui-chat-item-action"
              title={copy.folderOptions}
              aria-label={`${copy.folderOptions}: ${node.name}`}
              onClick={(e) => {
                e.stopPropagation();
                anchorMenu(e.currentTarget);
                setMovingSessionId(null);
                setSessionMenuId(null);
                setFolderMenuId(folderMenuId === node.id ? null : node.id);
              }}
            >
              <MoreVertical size={13} />
            </button>
            {folderMenuId === node.id && (
              <div className="owui-menu" style={{ top: menuPos.top, right: menuPos.right }} onClick={(e) => e.stopPropagation()}>
                <button type="button" onClick={() => { setFolderMenuId(null); openNewFolder(node.id); }}>
                  <FolderPlus size={12} />{copy.newSubfolder}
                </button>
                <button type="button" onClick={() => { setFolderMenuId(null); setRenamingFolderId(node.id); setRenamingFolderName(node.name); }}>
                  <Edit2 size={12} />{copy.rename}
                </button>
                <button type="button" className="is-danger" onClick={() => { setFolderMenuId(null); void deleteFolder(node.id, node.name); }}>
                  <Trash2 size={12} />{copy.deleteFolder}
                </button>
              </div>
            )}
          </div>
        </div>
        {isOpen && (
          <>
            {creatingParent === node.id && renderNewFolderInput(depth + 1)}
            {node.children.map(child => renderFolderNode(child, depth + 1))}
            {ownSessions.map(session => renderSessionRow(session, depth + 1))}
            {node.children.length === 0 && ownSessions.length === 0 && creatingParent !== node.id && (
              <div className="owui-folder-empty" style={{ paddingLeft: `${(depth + 1) * 14 + 18}px` }}>{copy.emptyFolder}</div>
            )}
          </>
        )}
      </Fragment>
    );
  };

  /** Picking a chat from an overlaying sidebar closes it, so the conversation
   *  the user just chose is the thing they end up looking at. */
  const openChat = (chatId: string) => {
    selectChat(chatId);
    if (window.innerWidth < NARROW_PX) setSidebarCollapsed(true);
  };

  const dashboardSession = chatSessions.find(session => session.id === 'dashboard');

  // Unfiled chats are the ones Open WebUI's date buckets apply to; filed chats
  // live under their folder and keep the folder's own order.
  const bucketedSessions = useMemo(() => {
    const order: Bucket[] = ['today', 'yesterday', 'last7', 'last30', 'older'];
    const groups = new Map<Bucket, ChatSession[]>();
    (sessionsByFolder.get('') || []).forEach(session => {
      const bucket = bucketOf(session.updated_at);
      if (!groups.has(bucket)) groups.set(bucket, []);
      groups.get(bucket)!.push(session);
    });
    return order
      .filter(bucket => groups.has(bucket))
      .map(bucket => [bucket, groups.get(bucket) as ChatSession[]] as [Bucket, ChatSession[]]);
  }, [sessionsByFolder]);

  const activeModelLabel = modelSelection?.id || catalog.default.name || '';
  const newChat = onQuickNewChat || handleCreateNewSession;

  return (
    <div className="owui" data-theme={theme}>
      <aside className={`owui-sidebar${sidebarCollapsed ? ' is-collapsed' : ''}`}>
        <div className="owui-sidebar-top">
          <div className="owui-sidebar-brand">
            <span className="owui-sidebar-brand-mark">V</span>
            <span>Vexa</span>
          </div>
          <button
            type="button"
            className="owui-icon-btn"
            onClick={() => setSidebarCollapsed(true)}
            title={copy.toggleSidebar}
            aria-label={copy.toggleSidebar}
          >
            <PanelLeft size={16} />
          </button>
        </div>

        <button type="button" className="owui-new-chat" onClick={newChat}>
          <Plus size={16} />
          <span>{copy.newChat}</span>
        </button>

        <div className="owui-search">
          <Search size={15} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={copy.search}
            aria-label={copy.search}
          />
          {search && (
            <button
              type="button"
              className="owui-chat-item-action"
              onClick={() => setSearch('')}
              aria-label={copy.cancel}
            >
              <XIcon size={13} />
            </button>
          )}
        </div>

        <div className="owui-sidebar-scroll">
          {dashboardSession && matchesSearch(dashboardSession) && renderSessionRow(dashboardSession)}

          <div className="owui-section-label">
            <span>{copy.folders}</span>
            <button
              type="button"
              className="owui-chat-item-action"
              title={copy.newFolder}
              aria-label={copy.newFolder}
              onClick={() => openNewFolder(null)}
            >
              <FolderPlus size={14} />
            </button>
          </div>
          {creatingParent === null && renderNewFolderInput(0)}
          {folderTree.map(node => renderFolderNode(node, 0))}

          {bucketedSessions.map(([bucket, sessions]) => (
            <Fragment key={bucket}>
              <div className="owui-section-label"><span>{copy[bucket]}</span></div>
              {sessions.map(session => renderSessionRow(session))}
            </Fragment>
          ))}

          {query && bucketedSessions.length === 0 && folderTree.length === 0 && (
            <div className="owui-folder-empty" style={{ paddingLeft: 10 }}>{copy.nothingFound}</div>
          )}
        </div>

        <div className="owui-sidebar-foot">
          <button type="button" className="owui-user-card" onClick={onSwitchToImmersive} title={copy.immersive}>
            <span className="owui-avatar">{(userName[0] || 'A').toUpperCase()}</span>
            <span>{userName}</span>
            <Sparkles size={13} style={{ marginLeft: 'auto', opacity: .6 }} />
          </button>
        </div>
      </aside>

      <div className="owui-main">
        <header className="owui-navbar">
          <div className="owui-navbar-left">
            {sidebarCollapsed && (
              <button
                type="button"
                className="owui-icon-btn"
                onClick={() => setSidebarCollapsed(false)}
                title={copy.toggleSidebar}
                aria-label={copy.toggleSidebar}
              >
                <Menu size={16} />
              </button>
            )}
            <ModelPicker
              catalog={catalog}
              selection={modelSelection}
              onSelect={onModelSelect}
              onSetDefault={pickDefault}
              defaultSelection={defaultSelection}
              onRefresh={() => void loadModels(true)}
              loading={modelsLoading}
              language={language}
              disabled={isGenerating}
            />
          </div>
          <div className="owui-navbar-right">
            <button
              type="button"
              className="owui-mode-btn"
              onClick={onSwitchToImmersive}
              title={copy.immersive}
            >
              <Sparkles size={14} />
              <span>{copy.immersive}</span>
            </button>
            <button
              type="button"
              className={`owui-icon-btn${isTTSEnabled ? ' is-active' : ''}`}
              onClick={toggleTTS}
              title={isTTSEnabled ? copy.voiceOn : copy.voiceOff}
              aria-pressed={isTTSEnabled}
            >
              {isTTSEnabled ? <Volume2 size={16} /> : <VolumeX size={16} />}
            </button>
            <button
              type="button"
              className="owui-icon-btn"
              onClick={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))}
              title={copy.theme}
              aria-label={copy.theme}
            >
              {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button
              type="button"
              className="owui-icon-btn"
              onClick={newChat}
              title={copy.newChat}
              aria-label={copy.newChat}
            >
              <Plus size={17} />
            </button>
          </div>
        </header>

        {visibleMessages.length === 0 ? (
          <div className="owui-placeholder">
            <h1>{copy.empty}</h1>
            <p>{copy.emptyHint}</p>
            <div className="owui-suggestions">
              {copy.suggestions.map(([title, hint]) => (
                <button
                  key={title}
                  type="button"
                  className="owui-suggestion"
                  onClick={() => {
                    setInputValue(title);
                    textareaRef.current?.focus();
                  }}
                >
                  <strong>{title}</strong>
                  <span>{hint}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="owui-messages" ref={scrollRef} onScroll={onScroll}>
            <div className="owui-messages-inner">
              {visibleMessages.map((msg, idx) => {
                const isLast = idx === visibleMessages.length - 1;
                if (msg.role === 'user') {
                  return (
                    <div key={msg.id ?? idx} className="owui-message is-user">
                      <div className="owui-message-user-wrap">
                        {editingIndex === idx ? (
                          <div className="owui-edit-box">
                            <textarea
                              autoFocus
                              value={editingText}
                              onChange={(e) => setEditingText(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitEdit(); }
                                else if (e.key === 'Escape') setEditingIndex(null);
                              }}
                              aria-label={copy.edit}
                            />
                            <div className="owui-edit-actions">
                              <button type="button" className="owui-btn" onClick={() => setEditingIndex(null)}>{copy.cancel}</button>
                              <button type="button" className="owui-btn is-primary" onClick={submitEdit}>{copy.save}</button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <div className="owui-bubble">{msg.content}</div>
                            <div className="owui-actions">
                              <CopyAction text={msg.content} label={copy.copy} copiedLabel={copy.copied} />
                              {onSendText && (
                                <button
                                  type="button"
                                  className="owui-action"
                                  title={copy.edit}
                                  aria-label={copy.edit}
                                  onClick={() => { setEditingIndex(idx); setEditingText(msg.content); }}
                                >
                                  <Pencil size={14} />
                                </button>
                              )}
                              {onDeleteMessage && msg.id != null && (
                                <button
                                  type="button"
                                  className="owui-action is-danger"
                                  title={copy.delete}
                                  aria-label={copy.delete}
                                  onClick={() => onDeleteMessage(msg.id as number)}
                                >
                                  <Trash2 size={14} />
                                </button>
                              )}
                            </div>
                          </>
                        )}
                      </div>
                    </div>
                  );
                }

                return (
                  <div key={msg.id ?? idx} className="owui-message is-assistant">
                    <div className="owui-message-head">
                      <span className="owui-message-name">{copy.assistant}</span>
                      {msg.meta?.model && <span className="owui-message-model">{msg.meta.model}</span>}
                    </div>
                    <div className="owui-assistant-body">
                      {msg.content
                        ? <Markdown text={msg.content} />
                        : msg.streaming && <div className="owui-typing"><span /><span /><span /></div>}
                    </div>
                    <LiveRate msg={msg} copy={copy} streamStartedAt={streamStartedAtRef} />
                    {!msg.streaming && (
                      <div className={`owui-actions${isLast ? ' is-pinned' : ''}`}>
                        <CopyAction text={msg.content} label={copy.copy} copiedLabel={copy.copied} />
                        {speakText && (
                          <button
                            type="button"
                            className={`owui-action${playingMsgIndex === idx ? ' is-active' : ''}`}
                            title={copy.readAloud}
                            aria-label={copy.readAloud}
                            onClick={() => speakText(msg.content, idx)}
                          >
                            {playingMsgIndex === idx ? <Square size={12} fill="currentColor" /> : <Volume2 size={14} />}
                          </button>
                        )}
                        <StatsAction msg={msg} copy={copy} numberFormat={numberFormat} />
                        {onRegenerate && isLast && !isGenerating && (
                          <button
                            type="button"
                            className="owui-action"
                            title={copy.regenerate}
                            aria-label={copy.regenerate}
                            onClick={onRegenerate}
                          >
                            <RefreshCw size={14} />
                          </button>
                        )}
                        {onDeleteMessage && msg.id != null && (
                          <button
                            type="button"
                            className="owui-action is-danger"
                            title={copy.delete}
                            aria-label={copy.delete}
                            onClick={() => onDeleteMessage(msg.id as number)}
                          >
                            <Trash2 size={14} />
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
              <div ref={mainChatEndRef} />
            </div>
          </div>
        )}

        {showScrollDown && (
          <button type="button" className="owui-scroll-bottom" onClick={scrollToBottom} title={copy.scrollDown} aria-label={copy.scrollDown}>
            <ArrowDown size={15} />
          </button>
        )}

        <div className="owui-composer-wrap">
          <div className="owui-composer-inner">
            {attachedFile && (
              <div className="owui-attachment">
                <Paperclip size={13} />
                <span>{attachedFile.name}</span>
                <button type="button" onClick={() => setAttachedFile(null)} aria-label="Remove attachment" title="Remove attachment">
                  <XIcon size={13} />
                </button>
              </div>
            )}
            <form className="owui-composer" ref={formRef} onSubmit={handleSendMessage}>
              <textarea
                ref={textareaRef}
                rows={1}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={onComposerKeyDown}
                placeholder={copy.placeholder}
                aria-label={copy.placeholder}
                disabled={!isConnected || isUploading}
              />
              <div className="owui-composer-row">
                <label className="owui-attach" title={copy.attach} aria-label={copy.attach}>
                  <input
                    type="file"
                    onChange={handleChatFileAttach}
                    style={{ display: 'none' }}
                    disabled={!isConnected || isUploading}
                    accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls"
                  />
                  <Paperclip size={17} />
                </label>
                <div className="owui-spacer" />
                <button
                  type="button"
                  className={`owui-icon-btn owui-mic${micState !== 'off' ? ' is-active' : ''}`}
                  onClick={onVoiceToggle}
                  disabled={micState === 'transcribing' || !isConnected}
                  title={micState === 'capturing' ? copy.stop : undefined}
                >
                  {micState === 'capturing' ? <MicOff size={17} /> : <Mic size={17} />}
                </button>
                {isGenerating ? (
                  <button type="button" className="owui-send is-stop" onClick={onStopGeneration} title={copy.stop} aria-label={copy.stop}>
                    <Square size={13} fill="currentColor" />
                  </button>
                ) : (
                  <button
                    type="submit"
                    className="owui-send"
                    disabled={!isConnected || isUploading || !inputValue.trim()}
                    title={copy.send}
                    aria-label={copy.send}
                  >
                    <Send size={15} />
                  </button>
                )}
              </div>
            </form>
            <div className={`owui-composer-hint${isConnected ? '' : ' is-offline'}`}>
              {isConnected
                ? <>{activeModelLabel && <><span className="owui-dot" />{activeModelLabel} · </>}{copy.enterToSend}</>
                : copy.offline}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
