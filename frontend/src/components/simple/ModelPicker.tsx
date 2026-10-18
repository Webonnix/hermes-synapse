import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Check, ChevronDown, Cpu, Cloud, Pin, RefreshCw, Search } from 'lucide-react';
import type { ChatModel, ChatModelCatalog, ModelSelection, ModelSource } from './chatModels';
import { modelMeta } from './chatModels';

/**
 * The navbar model selector, modelled on Open WebUI's: a trigger showing the
 * active model, and a dropdown with a search field, Local/External filters and
 * the catalogue grouped by provider.
 *
 * "Local" is the llama.cpp server on this box; "External" is everything OmniRoute can route to.
 * The two are never merged into one flat list — which side a model sits on
 * decides whether the conversation leaves the machine, and that is the single
 * most important thing this control communicates.
 */

interface ModelPickerProps {
  catalog: ChatModelCatalog;
  selection: ModelSelection | null;
  onSelect: (selection: ModelSelection) => void;
  onSetDefault: (selection: ModelSelection | null) => void;
  defaultSelection: ModelSelection | null;
  onRefresh: () => void;
  loading: boolean;
  language: 'ru' | 'en';
  /** Disabled while a reply is streaming — swapping mid-turn would be a lie. */
  disabled?: boolean;
}

const COPY = {
  ru: {
    select: 'Выбрать модель',
    search: 'Поиск модели',
    all: 'Все',
    local: 'Локальные',
    external: 'Внешние',
    noResults: 'Ничего не найдено',
    noModels: 'Моделей нет',
    connectHint: 'Запустите локальный сервер llama.cpp или укажите ключ OmniRoute в «Настройки → API-ключи».',
    setDefault: 'Сделать моделью по умолчанию',
    isDefault: 'Модель по умолчанию',
    refresh: 'Обновить список',
    defaultModel: 'По умолчанию',
    unavailable: 'недоступно',
    tools: 'инструменты',
    vision: 'зрение',
    loaded: 'в памяти',
    hidden: 'Скрыто тегов, непригодных для чата',
    hiddenBackup: 'резервная копия',
    hiddenNotChat: 'не чат-модель',
  },
  en: {
    select: 'Select a model',
    search: 'Search a model',
    all: 'All',
    local: 'Local',
    external: 'External',
    noResults: 'No results found',
    noModels: 'No models available',
    connectHint: 'Start the local llama.cpp server, or add the OmniRoute key under Settings → API keys.',
    setDefault: 'Set as default',
    isDefault: 'Default model',
    refresh: 'Refresh list',
    defaultModel: 'Default',
    unavailable: 'unavailable',
    tools: 'tools',
    vision: 'vision',
    loaded: 'in memory',
    hidden: 'Hidden tags that cannot serve a chat',
    hiddenBackup: 'backup',
    hiddenNotChat: 'not a chat model',
  },
} as const;

type Filter = 'all' | ModelSource;
// Widened from the literal types COPY carries, so either language's table fits.
type Copy = { [K in keyof (typeof COPY)['ru']]: string };

/** Group rows the way the dropdown lists them: local under "llama.cpp", external
 * under whichever provider OmniRoute says owns the model. */
function groupModels(models: ChatModel[]): [string, ChatModel[]][] {
  const groups = new Map<string, ChatModel[]>();
  models.forEach(model => {
    const key = model.source === 'local' ? 'llama.cpp' : (model.owned_by || 'OmniRoute');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(model);
  });
  // Local first, then providers alphabetically — the local group is the one a
  // user reaches for most, and it should not move as the router catalogue grows.
  return [...groups.entries()].sort((a, b) => {
    if (a[0] === 'llama.cpp') return -1;
    if (b[0] === 'llama.cpp') return 1;
    return a[0].localeCompare(b[0]);
  });
}

function capabilityBadges(model: ChatModel, copy: Copy): string[] {
  const caps = model.capabilities || {};
  const badges: string[] = [];
  // Which local model is resident right now is the most practical fact in the
  // list: any other one costs a ~20GB reload before it answers.
  if (model.source === 'local' && model.loaded) badges.push(copy.loaded);
  if (caps.tools || caps.function_calling) badges.push(copy.tools);
  if (caps.vision || caps.image_input) badges.push(copy.vision);
  return badges;
}

