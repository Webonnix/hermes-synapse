import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Check, ChevronLeft, ChevronRight, CircleDollarSign, Layers3, Rocket, Settings2, WandSparkles } from 'lucide-react';
import type {
  CFChannel, CFPipelineCreateRequest, CFProviderRow, CFSettings, CFTemplate,
} from './types';
import './pipeline-wizard.css';

type Props = {
  language: 'ru' | 'en';
  templates: CFTemplate[];
  channels: CFChannel[];
  providers: CFProviderRow[];
  settings: CFSettings | null;
  busy: boolean;
  onCreate: (template: CFTemplate, request: CFPipelineCreateRequest) => Promise<void>;
};

const STEPS = [Layers3, WandSparkles, Settings2, Rocket];

function initialRequest(settings: CFSettings | null, language: 'ru' | 'en'): CFPipelineCreateRequest {
  return {
    name: '', templateId: '', activate: false,
    options: {
      channelId: '',
      language: settings?.general?.defaultLanguage ?? language,
      niche: settings?.general?.contentNiche ?? (language === 'ru' ? 'искусственный интеллект' : 'artificial intelligence'),
      targetAudience: settings?.general?.targetAudience ?? (language === 'ru' ? 'широкая аудитория' : 'general audience'),
      brandTone: settings?.general?.brandTone ?? (language === 'ru' ? 'экспертный и понятный' : 'expert and clear'),
      preset: settings?.general?.defaultPreset ?? 'YOUTUBE_SHORT',
      duration: settings?.general?.defaultDuration ?? 45,
      maxSources: settings?.generation?.maxSources ?? 6,
      temperature: settings?.generation?.temperature ?? 0.4,
      providerTimeoutSeconds: settings?.generation?.providerTimeoutSeconds ?? 300,
      maxAttempts: settings?.retry?.maxAttempts ?? 3,
      fallbackEnabled: settings?.retry?.fallbackEnabled ?? true,
      llmFallbackProviders: settings?.generation?.llmFallbackProviders ?? [],
      maxCostPerRun: settings?.budgets?.maxCostPerRun ?? 2,
      maxConcurrentRuns: settings?.advanced?.maxConcurrentRuns ?? 1,
      publishingMode: settings?.publishing?.mode ?? 'REVIEW_REQUIRED',
      privacy: settings?.publishing?.defaultPrivacy ?? 'private',
      subtitlesEnabled: settings?.rendering?.subtitlesEnabled ?? true,
      madeForKids: settings?.publishing?.madeForKids ?? false,
    },
  };
}

