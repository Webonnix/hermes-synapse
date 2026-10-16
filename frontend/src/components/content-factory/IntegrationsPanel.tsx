/**
 * Integrations page (§62, §49, §50).
 *
 * One screen answering "what is this factory connected to, and what will break
 * because it is not". The warnings at the top are the point: a missing video
 * provider or local-disk storage is not visible anywhere else until a run fails
 * at 09:00, and this is where that becomes obvious beforehand.
 *
 * No key, token or credential reference is displayed — the backend never sends
 * one (§89). Connecting one happens in the panel below, which posts write-only
 * fields and gets back only where each credential came from; external language
 * models still go through the governed approval flow rather than round it.
 */

import { AlertTriangle, CheckCircle2, Cloud, Link2, PlugZap, Radio, ShieldAlert } from 'lucide-react';
import { COLORS, Chip, EmptyState, Panel, bytes, dateTime } from './ui';
import { ProvidersPanel } from './ProvidersPanel';
import type { CFIntegrations, CFLLMEndpoints, CFMediaProvider, CFStorageUsage } from './types';

const CATEGORY_LABEL: Record<string, [string, string]> = {
  LLM: ['Языковые модели', 'Language models'],
  IMAGE: ['Генерация изображений', 'Image generation'],
  VIDEO: ['Генерация видео', 'Video generation'],
  VOICE: ['Озвучка', 'Voice'],
  PUBLISHING: ['Публикация', 'Publishing'],
};

/** What to do about an empty category — the panel below for everything that can
 *  be connected here, the platform's own OAuth flow for publishing. */
const CONNECT_HINT: Record<string, [string, string]> = {
  IMAGE: ['Подключите провайдера ниже', 'Connect a provider below'],
  VIDEO: ['Подключите провайдера ниже', 'Connect a provider below'],
  VOICE: ['Подключите провайдера ниже', 'Connect a provider below'],
  LLM: ['Добавьте внешнюю модель ниже', 'Add an external model below'],
  PUBLISHING: ['Подключите аккаунт во вкладке «Аккаунты»', 'Connect an account on the Accounts tab'],
};

