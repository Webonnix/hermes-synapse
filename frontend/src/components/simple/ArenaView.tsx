import { useState } from 'react';
import { Send, Swords, X as XIcon } from 'lucide-react';
import { Markdown } from './Markdown';
import { ModelPicker } from './ModelPicker';
import { runArena } from './chatApi';
import type { ArenaResult } from './chatApi';
import type { ChatModelCatalog, ModelSelection } from './chatModels';

/**
 * Model arena: one prompt, two models, answers side by side
 * (backend/chat_arena.py). A plain completion — no tools, no chat history,
 * nothing saved — so comparing wording can never perform an action twice.
 */
const COPY = {
  ru: {
    title: 'Арена моделей',
    close: 'Закрыть',
    placeholder: 'Вопрос для обеих моделей…',
    run: 'Сравнить',
    running: 'Модели отвечают по очереди…',
    note: 'Без инструментов и истории чата, ничего не сохраняется.',
    sameModel: 'Выберите две разные модели.',
    failed: 'Ошибка',
    seconds: 'с',
    tokens: 'ток.',
    defaultModel: 'Модель по умолчанию',
  },
  en: {
    title: 'Model arena',
    close: 'Close',
    placeholder: 'A question for both models…',
    run: 'Compare',
    running: 'The models answer one after another…',
    note: 'No tools, no chat history, nothing is saved.',
    sameModel: 'Pick two different models.',
    failed: 'Error',
    seconds: 's',
    tokens: 'tok',
    defaultModel: 'Default model',
  },
};

interface ArenaViewProps {
  language: 'ru' | 'en';
  catalog: ChatModelCatalog;
  modelsLoading: boolean;
  onRefreshModels: () => void;
  initialLeft: ModelSelection | null;
  systemPrompt?: string;
  onClose: () => void;
}

function sameSelection(a: ModelSelection | null, b: ModelSelection | null): boolean {
  return (a?.id || '') === (b?.id || '') && (a?.source || '') === (b?.source || '');
}

export function ArenaView({ language, catalog, modelsLoading, onRefreshModels, initialLeft, systemPrompt, onClose }: ArenaViewProps) {
  const copy = COPY[language];
  const [left, setLeft] = useState<ModelSelection | null>(initialLeft);
  const [right, setRight] = useState<ModelSelection | null>(null);
  const [prompt, setPrompt] = useState('');
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<ArenaResult[] | null>(null);
  const [error, setError] = useState('');

  const identical = sameSelection(left, right);

  const run = async () => {
    const text = prompt.trim();
    if (!text || identical) return;
    setRunning(true);
    setError('');
    setResults(null);
    try {
      setResults(await runArena(text, [left, right], { systemPrompt }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  const picker = (selection: ModelSelection | null, onSelect: (s: ModelSelection | null) => void) => (
    <ModelPicker
      catalog={catalog}
      selection={selection}
      onSelect={onSelect}
      onSetDefault={() => undefined}
      defaultSelection={null}
      onRefresh={onRefreshModels}
      loading={modelsLoading}
      language={language}
      disabled={running}
    />
  );

  const column = (result: ArenaResult | undefined, selection: ModelSelection | null, key: string) => (
    <section key={key} className="owui-arena-col" aria-label={selection?.id || copy.defaultModel}>
      <header>
        <span>{result?.model || selection?.id || catalog.default.name || copy.defaultModel}</span>
        {result && result.latency_ms != null && (
          <small>
            {(result.latency_ms / 1000).toFixed(1)} {copy.seconds}
            {result.output_tokens != null && ` · ${result.output_tokens} ${copy.tokens}`}
          </small>
        )}
      </header>
      {running && !result && <div className="owui-typing"><span /><span /><span /></div>}
      {result && result.error && !result.content && <div className="owui-error">{copy.failed}: {result.error}</div>}
      {result && result.content && <Markdown text={result.content} />}
    </section>
  );

  return (
    <div className="owui-arena" role="region" aria-label={copy.title}>
      <div className="owui-arena-bar">
        <Swords size={16} />
        <strong>{copy.title}</strong>
        {picker(left, setLeft)}
        <span className="owui-arena-vs">VS</span>
        {picker(right, setRight)}
        <div className="owui-spacer" />
        <button type="button" className="owui-icon-btn" onClick={onClose} aria-label={copy.close} title={copy.close}>
          <XIcon size={16} />
        </button>
      </div>
      <div className="owui-arena-grid">
        {column(results?.[0], left, 'left')}
        {column(results?.[1], right, 'right')}
      </div>
      <div className="owui-arena-input">
        <textarea
          className="owui-arena-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void run(); }
          }}
          placeholder={copy.placeholder}
          aria-label={copy.placeholder}
          disabled={running}
        />
        <button
          type="button"
          className="owui-send"
          onClick={() => void run()}
          disabled={running || !prompt.trim() || identical}
          title={copy.run}
          aria-label={copy.run}
        >
          <Send size={15} />
        </button>
      </div>
      <div className={`owui-composer-hint${error || identical ? ' is-offline' : ''}`}>
        {error || (identical ? copy.sameModel : running ? copy.running : copy.note)}
      </div>
    </div>
  );
}