export function PipelineWizard({ language, templates, channels, providers, settings, busy, onCreate }: Props) {
  const ru = language === 'ru';
  const [step, setStep] = useState(0);
  const [seeded, setSeeded] = useState(false);
  const [draft, setDraft] = useState<CFPipelineCreateRequest>(() => initialRequest(settings, language));

  useEffect(() => {
    if (!settings || seeded) return;
    setDraft(current => ({ ...initialRequest(settings, language), name: current.name, templateId: current.templateId }));
    setSeeded(true);
  }, [settings, language, seeded]);

  const template = templates.find(item => item.id === draft.templateId);
  const channel = channels.find(item => item.id === draft.options.channelId);
  const hasPublish = draft.templateId !== 'manual_video';
  const llmProviders = providers.filter(item => item.category.toUpperCase() === 'LLM');
  const errors = useMemo(() => {
    const items: string[] = [];
    if (!draft.templateId) items.push(ru ? 'Выберите шаблон' : 'Select a template');
    if (draft.name.trim().length < 3) items.push(ru ? 'Название должно содержать минимум 3 символа' : 'Name must contain at least 3 characters');
    if (draft.options.duration < 10 || draft.options.duration > 600) items.push(ru ? 'Длительность: от 10 до 600 секунд' : 'Duration must be between 10 and 600 seconds');
    if (draft.options.maxSources < 1 || draft.options.maxSources > 50) items.push(ru ? 'Источников: от 1 до 50' : 'Sources must be between 1 and 50');
    if (draft.options.temperature < 0 || draft.options.temperature > 2) items.push('Temperature: 0–2');
    if (draft.options.maxAttempts < 1 || draft.options.maxAttempts > 10) items.push(ru ? 'Попыток: от 1 до 10' : 'Attempts must be between 1 and 10');
    if (draft.options.providerTimeoutSeconds < 10 || draft.options.providerTimeoutSeconds > 7200) items.push(ru ? 'Таймаут: от 10 до 7200 секунд' : 'Timeout must be between 10 and 7200 seconds');
    if (draft.options.maxCostPerRun != null && draft.options.maxCostPerRun < 0) items.push(ru ? 'Бюджет не может быть отрицательным' : 'Run budget cannot be negative');
    if (draft.options.maxConcurrentRuns < 1 || draft.options.maxConcurrentRuns > 20) items.push(ru ? 'Параллельных прогонов: от 1 до 20' : 'Concurrent runs must be between 1 and 20');
    if (hasPublish && draft.activate && !draft.options.channelId) items.push(ru ? 'Для активации подключите аккаунт публикации' : 'Choose a publishing account before activation');
    if (hasPublish && draft.options.publishingMode === 'FULL_AUTO' && !draft.options.channelId) items.push(ru ? 'FULL_AUTO невозможен без аккаунта' : 'FULL_AUTO requires an account');
    if (hasPublish && draft.activate && draft.options.channelId && channel?.status !== 'CONNECTED') items.push(ru ? 'Выбранный аккаунт не подключён' : 'The selected account is not connected');
    if (hasPublish && draft.activate && channel && settings?.network?.requireProxyForPublishing && !channel.proxy_enabled) {
      items.push(ru ? 'Для публикации обязателен proxy, но у аккаунта он не настроен' : 'Publishing requires a proxy, but this account has none');
    }
    return items;
  }, [draft, hasPublish, channel, settings, ru]);
  const warnings = [
    ...(draft.options.fallbackEnabled && !draft.options.llmFallbackProviders.length
      ? [ru ? 'Не выбран LLM fallback: сбой основной модели остановит AI-этап.' : 'No LLM fallback selected: a primary model outage will stop AI stages.'] : []),
    ...(channel && channel.status !== 'CONNECTED' && !draft.activate
      ? [ru ? 'Выбранный аккаунт не подключён.' : 'The selected account is not connected.'] : []),
    ...(draft.activate ? [] : [ru ? 'Pipeline будет создан как черновик.' : 'The pipeline will be created as a draft.']),
  ];

  const patchOptions = (values: Partial<CFPipelineCreateRequest['options']>) => {
    setDraft(current => ({ ...current, options: { ...current.options, ...values } }));
  };
  const stepNames = ru
    ? ['Основа', 'Контент', 'Надёжность', 'Запуск']
    : ['Foundation', 'Content', 'Reliability', 'Launch'];
  const canContinue = step > 0 || (!!draft.templateId && draft.name.trim().length >= 3);

  return (
    <div className="cfw-wizard">
      <header><WandSparkles size={14} /><span>{ru ? 'Мастер pipeline' : 'Pipeline wizard'}</span><b>{step + 1}/4</b></header>
      <div className="cfw-wizard-steps">
        {STEPS.map((Icon, index) => (
          <button key={stepNames[index]} type="button" className={index === step ? 'active' : index < step ? 'done' : ''}
            onClick={() => { if (index <= step || canContinue) setStep(index); }}>
            <Icon size={12} /><span>{stepNames[index]}</span>{index < step && <Check size={10} />}
          </button>
        ))}
      </div>

      <div className="cfw-wizard-body">
        {step === 0 && <>
          <label>{ru ? 'Название pipeline' : 'Pipeline name'}</label>
          <input aria-label={ru ? 'Название нового pipeline' : 'New pipeline name'} value={draft.name}
            onChange={event => setDraft(current => ({ ...current, name: event.target.value }))}
            placeholder={ru ? 'Например: AI-дайджест каждый день' : 'For example: Daily AI digest'} />
          <label>{ru ? 'Сценарий производства' : 'Production workflow'}</label>
          <div className="cfw-wizard-templates">
            {templates.map(item => <button type="button" key={item.id} className={item.id === draft.templateId ? 'selected' : ''}
              onClick={() => setDraft(current => ({ ...current, templateId: item.id, name: current.name || item.name }))}>
              <span><Layers3 size={13} /><strong>{item.name}</strong>{item.id === 'ai_news_shorts' && <em>{ru ? 'Рекомендуется' : 'Recommended'}</em>}</span>
              <small>{item.description}</small>
            </button>)}
          </div>
        </>}

        {step === 1 && <>
          <div className="cfw-wizard-row">
            <div><label>{ru ? 'Язык' : 'Language'}</label><select value={draft.options.language} onChange={event => patchOptions({ language: event.target.value })}>
              <option value="ru">Русский</option><option value="en">English</option>
            </select></div>
            <div><label>{ru ? 'Формат' : 'Format'}</label><select value={draft.options.preset} onChange={event => patchOptions({ preset: event.target.value })}>
              <option value="YOUTUBE_SHORT">Shorts 9:16</option><option value="TIKTOK">TikTok 9:16</option>
              <option value="YOUTUBE_STANDARD">YouTube 16:9</option>
            </select></div>
          </div>
          <label>{ru ? 'Тематика / ниша' : 'Topic / niche'}</label>
          <input value={draft.options.niche} onChange={event => patchOptions({ niche: event.target.value })} />
          <label>{ru ? 'Целевая аудитория' : 'Target audience'}</label>
          <input value={draft.options.targetAudience} onChange={event => patchOptions({ targetAudience: event.target.value })} />
          <label>{ru ? 'Тон и стиль' : 'Tone and style'}</label>
          <input value={draft.options.brandTone} onChange={event => patchOptions({ brandTone: event.target.value })} />
          <div className="cfw-wizard-row">
            <div><label>{ru ? 'Длительность, сек' : 'Duration, sec'}</label><input type="number" min={10} max={600} value={draft.options.duration}
              onChange={event => patchOptions({ duration: Number(event.target.value) })} /></div>
            <div className="cfw-wizard-toggle"><label><input type="checkbox" checked={draft.options.subtitlesEnabled}
              onChange={event => patchOptions({ subtitlesEnabled: event.target.checked })} />{ru ? 'Субтитры' : 'Subtitles'}</label></div>
          </div>
        </>}

        {step === 2 && <>
          <div className="cfw-wizard-row">
            <div><label>{ru ? 'Источников' : 'Sources'}</label><input type="number" min={1} max={50} value={draft.options.maxSources}
              onChange={event => patchOptions({ maxSources: Number(event.target.value) })} /></div>
            <div><label>Temperature</label><input type="number" min={0} max={2} step={0.1} value={draft.options.temperature}
              onChange={event => patchOptions({ temperature: Number(event.target.value) })} /></div>
          </div>
          <div className="cfw-wizard-row">
            <div><label>{ru ? 'Попыток на этап' : 'Attempts per stage'}</label><input type="number" min={1} max={10} value={draft.options.maxAttempts}
              onChange={event => patchOptions({ maxAttempts: Number(event.target.value) })} /></div>
            <div><label>{ru ? 'Таймаут, сек' : 'Timeout, sec'}</label><input type="number" min={10} max={7200} value={draft.options.providerTimeoutSeconds}
              onChange={event => patchOptions({ providerTimeoutSeconds: Number(event.target.value) })} /></div>
          </div>
          <div className="cfw-wizard-row">
            <div><label>{ru ? 'Бюджет прогона, $' : 'Run budget, $'}</label><input type="number" min={0} step={0.5} value={draft.options.maxCostPerRun ?? ''}
              onChange={event => patchOptions({ maxCostPerRun: event.target.value === '' ? null : Number(event.target.value) })} /></div>
            <div><label>{ru ? 'Параллельных прогонов' : 'Concurrent runs'}</label><input type="number" min={1} max={20} value={draft.options.maxConcurrentRuns}
              onChange={event => patchOptions({ maxConcurrentRuns: Number(event.target.value) })} /></div>
          </div>
          <label className="cfw-wizard-switch"><span><strong>Fallback</strong><small>{ru ? 'Переключаться при ошибках и rate limit' : 'Switch on errors and rate limits'}</small></span>
            <input type="checkbox" checked={draft.options.fallbackEnabled} onChange={event => patchOptions({ fallbackEnabled: event.target.checked })} /></label>
          {draft.options.fallbackEnabled && <div className="cfw-provider-picks">
            {llmProviders.map(provider => {
              const checked = draft.options.llmFallbackProviders.includes(provider.id);
              return <label key={provider.id} className={checked ? 'selected' : ''}><input type="checkbox" checked={checked} onChange={() => patchOptions({
                llmFallbackProviders: checked
                  ? draft.options.llmFallbackProviders.filter(id => id !== provider.id)
                  : [...draft.options.llmFallbackProviders, provider.id],
              })} /><span>{provider.id}<small>{provider.model || provider.category}</small></span></label>;
            })}
            {!llmProviders.length && <p>{ru ? 'Сначала подключите LLM во вкладке «Интеграции».' : 'Connect an LLM in Integrations first.'}</p>}
          </div>}
        </>}

        {step === 3 && <>
          {hasPublish && <>
            <label>{ru ? 'Аккаунт публикации' : 'Publishing account'}</label>
            <select value={draft.options.channelId} onChange={event => patchOptions({ channelId: event.target.value })}>
              <option value="">{ru ? 'Не подключать — настроить позже' : 'None — configure later'}</option>
              {channels.map(item => <option key={item.id} value={item.id}>{item.platform} · {item.account_name} · {item.status}</option>)}
            </select>
            <div className="cfw-wizard-row">
              <div><label>{ru ? 'Режим' : 'Mode'}</label><select value={draft.options.publishingMode} onChange={event => patchOptions({ publishingMode: event.target.value })}>
                <option value="MANUAL">MANUAL</option><option value="REVIEW_REQUIRED">REVIEW_REQUIRED</option><option value="FULL_AUTO">FULL_AUTO</option>
              </select></div>
              <div><label>{ru ? 'Видимость' : 'Privacy'}</label><select value={draft.options.privacy} onChange={event => patchOptions({ privacy: event.target.value })}>
                <option value="private">private</option><option value="unlisted">unlisted</option><option value="public">public</option>
              </select></div>
            </div>
            <label className="cfw-wizard-switch"><span><strong>{ru ? 'Контент для детей' : 'Made for kids'}</strong><small>{ru ? 'Передаётся платформе при публикации' : 'Sent to the platform when publishing'}</small></span>
              <input type="checkbox" checked={draft.options.madeForKids} onChange={event => patchOptions({ madeForKids: event.target.checked })} /></label>
          </>}
          <label className="cfw-wizard-switch"><span><strong>{ru ? 'Активировать сразу' : 'Activate immediately'}</strong><small>{ru ? 'Только если backend-валидация пройдена' : 'Only when backend validation passes'}</small></span>
            <input type="checkbox" checked={draft.activate} onChange={event => setDraft(current => ({ ...current, activate: event.target.checked }))} /></label>
          <div className="cfw-wizard-summary">
            <h4>{ru ? 'Итоговая конфигурация' : 'Configuration summary'}</h4>
            <dl><div><dt>{ru ? 'Шаблон' : 'Template'}</dt><dd>{template?.name ?? '—'}</dd></div>
              <div><dt>{ru ? 'Контент' : 'Content'}</dt><dd>{draft.options.language.toUpperCase()} · {draft.options.preset} · {draft.options.duration}s</dd></div>
              <div><dt>{ru ? 'Надёжность' : 'Reliability'}</dt><dd>{draft.options.maxAttempts}× · {draft.options.llmFallbackProviders.length} fallback</dd></div>
              <div><dt>{ru ? 'Лимит' : 'Limit'}</dt><dd>{draft.options.maxCostPerRun == null ? '∞' : `$${draft.options.maxCostPerRun}`}</dd></div>
              <div><dt>{ru ? 'Публикация' : 'Publishing'}</dt><dd>{hasPublish ? (channel?.account_name || (ru ? 'позже' : 'later')) : (ru ? 'отключена' : 'disabled')}</dd></div></dl>
          </div>
          {!!errors.length && <div className="cfw-wizard-messages error"><AlertTriangle size={13} /><span>{errors.map(item => <small key={item}>{item}</small>)}</span></div>}
          {!!warnings.length && <div className="cfw-wizard-messages warning"><AlertTriangle size={13} /><span>{warnings.map(item => <small key={item}>{item}</small>)}</span></div>}
        </>}
      </div>

      <footer>
        <button type="button" disabled={step === 0 || busy} onClick={() => setStep(value => value - 1)}><ChevronLeft size={12} />{ru ? 'Назад' : 'Back'}</button>
        <span />
        {step < 3
          ? <button type="button" className="primary" disabled={!canContinue || busy} onClick={() => setStep(value => value + 1)}>{ru ? 'Далее' : 'Next'}<ChevronRight size={12} /></button>
          : <button type="button" className="primary" disabled={busy || errors.length > 0} onClick={async () => {
              if (hasPublish && draft.options.publishingMode === 'FULL_AUTO' && !window.confirm(ru
                ? 'Создать pipeline с автоматической публикацией без ручного подтверждения?'
                : 'Create a pipeline with publishing that bypasses manual review?')) return;
              if (!template) return;
              await onCreate(template, { ...draft, name: draft.name.trim() });
            }}><Rocket size={12} />{draft.activate ? (ru ? 'Создать и активировать' : 'Create & activate') : (ru ? 'Создать черновик' : 'Create draft')}</button>}
      </footer>
      <div className="cfw-wizard-footnote"><CircleDollarSign size={11} />{ru ? 'Любая сумма — это верхний лимит, а не ожидаемая стоимость.' : 'Any amount is a hard ceiling, not an estimated cost.'}</div>
    </div>
  );
}
