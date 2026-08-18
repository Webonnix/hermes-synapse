import { useMemo, useRef, useState } from 'react';
import { Info, Mic, MicOff, Paperclip, Plus, Send, Sparkles, Square, Trash2, Volume2, VolumeX, X as XIcon, Zap } from 'lucide-react';
import type { ChatMessage, ChatSession } from '../types';
import { renderMarkdown } from '../utils';

// While a reply streams the backend hasn't reported a token count yet, so the
// live figure is derived from the text so far. Qwen's BPE lands around 3-4
// characters per token on mixed Russian/English, which is close enough for a
// progress read-out — it is always shown with a "≈" and replaced by the exact
// number the moment the run finishes.
const CHARS_PER_TOKEN = 3.5;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
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
}

const COPY = {
  ru: {
    newChat: 'Новый чат',
    placeholder: 'Спросите что-нибудь у Vexa',
    empty: 'С чего начнём?',
    send: 'Отправить',
    stop: 'Остановить',
    delete: 'Удалить',
    confirmDelete: 'Точно?',
    immersive: 'Обычный режим',
    you: 'Вы',
    voiceOn: 'Озвучивание включено',
    voiceOff: 'Озвучивание выключено',
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
  },
  en: {
    newChat: 'New chat',
    placeholder: 'Ask Vexa anything',
    empty: 'Where should we begin?',
    send: 'Send',
    stop: 'Stop',
    delete: 'Delete',
    confirmDelete: 'Sure?',
    immersive: 'Immersive mode',
    you: 'You',
    voiceOn: 'Voice replies on',
    voiceOff: 'Voice replies off',
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
  },
} as const;

// Widened from the literal types COPY carries, so either language's table fits.
type Copy = { [K in keyof (typeof COPY)['ru']]: string };

