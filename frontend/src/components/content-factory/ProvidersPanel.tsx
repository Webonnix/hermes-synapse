/**
 * Connecting external models (§62, §63, §100).
 *
 * The Integrations screen used to be read-only: it told the owner that no image
 * provider was configured and then named the environment variable to go and set
 * on the host. This is the other half — the keys, models and prices are entered
 * here and take effect on the next node, without a redeploy.
 *
 * Two boundaries the screen keeps visible rather than hiding:
 *
 * - **A key is never displayed.** The backend reports where each credential came
 *   from (`.env`, stored, none) and nothing else, so a field left empty means
 *   "keep what is stored". A key that lives in the environment is shown as such
 *   and cannot be overwritten from here — the deployment owns it.
 * - **External LLMs stay governed.** Adding one opens an R3 approval task
 *   (`provider_governance`); until an owner approves it the endpoint exists but
 *   nothing may call it. This screen starts that flow and shows its state; it is
 *   not a second way around it.
 */

import { useEffect, useState, type ReactNode } from 'react';
import {
  Brain, Check, KeyRound, Plus, Star, Trash2, Zap,
} from 'lucide-react';
import { COLORS, Chip, Field, Panel, inputStyle } from './ui';
import type { CFLLMEndpoints, CFMediaProvider } from './types';

