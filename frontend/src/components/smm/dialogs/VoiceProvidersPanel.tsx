import { useCallback, useEffect, useMemo, useState } from 'react';
import { KeyRound, ListMusic, Mic, Plus, RefreshCw, Stethoscope, Trash2 } from 'lucide-react';
import type { VoiceInfo, VoiceProvider, VoiceProviderKind, VoiceTarget } from './inboxTypes';
import { voiceApi } from './inboxApi';
import './inbox.css';

const POLICY_LABELS: Record<string, string> = {
  fallback_internal: 'Ответит агент канала',
  fail_closed: 'Не отвечать',
  human_takeover: 'Передать оператору',
};
const MODE_LABELS: Record<string, string> = { voice_agent: 'Голосовой агент', tts: 'Озвучка ответа' };

const emptyProvider = { name: '', kind: 'openai_compatible', base_url: '', api_key: '', config: '', timeout_seconds: '20', max_retries: '1' };
const emptyTarget = { provider_id: '', name: '', capability_mode: 'voice_agent', voice_id: '', agent_id: '', failure_policy: 'fallback_internal' };

/**
 * External voice providers and the targets conversation routing can pin a
 * conversation to. Health is a button, never a gate: the page renders from what
 * is stored and a slow provider can only slow down its own «Проверить».
 */
