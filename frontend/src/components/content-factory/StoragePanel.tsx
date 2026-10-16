/**
 * Asset storage (§44–46).
 *
 * Until this screen existed, moving the factory off local disk meant editing
 * `.env` on the host and restarting the backend — impossible from here, and the
 * single reason publishing does not work on a fresh install (a platform pulls
 * the finished video over a public link, which a local directory cannot serve).
 * So the choice is made here, and the warning that explains why it matters sits
 * next to it rather than three screens away.
 *
 * Passwords are write-only: the backend reports *where* a credential came from
 * (`env`, `stored`, `none`) and never its value, so an empty password box means
 * "leave the stored one alone", not "there is none". Clearing it deliberately is
 * a separate, explicit action.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, Cloud, HardDrive, PlugZap, Save, Server, Trash2,
} from 'lucide-react';
import { COLORS, Chip, Field, Panel, bytes, inputStyle } from './ui';
import type { CFStorageConfig, CFStorageUsage } from './types';

type Draft = {
  backend: CFStorageConfig['backend'];
  nextcloud: CFStorageConfig['nextcloud'];
  s3: CFStorageConfig['s3'];
  onedrive: CFStorageConfig['onedrive'];
  local: CFStorageConfig['local'];
  nextcloudPassword: string;
  s3AccessKey: string;
  s3SecretKey: string;
};

type Props = {
  config: CFStorageConfig | null;
  usage: CFStorageUsage | null;
  language: 'ru' | 'en';
  busy?: boolean;
  canEdit?: boolean;
  onSave: (patch: Record<string, unknown>) => Promise<void>;
  onTest: (patch: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onClearSecret: (field: string) => Promise<void>;
  onOneDriveLogin: () => Promise<Record<string, unknown>>;
  onOneDrivePoll: () => Promise<Record<string, unknown>>;
  onOneDriveSignOut: () => Promise<void>;
};

const BACKEND_LABEL: Record<string, [string, string]> = {
  env: ['Как задано в .env', 'As configured in .env'],
  local: ['Локальный диск', 'Local disk'],
  nextcloud: ['Nextcloud', 'Nextcloud'],
  s3: ['S3 / MinIO / R2', 'S3 / MinIO / R2'],
  onedrive: ['OneDrive', 'OneDrive'],
};

const BACKEND_HINT: Record<string, [string, string]> = {
  env: [
    'Решение остаётся за переменными окружения контейнера.',
    'The container environment keeps deciding.',
  ],
  local: [
    'Только для разработки: площадки не смогут скачать готовое видео по ссылке.',
    'Development only: a platform cannot fetch the finished video over a link.',
  ],
  nextcloud: [
    'WebDAV для файлов и публичные ссылки с истечением срока — их и скачивают YouTube и TikTok. '
    + 'Нужен пароль приложения (Настройки → Безопасность), а не пароль аккаунта.',
    'WebDAV for files and expiring public links, which is what YouTube and TikTok fetch. '
    + 'Use an app password (Settings → Security), never the account password.',
  ],
  s3: [
    'Любой S3-совместимый endpoint: AWS, MinIO, Backblaze B2, Cloudflare R2.',
    'Any S3-compatible endpoint: AWS, MinIO, Backblaze B2, Cloudflare R2.',
  ],
  onedrive: [
    'Личный или рабочий OneDrive через Microsoft Graph. Пароль не нужен: вход один раз по коду, '
    + 'дальше работает refresh token. Ссылки для площадок — временные прямые адреса Graph.',
    'Personal or work OneDrive over Microsoft Graph. No password: one device-code sign-in, then a '
    + 'refresh token. Platform links are Graph\'s own short-lived direct URLs.',
  ],
};

function toDraft(config: CFStorageConfig): Draft {
  return {
    backend: config.backend,
    nextcloud: { ...config.nextcloud },
    s3: { ...config.s3 },
    onedrive: { ...(config.onedrive ?? { clientId: '', tenant: 'consumers', root: '' }) },
    local: { ...config.local },
    nextcloudPassword: '',
    s3AccessKey: '',
    s3SecretKey: '',
  };
}

export function StoragePanel({
  config, usage, language, busy, canEdit = true, onSave, onTest, onClearSecret,
  onOneDriveLogin, onOneDrivePoll, onOneDriveSignOut,
}: Props) {
  const ru = language === 'ru';
  const [draft, setDraft] = useState<Draft | null>(config ? toDraft(config) : null);
  const [dirty, setDirty] = useState(false);
  const [probe, setProbe] = useState<Record<string, unknown> | null>(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    setDraft(config ? toDraft(config) : null);
    setDirty(false);
    setProbe(null);
  }, [config]);

  const activeName = String((config?.health as { storage?: string } | undefined)?.storage ?? '—');
  const activeStatus = String((config?.health as { status?: string } | undefined)?.status ?? 'unknown');

  const payload = useMemo(() => {
    if (!draft) return {};
    const body: Record<string, unknown> = {
      backend: draft.backend,
      nextcloud: draft.nextcloud,
      s3: draft.s3,
      onedrive: draft.onedrive,
      local: draft.local,
    };
    // An empty secret box means "keep what is stored" — sending "" would be
    // indistinguishable from it, so the field is simply omitted.
    if (draft.nextcloudPassword.trim()) body.nextcloudPassword = draft.nextcloudPassword.trim();
    if (draft.s3AccessKey.trim()) body.s3AccessKey = draft.s3AccessKey.trim();
    if (draft.s3SecretKey.trim()) body.s3SecretKey = draft.s3SecretKey.trim();
    return body;
  }, [draft]);

  if (!config || !draft) {
    return <div style={{ color: COLORS.faint, fontSize: 12 }}>{ru ? 'Загрузка…' : 'Loading…'}</div>;
  }

  const set = (values: Partial<Draft>) => {
    setDraft((current) => (current ? { ...current, ...values } : current));
    setDirty(true);
  };

  const effective = draft.backend === 'env' ? config.envBackend : draft.backend;
  const localWarning = effective === 'local';

  const runTest = async () => {
    setProbing(true);
    try {
      setProbe(await onTest(payload));
    } catch (exc) {
      setProbe({ status: 'error', detail: (exc as Error).message });
    } finally {
      setProbing(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <StatusTile
          icon={<Cloud size={13} />}
          label={ru ? 'Активное хранилище' : 'Active storage'}
          value={activeName}
          chip={<Chip tone={activeStatus === 'connected' ? (activeName === 'local' ? 'amber' : 'green') : 'red'}>
            {activeStatus}
          </Chip>}
        />
        <StatusTile icon={<HardDrive size={13} />} label={ru ? 'Файлов' : 'Files'}
          value={String(usage?.totalFiles ?? 0)} />
        <StatusTile icon={<Server size={13} />} label={ru ? 'Объём' : 'Size'}
          value={bytes(usage?.totalBytes)} />
      </div>

      {localWarning && (
        <Banner tone="red" icon={<AlertTriangle size={14} />}>
          {ru
            ? 'Ассеты пишутся на локальный диск. YouTube и TikTok скачивают файл по внешней ссылке — '
              + 'с локального диска её выдать нельзя, поэтому публикация работать не будет. '
              + 'Выберите Nextcloud или S3 ниже.'
            : 'Assets are written to local disk. YouTube and TikTok fetch the file over a public link, '
              + 'which local disk cannot provide, so publishing will not work. Pick Nextcloud or S3 below.'}
        </Banner>
      )}

      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <Panel
          title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <PlugZap size={12} />{ru ? 'Внешнее хранилище' : 'External storage'}
          </span>}
          style={{ flex: '1 1 520px', minWidth: 420 }}
        >
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 14 }}>
            {config.backends.map((option) => {
              const selected = draft.backend === option;
              return (
                <button
                  key={option}
                  type="button"
                  disabled={!canEdit}
                  onClick={() => set({ backend: option as Draft['backend'] })}
                  style={{
                    padding: '8px 12px', borderRadius: 9, fontSize: 12, cursor: canEdit ? 'pointer' : 'default',
                    color: selected ? COLORS.cyanSoft : COLORS.muted,
                    background: selected ? 'rgba(54,207,251,0.10)' : COLORS.panelSoft,
                    border: `1px solid ${selected ? COLORS.cyan : COLORS.line}`,
                  }}
                >
                  {(BACKEND_LABEL[option] ?? [option, option])[ru ? 0 : 1]}
                  {option === 'env' && (
                    <span style={{ color: COLORS.faint, fontSize: 10.5 }}> · {config.envBackend}</span>
                  )}
                </button>
              );
            })}
          </div>

          <p style={{ margin: '0 0 14px', fontSize: 11, color: COLORS.faint, lineHeight: 1.55 }}>
            {(BACKEND_HINT[draft.backend] ?? ['', ''])[ru ? 0 : 1]}
          </p>

          {draft.backend === 'nextcloud' && (
            <>
              <Field label={ru ? 'Адрес Nextcloud' : 'Nextcloud URL'}
                hint="https://cloud.example.com · http://192.168.0.200:8080">
                <input style={inputStyle} value={draft.nextcloud.url} disabled={!canEdit}
                  onChange={(event) => set({ nextcloud: { ...draft.nextcloud, url: event.target.value } })} />
              </Field>
              <Field label={ru ? 'Пользователь' : 'User name'}>
                <input style={inputStyle} value={draft.nextcloud.username} disabled={!canEdit}
                  onChange={(event) => set({ nextcloud: { ...draft.nextcloud, username: event.target.value } })} />
              </Field>
              <SecretField
                label={ru ? 'Пароль приложения' : 'App password'}
                hint={ru
                  ? 'Nextcloud → Настройки → Безопасность → Пароль приложения. Его можно отозвать отдельно.'
                  : 'Nextcloud → Settings → Security → App password. It can be revoked on its own.'}
                source={config.secrets.nextcloudPassword}
                value={draft.nextcloudPassword}
                disabled={!canEdit}
                ru={ru}
                onChange={(value) => set({ nextcloudPassword: value })}
                onClear={() => onClearSecret('nextcloudPassword')}
              />
              <Field label={ru ? 'Папка' : 'Folder'}
                hint={ru
                  ? 'Необязательно: дополнительная папка на общей инсталляции.'
                  : 'Optional: an extra folder on a shared instance.'}>
                <input style={inputStyle} value={draft.nextcloud.root} disabled={!canEdit}
                  onChange={(event) => set({ nextcloud: { ...draft.nextcloud, root: event.target.value } })} />
              </Field>
              <Field label={ru ? 'Внешний адрес' : 'Public URL'}
                hint={ru
                  ? 'Если снаружи инсталляция доступна по другому имени — этот адрес получат площадки.'
                  : 'When the instance answers to a different name from outside, platforms get this one.'}>
                <input style={inputStyle} value={draft.nextcloud.publicUrl} disabled={!canEdit}
                  onChange={(event) => set({ nextcloud: { ...draft.nextcloud, publicUrl: event.target.value } })} />
              </Field>
              <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
                <div style={{ flex: '1 1 160px' }}>
                  <Field label={ru ? 'Срок жизни ссылки, дней' : 'Share expiry, days'}>
                    <input type="number" min={1} max={365} style={inputStyle}
                      value={draft.nextcloud.shareDays} disabled={!canEdit}
                      onChange={(event) => set({
                        nextcloud: { ...draft.nextcloud, shareDays: Number(event.target.value) || 7 },
                      })} />
                  </Field>
                </div>
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                  color: COLORS.muted, marginTop: 22 }}>
                  <input type="checkbox" checked={draft.nextcloud.verifyTls} disabled={!canEdit}
                    onChange={(event) => set({
                      nextcloud: { ...draft.nextcloud, verifyTls: event.target.checked },
                    })} />
                  {ru ? 'Проверять TLS-сертификат' : 'Verify the TLS certificate'}
                </label>
              </div>
            </>
          )}

          {draft.backend === 's3' && (
            <>
              <Field label={ru ? 'Бакет' : 'Bucket'}>
                <input style={inputStyle} value={draft.s3.bucket} disabled={!canEdit}
                  onChange={(event) => set({ s3: { ...draft.s3, bucket: event.target.value } })} />
              </Field>
              <Field label="Endpoint" hint={ru
                ? 'Пусто — AWS. Для MinIO/R2 укажите адрес.'
                : 'Empty means AWS; set the address for MinIO or R2.'}>
                <input style={inputStyle} value={draft.s3.endpoint} disabled={!canEdit}
                  onChange={(event) => set({ s3: { ...draft.s3, endpoint: event.target.value } })} />
              </Field>
              <Field label={ru ? 'Регион' : 'Region'}>
                <input style={inputStyle} value={draft.s3.region} disabled={!canEdit}
                  onChange={(event) => set({ s3: { ...draft.s3, region: event.target.value } })} />
              </Field>
              <SecretField label="Access key" source={config.secrets.s3AccessKey} ru={ru}
                value={draft.s3AccessKey} disabled={!canEdit}
                onChange={(value) => set({ s3AccessKey: value })}
                onClear={() => onClearSecret('s3AccessKey')} />
              <SecretField label="Secret key" source={config.secrets.s3SecretKey} ru={ru}
                value={draft.s3SecretKey} disabled={!canEdit}
                onChange={(value) => set({ s3SecretKey: value })}
                onClear={() => onClearSecret('s3SecretKey')} />
              <Field label={ru ? 'Публичный базовый адрес' : 'Public base URL'}>
                <input style={inputStyle} value={draft.s3.publicUrl} disabled={!canEdit}
                  onChange={(event) => set({ s3: { ...draft.s3, publicUrl: event.target.value } })} />
              </Field>
            </>
          )}

          {draft.backend === 'onedrive' && (
            <>
              <Field label="Client ID"
                hint={ru
                  ? 'Регистрация приложения в Entra (portal.azure.com → App registrations), тип «Public client», '
                    + 'разрешения Files.ReadWrite и offline_access. Секрет приложения не нужен.'
                  : 'An Entra app registration (portal.azure.com → App registrations), public-client type, '
                    + 'with Files.ReadWrite and offline_access. No client secret needed.'}>
                <input style={inputStyle} value={draft.onedrive.clientId} disabled={!canEdit}
                  onChange={(event) => set({ onedrive: { ...draft.onedrive, clientId: event.target.value } })} />
              </Field>
              <Field label={ru ? 'Тип аккаунта' : 'Account type'}
                hint={ru
                  ? 'consumers — личный OneDrive, organizations — рабочий, common — оба.'
                  : 'consumers is a personal OneDrive, organizations a work one, common allows both.'}>
                <select style={inputStyle} value={draft.onedrive.tenant} disabled={!canEdit}
                  onChange={(event) => set({ onedrive: { ...draft.onedrive, tenant: event.target.value } })}>
                  <option value="consumers">consumers</option>
                  <option value="organizations">organizations</option>
                  <option value="common">common</option>
                </select>
              </Field>
              <Field label={ru ? 'Папка' : 'Folder'}
                hint={ru ? 'Необязательно: папка внутри диска.' : 'Optional: a folder inside the drive.'}>
                <input style={inputStyle} value={draft.onedrive.root} disabled={!canEdit}
                  onChange={(event) => set({ onedrive: { ...draft.onedrive, root: event.target.value } })} />
              </Field>
              <OneDriveSignIn
                ru={ru}
                canEdit={canEdit}
                busy={busy}
                dirty={dirty}
                signedIn={config.secrets.onedriveRefreshToken === 'stored'
                  || config.secrets.onedriveRefreshToken === 'env'}
                onLogin={onOneDriveLogin}
                onPoll={onOneDrivePoll}
                onSignOut={onOneDriveSignOut}
              />
            </>
          )}

          {draft.backend === 'local' && (
            <Field label={ru ? 'Каталог на диске' : 'Directory on disk'}
              hint={ru ? 'Пусто — каталог по умолчанию внутри контейнера.' : 'Empty uses the default inside the container.'}>
              <input style={inputStyle} value={draft.local.root} disabled={!canEdit}
                onChange={(event) => set({ local: { ...draft.local, root: event.target.value } })} />
            </Field>
          )}

          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6 }}>
            <button type="button" className="btn-ghost" style={{ fontSize: 11.5 }}
              disabled={probing || !canEdit} onClick={() => void runTest()}>
              {probing ? (ru ? 'Проверяем…' : 'Testing…') : (ru ? 'Проверить подключение' : 'Test connection')}
            </button>
            <span style={{ flex: 1 }} />
            <span style={{ fontSize: 11.5, color: dirty ? COLORS.amberSoft : COLORS.faint }}>
              {dirty ? (ru ? 'Есть несохранённые изменения' : 'Unsaved changes') : (ru ? 'Сохранено' : 'Saved')}
            </span>
            <button
              type="button"
              className="btn-primary"
              style={{ fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6 }}
              disabled={!dirty || busy || !canEdit}
              onClick={async () => { await onSave(payload); setDirty(false); }}
            >
              <Save size={12} />{ru ? 'Сохранить' : 'Save'}
            </button>
          </div>

          {probe && (
            <div style={{ marginTop: 12 }}>
              <Banner
                tone={probe.status === 'connected' ? 'green' : 'red'}
                icon={probe.status === 'connected' ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
              >
                {probe.status === 'connected'
                  ? (ru ? 'Подключение установлено.' : 'Connected.')
                  : `${ru ? 'Не удалось подключиться' : 'Could not connect'}: ${String(probe.detail ?? probe.status)}`}
              </Banner>
            </div>
          )}
        </Panel>

        <Panel
          title={<span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <HardDrive size={12} />{ru ? 'Что лежит в хранилище' : 'What is stored'}
          </span>}
          style={{ flex: '1 1 300px', minWidth: 280 }}
        >
          {!usage?.byType.length && (
            <p style={{ margin: 0, fontSize: 11.5, color: COLORS.faint }}>
              {ru ? 'Ассетов пока нет — они появятся после первого прогона.' : 'No assets yet — they appear after the first run.'}
            </p>
          )}
          {usage?.byType.map((row) => (
            <div key={row.type} style={{
              display: 'flex', justifyContent: 'space-between', fontSize: 11.5, color: COLORS.muted,
              padding: '7px 0', borderTop: `1px solid ${COLORS.line}`,
            }}>
              <span>{row.type}</span>
              <span style={{ color: COLORS.text }}>{row.files} · {bytes(row.bytes)}</span>
            </div>
          ))}
          {config.requireRemote && (
            <p style={{ margin: '12px 0 0', fontSize: 10.5, color: COLORS.amberSoft, lineHeight: 1.5 }}>
              {ru
                ? 'CONTENT_FACTORY_REQUIRE_REMOTE включён: при недоступном внешнем хранилище backend откажется стартовать вместо тихой записи на диск.'
                : 'CONTENT_FACTORY_REQUIRE_REMOTE is on: an unreachable remote store fails startup instead of silently writing to disk.'}
            </p>
          )}
        </Panel>
      </div>
    </div>
  );
}

/** The OneDrive sign-in, which is a small state machine rather than a field:
 *  ask Microsoft for a code, show it to the owner, poll until they have typed
 *  it into their browser. Polling stops on any terminal answer — connected,
 *  expired, or an error — so a forgotten tab does not keep hitting Microsoft. */
