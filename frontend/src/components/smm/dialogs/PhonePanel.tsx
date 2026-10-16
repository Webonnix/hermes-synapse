import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity, CheckCircle2, KeyRound, Mic, Pencil, PhoneCall, PhoneOff, Plus, RefreshCw, Search, Settings2, ShoppingCart,
  Star, Trash2, Volume2, X,
} from 'lucide-react';
import type {
  AvailableNumber, OwnedNumber, PhoneCallRecord, PhoneLine, PhoneOverview, PhoneProvider, RouteTarget, SpeechConfig,
  SpeechProvider,
} from './inboxTypes';
import {
  CALL_END_LABELS, CALL_STATUS_LABELS, formatDuration, formatWhen, inboxApi, openInboxConversation, phoneApi,
} from './inboxApi';
import { CallDialog } from './CallDialog';

function Recommended() {
  return <span className="ph-rec" title="Рекомендуется"><Star size={11} /> рекомендуется</span>;
}

/** «Звонки» (SMM Factory → Диалоги): lines, numbers, speech services, live and recent calls. */
export function PhonePanel() {
  const [overview, setOverview] = useState<PhoneOverview | null>(null);
  const [agents, setAgents] = useState<RouteTarget[]>([]);
  const [calls, setCalls] = useState<PhoneCallRecord[]>([]);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<PhoneLine | 'new' | null>(null);
  const [numbersFor, setNumbersFor] = useState<PhoneLine | null>(null);
  const [calling, setCalling] = useState(false);

  const load = useCallback(async () => {
    try {
      const [data, recent] = await Promise.all([phoneApi.overview(), phoneApi.calls({ limit: 30 })]);
      setOverview(data);
      setCalls(recent);
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    inboxApi.targets().then(list => setAgents(list.filter(t => t.type === 'subagent'))).catch(() => setAgents([]));
    const timer = setInterval(() => void load(), 15000);
    return () => clearInterval(timer);
  }, [load]);

  if (!overview) {
    return <div className="glass-panel ph-card">{error ? <div className="cl-error-banner" role="alert">{error}</div> : 'Загрузка…'}</div>;
  }
  const agentName = (id: string) => agents.find(a => a.id === id)?.name || id || '— не назначен —';

  return (
    <div className="ph-root">
      {error && <div className="cl-error-banner" role="alert">{error}</div>}
      <StatusCard overview={overview} onChanged={load} onCall={() => setCalling(true)} />

      <section className="glass-panel ph-card">
        <div className="cl-settings-head">
          <div>
            <h3><PhoneCall size={15} /> Линии</h3>
            <p className="cl-muted">Номер, провайдер и агент, который разговаривает. Звонки и расшифровки попадают в «Все диалоги».</p>
          </div>
          {overview.can_manage && (
            <button type="button" className="btn-primary" onClick={() => setEditing('new')}><Plus size={14} /><span>Добавить линию</span></button>
          )}
        </div>
        {overview.lines.length === 0 && <div className="admin-empty">Линий пока нет.</div>}
        <div className="ph-grid">
          {overview.lines.map(line => (
            <LineCard key={line.id} line={line} provider={overview.providers[line.provider]} agentName={agentName(line.subagent_id)}
                      canManage={overview.can_manage} publicBase={overview.status.settings.public_base_url}
                      onEdit={() => setEditing(line)} onNumbers={() => setNumbersFor(line)} onChanged={load} />
          ))}
        </div>
      </section>

      <SpeechCard overview={overview} onChanged={load} />
      <CallsCard calls={calls} overview={overview} onChanged={load} />

      {editing && (
        <LineForm overview={overview} agents={agents} line={editing === 'new' ? null : editing}
                  onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void load(); }} />
      )}
      {numbersFor && <NumbersDialog line={numbersFor} onClose={() => { setNumbersFor(null); void load(); }} />}
      {calling && <CallDialog onClose={() => { setCalling(false); void load(); }} />}
    </div>
  );
}

