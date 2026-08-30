/**
 * Content Factory settings (§101).
 *
 * Edits a local copy and saves the whole document on demand, so a half-typed
 * budget never reaches the backend. Two things are deliberately harder than the
 * rest: switching publishing to FULL_AUTO and clearing a budget both ask for
 * confirmation, because either one turns a supervised system into an
 * unsupervised one that spends money.
 */

import { useEffect, useState } from 'react';
import { Save, ShieldAlert } from 'lucide-react';
import { COLORS, Field, Panel, inputStyle } from './ui';
import type { CFSettings } from './types';

type Props = {
  settings: CFSettings | null;
  language: 'ru' | 'en';
  busy?: boolean;
  canEdit?: boolean;
  onSave: (patch: Partial<CFSettings>) => Promise<void>;
};

const SETTINGS_FALLBACK: CFSettings = {
  general: {
    defaultLanguage: 'ru', timezone: 'UTC', defaultPreset: 'YOUTUBE_SHORT', defaultDuration: 45,
    contentNiche: 'искусственный интеллект', targetAudience: 'широкая аудитория',
    brandTone: 'экспертный и понятный',
  },
  generation: {
    maxSources: 6, temperature: 0.4, maxTokens: 4000, imageParallelism: 3, clipDuration: 5,
    providerTimeoutSeconds: 300, llmFallbackProviders: [], imageFallbackProviders: [],
    videoFallbackProviders: [], voiceFallbackProviders: [],
  },
  publishing: {
    mode: 'REVIEW_REQUIRED', defaultPrivacy: 'private', discloseAiGenerated: true,
    madeForKids: false, youtubeCategoryId: '22', defaultTags: [],
  },
  budgets: { maxCostPerRun: 2, dailyBudget: 25, monthlyBudget: 400, onExceeded: 'stop' },
  rendering: {
    subtitlesEnabled: true, subtitleStyle: 'default', subtitleFont: 'Inter',
    subtitlePosition: 'bottom', musicGainDb: -18, normalizeAudio: true,
    loudnessTargetLufs: -14, transition: 'fade', watermarkAssetId: '',
  },
  policies: {
    denylist: [], minQualityScore: 0.7, requireSources: true, minSources: 2,
    maxSourceAgeDays: 30, blockProfanity: true,
  },
  retry: {
    maxAttempts: 3, initialDelaySeconds: 5, maxDelaySeconds: 60,
    backoffMultiplier: 2, fallbackEnabled: true,
  },
  network: { requireProxyForPublishing: false, connectionTimeoutSeconds: 120, analyticsRefreshMinutes: 60 },
  storage: { retentionDays: 90, keepSourceAssets: true, keepFailedRuns: true, signedUrlMinutes: 360 },
  notifications: {
    onFailure: true, onReviewRequired: true, onBudgetReached: true, onPublished: false,
    onProviderFallback: true, onPolicyBlocked: true,
  },
  advanced: {
    pollSeconds: 5, maxConcurrentRuns: 5, maxParallelNodes: 4, leaseSeconds: 120,
    logRetentionDays: 30,
  },
};

function normalizeSettings(settings: CFSettings | null): CFSettings | null {
  if (!settings) return null;
  return Object.fromEntries(Object.entries(SETTINGS_FALLBACK).map(([section, defaults]) => [
    section,
    { ...defaults, ...((settings as unknown as Record<string, Record<string, unknown>>)[section] || {}) },
  ])) as unknown as CFSettings;
}