type Props = {
  providers: CFMediaProvider[];
  llm: CFLLMEndpoints | null;
  language: 'ru' | 'en';
  busy?: boolean;
  canEdit?: boolean;
  onSave: (providerId: string, patch: Record<string, unknown>) => Promise<void>;
  onDelete: (providerId: string) => Promise<void>;
  onTest: (providerId: string) => Promise<Record<string, unknown>>;
  onSetDefault: (category: string, providerId: string) => Promise<void>;
  onCreateLlm: (payload: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onDeleteLlm: (bindingId: string) => Promise<void>;
  onUpdateLlm: (bindingId: string, defaultModel: string) => Promise<void>;
  onLoadLlmModels: (bindingId: string) => Promise<string[]>;
};

const CATEGORY_LABEL: Record<string, [string, string]> = {
  IMAGE: ['Генерация изображений', 'Image generation'],
  VIDEO: ['Генерация видео', 'Video generation'],
  VOICE: ['Озвучка', 'Voice'],
};

const CATEGORY_ORDER = ['IMAGE', 'VIDEO', 'VOICE'];

export function ProvidersPanel({
  providers, llm, language, busy, canEdit = true,
  onSave, onDelete, onTest, onSetDefault, onCreateLlm, onDeleteLlm, onUpdateLlm, onLoadLlmModels,
}: Props) {
  const ru = language === 'ru';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Panel
        title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <KeyRound size={12} />{ru ? 'Подключение моделей' : 'Connect models'}
        </span>}
        bodyStyle={{ padding: 0 }}
      >
        <p style={{ margin: 0, padding: '12px 14px', fontSize: 11, color: COLORS.faint, lineHeight: 1.55 }}>
          {ru
            ? 'Ключи хранятся в защищённом хранилище секретов, а не в настройках, и никогда не возвращаются обратно в интерфейс. Ключ, заданный в окружении контейнера, продолжает действовать и отсюда не перезаписывается.'
            : 'Keys go to the secret store, never into the settings document, and are never sent back to the UI. A key set in the container environment keeps working and cannot be overwritten from here.'}
        </p>
        {CATEGORY_ORDER.map((category) => {
          const rows = providers.filter((provider) => provider.category === category);
          if (!rows.length) return null;
          return (
            <div key={category} style={{ borderTop: `1px solid ${COLORS.line}`, padding: '12px 14px' }}>
              {/* No category status chip here: the panel above already states
                  it, and each card below says what it is on its own. */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 10 }}>
                <strong style={{ fontSize: 12, color: COLORS.text }}>
                  {(CATEGORY_LABEL[category] ?? [category, category])[ru ? 0 : 1]}
                </strong>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {rows.map((provider) => (
                  <ProviderCard
                    key={provider.id}
                    provider={provider}
                    ru={ru}
                    busy={busy}
                    canEdit={canEdit}
                    onSave={onSave}
                    onDelete={onDelete}
                    onTest={onTest}
                    onSetDefault={onSetDefault}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </Panel>

      <LlmEndpoints
        llm={llm}
        ru={ru}
        busy={busy}
        canEdit={canEdit}
        onCreate={onCreateLlm}
        onDelete={onDeleteLlm}
        onSetDefault={onSetDefault}
        onUpdateModel={onUpdateLlm}
        onLoadModels={onLoadLlmModels}
      />
    </div>
  );
}

function ProviderCard({ provider, ru, busy, canEdit, onSave, onDelete, onTest, onSetDefault }: {
  provider: CFMediaProvider;
  ru: boolean;
  busy?: boolean;
  canEdit: boolean;
  onSave: (providerId: string, patch: Record<string, unknown>) => Promise<void>;
  onDelete: (providerId: string) => Promise<void>;
  onTest: (providerId: string) => Promise<Record<string, unknown>>;
  onSetDefault: (category: string, providerId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [probe, setProbe] = useState<string>('');

  useEffect(() => {
    setValues({
      model: provider.model,
      ...Object.fromEntries(Object.entries(provider.options).map(([key, value]) => [key, String(value)])),
    });
    setProbe('');
  }, [provider]);

  const secretFields = provider.fields.filter((field) => field.type === 'secret');
  const plainFields = provider.fields.filter((field) => field.type !== 'secret');
  const keySource = provider.secrets.apiKey ?? 'none';

  const save = async () => {
    const patch: Record<string, unknown> = { model: values.model ?? '', options: {} };
    const options: Record<string, unknown> = {};
    for (const field of plainFields) {
      if (field.name === 'model') continue;
      options[field.name] = values[field.name] ?? '';
    }
    patch.options = options;
    for (const field of secretFields) {
      const typed = (values[field.name] ?? '').trim();
      // Empty means "leave the stored one alone"; clearing is the trash button.
      if (typed) patch[field.name] = typed;
    }
    await onSave(provider.id, patch);
    setValues((current) => ({
      ...current,
      ...Object.fromEntries(secretFields.map((field) => [field.name, ''])),
    }));
  };

  return (
    <div style={{
      borderRadius: 10, border: `1px solid ${provider.active ? `${COLORS.green}55` : COLORS.line}`,
      background: COLORS.panelSoft,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '10px 12px' }}>
        <button type="button" onClick={() => setOpen((current) => !current)} style={{
          flex: 1, display: 'flex', alignItems: 'center', gap: 9, background: 'none', border: 'none',
          padding: 0, cursor: 'pointer', textAlign: 'left', color: COLORS.text, fontSize: 12.5,
        }}>
          {provider.isDefault && <Star size={12} color={COLORS.cyanSoft} fill={COLORS.cyanSoft} />}
          <span>{provider.label[ru ? 'ru' : 'en']}</span>
          {provider.model && <span style={{ color: COLORS.faint, fontSize: 11 }}>· {provider.model}</span>}
        </button>
        <Chip tone={provider.active ? 'green' : provider.configured ? 'amber' : 'muted'}>
          {provider.active
            ? (ru ? 'активен' : 'active')
            : provider.configured
              ? (ru ? 'выключен' : 'disabled')
              : (ru ? 'не подключён' : 'not connected')}
        </Chip>
        {keySource === 'env' && <Chip tone="cyan">.env</Chip>}
        <button type="button" className="btn-ghost" style={{ fontSize: 10.5 }}
          onClick={() => setOpen((current) => !current)}>
          {open ? (ru ? 'Свернуть' : 'Close') : (ru ? 'Настроить' : 'Configure')}
        </button>
      </div>

      {open && (
        <div style={{ padding: '4px 12px 12px', borderTop: `1px solid ${COLORS.line}` }}>
          {provider.hint[ru ? 'ru' : 'en'] && (
            <p style={{ margin: '10px 0', fontSize: 10.5, color: COLORS.faint, lineHeight: 1.5 }}>
              {provider.hint[ru ? 'ru' : 'en']}
              {provider.docs && (
                <>
                  {' · '}
                  <a href={provider.docs} target="_blank" rel="noreferrer"
                    style={{ color: COLORS.cyanSoft }}>{ru ? 'где взять ключ' : 'where to get a key'}</a>
                </>
              )}
            </p>
          )}

          {secretFields.map((field) => {
            const source = provider.secrets[field.name] ?? 'none';
            return (
              <Field
                key={field.name}
                label={field.label[ru ? 'ru' : 'en']}
                hint={source === 'env'
                  ? (ru ? 'Задан в окружении контейнера.' : 'Set in the container environment.')
                  : undefined}
              >
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <input
                    type="password"
                    style={{ ...inputStyle, flex: 1 }}
                    disabled={!canEdit || source === 'env'}
                    value={values[field.name] ?? ''}
                    placeholder={source === 'none'
                      ? (ru ? 'не задан' : 'not set')
                      : (ru ? '•••••• сохранён' : '•••••• stored')}
                    onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
                  />
                  <Chip tone={source === 'none' ? 'red' : source === 'env' ? 'cyan' : 'green'}>
                    {source === 'none' ? (ru ? 'нет' : 'none') : source === 'env' ? '.env' : (ru ? 'сохранён' : 'stored')}
                  </Chip>
                </div>
              </Field>
            );
          })}

          {plainFields.map((field) => (
            <Field key={field.name} label={field.label[ru ? 'ru' : 'en']}>
              <input
                type={field.type === 'number' ? 'number' : 'text'}
                step={field.type === 'number' ? '0.001' : undefined}
                style={inputStyle}
                disabled={!canEdit}
                value={values[field.name] ?? ''}
                onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))}
              />
            </Field>
          ))}

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
            <button type="button" className="btn-primary" style={{ fontSize: 11.5 }}
              disabled={!canEdit || busy} onClick={() => void save()}>
              {ru ? 'Сохранить' : 'Save'}
            </button>
            <button type="button" className="btn-ghost" style={{ fontSize: 11.5 }}
              disabled={!canEdit || busy}
              onClick={async () => {
                const result = await onTest(provider.id);
                setProbe(String(result.status ?? 'unknown'));
              }}>
              <Zap size={12} /> {ru ? 'Проверить' : 'Test'}
            </button>
            <button type="button" className="btn-ghost" style={{ fontSize: 11.5 }}
              disabled={!canEdit || busy || !provider.configured}
              onClick={() => void onSave(provider.id, { enabled: !provider.enabled })}>
              {provider.enabled ? (ru ? 'Выключить' : 'Disable') : (ru ? 'Включить' : 'Enable')}
            </button>
            {provider.active && !provider.isDefault && (
              <button type="button" className="btn-ghost" style={{ fontSize: 11.5 }}
                disabled={!canEdit || busy}
                onClick={() => void onSetDefault(provider.category, provider.id)}>
                <Star size={12} /> {ru ? 'По умолчанию' : 'Make default'}
              </button>
            )}
            <span style={{ flex: 1 }} />
            {probe && (
              <span style={{ fontSize: 11, color: probe === 'connected' ? COLORS.greenSoft : COLORS.redSoft }}>
                {probe === 'connected' ? <Check size={12} /> : null} {probe}
              </span>
            )}
            {provider.secrets.apiKey === 'stored' && (
              <button type="button" className="btn-ghost" style={{ fontSize: 11.5, color: COLORS.redSoft }}
                disabled={!canEdit || busy} onClick={() => void onDelete(provider.id)}
                title={ru ? 'Удалить ключ и настройки' : 'Delete the key and its configuration'}>
                <Trash2 size={12} />
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function LlmEndpoints({
  llm, ru, busy, canEdit, onCreate, onDelete, onSetDefault, onUpdateModel, onLoadModels,
}: {
  llm: CFLLMEndpoints | null;
  ru: boolean;
  busy?: boolean;
  canEdit: boolean;
  onCreate: (payload: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onDelete: (bindingId: string) => Promise<void>;
  onSetDefault: (category: string, providerId: string) => Promise<void>;
  onUpdateModel: (bindingId: string, defaultModel: string) => Promise<void>;
  onLoadModels: (bindingId: string) => Promise<string[]>;
}) {
  const [form, setForm] = useState({ name: '', apiBase: '', apiKey: '', defaultModel: '' });
  const [notice, setNotice] = useState('');

  const submit = async () => {
    const result = await onCreate({ ...form });
    setForm({ name: '', apiBase: '', apiKey: '', defaultModel: '' });
    setNotice(result.approvalRequired
      ? (ru
        ? 'Провайдер создан и ждёт подтверждения владельца — до одобрения он не используется.'
        : 'Created and awaiting owner approval — nothing calls it until then.')
      : (ru ? 'Провайдер активен.' : 'Provider is active.'));
  };

  return (
    <Panel
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Brain size={12} />{ru ? 'Внешние языковые модели' : 'External language models'}
      </span>}
    >
      <p style={{ margin: '0 0 12px', fontSize: 11, color: COLORS.faint, lineHeight: 1.55 }}>
        {ru
          ? 'Любой OpenAI-совместимый endpoint: 9Router, DeepSeek, OpenAI, vLLM. Новый провайдер требует подтверждения владельца — ключ до этого момента хранится с ограниченным сроком жизни. Звёздочкой отмечена модель, которой отвечают текстовые ноды, когда не выбрана своя.'
          : 'Any OpenAI-compatible endpoint: 9Router, DeepSeek, OpenAI, vLLM. A new one needs owner approval; until then its key is stored with a short expiry. The star marks the model text nodes use when they name none.'}
      </p>

      {llm && (
        <div style={{ marginBottom: 12 }}>
          <EndpointRow
            name={llm.local.name}
            detail={`${llm.local.api_base} · ${llm.local.default_model}`}
            status={ru ? 'локальная' : 'local'}
            tone="cyan"
            isDefault={!llm.default || llm.default === llm.local.id}
            onMakeDefault={canEdit ? () => onSetDefault('LLM', llm.local.id) : undefined}
            ru={ru}
          />
          {llm.bindings.map((binding) => (
            <EndpointRow
              key={binding.id}
              name={binding.name}
              detail={`${binding.api_base}${binding.default_model ? ` · ${binding.default_model}` : ''}`}
              status={binding.status}
              tone={binding.status === 'active' ? 'green' : binding.status === 'awaiting_approval' ? 'amber' : 'muted'}
              onDelete={canEdit ? () => onDelete(binding.id) : undefined}
              isDefault={llm.default === binding.id}
              // Only an active binding can be the default: an unapproved one
              // would leave every text node pointing at something nothing may
              // call yet.
              onMakeDefault={canEdit && binding.status === 'active'
                ? () => onSetDefault('LLM', binding.id) : undefined}
              modelEditor={canEdit && binding.status === 'active' ? (
                <ModelPicker
                  ru={ru}
                  busy={busy}
                  value={binding.default_model || ''}
                  onLoad={() => onLoadModels(binding.id)}
                  onSave={(model) => onUpdateModel(binding.id, model)}
                />
              ) : undefined}
              ru={ru}
            />
          ))}
          {llm.bindings.some((binding) => binding.status === 'active' && !binding.default_model) && (
            <p style={{ margin: '8px 0 0', fontSize: 10.5, color: COLORS.amberSoft, lineHeight: 1.5 }}>
              {ru
                ? 'У активного провайдера не выбрана модель — нода, направленная на него, остановится с ошибкой. Выберите модель ниже.'
                : 'An active provider has no model — a node routed to it stops with an error. Pick a model below.'}
            </p>
          )}
        </div>
      )}

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ flex: '1 1 180px' }}>
          <Field label={ru ? 'Имя' : 'Name'} hint="deepseek, openai, 9router">
            <input style={inputStyle} value={form.name} disabled={!canEdit}
              onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </Field>
        </div>
        <div style={{ flex: '1 1 240px' }}>
          <Field label="API base" hint="https://api.deepseek.com/v1">
            <input style={inputStyle} value={form.apiBase} disabled={!canEdit}
              onChange={(event) => setForm({ ...form, apiBase: event.target.value })} />
          </Field>
        </div>
        <div style={{ flex: '1 1 180px' }}>
          <Field label={ru ? 'Модель по умолчанию' : 'Default model'}>
            <input style={inputStyle} value={form.defaultModel} disabled={!canEdit}
              onChange={(event) => setForm({ ...form, defaultModel: event.target.value })} />
          </Field>
        </div>
        <div style={{ flex: '1 1 200px' }}>
          <Field label="API key">
            <input type="password" style={inputStyle} value={form.apiKey} disabled={!canEdit}
              onChange={(event) => setForm({ ...form, apiKey: event.target.value })} />
          </Field>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <button type="button" className="btn-primary" style={{ fontSize: 11.5, display: 'flex', gap: 6 }}
          disabled={!canEdit || busy || !form.name.trim() || !form.apiBase.trim() || !form.apiKey.trim()}
          onClick={() => void submit()}>
          <Plus size={12} />{ru ? 'Подключить модель' : 'Connect model'}
        </button>
        {notice && <span style={{ fontSize: 11, color: COLORS.amberSoft }}>{notice}</span>}
      </div>
    </Panel>
  );
}

function EndpointRow({
  name, detail, status, tone, onDelete, isDefault, onMakeDefault, modelEditor, ru,
}: {
  name: string;
  detail: string;
  status: string;
  tone: 'cyan' | 'green' | 'amber' | 'muted';
  onDelete?: () => Promise<void>;
  isDefault?: boolean;
  onMakeDefault?: () => Promise<void>;
  modelEditor?: ReactNode;
  ru?: boolean;
}) {
  return (
    <div style={{ padding: '8px 0', borderTop: `1px solid ${COLORS.line}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, color: COLORS.text, display: 'flex', alignItems: 'center', gap: 6 }}>
            {name}
            {isDefault && (
              <span title={ru ? 'По умолчанию для текстовых нод' : 'Default for text nodes'}
                style={{ display: 'inline-flex', color: COLORS.amberSoft }}>
                <Star size={11} />
              </span>
            )}
          </div>
          <div style={{ fontSize: 10, color: COLORS.faint, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {detail}
          </div>
        </div>
        <Chip tone={tone}>{status}</Chip>
        {onMakeDefault && !isDefault && (
          <button type="button" className="btn-ghost" style={{ fontSize: 10.5 }}
            onClick={() => void onMakeDefault()}>
            <Star size={12} /> {ru ? 'По умолчанию' : 'Make default'}
          </button>
        )}
        {onDelete && (
          <button type="button" className="btn-ghost" style={{ fontSize: 10.5, color: COLORS.redSoft }}
            onClick={() => void onDelete()}>
            <Trash2 size={12} />
          </button>
        )}
      </div>
      {modelEditor && <div style={{ marginTop: 6 }}>{modelEditor}</div>}
    </div>
  );
}

/** Model field for one endpoint: a text input until the endpoint's own
 * `GET /models` answers, then a list to pick from. It stays a text input when
 * that call fails, because an endpoint that cannot list its models is still
 * perfectly able to serve one you name. */
function ModelPicker({ value, ru, busy, onLoad, onSave }: {
  value: string;
  ru: boolean;
  busy?: boolean;
  onLoad: () => Promise<string[]>;
  onSave: (model: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(value);
  const [models, setModels] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState('');

  useEffect(() => { setDraft(value); }, [value]);

  const load = async () => {
    setLoading(true);
    setNote('');
    try {
      const list = await onLoad();
      setModels(list);
      if (list.length === 0) {
        setNote(ru ? 'Endpoint не отдал список — впишите имя модели вручную.'
          : 'The endpoint did not list any — type the model name.');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
      <div style={{ flex: '1 1 220px' }}>
        <Field label={ru ? 'Модель по умолчанию' : 'Default model'}>
          {models && models.length > 0 ? (
            <select style={inputStyle} value={draft} onChange={(event) => setDraft(event.target.value)}>
              <option value="">{ru ? '— не выбрана —' : '— none —'}</option>
              {models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          ) : (
            <input style={inputStyle} value={draft} placeholder="ag/gemini-3.7-flash-high"
              onChange={(event) => setDraft(event.target.value)} />
          )}
        </Field>
      </div>
      <button type="button" className="btn-ghost" style={{ fontSize: 11 }}
        disabled={loading || busy} onClick={() => void load()}>
        {loading ? (ru ? 'Загрузка…' : 'Loading…') : (ru ? 'Список моделей' : 'List models')}
      </button>
      <button type="button" className="btn-primary" style={{ fontSize: 11 }}
        disabled={busy || draft === value} onClick={() => void onSave(draft)}>
        {ru ? 'Сохранить' : 'Save'}
      </button>
      {note && <span style={{ fontSize: 10.5, color: COLORS.faint }}>{note}</span>}
    </div>
  );
}