export function ModelPicker({
  catalog, selection, onSelect, onSetDefault, defaultSelection,
  onRefresh, loading, language, disabled = false,
}: ModelPickerProps) {
  const copy = COPY[language];
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const active = useMemo(() => {
    if (!selection) return null;
    return (catalog[selection.source] || []).find(m => m.id === selection.id) || null;
  }, [catalog, selection]);

  const triggerLabel = active?.name || selection?.id || catalog.default.name || copy.select;

  const filtered = useMemo(() => {
    const pool = filter === 'all'
      ? [...catalog.local, ...catalog.router]
      : catalog[filter];
    const q = query.trim().toLowerCase();
    if (!q) return pool;
    return pool.filter(m =>
      m.id.toLowerCase().includes(q) ||
      m.name.toLowerCase().includes(q) ||
      (m.owned_by || '').toLowerCase().includes(q));
  }, [catalog, filter, query]);

  const grouped = useMemo(() => groupModels(filtered), [filtered]);
  // The flat order the arrow keys walk, kept in sync with what is rendered.
  const flat = useMemo(() => grouped.flatMap(([, models]) => models), [grouped]);

  useEffect(() => { setHighlight(0); }, [query, filter, open]);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onClickOutside);
    return () => window.removeEventListener('mousedown', onClickOutside);
  }, [open]);

  useEffect(() => {
    if (open) window.setTimeout(() => searchRef.current?.focus(), 10);
  }, [open]);

  // Keep the keyboard-highlighted row visible without yanking the mouse user
  // around — only the arrow keys change `highlight`.
  useEffect(() => {
    if (!open || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>('.owui-model-row.is-highlighted');
    el?.scrollIntoView({ block: 'nearest' });
  }, [highlight, open]);

  const choose = (model: ChatModel) => {
    onSelect({ id: model.id, source: model.source });
    setOpen(false);
    setQuery('');
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { setOpen(false); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); setHighlight(h => Math.min(h + 1, flat.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHighlight(h => Math.max(h - 1, 0)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const model = flat[highlight];
      if (model) choose(model);
    }
  };

  const isDefault = (model: ChatModel) =>
    defaultSelection?.id === model.id && defaultSelection?.source === model.source;

  const counts = { all: catalog.local.length + catalog.router.length, local: catalog.local.length, router: catalog.router.length };
  const errors = [catalog.local_error, catalog.router_error].filter(Boolean) as string[];
  // Named, not just counted: a tag disappearing from the picker is exactly the
  // kind of change that reads as a bug unless the reason is right there.
  const hidden = catalog.local_hidden || [];
  const hiddenNote = hidden.length === 0 ? '' : `${copy.hidden}: ${hidden
    .map(entry => `${entry.id} (${entry.reason === 'backup' ? copy.hiddenBackup : copy.hiddenNotChat})`)
    .join(', ')}`;

  return (
    <div className="owui-model-picker" ref={rootRef}>
      <button
        type="button"
        className={`owui-model-trigger${open ? ' is-open' : ''}`}
        onClick={() => setOpen(v => !v)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={selection?.id || triggerLabel}
      >
        {active?.source === 'router'
          ? <Cloud size={14} className="owui-model-trigger-icon" />
          : <Cpu size={14} className="owui-model-trigger-icon" />}
        <span className="owui-model-trigger-label">{triggerLabel}</span>
        <ChevronDown size={14} className="owui-model-chevron" />
      </button>

      {open && (
        <div className="owui-model-menu" role="listbox" onKeyDown={onKeyDown}>
          <div className="owui-model-search">
            <Search size={14} />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={copy.search}
              aria-label={copy.search}
            />
            <button
              type="button"
              className={`owui-icon-btn owui-model-refresh${loading ? ' is-spinning' : ''}`}
              onClick={onRefresh}
              title={copy.refresh}
              aria-label={copy.refresh}
            >
              <RefreshCw size={13} />
            </button>
          </div>

          <div className="owui-model-filters">
            {([['all', copy.all], ['local', copy.local], ['router', copy.external]] as [Filter, string][])
              .map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={`owui-chip${filter === value ? ' is-active' : ''}`}
                  onClick={() => setFilter(value)}
                >
                  {label}
                  <span className="owui-chip-count">{counts[value === 'router' ? 'router' : value]}</span>
                </button>
              ))}
          </div>

          <div className="owui-model-list" ref={listRef}>
            {grouped.length === 0 ? (
              <div className="owui-model-empty">
                <div>{counts.all === 0 ? copy.noModels : copy.noResults}</div>
                {counts.all === 0 && <p>{copy.connectHint}</p>}
              </div>
            ) : grouped.map(([group, models]) => (
              <div key={group} className="owui-model-group">
                <div className="owui-model-group-label">
                  {group === 'llama.cpp' ? <Cpu size={11} /> : <Cloud size={11} />}
                  <span>{group}</span>
                  <span className="owui-model-group-count">{models.length}</span>
                </div>
                {models.map(model => {
                  const index = flat.indexOf(model);
                  const selected = selection?.id === model.id && selection?.source === model.source;
                  const badges = capabilityBadges(model, copy);
                  return (
                    <div
                      key={`${model.source}:${model.id}`}
                      className={`owui-model-row${selected ? ' is-selected' : ''}${index === highlight ? ' is-highlighted' : ''}`}
                      role="option"
                      aria-selected={selected}
                      tabIndex={-1}
                      onClick={() => choose(model)}
                      onMouseEnter={() => setHighlight(index)}
                    >
                      <div className="owui-model-row-main">
                        <span className="owui-model-row-name">{model.name}</span>
                        {badges.map(badge => <span key={badge} className="owui-badge">{badge}</span>)}
                        {isDefault(model) && <span className="owui-badge is-default">{copy.defaultModel}</span>}
                      </div>
                      <div className="owui-model-row-meta">{modelMeta(model)}</div>
                      <div className="owui-model-row-actions">
                        <button
                          type="button"
                          className={`owui-icon-btn owui-model-pin${isDefault(model) ? ' is-active' : ''}`}
                          title={isDefault(model) ? copy.isDefault : copy.setDefault}
                          aria-label={isDefault(model) ? copy.isDefault : copy.setDefault}
                          onClick={(e) => {
                            e.stopPropagation();
                            onSetDefault(isDefault(model) ? null : { id: model.id, source: model.source });
                          }}
                        >
                          <Pin size={12} />
                        </button>
                        {selected && <Check size={14} className="owui-model-check" />}
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>

          {(errors.length > 0 || hiddenNote) && (
            <div className="owui-model-errors">
              {errors.map(err => (
                <div key={err} className="owui-model-error">
                  <AlertTriangle size={12} />
                  <span>{err}</span>
                </div>
              ))}
              {hiddenNote && <div className="owui-model-hidden-note">{hiddenNote}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
