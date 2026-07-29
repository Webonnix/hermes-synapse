import { useMemo, useRef, useState } from 'react';
import { Mic, MicOff, Paperclip, Plus, Send, Sparkles, Square, Trash2, Volume2, VolumeX, X as XIcon } from 'lucide-react';
import type { ChatMessage, ChatSession } from '../types';
import { renderMarkdown } from '../utils';

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
  },
} as const;

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
              <button type="button" onClick={() => setAttachedFile(null)}><XIcon size={13} /></button>
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