function OneDriveSignIn({ ru, canEdit, busy, dirty, signedIn, onLogin, onPoll, onSignOut }: {
  ru: boolean;
  canEdit: boolean;
  busy?: boolean;
  dirty: boolean;
  signedIn: boolean;
  onLogin: () => Promise<Record<string, unknown>>;
  onPoll: () => Promise<Record<string, unknown>>;
  onSignOut: () => Promise<void>;
}) {
  const [code, setCode] = useState('');
  const [uri, setUri] = useState('');
  const [state, setState] = useState<'idle' | 'waiting' | 'done' | 'error'>('idle');
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (state !== 'waiting') return undefined;
    const timer = setInterval(async () => {
      try {
        const result = await onPoll();
        const status = String(result.status ?? '');
        if (status === 'connected') {
          setState('done');
          setMessage(ru ? 'OneDrive подключён.' : 'OneDrive connected.');
        } else if (status === 'expired') {
          setState('error');
          setMessage(ru ? 'Код истёк — начните вход заново.' : 'The code expired — start again.');
        }
      } catch (error) {
        setState('error');
        setMessage(error instanceof Error ? error.message : String(error));
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [state, onPoll, ru]);

  const start = async () => {
    setState('idle');
    setMessage('');
    try {
      const result = await onLogin();
      setCode(String(result.userCode ?? ''));
      setUri(String(result.verificationUri ?? ''));
      setState('waiting');
    } catch (error) {
      setState('error');
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div style={{
      borderRadius: 10, border: `1px solid ${COLORS.line}`, background: COLORS.panelSoft,
      padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 12, color: COLORS.text }}>
          {ru ? 'Вход в аккаунт' : 'Account sign-in'}
        </strong>
        <Chip tone={signedIn ? 'green' : 'muted'}>
          {signedIn ? (ru ? 'выполнен' : 'signed in') : (ru ? 'не выполнен' : 'signed out')}
        </Chip>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn-ghost" style={{ fontSize: 11.5 }}
          disabled={!canEdit || busy || state === 'waiting'} onClick={() => void start()}>
          {signedIn ? (ru ? 'Войти заново' : 'Sign in again') : (ru ? 'Войти' : 'Sign in')}
        </button>
        {signedIn && (
          <button type="button" className="btn-ghost" style={{ fontSize: 11.5, color: COLORS.redSoft }}
            disabled={!canEdit || busy} onClick={() => void onSignOut()}>
            {ru ? 'Выйти' : 'Sign out'}
          </button>
        )}
      </div>
      {dirty && (
        <span style={{ fontSize: 11, color: COLORS.amberSoft }}>
          {ru
            ? 'Сначала сохраните Client ID — вход использует сохранённое значение, а не поле выше.'
            : 'Save the client id first — the sign-in uses the stored value, not the field above.'}
        </span>
      )}
      {state === 'waiting' && (
        <div style={{ fontSize: 11.5, color: COLORS.muted, lineHeight: 1.6 }}>
          {ru ? 'Откройте ' : 'Open '}
          <a href={uri} target="_blank" rel="noreferrer" style={{ color: COLORS.cyanSoft }}>{uri}</a>
          {ru ? ' и введите код ' : ' and enter the code '}
          <code style={{ fontSize: 13, color: COLORS.text, letterSpacing: 1 }}>{code}</code>
          {ru ? '. Страница обновится сама.' : '. This panel updates itself.'}
        </div>
      )}
      {message && (
        <span style={{ fontSize: 11.5, color: state === 'error' ? COLORS.redSoft : COLORS.greenSoft }}>
          {message}
        </span>
      )}
    </div>
  );
}

function SecretField({ label, hint, source, value, disabled, ru, onChange, onClear }: {
  label: string;
  hint?: string;
  source?: 'env' | 'stored' | 'none';
  value: string;
  disabled?: boolean;
  ru: boolean;
  onChange: (value: string) => void;
  onClear: () => Promise<void>;
}) {
  const state = source ?? 'none';
  return (
    <Field
      label={label}
      hint={hint ?? (state === 'env'
        ? (ru ? 'Задан в окружении контейнера — поле ниже его не перебьёт.' : 'Set in the container environment; the box below cannot override it.')
        : undefined)}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          type="password"
          style={{ ...inputStyle, flex: 1 }}
          value={value}
          disabled={disabled || state === 'env'}
          placeholder={state === 'none'
            ? (ru ? 'не задан' : 'not set')
            : (ru ? '•••••• сохранён' : '•••••• stored')}
          onChange={(event) => onChange(event.target.value)}
        />
        <Chip tone={state === 'none' ? 'red' : state === 'env' ? 'cyan' : 'green'}>
          {state === 'none' ? (ru ? 'нет' : 'none') : state === 'env' ? '.env' : (ru ? 'сохранён' : 'stored')}
        </Chip>
        {state === 'stored' && (
          <button type="button" className="btn-ghost" style={{ fontSize: 10.5 }}
            disabled={disabled} onClick={() => void onClear()}
            title={ru ? 'Удалить сохранённый секрет' : 'Delete the stored secret'}>
            <Trash2 size={12} />
          </button>
        )}
      </div>
    </Field>
  );
}

function StatusTile({ icon, label, value, chip }: {
  icon: React.ReactNode; label: string; value: string; chip?: React.ReactNode;
}) {
  return (
    <div style={{
      flex: '1 1 160px', minWidth: 150, padding: '12px 14px', borderRadius: 12,
      background: COLORS.panelSoft, border: `1px solid ${COLORS.line}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: COLORS.faint, fontSize: 10,
        letterSpacing: 0.7, textTransform: 'uppercase' }}>
        {icon}<span>{label}</span>
      </div>
      <div style={{ marginTop: 7, display: 'flex', alignItems: 'center', gap: 9 }}>
        <span style={{ fontSize: 20, fontWeight: 700, color: COLORS.text }}>{value}</span>
        {chip}
      </div>
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