const seconds = (ms: number, unit: string) =>
  ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} ${unit}` : `${Math.round(ms)} ms`;

/** The per-reply generation read-out: rate always visible, full breakdown on demand. */
function MessageStats({
  msg,
  copy,
  numberFormat,
  streamStartedAt,
}: {
  msg: ChatMessage;
  copy: Copy;
  numberFormat: string;
  streamStartedAt: React.MutableRefObject<Map<string, number>>;
}) {
  const [open, setOpen] = useState(false);

  if (msg.role !== 'assistant') return null;

  if (msg.streaming) {
    const key = msg.run_id || String(msg.id ?? '');
    const text = msg.content || '';
    if (!key || !text) return null;
    if (!streamStartedAt.current.has(key)) streamStartedAt.current.set(key, Date.now());
    const elapsed = (Date.now() - (streamStartedAt.current.get(key) as number)) / 1000;
    // Below a second the rate is mostly noise from the first chunk landing.
    if (elapsed < 1) return null;
    return (
      <div className="simple-chat-stats is-live">
        <Zap size={11} />
        <span>≈ {(text.length / CHARS_PER_TOKEN / elapsed).toFixed(0)} {copy.tokensPerSec}</span>
      </div>
    );
  }

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
    <div className="simple-chat-stats-block">
      <div className="simple-chat-stats">
        <Zap size={11} />
        <span className="simple-chat-stats-rate">{rate.toFixed(1)} {copy.tokensPerSec}</span>
        <span>{outputTokens.toLocaleString(numberFormat)} {copy.tokens}</span>
        <span>
          {seconds(waitMs, copy.secondsUnit)} {copy.generating}
          {toolMs && ` · ${seconds(toolMs, copy.secondsUnit)} ${copy.withTools}`}
        </span>
        <button
          type="button"
          className={`simple-chat-stats-toggle${open ? ' is-open' : ''}`}
          onClick={() => setOpen(v => !v)}
          title={copy.statDetails}
          aria-expanded={open}
          aria-label={copy.statDetails}
        >
          <Info size={11} />
        </button>
      </div>
      {open && (
        <dl className="simple-chat-stats-details">
          {rows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
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
}: SimpleChatViewProps) {
  const copy = COPY[language];
  const [hoveredSession, setHoveredSession] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const confirmTimerRef = useRef<number | null>(null);
  const visibleMessages = useMemo(() => messages.filter(m => m.role !== 'system'), [messages]);
  // First time we render each streaming reply, so the live rate has a start
  // point. Keyed by run_id, which is what the stream events carry.
  const streamStartedAtRef = useRef<Map<string, number>>(new Map());

  const numberFormat = language === 'ru' ? 'ru-RU' : 'en-US';

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

  const toggleTTS = () => {
    if (isTTSEnabled) {
      window.speechSynthesis?.cancel();
      setIsSpeaking(false);
    }
    setIsTTSEnabled(v => !v);
  };

  return (
    <div className="simple-chat">
      <aside className="simple-chat-sidebar">
        <button type="button" className="simple-chat-new" onClick={handleCreateNewSession}>
          <Plus size={15} />
          <span>{copy.newChat}</span>
        </button>
        <div className="simple-chat-sessions">
          {chatSessions.map(session => (
            <div
              key={session.id}
              className={`simple-chat-session${currentChatId === session.id ? ' is-active' : ''}`}
              onClick={() => selectChat(session.id)}
              onMouseEnter={() => setHoveredSession(session.id)}
              onMouseLeave={() => setHoveredSession(current => (current === session.id ? null : current))}
            >
              <span>{session.title || getSessionLabel(session.id)}</span>
              {(hoveredSession === session.id || confirmingDelete === session.id) && (
                <button
                  type="button"
                  className={`simple-chat-session-delete${confirmingDelete === session.id ? ' is-confirming' : ''}`}
                  title={confirmingDelete === session.id ? copy.confirmDelete : copy.delete}
                  onClick={(e) => { e.stopPropagation(); handleDeleteClick(session.id); }}
                >
                  {confirmingDelete === session.id ? <span className="simple-chat-confirm-label">{copy.confirmDelete}</span> : <Trash2 size={13} />}
                </button>
              )}
            </div>
          ))}
        </div>
      </aside>

      <div className="simple-chat-main">
        <header className="simple-chat-header">
          <span className="simple-chat-brand">Vexa</span>
          <div className="simple-chat-header-controls">
            <button
              type="button"
              className={`simple-chat-tts-toggle${isTTSEnabled ? ' is-active' : ''}`}
              onClick={toggleTTS}
              title={isTTSEnabled ? copy.voiceOn : copy.voiceOff}
              aria-pressed={isTTSEnabled}
            >
              {isTTSEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
            </button>
            <button type="button" className="simple-chat-mode-switch" onClick={onSwitchToImmersive} title={copy.immersive}>
              <Sparkles size={14} />
              <span>{copy.immersive}</span>
            </button>
          </div>
        </header>

        {visibleMessages.length === 0 ? (
          <div className="simple-chat-empty">
            <h1>{copy.empty}</h1>
          </div>
        ) : (
          <div className="simple-chat-messages">
            {visibleMessages.map((msg, idx) => (
              <div key={msg.id ?? idx} className={`simple-chat-message is-${msg.role}`}>
                {msg.role === 'user' ? (
                  <div className="simple-chat-bubble">{msg.content}</div>
                ) : (
                  <div className="simple-chat-assistant-text">
                    {renderMarkdown(msg.content || (msg.streaming ? '' : ''))}
                    <MessageStats msg={msg} copy={copy} numberFormat={numberFormat} streamStartedAt={streamStartedAtRef} />
                  </div>
                )}
              </div>
            ))}
            <div ref={mainChatEndRef} />
          </div>
        )}

        <div className="simple-chat-composer-wrap">
          {attachedFile && (
            <div className="simple-chat-attachment">
              <span>{attachedFile.name}</span>
              <button type="button" onClick={() => setAttachedFile(null)} aria-label="Remove attachment" title="Remove attachment"><XIcon size={13} /></button>
            </div>
          )}
          <form className="simple-chat-composer" onSubmit={handleSendMessage}>
            <label className="simple-chat-attach">
              <input
                type="file"
                onChange={handleChatFileAttach}
                style={{ display: 'none' }}
                disabled={!isConnected || isUploading}
                accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls"
              />
              <Paperclip size={17} />
            </label>
            <input
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder={copy.placeholder}
              aria-label={copy.placeholder}
              disabled={!isConnected || isUploading}
              autoComplete="off"
            />
            <button
              type="button"
              className={`simple-chat-mic${micState !== 'off' ? ' is-active' : ''}`}
              onClick={onVoiceToggle}
              disabled={micState === 'transcribing' || !isConnected}
              title={micState === 'capturing' ? copy.stop : undefined}
            >
              {micState === 'capturing' ? <MicOff size={17} /> : <Mic size={17} />}
            </button>
            {isGenerating ? (
              <button type="button" className="simple-chat-send is-stop" onClick={onStopGeneration} title={copy.stop}>
                <Square size={14} fill="currentColor" />
              </button>
            ) : (
              <button
                type="submit"
                className="simple-chat-send"
                disabled={!isConnected || isUploading || !inputValue.trim()}
                title={copy.send}
              >
                <Send size={16} />
              </button>
            )}
          </form>
        </div>
      </div>
    </div>
  );
}