function StatusCard({ overview, onChanged, onCall }: { overview: PhoneOverview; onChanged: () => void; onCall: () => void }) {
  const asterisk = overview.status.asterisk;
  const [baseUrl, setBaseUrl] = useState(overview.status.settings.public_base_url);
  const [maxCalls, setMaxCalls] = useState(String(overview.status.settings.max_concurrent_calls));
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const needsPublicUrl = overview.lines.some(line => overview.providers[line.provider]?.needs_public_url);

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setMessage('');
    try {
      setMessage(await action());
      onChanged();
    } catch (exc) {
      setMessage((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="glass-panel ph-card">
      <div className="cl-settings-head">
        <div>
          <h3><Activity size={15} /> Состояние</h3>
          <p className="cl-muted">
            Asterisk (SIP для Zadarma):{' '}
            <span className={`admin-status-chip ${asterisk.connected ? 'is-active' : 'is-revoked'}`}>
              <i className="dot" />{asterisk.connected ? `на связи${asterisk.version ? ` · ${asterisk.version}` : ''}` : (asterisk.last_error || 'нет связи')}
            </span>{' '}· активных звонков: {overview.status.active_calls.length}
          </p>
        </div>
        <div className="ph-actions">
          {overview.can_call && <button type="button" className="btn-primary" onClick={onCall}><PhoneCall size={14} /><span>Позвонить</span></button>}
          {overview.can_manage && (
            <button type="button" className="btn-ghost" disabled={busy} onClick={() => void run(async () => {
              const result = await phoneApi.selftest();
              return result.ok ? `Самотест пройден: ${result.audio_ms} мс звука дошло до агента` : `Самотест не пройден: ${result.error || 'звук не дошёл'}`;
            })}><CheckCircle2 size={14} /><span>Самотест звука</span></button>
          )}
        </div>
      </div>
      {overview.can_manage && (
        <div className="ph-form-row">
          <label className="ph-field">Публичный HTTPS-адрес (для Twilio и Telnyx)
            <input className="form-input" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder="https://hermes.example.com" />
          </label>
          <label className="ph-field ph-narrow">Одновременных звонков
            <input className="form-input" type="number" min={1} max={50} value={maxCalls} onChange={e => setMaxCalls(e.target.value)} />
          </label>
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => void run(async () => {
            await phoneApi.saveSettings({ public_base_url: baseUrl, max_concurrent_calls: Number(maxCalls) });
            return 'Сохранено';
          })}><Settings2 size={14} /><span>Сохранить</span></button>
        </div>
      )}
      {needsPublicUrl && (
        <p className="cl-hint">
          Twilio и Telnyx стучатся на этот адрес снаружи. Если домен закрыт Cloudflare Access, добавьте правило Bypass для путей
          {' '}<code>/api/phone/twilio/*</code> и <code>/api/phone/telnyx/*</code> — они защищены подписью провайдера.
        </p>
      )}
      {message && <p className="cl-muted" role="status">{message}</p>}
    </section>
  );
}

function LineCard({ line, provider, agentName, canManage, publicBase, onEdit, onNumbers, onChanged }: {
  line: PhoneLine; provider?: PhoneProvider; agentName: string; canManage: boolean; publicBase: string;
  onEdit: () => void; onNumbers: () => void; onChanged: () => void;
}) {
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const webhook = line.provider === 'twilio'
    ? `${publicBase}/api/phone/twilio/voice/${line.id}`
    : line.provider === 'telnyx' ? `${publicBase}/api/phone/telnyx/events/${line.id}` : '';

  return (
    <article className="glass-panel ph-line">
      <header>
        <div>
          <strong>{line.name}</strong>
          <div className="cl-muted">{line.provider_label}{provider?.recommended ? ' · рекомендуется' : ''}</div>
        </div>
        <span className={`admin-status-chip ${line.enabled ? (line.status === 'error' ? 'is-revoked' : 'is-active') : ''}`}>
          <i className="dot" />{!line.enabled ? 'выключена' : line.status === 'error' ? 'ошибка' : line.status === 'ok' ? 'работает' : 'не проверена'}
        </span>
      </header>
      <div className="cl-info-row"><span>Номер</span><strong>{line.number || '—'}</strong></div>
      <div className="cl-info-row"><span>Агент</span><strong>{agentName}</strong></div>
      <div className="cl-info-row"><span>Звонки</span><strong>{[line.inbound_enabled && 'входящие', line.outbound_enabled && 'исходящие'].filter(Boolean).join(', ') || 'выключены'}</strong></div>
      <div className="cl-info-row"><span>Перевод на</span><strong>{line.owner_number || '—'}</strong></div>
      {line.last_error && <div className="cl-error-note">{line.last_error}</div>}
      {webhook && publicBase && <div className="cl-hint">Вебхук: <code className="ibx-mono">{webhook}</code></div>}
      {message && <div className="cl-muted" role="status">{message}</div>}
      {canManage && (
        <div className="ph-actions">
          <button type="button" className="btn-ghost" disabled={busy} onClick={async () => {
            setBusy(true);
            try {
              const result = await phoneApi.checkLine(line.id);
              setMessage(result.ok ? 'Аккаунт в порядке' : `Ошибка: ${result.error}`);
              onChanged();
            } catch (exc) {
              setMessage((exc as Error).message);
            } finally {
              setBusy(false);
            }
          }}><RefreshCw size={13} /><span>Проверить</span></button>
          <button type="button" className="btn-ghost" onClick={onEdit}><Pencil size={13} /><span>Изменить</span></button>
          {provider?.number_purchase && <button type="button" className="btn-ghost" onClick={onNumbers}><ShoppingCart size={13} /><span>Номера</span></button>}
          {provider && !provider.number_purchase && provider.cabinet_url && (
            <a className="btn-ghost" href={provider.cabinet_url} target="_blank" rel="noreferrer">Кабинет {provider.label}</a>
          )}
          <button type="button" className="btn-ghost danger" aria-label={`Удалить линию ${line.name}`} onClick={async () => {
            if (!window.confirm(`Удалить линию «${line.name}»? Номер у провайдера останется за вами.`)) return;
            try {
              await phoneApi.deleteLine(line.id);
              onChanged();
            } catch (exc) {
              setMessage((exc as Error).message);
            }
          }}><Trash2 size={13} /></button>
        </div>
      )}
    </article>
  );
}

const LANGUAGE_LABELS: Record<string, string> = { auto: 'Автоопределение (русский/английский)', ru: 'Русский', en: 'English' };

function LineForm({ overview, agents, line, onClose, onSaved }: {
  overview: PhoneOverview; agents: RouteTarget[]; line: PhoneLine | null; onClose: () => void; onSaved: () => void;
}) {
  const providerIds = Object.keys(overview.providers);
  const [provider, setProvider] = useState(line?.provider || providerIds.find(id => overview.providers[id].recommended) || providerIds[0]);
  const [values, setValues] = useState<Record<string, string | boolean | number>>(() => ({
    name: line?.name || '', number: line?.number || '', subagent_id: line?.subagent_id || '',
    greeting: line?.greeting ?? 'Здравствуйте! Я ИИ-ассистент. Чем могу помочь?', language: line?.language || 'auto',
    owner_number: line?.owner_number || '', enabled: line?.enabled ?? true, inbound_enabled: line?.inbound_enabled ?? true,
    outbound_enabled: line?.outbound_enabled ?? true, auto_book: line?.auto_book ?? false, max_call_minutes: line?.max_call_minutes ?? 20,
    ...(line?.config || {}),
  }));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const spec = overview.providers[provider];
  const set = (key: string, value: string | boolean | number) => setValues(prev => ({ ...prev, [key]: value }));

  const save = async () => {
    setBusy(true);
    setError('');
    const payload: Record<string, unknown> = { ...values, provider };
    for (const field of spec.fields) {
      if (field.secret && !String(values[field.key] || '')) delete payload[field.key];
    }
    try {
      if (line) await phoneApi.updateLine(line.id, payload);
      else await phoneApi.createLine(payload);
      onSaved();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ibx-modal-backdrop" role="presentation">
      <div className="ibx-modal glass-panel ph-wide" role="dialog" aria-modal="true" aria-label={line ? 'Изменить линию' : 'Новая линия'}>
        <header>
          <h3>{line ? `Линия «${line.name}»` : 'Новая линия'}</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть"><X size={16} /></button>
        </header>
        <div className="ibx-modal-body">
          {!line && (
            <div className="ph-provider-cards" role="radiogroup" aria-label="Провайдер">
              {providerIds.map(id => (
                <button key={id} type="button" role="radio" aria-checked={provider === id}
                        className={`ph-provider-card ${provider === id ? 'is-selected' : ''}`} onClick={() => setProvider(id)}>
                  <strong>{overview.providers[id].label} {overview.providers[id].recommended && <Recommended />}</strong>
                  <span>{overview.providers[id].note}</span>
                </button>
              ))}
            </div>
          )}
          <div className="ph-form-grid">
            <label className="ph-field">Название<input className="form-input" value={String(values.name)} onChange={e => set('name', e.target.value)} /></label>
            <label className="ph-field">Номер линии<input className="form-input" value={String(values.number)} placeholder="+972501234567" onChange={e => set('number', e.target.value)} /></label>
            {spec.fields.map(field => (
              <label key={field.key} className="ph-field">
                {field.label}{field.secret && line?.secrets_set?.[field.key] ? ' (сохранён — оставьте пустым, чтобы не менять)' : ''}
                <input className="form-input" type={field.secret ? 'password' : 'text'} autoComplete={field.secret ? 'new-password' : 'off'}
                       value={field.secret ? String(values[field.key] || '') : String(values[field.key] ?? field.default ?? '')}
                       onChange={e => set(field.key, e.target.value)} />
              </label>
            ))}
            <label className="ph-field">Агент, который разговаривает
              <select className="form-input" value={String(values.subagent_id)} onChange={e => set('subagent_id', e.target.value)}>
                <option value="">— выберите агента —</option>
                {agents.map(agent => <option key={agent.id} value={agent.id} disabled={!agent.available}>{agent.name}</option>)}
              </select>
            </label>
            <label className="ph-field">Язык разговора
              <select className="form-input" value={String(values.language)} onChange={e => set('language', e.target.value)}>
                {Object.entries(LANGUAGE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="ph-field">Перевод на владельца (номер)
              <input className="form-input" value={String(values.owner_number)} placeholder="+972509999999" onChange={e => set('owner_number', e.target.value)} />
            </label>
            <label className="ph-field">Максимум минут на звонок
              <input className="form-input" type="number" min={1} max={120} value={Number(values.max_call_minutes)} onChange={e => set('max_call_minutes', Number(e.target.value))} />
            </label>
          </div>
          <label className="ph-field">Приветствие на входящих (скажите, что это ИИ-ассистент)
            <textarea className="form-input" rows={2} value={String(values.greeting)} onChange={e => set('greeting', e.target.value)} />
          </label>
          <div className="ph-toggles">
            {([['enabled', 'Линия включена'], ['inbound_enabled', 'Принимать входящие'], ['outbound_enabled', 'Разрешить исходящие'],
               ['auto_book', 'Записывать встречи сразу в календарь (иначе — на подтверждение)']] as const).map(([key, label]) => (
              <label key={key} className="ibx-check">
                <input type="checkbox" checked={Boolean(values[key])} onChange={e => set(key, e.target.checked)} /><span>{label}</span>
              </label>
            ))}
          </div>
          {spec.needs_public_url && !overview.status.settings.public_base_url && (
            <div className="cl-error-banner" role="alert">Для {spec.label} сначала задайте публичный HTTPS-адрес в «Состоянии».</div>
          )}
          {error && <div className="cl-error-banner" role="alert">{error}</div>}
          <div className="ibx-modal-actions">
            <button type="button" className="btn-ghost" onClick={onClose}>Отмена</button>
            <button type="button" className="btn-primary" disabled={busy} onClick={() => void save()}>{busy ? 'Сохраняю…' : 'Сохранить'}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function NumbersDialog({ line, onClose }: { line: PhoneLine; onClose: () => void }) {
  const [country, setCountry] = useState('US');
  const [kind, setKind] = useState('Local');
  const [contains, setContains] = useState('');
  const [results, setResults] = useState<AvailableNumber[]>([]);
  const [owned, setOwned] = useState<OwnedNumber[]>([]);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    phoneApi.ownedNumbers(line.id).then(setOwned).catch(() => setOwned([]));
  }, [line.id]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ibx-modal-backdrop" role="presentation">
      <div className="ibx-modal glass-panel ph-wide" role="dialog" aria-modal="true" aria-label="Номера">
        <header>
          <h3><ShoppingCart size={16} /> Номера · {line.name}</h3>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть"><X size={16} /></button>
        </header>
        <div className="ibx-modal-body">
          <div className="ph-form-row">
            <label className="ph-field ph-narrow">Страна (код)
              <input className="form-input" value={country} maxLength={2} onChange={e => setCountry(e.target.value.toUpperCase())} aria-label="Код страны" />
            </label>
            {line.provider === 'twilio' && (
              <label className="ph-field ph-narrow">Тип
                <select className="form-input" value={kind} onChange={e => setKind(e.target.value)}>
                  <option value="Local">Городской</option><option value="Mobile">Мобильный</option><option value="TollFree">Бесплатный</option>
                </select>
              </label>
            )}
            <label className="ph-field ph-narrow">Содержит цифры
              <input className="form-input" value={contains} onChange={e => setContains(e.target.value)} />
            </label>
            <button type="button" className="btn-primary" disabled={busy || country.length !== 2}
                    onClick={() => void run(async () => setResults(await phoneApi.searchNumbers(line.id, country, kind, contains)))}>
              <Search size={14} /><span>Найти</span>
            </button>
          </div>
          {error && <div className="cl-error-banner" role="alert">{error}</div>}
          {message && <div className="cl-muted" role="status">{message}</div>}
          <div className="ph-table-wrap">
            <table className="ph-table">
              <tbody>
                {results.map(item => (
                  <tr key={item.phone_number}>
                    <td className="ibx-mono">{item.phone_number}</td>
                    <td>{item.locality || item.kind}</td>
                    <td>{item.monthly_cost ? `${item.monthly_cost} ${item.currency || ''}/мес` : 'цена у провайдера'}</td>
                    <td>
                      <button type="button" className="btn-ghost" disabled={busy} onClick={() => {
                        const price = item.monthly_cost ? `${item.monthly_cost} ${item.currency || ''} в месяц` : 'по тарифу провайдера';
                        if (!window.confirm(`Купить номер ${item.phone_number} (${price})? Оплата спишется с вашего аккаунта ${line.provider_label}.`)) return;
                        void run(async () => {
                          await phoneApi.buyNumber(line.id, item.phone_number);
                          setMessage(`Номер ${item.phone_number} куплен и подключён к линии`);
                        });
                      }}>Купить</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {owned.length > 0 && (
            <>
              <h4>Уже есть в аккаунте</h4>
              <div className="ph-table-wrap">
                <table className="ph-table">
                  <tbody>
                    {owned.map(item => (
                      <tr key={item.sid}>
                        <td className="ibx-mono">{item.phone_number}</td>
                        <td>
                          <button type="button" className="btn-ghost" disabled={busy || item.phone_number === line.number}
                                  onClick={() => void run(async () => {
                                    await phoneApi.attachNumber(line.id, item.phone_number);
                                    setMessage(`Номер ${item.phone_number} подключён к линии`);
                                  })}>
                            {item.phone_number === line.number ? 'Подключён' : 'Подключить'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function SpeechCard({ overview, onChanged }: { overview: PhoneOverview; onChanged: () => void }) {
  const [config, setConfig] = useState<SpeechConfig>(overview.speech.config);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const canManage = overview.can_manage;
  const stt = overview.speech.stt.find(p => p.id === config.stt_provider);
  const tts = overview.speech.tts.find(p => p.id === config.tts_provider);
  const keyProviders = useMemo(() => {
    const seen = new Map<string, SpeechProvider>();
    [...overview.speech.stt, ...overview.speech.tts].forEach(p => { if (p.needs_key && !seen.has(p.id)) seen.set(p.id, p); });
    return [...seen.values()];
  }, [overview.speech]);

  useEffect(() => { setConfig(overview.speech.config); }, [overview.speech.config]);

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setMessage('');
    try {
      setMessage(await action());
      onChanged();
    } catch (exc) {
      setMessage((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const pick = (kind: 'stt' | 'tts', id: string) => {
    const provider = overview.speech[kind].find(p => p.id === id);
    if (!provider) return;
    setConfig(prev => kind === 'stt'
      ? { ...prev, stt_provider: id, stt_model: provider.default_model, stt_language: provider.default_language || 'auto' }
      : { ...prev, tts_provider: id, tts_model: provider.default_model, tts_voice: provider.default_voice || '' });
  };

  return (
    <section className="glass-panel ph-card">
      <div className="cl-settings-head">
        <div>
          <h3><Mic size={15} /> Распознавание и синтез речи</h3>
          <p className="cl-muted">Думает ваш локальный агент; облако только слышит и говорит. Если облачный сервис недоступен — звонок продолжится на локальных движках.</p>
        </div>
        {canManage && (
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => void run(async () => {
            const result = await phoneApi.testSpeech();
            return result.ok ? `Голос работает (${result.engine}): первый звук через ${result.first_audio_ms} мс${result.degraded ? ' — облако не ответило, сработал резерв' : ''}` : `Не работает: ${result.error}`;
          })}><Volume2 size={14} /><span>Проверить голос</span></button>
        )}
      </div>
      <div className="ph-speech-grid">
        {(['stt', 'tts'] as const).map(kind => (
          <div key={kind} className="ph-speech-col">
            <h4>{kind === 'stt' ? 'Слышит (распознавание)' : 'Говорит (синтез)'}</h4>
            <div className="ph-provider-cards" role="radiogroup" aria-label={kind === 'stt' ? 'Сервис распознавания' : 'Сервис синтеза'}>
              {overview.speech[kind].map(provider => (
                <button key={provider.id} type="button" role="radio" disabled={!canManage}
                        aria-checked={config[`${kind}_provider`] === provider.id}
                        className={`ph-provider-card ${config[`${kind}_provider`] === provider.id ? 'is-selected' : ''}`}
                        onClick={() => pick(kind, provider.id)}>
                  <strong>{provider.label} {provider.recommended && <Recommended />}</strong>
                  <span>{provider.note}</span>
                  {provider.needs_key && <em className={provider.has_secret ? 'is-ok' : 'is-missing'}>{provider.has_secret ? 'ключ сохранён' : 'нужен ключ'}</em>}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="ph-form-grid">
        {stt && (
          <label className="ph-field">Модель распознавания
            <select className="form-input" disabled={!canManage} value={config.stt_model} onChange={e => setConfig({ ...config, stt_model: e.target.value })}>
              {stt.models.map(model => <option key={model} value={model}>{model}</option>)}
            </select>
          </label>
        )}
        {stt?.languages && (
          <label className="ph-field">Язык распознавания
            <select className="form-input" disabled={!canManage} value={config.stt_language} onChange={e => setConfig({ ...config, stt_language: e.target.value })}>
              {stt.languages.map(language => <option key={language} value={language}>{language === 'multi' ? 'русский + английский' : language}</option>)}
            </select>
          </label>
        )}
        {tts && (
          <label className="ph-field">Модель синтеза
            <select className="form-input" disabled={!canManage} value={config.tts_model} onChange={e => setConfig({ ...config, tts_model: e.target.value })}>
              {tts.models.map(model => <option key={model} value={model}>{model}</option>)}
            </select>
          </label>
        )}
        {tts && tts.id !== 'local' && (
          <label className="ph-field">Голос{tts.id === 'elevenlabs' ? ' (Voice ID из библиотеки ElevenLabs)' : ''}
            {tts.voices ? (
              <select className="form-input" disabled={!canManage} value={config.tts_voice} onChange={e => setConfig({ ...config, tts_voice: e.target.value })}>
                {tts.voices.map(voice => <option key={voice} value={voice}>{voice}</option>)}
              </select>
            ) : (
              <input className="form-input" disabled={!canManage} value={config.tts_voice} onChange={e => setConfig({ ...config, tts_voice: e.target.value })} />
            )}
          </label>
        )}
        {tts && (tts.id === 'elevenlabs' || tts.id === 'yandex') && (
          <label className="ph-field">Голос для английского (необязательно)
            <input className="form-input" disabled={!canManage} value={config.tts_voice_en} onChange={e => setConfig({ ...config, tts_voice_en: e.target.value })} />
          </label>
        )}
        {(stt?.needs_folder || tts?.needs_folder) && (
          <label className="ph-field">Yandex Cloud folder ID
            <input className="form-input" disabled={!canManage} value={config.yandex_folder_id} onChange={e => setConfig({ ...config, yandex_folder_id: e.target.value })} />
          </label>
        )}
      </div>
      <label className="ibx-check">
        <input type="checkbox" disabled={!canManage} checked={config.fallback_local} onChange={e => setConfig({ ...config, fallback_local: e.target.checked })} />
        <span>Если облако недоступно — продолжать на локальных движках (медленнее, но разговор не обрывается)</span>
      </label>
      {canManage && (
        <>
          <div className="ph-actions">
            <button type="button" className="btn-primary" disabled={busy} onClick={() => void run(async () => {
              await phoneApi.saveSpeech(config);
              return 'Настройки речи сохранены';
            })}>Сохранить выбор</button>
          </div>
          <h4><KeyRound size={13} /> Ключи сервисов</h4>
          <div className="ph-keys">
            {keyProviders.map(provider => (
              <div key={provider.id} className="ph-key-row">
                <span>{provider.label}</span>
                <input className="form-input" type="password" autoComplete="new-password" placeholder={provider.has_secret ? 'сохранён' : 'API-ключ'}
                       value={keys[provider.id] || ''} onChange={e => setKeys({ ...keys, [provider.id]: e.target.value })}
                       aria-label={`Ключ ${provider.label}`} />
                <button type="button" className="btn-ghost" disabled={busy || !(keys[provider.id] || '').trim()} onClick={() => void run(async () => {
                  await phoneApi.setSpeechKey(provider.id, keys[provider.id]);
                  setKeys(prev => ({ ...prev, [provider.id]: '' }));
                  return `Ключ ${provider.label} сохранён`;
                })}>Сохранить</button>
                {provider.has_secret && (
                  <button type="button" className="btn-ghost danger" disabled={busy} onClick={() => void run(async () => {
                    await phoneApi.clearSpeechKey(provider.id);
                    return `Ключ ${provider.label} удалён`;
                  })}>Удалить</button>
                )}
              </div>
            ))}
          </div>
        </>
      )}
      {message && <p className="cl-muted" role="status">{message}</p>}
    </section>
  );
}

function CallsCard({ calls, overview, onChanged }: { calls: PhoneCallRecord[]; overview: PhoneOverview; onChanged: () => void }) {
  const lineName = (id: string) => overview.lines.find(line => line.id === id)?.name || id;
  const [error, setError] = useState('');

  return (
    <section className="glass-panel ph-card">
      <div className="cl-settings-head">
        <div>
          <h3><PhoneCall size={15} /> Звонки</h3>
          <p className="cl-muted">«Отклик» — время от конца фразы собеседника до первого звука ответа, среднее за звонок.</p>
        </div>
      </div>
      {error && <div className="cl-error-banner" role="alert">{error}</div>}
      {calls.length === 0 ? <div className="admin-empty">Звонков пока не было.</div> : (
        <div className="ph-table-wrap">
          <table className="ph-table">
            <thead>
              <tr><th>Когда</th><th>Кто</th><th>Линия</th><th>Статус</th><th>Длительность</th><th>Отклик</th><th /></tr>
            </thead>
            <tbody>
              {calls.map(call => (
                <tr key={call.id}>
                  <td>{formatWhen(call.started_at)}</td>
                  <td>{call.direction === 'inbound' ? '← ' : '→ '}<span className="ibx-mono">{call.remote_number || 'скрыт'}</span>
                    {call.purpose && <div className="cl-muted">{call.purpose}</div>}</td>
                  <td>{lineName(call.line_id)}</td>
                  <td>{CALL_STATUS_LABELS[call.status] || call.status}{call.end_reason ? ` · ${CALL_END_LABELS[call.end_reason] || call.end_reason}` : ''}
                    {call.degraded.length > 0 && <div className="cl-muted">резерв: {call.degraded.join(', ')}</div>}</td>
                  <td>{formatDuration(call.duration_s)}</td>
                  <td>{call.first_audio_ms_avg !== null ? `${(call.first_audio_ms_avg / 1000).toFixed(1)} с` : '—'}</td>
                  <td className="ph-actions">
                    {call.conversation_id && (
                      <button type="button" className="btn-ghost" onClick={() => openInboxConversation(call.conversation_id!)}>Разговор</button>
                    )}
                    {call.status === 'active' && overview.can_control_calls && (
                      <>
                        <button type="button" className="btn-ghost" onClick={async () => {
                          try { await phoneApi.transferCall(call.id); onChanged(); } catch (exc) { setError((exc as Error).message); }
                        }}>Перевести на меня</button>
                        <button type="button" className="btn-ghost danger" onClick={async () => {
                          try { await phoneApi.hangupCall(call.id); onChanged(); } catch (exc) { setError((exc as Error).message); }
                        }}><PhoneOff size={13} /><span>Завершить</span></button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