export function VoiceProvidersPanel() {
  const [kinds, setKinds] = useState<Record<string, VoiceProviderKind>>({});
  const [providers, setProviders] = useState<VoiceProvider[]>([]);
  const [targets, setTargets] = useState<VoiceTarget[]>([]);
  const [error, setError] = useState('');
  const [providerForm, setProviderForm] = useState(emptyProvider);
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [targetForm, setTargetForm] = useState(emptyTarget);
  const [voices, setVoices] = useState<Record<string, VoiceInfo[]>>({});
  const [busy, setBusy] = useState('');
  const [revealedSecret, setRevealedSecret] = useState<{ providerId: string; secret: string; path: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [providerResult, targetList] = await Promise.all([voiceApi.providers(), voiceApi.targets()]);
      setKinds(providerResult.kinds);
      setProviders(providerResult.items);
      setTargets(targetList);
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const kindCaps = useMemo(() => kinds[providerForm.kind]?.capabilities ?? [], [kinds, providerForm.kind]);
  useEffect(() => { setCapabilities(kindCaps); }, [kindCaps]);

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    setError('');
    try {
      await action();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy('');
    }
  };

  const createProvider = () => run('create-provider', async () => {
    let config: Record<string, unknown> | undefined;
    if (providerForm.config.trim()) {
      try {
        config = JSON.parse(providerForm.config);
      } catch {
        throw new Error('config — не валидный JSON');
      }
    }
    await voiceApi.createProvider({
      name: providerForm.name, kind: providerForm.kind, base_url: providerForm.base_url,
      api_key: providerForm.api_key || undefined, capabilities, config,
      timeout_seconds: Number(providerForm.timeout_seconds) || 20, max_retries: Number(providerForm.max_retries) || 0,
    });
    // The key is write-only: once saved it is never shown again, not even in this form.
    setProviderForm(emptyProvider);
    await load();
  });

  const selectedProvider = providers.find(p => p.id === targetForm.provider_id);
  const modes = (selectedProvider?.capabilities ?? []).filter(cap => cap === 'voice_agent' || cap === 'tts');

  return (
    <div className="glass-panel" style={{ padding: 20 }}>
      <div className="admin-section-head">
        <div>
          <h3><Mic size={16} /> Внешний голос</h3>
          <p>
            Провайдеры речи и удалённые голосовые агенты. Цель из списка ниже можно закрепить за разговором
            в SMM Factory → «Диалоги» — маршрутизация передаст ей ход, а при сбое сработает выбранная политика.
            Ключи хранятся в защищённом хранилище и после сохранения не показываются.
          </p>
        </div>
        <button type="button" className="btn-ghost" onClick={() => void load()}><RefreshCw size={14} /><span>Обновить</span></button>
      </div>

      {error && <div className="cl-error-banner" role="alert">{error}</div>}

      <h4 className="vp-subhead">Провайдеры</h4>
      <div className="vp-grid">
        {providers.length === 0 && <div className="admin-empty">Провайдеров пока нет.</div>}
        {providers.map(provider => (
          <article key={provider.id} className="glass-panel vp-card">
            <header>
              <div>
                <strong>{provider.name}</strong>
                <div className="cl-muted">{provider.kind_label}</div>
              </div>
              <span className={`admin-status-chip ${provider.enabled ? 'is-active' : 'is-revoked'}`}>
                <i className="dot" />{provider.enabled ? 'включён' : 'выключен'}
              </span>
            </header>
            <div className="ibx-mono">{provider.base_url}</div>
            <div className="vp-caps">{provider.capabilities.map(cap => <span key={cap} className="vp-cap">{cap}</span>)}</div>
            <div className="cl-info-row"><span>Ключ</span><strong>{provider.has_secret ? 'сохранён' : 'не задан'}</strong></div>
            <div className="cl-info-row">
              <span>Проверка</span>
              <strong>
                {provider.last_health?.checked_at
                  ? (provider.last_health.ok ? `ok · ${provider.last_health.latency_ms} мс` : `ошибка: ${provider.last_health.error || '—'}`)
                  : 'не проверялся'}
              </strong>
            </div>
            {revealedSecret?.providerId === provider.id && (
              <div className="vp-secret" role="status">
                Секрет webhook (показан один раз): {revealedSecret.secret}<br />Адрес: {revealedSecret.path}
              </div>
            )}
            {voices[provider.id] && (
              <div className="vp-voices">
                {voices[provider.id].length === 0 ? <span className="cl-muted">Голоса не получены</span>
                  : voices[provider.id].map(v => <span key={v.id} className="vp-cap" title={v.id}>{v.name}{v.language ? ` · ${v.language}` : ''}</span>)}
              </div>
            )}
            <div className="vp-actions">
              <button type="button" className="btn-ghost" disabled={busy === `test-${provider.id}`}
                      onClick={() => run(`test-${provider.id}`, async () => { await voiceApi.testProvider(provider.id); await load(); })}>
                <Stethoscope size={13} /><span>{busy === `test-${provider.id}` ? 'Проверяю…' : 'Проверить'}</span>
              </button>
              {provider.capabilities.includes('voices') && (
                <button type="button" className="btn-ghost" disabled={busy === `voices-${provider.id}`}
                        onClick={() => run(`voices-${provider.id}`, async () => {
                          const list = await voiceApi.voices(provider.id);
                          setVoices(prev => ({ ...prev, [provider.id]: list }));
                        })}>
                  <ListMusic size={13} /><span>Голоса</span>
                </button>
              )}
              <button type="button" className="btn-ghost"
                      onClick={() => run(`secret-${provider.id}`, async () => {
                        const result = await voiceApi.rotateWebhookSecret(provider.id);
                        setRevealedSecret({ providerId: provider.id, secret: result.webhook_secret, path: result.webhook_path });
                        await load();
                      })}>
                <KeyRound size={13} /><span>{provider.has_webhook_secret ? 'Новый секрет webhook' : 'Секрет webhook'}</span>
              </button>
              <button type="button" className="btn-ghost"
                      onClick={() => run(`toggle-${provider.id}`, async () => { await voiceApi.updateProvider(provider.id, { enabled: !provider.enabled }); await load(); })}>
                <span>{provider.enabled ? 'Выключить' : 'Включить'}</span>
              </button>
              <button type="button" className="btn-ghost danger" aria-label={`Удалить провайдера ${provider.name}`}
                      onClick={() => run(`delete-${provider.id}`, async () => {
                        if (!window.confirm(`Удалить провайдера «${provider.name}» и его цели?`)) return;
                        await voiceApi.deleteProvider(provider.id);
                        await load();
                      })}>
                <Trash2 size={13} />
              </button>
            </div>
          </article>
        ))}
      </div>

      <div className="vp-form">
        <label>Название<input className="form-input" value={providerForm.name} onChange={e => setProviderForm({ ...providerForm, name: e.target.value })} /></label>
        <label>Тип
          <select className="form-input" value={providerForm.kind} onChange={e => setProviderForm({ ...providerForm, kind: e.target.value })}>
            {Object.entries(kinds).map(([kind, info]) => <option key={kind} value={kind}>{info.label}</option>)}
          </select>
        </label>
        <label>Base URL<input className="form-input" value={providerForm.base_url} placeholder="https://api.openai.com/v1"
                              onChange={e => setProviderForm({ ...providerForm, base_url: e.target.value })} /></label>
        <label>API-ключ<input className="form-input" type="password" autoComplete="new-password" value={providerForm.api_key}
                              onChange={e => setProviderForm({ ...providerForm, api_key: e.target.value })} /></label>
        <label>Таймаут, с<input className="form-input" type="number" min={1} max={120} value={providerForm.timeout_seconds}
                                onChange={e => setProviderForm({ ...providerForm, timeout_seconds: e.target.value })} /></label>
        <label>Повторы GET<input className="form-input" type="number" min={0} max={3} value={providerForm.max_retries}
                                 onChange={e => setProviderForm({ ...providerForm, max_retries: e.target.value })} /></label>
        <label>Config (JSON, без секретов)<input className="form-input" value={providerForm.config} placeholder='{"tts_model": "tts-1"}'
                                                 onChange={e => setProviderForm({ ...providerForm, config: e.target.value })} /></label>
        <div className="vp-caps">
          {kindCaps.map(cap => (
            <label key={cap} className="ibx-check">
              <input type="checkbox" checked={capabilities.includes(cap)}
                     onChange={e => setCapabilities(prev => e.target.checked ? [...prev, cap] : prev.filter(c => c !== cap))} />
              <span>{cap}</span>
            </label>
          ))}
        </div>
        <button type="button" className="btn-primary" disabled={busy === 'create-provider' || !providerForm.name || !providerForm.base_url}
                onClick={() => void createProvider()}>
          <Plus size={14} /><span>Добавить провайдера</span>
        </button>
      </div>

      <h4 className="vp-subhead">Цели маршрутизации</h4>
      <div className="vp-grid">
        {targets.length === 0 && <div className="admin-empty">Целей пока нет.</div>}
        {targets.map(target => (
          <article key={target.id} className="glass-panel vp-card is-voice">
            <header>
              <div>
                <strong>{target.name}</strong>
                <div className="cl-muted">{MODE_LABELS[target.capability_mode] || target.capability_mode} · {target.provider_name}</div>
              </div>
              <span className={`admin-status-chip ${target.available ? 'is-active' : 'is-revoked'}`}>
                <i className="dot" />{target.available ? 'доступна' : 'недоступна'}
              </span>
            </header>
            {target.voice_id && <div className="cl-info-row"><span>Голос</span><strong>{target.voice_id}</strong></div>}
            {target.agent_id && <div className="cl-info-row"><span>Агент</span><strong>{target.agent_id}</strong></div>}
            <div className="cl-info-row"><span>При сбое</span><strong>{POLICY_LABELS[target.failure_policy] || target.failure_policy}</strong></div>
            <div className="vp-actions">
              <button type="button" className="btn-ghost danger" aria-label={`Удалить цель ${target.name}`}
                      onClick={() => run(`delete-target-${target.id}`, async () => { await voiceApi.deleteTarget(target.id); await load(); })}>
                <Trash2 size={13} />
              </button>
            </div>
          </article>
        ))}
      </div>

      {providers.length > 0 && (
        <div className="vp-form">
          <label>Провайдер
            <select className="form-input" value={targetForm.provider_id}
                    onChange={e => setTargetForm({ ...targetForm, provider_id: e.target.value, capability_mode: '' })}>
              <option value="">—</option>
              {providers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label>Название<input className="form-input" value={targetForm.name} onChange={e => setTargetForm({ ...targetForm, name: e.target.value })} /></label>
          <label>Режим
            <select className="form-input" value={targetForm.capability_mode} onChange={e => setTargetForm({ ...targetForm, capability_mode: e.target.value })}>
              <option value="">—</option>
              {modes.map(mode => <option key={mode} value={mode}>{MODE_LABELS[mode]}</option>)}
            </select>
          </label>
          {targetForm.capability_mode === 'tts' && (
            <label>Голос
              {voices[targetForm.provider_id]?.length ? (
                <select className="form-input" value={targetForm.voice_id} onChange={e => setTargetForm({ ...targetForm, voice_id: e.target.value })}>
                  <option value="">—</option>
                  {voices[targetForm.provider_id].map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
                </select>
              ) : (
                <input className="form-input" value={targetForm.voice_id} placeholder="alloy"
                       onChange={e => setTargetForm({ ...targetForm, voice_id: e.target.value })} />
              )}
            </label>
          )}
          {targetForm.capability_mode === 'voice_agent' && (
            <label>ID удалённого агента<input className="form-input" value={targetForm.agent_id}
                                              onChange={e => setTargetForm({ ...targetForm, agent_id: e.target.value })} /></label>
          )}
          <label>При сбое
            <select className="form-input" value={targetForm.failure_policy} onChange={e => setTargetForm({ ...targetForm, failure_policy: e.target.value })}>
              {Object.entries(POLICY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <button type="button" className="btn-primary"
                  disabled={busy === 'create-target' || !targetForm.provider_id || !targetForm.name || !targetForm.capability_mode}
                  onClick={() => run('create-target', async () => {
                    await voiceApi.createTarget(targetForm);
                    setTargetForm(emptyTarget);
                    await load();
                  })}>
            <Plus size={14} /><span>Добавить цель</span>
          </button>
        </div>
      )}
    </div>
  );
}
