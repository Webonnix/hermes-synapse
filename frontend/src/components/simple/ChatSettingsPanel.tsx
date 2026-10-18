import { useEffect, useState } from 'react';
import { SlidersHorizontal, X as XIcon } from 'lucide-react';
import { DEFAULT_CHAT_SETTINGS, saveChatSettings } from './chatApi';
import type { ChatSettings, ThinkMode } from './chatApi';

/**
 * Per-chat instructions and generation settings (backend/chat_settings.py).
 * An empty field means "use the configured default", never zero: the backend
 * stores null for it and the agent falls back to its own value.
 */
const COPY = {
  ru: {
    title: 'Настройки чата',
    systemPrompt: 'Инструкции для этого чата',
    systemPromptHint: 'Добавляются к системному промпту агента только в этом чате.',
    temperature: 'Temperature',
    topP: 'Top-p',
    maxTokens: 'Макс. токенов ответа',
    defaultValue: 'по умолчанию',
    think: 'Рассуждения (reasoning)',
    thinkDefault: 'Как в настройках',
    thinkOn: 'Включены',
    thinkOff: 'Выключены',
    reset: 'Сбросить',
    save: 'Сохранить',
    cancel: 'Отмена',
    incognito: 'Инкогнито-чат: сообщения не попадают в список и удаляются при выходе.',
    saveFailed: 'Не удалось сохранить',
  },
  en: {
    title: 'Chat settings',
    systemPrompt: 'Instructions for this chat',
    systemPromptHint: "Appended to the agent's system prompt, in this chat only.",
    temperature: 'Temperature',
    topP: 'Top-p',
    maxTokens: 'Max answer tokens',
    defaultValue: 'default',
    think: 'Reasoning',
    thinkDefault: 'As configured',
    thinkOn: 'On',
    thinkOff: 'Off',
    reset: 'Reset',
    save: 'Save',
    cancel: 'Cancel',
    incognito: 'Incognito chat: not listed, deleted when you leave it.',
    saveFailed: 'Could not save',
  },
};

interface ChatSettingsPanelProps {
  language: 'ru' | 'en';
  sessionId: string;
  settings: ChatSettings;
  onClose: () => void;
  onSaved: (settings: ChatSettings) => void;
}

function toNumber(value: string): number | null {
  if (value.trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function ChatSettingsPanel({ language, sessionId, settings, onClose, onSaved }: ChatSettingsPanelProps) {
  const copy = COPY[language];
  const [systemPrompt, setSystemPrompt] = useState(settings.system_prompt);
  const [temperature, setTemperature] = useState(settings.temperature?.toString() ?? '');
  const [topP, setTopP] = useState(settings.top_p?.toString() ?? '');
  const [maxTokens, setMaxTokens] = useState(settings.max_tokens?.toString() ?? '');
  const [think, setThink] = useState<ThinkMode>(settings.think);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const reset = () => {
    setSystemPrompt('');
    setTemperature('');
    setTopP('');
    setMaxTokens('');
    setThink(null);
  };

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      const saved = await saveChatSettings(sessionId, {
        ...DEFAULT_CHAT_SETTINGS,
        system_prompt: systemPrompt,
        temperature: toNumber(temperature),
        top_p: toNumber(topP),
        max_tokens: toNumber(maxTokens),
        think,
        // Never flip a chat in or out of incognito from here.
        ephemeral: settings.ephemeral,
      });
      onSaved(saved);
      onClose();
    } catch (e) {
      setError(`${copy.saveFailed}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="owui-overlay" onClick={onClose}>
      <aside className="owui-drawer" role="dialog" aria-label={copy.title} onClick={(e) => e.stopPropagation()}>
        <div className="owui-drawer-head">
          <SlidersHorizontal size={16} />
          <span>{copy.title}</span>
          <button type="button" className="owui-icon-btn" onClick={onClose} aria-label={copy.cancel}>
            <XIcon size={16} />
          </button>
        </div>
        <div className="owui-drawer-body">
          {settings.ephemeral && <p className="owui-chip is-incognito">{copy.incognito}</p>}
          <label className="owui-field">
            <span>{copy.systemPrompt}</span>
            <textarea value={systemPrompt} maxLength={8000} onChange={(e) => setSystemPrompt(e.target.value)} />
            <small>{copy.systemPromptHint}</small>
          </label>
          <div className="owui-field-row">
            <label className="owui-field">
              <span>{copy.temperature}</span>
              <input type="number" min={0} max={2} step={0.05} value={temperature} placeholder={copy.defaultValue}
                onChange={(e) => setTemperature(e.target.value)} />
            </label>
            <label className="owui-field">
              <span>{copy.topP}</span>
              <input type="number" min={0} max={1} step={0.05} value={topP} placeholder={copy.defaultValue}
                onChange={(e) => setTopP(e.target.value)} />
            </label>
          </div>
          <div className="owui-field-row">
            <label className="owui-field">
              <span>{copy.maxTokens}</span>
              <input type="number" min={1} max={32768} step={64} value={maxTokens} placeholder={copy.defaultValue}
                onChange={(e) => setMaxTokens(e.target.value)} />
            </label>
            <label className="owui-field">
              <span>{copy.think}</span>
              <select value={think ?? ''} onChange={(e) => setThink((e.target.value || null) as ThinkMode)}>
                <option value="">{copy.thinkDefault}</option>
                <option value="on">{copy.thinkOn}</option>
                <option value="off">{copy.thinkOff}</option>
              </select>
            </label>
          </div>
          {error && <div className="owui-error" role="alert">{error}</div>}
        </div>
        <div className="owui-drawer-foot">
          <button type="button" className="owui-btn" onClick={reset} disabled={saving}>{copy.reset}</button>
          <button type="button" className="owui-btn" onClick={onClose} disabled={saving}>{copy.cancel}</button>
          <button type="button" className="owui-btn is-primary" onClick={() => void save()} disabled={saving}>{copy.save}</button>
        </div>
      </aside>
    </div>
  );
}