export function IntegrationsPanel({
  data, usage, providers, llm, language, busy, canEdit, onRefresh,
  onSaveProvider, onDeleteProvider, onTestProvider, onSetDefault, onCreateLlm, onDeleteLlm,
  onUpdateLlm, onLoadLlmModels,
}: {
  data: CFIntegrations | null;
  usage: CFStorageUsage | null;
  providers: CFMediaProvider[];
  llm: CFLLMEndpoints | null;
  language: 'ru' | 'en';
  busy?: boolean;
  canEdit?: boolean;
  onRefresh?: () => void;
  onSaveProvider: (providerId: string, patch: Record<string, unknown>) => Promise<void>;
  onDeleteProvider: (providerId: string) => Promise<void>;
  onTestProvider: (providerId: string) => Promise<Record<string, unknown>>;
  onSetDefault: (category: string, providerId: string) => Promise<void>;
  onCreateLlm: (payload: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onDeleteLlm: (bindingId: string) => Promise<void>;
  onUpdateLlm: (bindingId: string, defaultModel: string) => Promise<void>;
  onLoadLlmModels: (bindingId: string) => Promise<string[]>;
}) {
  const ru = language === 'ru';
  if (!data) {
    return <EmptyState title={ru ? 'Загрузка…' : 'Loading…'} />;
  }

  const storageName = String(data.storage?.storage ?? '—');
  const storageStatus = String(data.storage?.status ?? 'unknown');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {data.mockProviders && (
        <Banner tone="amber" icon={<ShieldAlert size={14} />}>
          {ru
            ? 'Включены mock-провайдеры: ни один платный API не вызывается, публикации не происходят.'
            : 'Mock providers are on: no paid API is called and nothing is published.'}
        </Banner>
      )}

      {data.warnings.map((warning) => (
        <Banner key={warning.code} tone={warning.code === 'LOCAL_STORAGE' ? 'red' : 'amber'}
          icon={<AlertTriangle size={14} />}>
          {warning.message}
        </Banner>
      ))}

      {!data.warnings.length && !data.mockProviders && (
        <Banner tone="green" icon={<CheckCircle2 size={14} />}>
          {ru ? 'Все категории провайдеров подключены.' : 'Every provider category is connected.'}
        </Banner>
      )}

      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <Panel
          title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <PlugZap size={12} />{ru ? 'Провайдеры' : 'Providers'}
          </span>}
          action={onRefresh && (
            <button type="button" className="btn-ghost" style={{ fontSize: 10.5 }} onClick={onRefresh}>
              {ru ? 'Обновить' : 'Refresh'}
            </button>
          )}
          style={{ flex: '1 1 460px', minWidth: 420 }}
          bodyStyle={{ padding: 0 }}
        >
          {data.categories.map((category) => (
            <div key={category.category} style={{ padding: '12px 14px', borderTop: `1px solid ${COLORS.line}` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 7 }}>
                <strong style={{ fontSize: 12, color: COLORS.text }}>
                  {(CATEGORY_LABEL[category.category] ?? [category.category, category.category])[ru ? 0 : 1]}
                </strong>
                <Chip tone={category.connected ? (category.paidAvailable ? 'green' : 'cyan') : 'red'}>
                  {category.connected
                    ? (category.paidAvailable ? (ru ? 'ПОДКЛЮЧЕНО' : 'CONNECTED') : (ru ? 'ТОЛЬКО ЛОКАЛЬНО' : 'LOCAL ONLY'))
                    : (ru ? 'НЕТ ПРОВАЙДЕРА' : 'NOT CONNECTED')}
                </Chip>
              </div>

              {category.providers.length > 0 ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {category.providers.map((provider) => (
                    <span key={provider.id} style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6, padding: '5px 9px',
                      borderRadius: 8, fontSize: 11, color: COLORS.text,
                      background: 'rgba(20,24,32,0.55)', border: `1px solid ${COLORS.line}`,
                    }}>
                      {provider.isDefault && <span style={{ color: COLORS.cyanSoft }}>★</span>}
                      {provider.id}
                      {provider.model && <span style={{ color: COLORS.faint }}>· {provider.model}</span>}
                      <span style={{ color: provider.free ? COLORS.greenSoft : COLORS.amberSoft, fontSize: 10 }}>
                        {provider.free ? (ru ? 'бесплатно' : 'free') : (ru ? 'платно' : 'paid')}
                      </span>
                      <span style={{ color: COLORS.faint, fontSize: 10 }}>×{provider.concurrency}</span>
                    </span>
                  ))}
                </div>
              ) : (
                <div style={{ fontSize: 10.5, color: COLORS.faint }}>
                  {(CONNECT_HINT[category.category] ?? ['—', '—'])[ru ? 0 : 1]}
                </div>
              )}
            </div>
          ))}
        </Panel>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, flex: '1 1 320px', minWidth: 300 }}>
          <Panel title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Cloud size={12} />{ru ? 'Хранилище ассетов' : 'Asset storage'}
          </span>}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 9 }}>
              <strong style={{ fontSize: 13, color: COLORS.text }}>{storageName}</strong>
              <Chip tone={storageStatus === 'connected' ? (storageName === 'local' ? 'amber' : 'green') : 'red'}>
                {storageStatus}
              </Chip>
            </div>
            {storageName === 'local' && (
              <p style={{ margin: '0 0 10px', fontSize: 10.5, color: COLORS.amberSoft, lineHeight: 1.5 }}>
                {ru
                  ? 'YouTube и TikTok скачивают файл по внешней ссылке — с локального диска её выдать нельзя. Подключите Nextcloud или S3 во вкладке «Хранилище».'
                  : 'YouTube and TikTok fetch the file over a public link, which local disk cannot provide. Connect Nextcloud or S3 on the Storage tab.'}
              </p>
            )}
            {usage && (
              <>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5,
                  color: COLORS.muted, marginBottom: 8 }}>
                  <span>{ru ? 'Файлов' : 'Files'}: <strong style={{ color: COLORS.text }}>{usage.totalFiles}</strong></span>
                  <span>{ru ? 'Объём' : 'Size'}: <strong style={{ color: COLORS.text }}>{bytes(usage.totalBytes)}</strong></span>
                </div>
                {usage.byType.map((row) => (
                  <div key={row.type} style={{ display: 'flex', justifyContent: 'space-between',
                    fontSize: 11, color: COLORS.faint, padding: '3px 0' }}>
                    <span>{row.type}</span>
                    <span>{row.files} · {bytes(row.bytes)}</span>
                  </div>
                ))}
                {!usage.byType.length && (
                  <div style={{ fontSize: 10.5, color: COLORS.faint }}>
                    {ru ? 'Ассетов ещё нет.' : 'No assets yet.'}
                  </div>
                )}
              </>
            )}
          </Panel>

          <Panel title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Radio size={12} />{ru ? 'Каналы публикации' : 'Publishing channels'}
          </span>}>
            {!data.channels.length && (
              <EmptyState
                title={ru ? 'Каналы не подключены' : 'No channels connected'}
                hint={ru
                  ? 'Без канала pipeline дойдёт до узла публикации и остановится на нём.'
                  : 'Without a channel a pipeline stops at its publish node.'}
              />
            )}
            {data.channels.map((channel) => (
              <div key={channel.id} style={{
                display: 'flex', alignItems: 'center', gap: 9, padding: '9px 0',
                borderTop: `1px solid ${COLORS.line}`,
              }}>
                <Link2 size={13} color={COLORS.cyanSoft} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, color: COLORS.text }}>{channel.account_name}</div>
                  <div style={{ fontSize: 10, color: COLORS.faint }}>
                    {channel.platform}
                    {channel.connected_at ? ` · ${dateTime(channel.connected_at)}` : ''}
                  </div>
                </div>
                <Chip tone={channel.status === 'CONNECTED' ? 'green' : 'muted'}>{channel.status}</Chip>
              </div>
            ))}
            <div style={{ marginTop: 10, fontSize: 10.5, color: COLORS.faint, lineHeight: 1.5 }}>
              {ru
                ? 'Публикация готова: '
                : 'Publishing ready: '}
              <strong style={{ color: data.publishReady ? COLORS.greenSoft : COLORS.redSoft }}>
                {data.publishReady ? (ru ? 'да' : 'yes') : (ru ? 'нет' : 'no')}
              </strong>
            </div>
          </Panel>
        </div>
      </div>

      <ProvidersPanel
        providers={providers}
        llm={llm}
        language={language}
        busy={busy}
        canEdit={canEdit}
        onSave={onSaveProvider}
        onDelete={onDeleteProvider}
        onTest={onTestProvider}
        onSetDefault={onSetDefault}
        onCreateLlm={onCreateLlm}
        onDeleteLlm={onDeleteLlm}
        onUpdateLlm={onUpdateLlm}
        onLoadLlmModels={onLoadLlmModels}
      />
    </div>
  );
}

function Banner({ tone, icon, children }: {
  tone: 'amber' | 'red' | 'green'; icon: React.ReactNode; children: React.ReactNode;
}) {
  const color = { amber: COLORS.amberSoft, red: COLORS.redSoft, green: COLORS.greenSoft }[tone];
  const background = {
    amber: 'rgba(44,51,64,0.5)', red: 'rgba(69,17,17,0.5)', green: 'rgba(11,52,29,0.45)',
  }[tone];
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: 9, padding: '10px 14px', borderRadius: 10,
      fontSize: 11.5, lineHeight: 1.5, color, background, border: `1px solid ${color}44`,
    }}>
      <span style={{ marginTop: 1 }}>{icon}</span>
      <span>{children}</span>
    </div>
  );
}