export function SettingsPanel({ settings, language, busy, canEdit = true, onSave }: Props) {
  const ru = language === 'ru';
  const [draft, setDraft] = useState<CFSettings | null>(() => normalizeSettings(settings));
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setDraft(normalizeSettings(settings));
    setDirty(false);
  }, [settings]);

  if (!draft) {
    return <div style={{ color: COLORS.faint, fontSize: 12 }}>{ru ? 'Загрузка…' : 'Loading…'}</div>;
  }

  const patch = <S extends keyof CFSettings>(section: S, values: Partial<CFSettings[S]>) => {
    setDraft((current) => (current ? { ...current, [section]: { ...current[section], ...values } } : current));
    setDirty(true);
  };

  const save = async () => {
    await onSave(draft);
    setDirty(false);
  };

  const fullAuto = draft.publishing.mode === 'FULL_AUTO';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ flex: 1, fontSize: 11.5, color: dirty ? COLORS.amberSoft : COLORS.faint }}>
          {dirty
            ? (ru ? 'Есть несохранённые изменения' : 'Unsaved changes')
            : (ru ? 'Все изменения сохранены' : 'All changes saved')}
        </span>
        <button
          type="button"
          className="btn-primary"
          style={{ fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6 }}
          disabled={!dirty || busy || !canEdit}
          title={!canEdit ? (ru ? 'Недостаточно прав' : 'Insufficient permissions') : undefined}
          onClick={() => void save()}
        >
          <Save size={12} />{ru ? 'Сохранить' : 'Save'}
        </button>
      </div>

      {fullAuto && (
        <div style={{
          display: 'flex', gap: 9, padding: '10px 14px', borderRadius: 10, fontSize: 11.5,
          color: COLORS.amberSoft, background: 'rgba(69,45,10,0.5)',
          border: '1px solid rgba(245,158,11,0.35)', lineHeight: 1.5,
        }}>
          <ShieldAlert size={14} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{ru
            ? 'FULL_AUTO: видео публикуются без вашей проверки. Убедитесь, что QA-порог и denylist настроены, а правила площадок по AI-контенту соблюдены.'
            : 'FULL_AUTO: videos publish without your review. Make sure the QA threshold and denylist are set and platform AI-content rules are met.'}
          </span>
        </div>
      )}

      <div style={{ padding: '9px 13px', borderRadius: 9, fontSize: 10.5, lineHeight: 1.5,
        color: COLORS.faint, background: 'rgba(14,116,144,0.08)', border: '1px solid rgba(34,211,238,0.18)' }}>
        {ru
          ? 'Глобальные параметры применяются к новым pipeline из шаблонов. Лимиты воркера и обязательный прокси вступают в силу сразу; существующие pipeline сохраняют свою версию настроек.'
          : 'Global defaults apply to new template pipelines. Worker limits and the mandatory-proxy policy take effect immediately; existing pipelines retain their versioned settings.'}
      </div>

      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <Panel title={ru ? 'Общие' : 'General'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Field label={ru ? 'Язык по умолчанию' : 'Default language'}>
            <select style={inputStyle} value={draft.general.defaultLanguage}
              onChange={(event) => patch('general', { defaultLanguage: event.target.value })}>
              <option value="ru">Русский</option>
              <option value="en">English</option>
            </select>
          </Field>
          <Field label={ru ? 'Часовой пояс' : 'Timezone'}
            hint={ru ? 'Расписания всегда считаются в нём, а не по часам сервера (§48).'
                     : 'Schedules are evaluated in it, never in server time (§48).'}>
            <input style={inputStyle} value={draft.general.timezone}
              onChange={(event) => patch('general', { timezone: event.target.value })} />
          </Field>
          <div style={{ display: 'flex', gap: 10 }}>
            <div style={{ flex: 1 }}>
              <Field label={ru ? 'Формат' : 'Preset'}>
                <select style={inputStyle} value={draft.general.defaultPreset}
                  onChange={(event) => patch('general', { defaultPreset: event.target.value })}>
                  <option value="YOUTUBE_SHORT">YouTube Shorts</option>
                  <option value="TIKTOK">TikTok</option>
                  <option value="YOUTUBE_STANDARD">YouTube 16:9</option>
                </select>
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label={ru ? 'Длительность, сек' : 'Duration, s'}>
                <input style={inputStyle} type="number" min={10} max={600}
                  value={draft.general.defaultDuration}
                  onChange={(event) => patch('general', { defaultDuration: Number(event.target.value) || 45 })} />
              </Field>
            </div>
          </div>
          <Field label={ru ? 'Тематика по умолчанию' : 'Default niche'}>
            <input style={inputStyle} value={draft.general.contentNiche}
              onChange={(event) => patch('general', { contentNiche: event.target.value })} />
          </Field>
          <Field label={ru ? 'Целевая аудитория' : 'Target audience'}>
            <input style={inputStyle} value={draft.general.targetAudience}
              onChange={(event) => patch('general', { targetAudience: event.target.value })} />
          </Field>
          <Field label={ru ? 'Тон бренда' : 'Brand tone'}>
            <input style={inputStyle} value={draft.general.brandTone}
              onChange={(event) => patch('general', { brandTone: event.target.value })} />
          </Field>
        </Panel>

        <Panel title={ru ? 'Генерация и модели' : 'Generation & models'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Источников' : 'Sources'} value={draft.generation.maxSources} min={1} max={50}
              onChange={(value) => patch('generation', { maxSources: value })} />
            <CompactNumber label={ru ? 'Температура' : 'Temperature'} value={draft.generation.temperature} min={0} max={2} step={0.1}
              onChange={(value) => patch('generation', { temperature: value })} />
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label="Max tokens" value={draft.generation.maxTokens} min={128} max={128000}
              onChange={(value) => patch('generation', { maxTokens: value })} />
            <CompactNumber label={ru ? 'Параллельно визуалов' : 'Parallel visuals'} value={draft.generation.imageParallelism} min={1} max={16}
              onChange={(value) => patch('generation', { imageParallelism: value })} />
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Клип, сек' : 'Clip, sec'} value={draft.generation.clipDuration} min={1} max={60}
              onChange={(value) => patch('generation', { clipDuration: value })} />
            <CompactNumber label={ru ? 'Таймаут модели, сек' : 'Model timeout, sec'} value={draft.generation.providerTimeoutSeconds} min={10} max={7200}
              onChange={(value) => patch('generation', { providerTimeoutSeconds: value })} />
          </div>
          <ListField label="LLM fallback" value={draft.generation.llmFallbackProviders}
            onChange={(value) => patch('generation', { llmFallbackProviders: value })} />
          <ListField label={ru ? 'Fallback изображений' : 'Image fallback'} value={draft.generation.imageFallbackProviders}
            onChange={(value) => patch('generation', { imageFallbackProviders: value })} />
          <ListField label={ru ? 'Fallback видео' : 'Video fallback'} value={draft.generation.videoFallbackProviders}
            onChange={(value) => patch('generation', { videoFallbackProviders: value })} />
          <ListField label={ru ? 'Fallback голоса' : 'Voice fallback'} value={draft.generation.voiceFallbackProviders}
            onChange={(value) => patch('generation', { voiceFallbackProviders: value })} />
        </Panel>

        <Panel title={ru ? 'Публикация' : 'Publishing'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Field label={ru ? 'Режим' : 'Mode'}
            hint={ru ? 'REVIEW_REQUIRED — по умолчанию: видео ждёт вашего одобрения.'
                     : 'REVIEW_REQUIRED is the default: a video waits for your approval.'}>
            <select style={inputStyle} value={draft.publishing.mode}
              onChange={(event) => {
                const value = event.target.value;
                if (value === 'FULL_AUTO' && !window.confirm(ru
                  ? 'Включить полностью автоматическую публикацию без ручной проверки?'
                  : 'Enable fully automatic publishing with no human review?')) return;
                patch('publishing', { mode: value });
              }}>
              <option value="MANUAL">MANUAL</option>
              <option value="REVIEW_REQUIRED">REVIEW_REQUIRED</option>
              <option value="FULL_AUTO">FULL_AUTO</option>
            </select>
          </Field>
          <Field label={ru ? 'Видимость по умолчанию' : 'Default privacy'}>
            <select style={inputStyle} value={draft.publishing.defaultPrivacy}
              onChange={(event) => patch('publishing', { defaultPrivacy: event.target.value })}>
              <option value="private">private</option>
              <option value="unlisted">unlisted</option>
              <option value="public">public</option>
            </select>
          </Field>
          <Toggle
            label={ru ? 'Помечать контент как AI-сгенерированный' : 'Disclose AI-generated content'}
            hint={ru ? 'Требование правил YouTube и TikTok. Выключать не рекомендуется.'
                     : 'Required by YouTube and TikTok policy. Not recommended to disable.'}
            checked={draft.publishing.discloseAiGenerated}
            onChange={(value) => patch('publishing', { discloseAiGenerated: value })}
          />
          <Toggle label={ru ? 'Контент для детей' : 'Made for kids'} checked={draft.publishing.madeForKids}
            onChange={(value) => patch('publishing', { madeForKids: value })} />
          <Field label={ru ? 'Категория YouTube' : 'YouTube category'}>
            <select style={inputStyle} value={draft.publishing.youtubeCategoryId}
              onChange={(event) => patch('publishing', { youtubeCategoryId: event.target.value })}>
              <option value="22">22 · People & Blogs</option>
              <option value="24">24 · Entertainment</option>
              <option value="27">27 · Education</option>
              <option value="28">28 · Science & Technology</option>
            </select>
          </Field>
          <ListField label={ru ? 'Теги по умолчанию' : 'Default tags'} value={draft.publishing.defaultTags}
            onChange={(value) => patch('publishing', { defaultTags: value })} />
        </Panel>

        <Panel title={ru ? 'Бюджеты' : 'Budgets'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <NumberField
            label={ru ? 'Максимум на прогон, $' : 'Max per run, $'}
            value={draft.budgets.maxCostPerRun}
            onChange={(value) => patch('budgets', { maxCostPerRun: value })}
            language={language}
          />
          <NumberField
            label={ru ? 'Дневной бюджет, $' : 'Daily budget, $'}
            value={draft.budgets.dailyBudget}
            onChange={(value) => patch('budgets', { dailyBudget: value })}
            language={language}
          />
          <NumberField
            label={ru ? 'Месячный бюджет, $' : 'Monthly budget, $'}
            value={draft.budgets.monthlyBudget}
            onChange={(value) => patch('budgets', { monthlyBudget: value })}
            language={language}
          />
          <Field label={ru ? 'При превышении' : 'On exceeded'}>
            <select style={inputStyle} value={draft.budgets.onExceeded}
              onChange={(event) => patch('budgets', { onExceeded: event.target.value })}>
              <option value="stop">{ru ? 'остановить прогон' : 'stop the run'}</option>
              <option value="approval">{ru ? 'запросить подтверждение' : 'ask for approval'}</option>
              <option value="cheaper">{ru ? 'перейти на дешёвую модель' : 'fall back to a cheaper model'}</option>
            </select>
          </Field>
        </Panel>

        <Panel title={ru ? 'Рендер' : 'Rendering'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Toggle
            label={ru ? 'Субтитры' : 'Subtitles'}
            checked={draft.rendering.subtitlesEnabled}
            onChange={(value) => patch('rendering', { subtitlesEnabled: value })}
          />
          <Field label={ru ? 'Стиль субтитров' : 'Subtitle style'}>
            <select style={inputStyle} value={draft.rendering.subtitleStyle}
              onChange={(event) => patch('rendering', { subtitleStyle: event.target.value })}>
              <option value="default">default</option><option value="clean">clean</option>
              <option value="karaoke">karaoke</option><option value="bold">bold</option>
            </select>
          </Field>
          <div style={{ display: 'flex', gap: 10 }}>
            <div style={{ flex: 1 }}><Field label={ru ? 'Шрифт' : 'Font'}>
              <input style={inputStyle} value={draft.rendering.subtitleFont}
                onChange={(event) => patch('rendering', { subtitleFont: event.target.value })} />
            </Field></div>
            <div style={{ flex: 1 }}><Field label={ru ? 'Позиция' : 'Position'}>
              <select style={inputStyle} value={draft.rendering.subtitlePosition}
                onChange={(event) => patch('rendering', { subtitlePosition: event.target.value })}>
                <option value="top">top</option><option value="middle">middle</option><option value="bottom">bottom</option>
              </select>
            </Field></div>
          </div>
          <Field label={ru ? 'Громкость музыки, дБ' : 'Music gain, dB'}>
            <input style={inputStyle} type="number" min={-40} max={0}
              value={draft.rendering.musicGainDb}
              onChange={(event) => patch('rendering', { musicGainDb: Number(event.target.value) || -18 })} />
          </Field>
          <Toggle label={ru ? 'Нормализовать звук' : 'Normalize audio'} checked={draft.rendering.normalizeAudio}
            onChange={(value) => patch('rendering', { normalizeAudio: value })} />
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label="Target LUFS" value={draft.rendering.loudnessTargetLufs} min={-30} max={-5}
              onChange={(value) => patch('rendering', { loudnessTargetLufs: value })} />
            <div style={{ flex: 1 }}><Field label={ru ? 'Переходы' : 'Transitions'}>
              <select style={inputStyle} value={draft.rendering.transition}
                onChange={(event) => patch('rendering', { transition: event.target.value })}>
                <option value="none">none</option><option value="cut">cut</option>
                <option value="fade">fade</option><option value="slide">slide</option>
              </select>
            </Field></div>
          </div>
          <Field label={ru ? 'ID логотипа / водяного знака' : 'Logo / watermark asset ID'}>
            <input style={inputStyle} value={draft.rendering.watermarkAssetId}
              placeholder={ru ? 'пусто = выключено' : 'empty = disabled'}
              onChange={(event) => patch('rendering', { watermarkAssetId: event.target.value })} />
          </Field>
        </Panel>

        <Panel title={ru ? 'Политики' : 'Policies'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Field label={ru ? 'Стоп-слова (через запятую)' : 'Denylist (comma separated)'}
            hint={ru ? 'Прогон остановится на policy-узле, если найдёт совпадение.'
                     : 'A run stops at the policy node on a match.'}>
            <input style={inputStyle} value={draft.policies.denylist.join(', ')}
              onChange={(event) => patch('policies', {
                denylist: event.target.value.split(',').map((item) => item.trim()).filter(Boolean),
              })} />
          </Field>
          <Field label={ru ? 'Минимальная оценка QA' : 'Minimum QA score'}>
            <input style={inputStyle} type="number" min={0} max={1} step={0.05}
              value={draft.policies.minQualityScore}
              onChange={(event) => patch('policies', { minQualityScore: Number(event.target.value) })} />
          </Field>
          <Toggle label={ru ? 'Требовать источники' : 'Require sources'} checked={draft.policies.requireSources}
            onChange={(value) => patch('policies', { requireSources: value })} />
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Мин. источников' : 'Min sources'} value={draft.policies.minSources} min={0} max={50}
              onChange={(value) => patch('policies', { minSources: value })} />
            <CompactNumber label={ru ? 'Возраст, дней' : 'Age, days'} value={draft.policies.maxSourceAgeDays} min={1} max={3650}
              onChange={(value) => patch('policies', { maxSourceAgeDays: value })} />
          </div>
          <Toggle label={ru ? 'Блокировать ненормативную лексику' : 'Block profanity'}
            checked={draft.policies.blockProfanity}
            onChange={(value) => patch('policies', { blockProfanity: value })} />
        </Panel>

        <Panel title={ru ? 'Повторы и fallback' : 'Retries & fallback'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Toggle label={ru ? 'Использовать резервные модели' : 'Use fallback providers'}
            checked={draft.retry.fallbackEnabled}
            onChange={(value) => patch('retry', { fallbackEnabled: value })} />
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Попыток' : 'Attempts'} value={draft.retry.maxAttempts} min={1} max={10}
              onChange={(value) => patch('retry', { maxAttempts: value })} />
            <CompactNumber label={ru ? 'Первая пауза, сек' : 'Initial delay, sec'} value={draft.retry.initialDelaySeconds} min={0} max={3600}
              onChange={(value) => patch('retry', { initialDelaySeconds: value })} />
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Макс. пауза, сек' : 'Max delay, sec'} value={draft.retry.maxDelaySeconds} min={0} max={86400}
              onChange={(value) => patch('retry', { maxDelaySeconds: value })} />
            <CompactNumber label={ru ? 'Множитель' : 'Multiplier'} value={draft.retry.backoffMultiplier} min={1} max={10} step={0.1}
              onChange={(value) => patch('retry', { backoffMultiplier: value })} />
          </div>
          <p style={{ color: COLORS.faint, fontSize: 10.5, lineHeight: 1.45 }}>
            {ru ? 'Списки провайдеров задаются в блоке «Генерация и модели» в порядке переключения.'
              : 'Provider lists are configured in Generation & models in failover order.'}
          </p>
        </Panel>

        <Panel title={ru ? 'Сеть и прокси' : 'Network & proxy'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Toggle label={ru ? 'Запретить публикацию без прокси' : 'Require a proxy for publishing'}
            hint={ru ? 'Проверяется для каждого аккаунта перед загрузкой. Сам URL хранится в настройках аккаунта.'
              : 'Checked per account before upload. The proxy URL stays in account settings.'}
            checked={draft.network.requireProxyForPublishing}
            onChange={(value) => patch('network', { requireProxyForPublishing: value })} />
          <CompactNumber label={ru ? 'Таймаут подключения, сек' : 'Connection timeout, sec'}
            value={draft.network.connectionTimeoutSeconds} min={5} max={600}
            onChange={(value) => patch('network', { connectionTimeoutSeconds: value })} />
          <CompactNumber label={ru ? 'Обновлять аналитику, мин' : 'Analytics refresh, min'}
            value={draft.network.analyticsRefreshMinutes} min={5} max={10080}
            onChange={(value) => patch('network', { analyticsRefreshMinutes: value })} />
        </Panel>

        <Panel title={ru ? 'Хранилище' : 'Storage'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <CompactNumber label={ru ? 'Хранить материалы, дней' : 'Asset retention, days'}
            value={draft.storage.retentionDays} min={1} max={3650}
            onChange={(value) => patch('storage', { retentionDays: value })} />
          <CompactNumber label={ru ? 'Ссылка действует, мин' : 'Signed URL lifetime, min'}
            value={draft.storage.signedUrlMinutes} min={1} max={10080}
            onChange={(value) => patch('storage', { signedUrlMinutes: value })} />
          <Toggle label={ru ? 'Хранить исходники' : 'Keep source assets'} checked={draft.storage.keepSourceAssets}
            onChange={(value) => patch('storage', { keepSourceAssets: value })} />
          <Toggle label={ru ? 'Хранить материалы упавших прогонов' : 'Keep failed-run assets'}
            checked={draft.storage.keepFailedRuns}
            onChange={(value) => patch('storage', { keepFailedRuns: value })} />
        </Panel>

        <Panel title={ru ? 'Уведомления' : 'Notifications'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <Toggle label={ru ? 'Прогон упал' : 'Run failed'} checked={draft.notifications.onFailure}
            onChange={(value) => patch('notifications', { onFailure: value })} />
          <Toggle label={ru ? 'Готово к проверке' : 'Ready for review'}
            checked={draft.notifications.onReviewRequired}
            onChange={(value) => patch('notifications', { onReviewRequired: value })} />
          <Toggle label={ru ? 'Достигнут бюджет' : 'Budget reached'}
            checked={draft.notifications.onBudgetReached}
            onChange={(value) => patch('notifications', { onBudgetReached: value })} />
          <Toggle label={ru ? 'Опубликовано' : 'Published'} checked={draft.notifications.onPublished}
            onChange={(value) => patch('notifications', { onPublished: value })} />
          <Toggle label={ru ? 'Сработал fallback' : 'Provider fallback used'}
            checked={draft.notifications.onProviderFallback}
            onChange={(value) => patch('notifications', { onProviderFallback: value })} />
          <Toggle label={ru ? 'Заблокировано политикой' : 'Blocked by policy'}
            checked={draft.notifications.onPolicyBlocked}
            onChange={(value) => patch('notifications', { onPolicyBlocked: value })} />
        </Panel>

        <Panel title={ru ? 'Воркеры и производительность' : 'Workers & performance'} style={{ flex: '1 1 300px', minWidth: 280 }}>
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Опрос, сек' : 'Poll, sec'} value={draft.advanced.pollSeconds} min={1} max={60}
              onChange={(value) => patch('advanced', { pollSeconds: value })} />
            <CompactNumber label={ru ? 'Прогонов' : 'Runs'} value={draft.advanced.maxConcurrentRuns} min={1} max={64}
              onChange={(value) => patch('advanced', { maxConcurrentRuns: value })} />
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <CompactNumber label={ru ? 'Узлов параллельно' : 'Parallel nodes'} value={draft.advanced.maxParallelNodes} min={1} max={64}
              onChange={(value) => patch('advanced', { maxParallelNodes: value })} />
            <CompactNumber label={ru ? 'Лизинг, сек' : 'Lease, sec'} value={draft.advanced.leaseSeconds} min={30} max={3600}
              onChange={(value) => patch('advanced', { leaseSeconds: value })} />
          </div>
          <CompactNumber label={ru ? 'Хранить логи, дней' : 'Log retention, days'} value={draft.advanced.logRetentionDays} min={1} max={3650}
            onChange={(value) => patch('advanced', { logRetentionDays: value })} />
        </Panel>
      </div>
    </div>
  );
}

function Toggle({ label, hint, checked, onChange }: {
  label: string; hint?: string; checked: boolean; onChange: (value: boolean) => void;
}) {
  return (
    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 9, marginBottom: 11, cursor: 'pointer' }}>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)}
        style={{ marginTop: 2, accentColor: COLORS.cyan }} />
      <span>
        <span style={{ display: 'block', fontSize: 11.5, color: COLORS.text }}>{label}</span>
        {hint && <span style={{ display: 'block', fontSize: 10.5, color: COLORS.faint, lineHeight: 1.45 }}>
          {hint}
        </span>}
      </span>
    </label>
  );
}

function CompactNumber({ label, value, min, max, step = 1, onChange }: {
  label: string; value: number; min: number; max: number; step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <Field label={label}>
        <input style={inputStyle} type="number" min={min} max={max} step={step} value={value}
          onChange={(event) => {
            const parsed = Number(event.target.value);
            if (Number.isFinite(parsed)) onChange(parsed);
          }} />
      </Field>
    </div>
  );
}

function ListField({ label, value, onChange }: {
  label: string; value: string[]; onChange: (value: string[]) => void;
}) {
  return (
    <Field label={label}>
      <input style={inputStyle} value={value.join(', ')} placeholder="provider-a, provider-b"
        onChange={(event) => onChange(event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} />
    </Field>
  );
}

function NumberField({ label, value, onChange, language }: {
  label: string; value: number | null; onChange: (value: number | null) => void; language: 'ru' | 'en';
}) {
  const ru = language === 'ru';
  return (
    <Field label={label}
      hint={value == null ? (ru ? 'Пусто = без ограничения' : 'Empty = no limit') : undefined}>
      <input
        style={inputStyle}
        type="number"
        min={0}
        step={0.5}
        value={value ?? ''}
        placeholder={ru ? 'без лимита' : 'no limit'}
        onChange={(event) => {
          const raw = event.target.value;
          if (raw === '') {
            if (!window.confirm(ru
              ? 'Снять лимит? Прогоны смогут тратить без ограничения.'
              : 'Remove the limit? Runs will be able to spend without a ceiling.')) return;
            onChange(null);
            return;
          }
          onChange(Number(raw));
        }}
      />
    </Field>
  );
}
