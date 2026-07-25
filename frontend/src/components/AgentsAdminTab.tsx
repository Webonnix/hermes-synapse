import { useEffect, useMemo, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { CheckCircle2, Edit3, Layers, Plus, Send, Server, Trash2, Users, X } from 'lucide-react';
import type { AgentBudgetStatus, AgentModel, AgentTelegramBinding, AgentTier, ProviderBinding } from '../types';
import { styles } from '../styles';

const emptyProviderForm = { name: '', provider_type: 'openai_compatible', api_base: '', api_key: '' };
const emptyTelegramForm = { bot_token: '', allowed_chat_ids: '' };
const emptyTierForm = {
  name: '', description: '', budget_usd_limit_default: '', budget_period_default: 'monthly',
  allow_external_provider: true, allow_messenger: true, is_active: true,
};

type AdminSection = 'agents' | 'providers' | 'tiers';

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

function statusChipClass(status: string | undefined): string {
  if (status === 'active') return 'is-active';
  if (status === 'awaiting_approval') return 'is-pending';
  return 'is-revoked';
}

interface AgentsAdminTabProps {
  agents: AgentModel[];
  models: { id: string; name: string }[];
  fetchAgents: () => void;
  t: (key: string) => string;
}

const emptyAgent: AgentModel = {
  id: '',
  name: '',
  system_prompt: '',
  model: 'qwen3:8b',
  agent_type: 'agent',
  role: 'Specialist',
  status: 'idle',
  is_enabled: true,
  model_provider: 'ollama',
  model_type: 'local',
  skills: '',
  temperature: 0.7,
  model_params: {},
};

export function AgentsAdminTab({ agents, models, fetchAgents, t }: AgentsAdminTabProps) {
  const [section, setSection] = useState<AdminSection>('agents');

  const [draft, setDraft] = useState<AgentModel>(emptyAgent);
  const [paramsText, setParamsText] = useState('{}');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const [providers, setProviders] = useState<ProviderBinding[]>([]);
  const [providersLoaded, setProvidersLoaded] = useState(false);
  const [showProviderForm, setShowProviderForm] = useState(false);
  const [providerForm, setProviderForm] = useState(emptyProviderForm);
  const [providerSaving, setProviderSaving] = useState(false);
  const [providerError, setProviderError] = useState('');
  const [providerNotice, setProviderNotice] = useState('');

  const [budgetStatus, setBudgetStatus] = useState<AgentBudgetStatus | null>(null);

  const [telegramBindings, setTelegramBindings] = useState<AgentTelegramBinding[]>([]);
  const [telegramForm, setTelegramForm] = useState(emptyTelegramForm);
  const [telegramSaving, setTelegramSaving] = useState(false);
  const [telegramError, setTelegramError] = useState('');
  const [telegramNotice, setTelegramNotice] = useState('');

  const [tiers, setTiers] = useState<AgentTier[]>([]);
  const [tiersLoaded, setTiersLoaded] = useState(false);
  const [showTierForm, setShowTierForm] = useState(false);
  const [tierForm, setTierForm] = useState(emptyTierForm);
  const [tierSaving, setTierSaving] = useState(false);
  const [tierError, setTierError] = useState('');

  const selected = useMemo(() => agents.find(agent => agent.id === draft.id), [agents, draft.id]);

  const fetchProviders = () => {
    fetch('/api/providers', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(data => { setProviders(data); setProvidersLoaded(true); })
      .catch(() => { setProviders([]); setProvidersLoaded(true); });
  };

  const fetchTiers = () => {
    fetch('/api/agent-tiers', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(data => { setTiers(data); setTiersLoaded(true); })
      .catch(() => { setTiers([]); setTiersLoaded(true); });
  };

  const fetchBudgetStatus = (agentId: string) => {
    fetch(`/api/agents/${agentId}/usage`, { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : null))
      .then(setBudgetStatus)
      .catch(() => setBudgetStatus(null));
  };

  const fetchTelegramBindings = (agentId: string) => {
    fetch(`/api/agents/${agentId}/telegram`, { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(setTelegramBindings)
      .catch(() => setTelegramBindings([]));
  };

  useEffect(() => {
    fetchProviders();
    fetchTiers();
  }, []);

  // Once loaded, default to showing the add-form when the list is empty so the page isn't blank.
  useEffect(() => {
    if (providersLoaded && providers.length === 0) setShowProviderForm(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providersLoaded]);

  useEffect(() => {
    if (tiersLoaded && tiers.length === 0) setShowTierForm(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tiersLoaded]);

  useEffect(() => {
    setParamsText(JSON.stringify(draft.model_params || {}, null, 2));
    setTelegramForm(emptyTelegramForm);
    setTelegramError('');
    setTelegramNotice('');
    if (draft.id && agents.some(agent => agent.id === draft.id)) {
      fetchBudgetStatus(draft.id);
      fetchTelegramBindings(draft.id);
    } else {
      setBudgetStatus(null);
      setTelegramBindings([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.id, draft.model_params]);

  const startCreate = () => {
    setSection('agents');
    setDraft({ ...emptyAgent, id: `agent_${Date.now().toString().slice(-5)}` });
    setParamsText('{}');
    setError('');
  };

  const startEdit = (agent: AgentModel) => {
    setSection('agents');
    setDraft({
      ...emptyAgent,
      ...agent,
      model_params: agent.model_params || {},
    });
    setParamsText(JSON.stringify(agent.model_params || {}, null, 2));
    setError('');
  };

  const saveAgent = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    let modelParams: Record<string, unknown> = {};
    try {
      modelParams = paramsText.trim() ? JSON.parse(paramsText) : {};
    } catch {
      setSaving(false);
      setError('model_params must be valid JSON.');
      return;
    }

    const cleanId = draft.id.replace(/[^a-zA-Z0-9_-]/g, '').toLowerCase();
    try {
      const token = localStorage.getItem('jarvis_auth_token');
      const response = await fetch('/api/agents', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          ...draft,
          id: cleanId,
          model_params: modelParams,
          status: draft.is_enabled ? (draft.status || 'idle') : 'disabled',
        }),
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      setDraft({ ...emptyAgent });
      setParamsText('{}');
      fetchAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const deleteAgent = async (agent: AgentModel) => {
    if (!window.confirm(`Delete agent "${agent.name}"?`)) return;
    const response = await fetch(`/api/subagents/${agent.id}`, { method: 'DELETE' });
    if (response.ok) {
      fetchAgents();
      if (draft.id === agent.id) setDraft({ ...emptyAgent });
    }
  };

  const toggleAgent = async (agent: AgentModel) => {
    const nextEnabled = !agent.is_enabled;
    const token = localStorage.getItem('jarvis_auth_token');
    await fetch('/api/agents', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        ...agent,
        is_enabled: nextEnabled,
        status: nextEnabled ? 'idle' : 'disabled',
        model_params: agent.model_params || {},
      }),
    });
    fetchAgents();
  };

  const addProvider = async (event: FormEvent) => {
    event.preventDefault();
    setProviderSaving(true);
    setProviderError('');
    setProviderNotice('');
    try {
      const response = await fetch('/api/providers', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(providerForm),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      setProviderNotice(`"${providerForm.name}" awaiting approval — confirm with /approve ${data.task_id} in Telegram.`);
      setProviderForm(emptyProviderForm);
      setShowProviderForm(false);
      fetchProviders();
    } catch (err) {
      setProviderError(err instanceof Error ? err.message : 'Failed to create binding');
    } finally {
      setProviderSaving(false);
    }
  };

  const cancelProviderForm = () => {
    setProviderForm(emptyProviderForm);
    setProviderError('');
    setShowProviderForm(false);
  };

  const deleteProvider = async (binding: ProviderBinding) => {
    if (!window.confirm(`Revoke provider "${binding.name}"? Agents using it will fall back to the local model.`)) return;
    await fetch(`/api/providers/${binding.id}`, { method: 'DELETE', headers: authHeaders() });
    fetchProviders();
  };

  const addTelegramBinding = async () => {
    if (!draft.id) return;
    setTelegramSaving(true);
    setTelegramError('');
    setTelegramNotice('');
    try {
      const allowed_chat_ids = telegramForm.allowed_chat_ids
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);
      const response = await fetch(`/api/agents/${draft.id}/telegram`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ bot_token: telegramForm.bot_token, allowed_chat_ids }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setTelegramForm(emptyTelegramForm);
      setTelegramNotice(`@${data.bot_username} awaiting approval — confirm with /approve ${data.task_id} in Telegram.`);
      fetchTelegramBindings(draft.id);
    } catch (err) {
      setTelegramError(err instanceof Error ? err.message : 'Failed to connect bot');
    } finally {
      setTelegramSaving(false);
    }
  };

  const deleteTelegramBinding = async (binding: AgentTelegramBinding) => {
    if (!window.confirm(`Disconnect bot @${binding.bot_username} from this agent?`)) return;
    await fetch(`/api/agents/telegram/${binding.id}`, { method: 'DELETE', headers: authHeaders() });
    if (draft.id) fetchTelegramBindings(draft.id);
  };

  const addTier = async (event: FormEvent) => {
    event.preventDefault();
    setTierSaving(true);
    setTierError('');
    try {
      const budget = tierForm.budget_usd_limit_default.trim() ? Number(tierForm.budget_usd_limit_default) : null;
      if (budget !== null && (!Number.isFinite(budget) || budget < 0)) {
        throw new Error('Default budget must be a non-negative number, or empty for unlimited.');
      }
      const response = await fetch('/api/agent-tiers', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          name: tierForm.name,
          description: tierForm.description,
          budget_usd_limit_default: budget,
          budget_period_default: tierForm.budget_period_default,
          allow_external_provider: tierForm.allow_external_provider,
          allow_messenger: tierForm.allow_messenger,
          is_active: tierForm.is_active,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setTierForm(emptyTierForm);
      setShowTierForm(false);
      fetchTiers();
    } catch (err) {
      setTierError(err instanceof Error ? err.message : 'Failed to create tier');
    } finally {
      setTierSaving(false);
    }
  };

  const cancelTierForm = () => {
    setTierForm(emptyTierForm);
    setTierError('');
    setShowTierForm(false);
  };

  const toggleTierActive = async (tier: AgentTier) => {
    await fetch(`/api/agent-tiers/${tier.id}`, {
      method: 'PUT',
      headers: authHeaders(),
      body: JSON.stringify({ ...tier, is_active: !tier.is_active }),
    });
    fetchTiers();
  };

  const deleteTier = async (tier: AgentTier) => {
    if (!window.confirm(`Delete tier "${tier.name}"? Agents assigned to it keep their current settings.`)) return;
    await fetch(`/api/agent-tiers/${tier.id}`, { method: 'DELETE', headers: authHeaders() });
    fetchTiers();
  };

  const field = (label: string, child: ReactNode) => (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
      {label}
      {child}
    </label>
  );

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>{t('agentAdminTitle')}</h2>
          <p style={styles.tabSubtitle}>{t('agentAdminSubtitle')}</p>
        </div>
        {section === 'agents' && (
          <button className="btn-primary" onClick={startCreate}>
            <Plus size={16} />
            <span>{t('createAgent')}</span>
          </button>
        )}
      </div>

      <nav className="admin-subnav">
        <button type="button" className={section === 'agents' ? 'is-active' : ''} onClick={() => setSection('agents')}>
          <Users size={15} />
          <span>{t('adminNavAgents')}</span>
          <em className="admin-subnav-count">{agents.length}</em>
        </button>
        <button type="button" className={section === 'providers' ? 'is-active' : ''} onClick={() => setSection('providers')}>
          <Server size={15} />
          <span>{t('adminNavProviders')}</span>
          <em className="admin-subnav-count">{providers.length}</em>
        </button>
        <button type="button" className={section === 'tiers' ? 'is-active' : ''} onClick={() => setSection('tiers')}>
          <Layers size={15} />
          <span>{t('adminNavTiers')}</span>
          <em className="admin-subnav-count">{tiers.length}</em>
        </button>
      </nav>

      {section === 'agents' && (
        <div className="admin-agents-grid">
          <div className="glass-panel" style={{ padding: 16, overflowY: 'auto' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {agents.length === 0 && <div className="admin-empty">{t('adminNoAgents')}</div>}
              {agents.map(agent => (
                <div key={agent.id} className={`admin-agent-card${draft.id === agent.id ? ' is-selected' : ''}`}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ color: '#fff', fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{agent.name}</div>
                      <div style={{ color: 'var(--text-dim)', fontSize: '0.75rem', marginTop: 3 }}>{agent.role || 'Specialist'} · {agent.model_type || 'local'} · {agent.model_provider || 'ollama'}</div>
                      <div style={{ marginTop: 8 }}>
                        <span className={`admin-status-chip ${agent.is_enabled ? 'is-active' : 'is-revoked'}`}>
                          <i className="dot" />{agent.is_enabled ? t('enabled') : t('disabled')} · {agent.status || 'idle'}
                        </span>
                      </div>
                      {(agent.budget_usd_limit != null || agent.tier_id) && (
                        <div style={{ color: 'var(--text-dim)', fontSize: '0.7rem', marginTop: 8 }}>
                          {agent.tier_id && (tiers.find(tr => tr.id === agent.tier_id)?.name || agent.tier_id)}
                          {agent.tier_id && agent.budget_usd_limit != null ? ' · ' : ''}
                          {agent.budget_usd_limit != null ? `budget $${agent.budget_usd_limit}/${agent.budget_period || 'monthly'}` : ''}
                        </div>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                      <button className="icon-btn" title={t('editAgent')} onClick={() => startEdit(agent)}><Edit3 size={14} /></button>
                      <button className="icon-btn" title={agent.is_enabled ? t('disable') : t('enable')} onClick={() => toggleAgent(agent)}><CheckCircle2 size={14} /></button>
                      <button className="icon-btn danger" title={t('deleteAgent')} onClick={() => deleteAgent(agent)}><Trash2 size={14} /></button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <form className="glass-panel" onSubmit={saveAgent} style={{ padding: 20, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <h3 style={{ fontSize: '1.1rem', marginBottom: 4 }}>{selected ? t('editAgent') : t('createAgent')}</h3>
              <p style={{ color: 'var(--text-dim)', fontSize: '0.82rem' }}>ID: {draft.id || 'new-agent'}</p>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              {field('ID', <input className="form-input" required value={draft.id} onChange={e => setDraft({ ...draft, id: e.target.value.replace(/[^a-zA-Z0-9_-]/g, '') })} />)}
              {field('Name', <input className="form-input" required value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} />)}
              {field(t('role'), <input className="form-input" value={draft.role || ''} onChange={e => setDraft({ ...draft, role: e.target.value })} />)}
              {field(t('status'), <input className="form-input" value={draft.status || 'idle'} onChange={e => setDraft({ ...draft, status: e.target.value })} />)}
              {field(t('modelType'), (
                <select className="form-input" value={draft.model_type || 'external'} onChange={e => setDraft({ ...draft, model_type: e.target.value })}>
                  <option value="external">{t('external')}</option>
                  <option value="local">{t('local')}</option>
                </select>
              ))}
              {field(t('provider'), (
                <select className="form-input" value={draft.model_provider || 'ollama'} onChange={e => setDraft({ ...draft, model_provider: e.target.value })}>
                  <option value="ollama">Ollama ({t('local')})</option>
                  {providers.filter(p => p.status === 'active').map(p => (
                    <option key={p.id} value={p.id}>{p.name} ({p.provider_type})</option>
                  ))}
                  {providers.filter(p => p.status !== 'active').map(p => (
                    <option key={p.id} value={p.id} disabled>{p.name} — {p.status}</option>
                  ))}
                </select>
              ))}
              {field('Tier', (
                <select
                  className="form-input"
                  value={draft.tier_id || ''}
                  onChange={e => {
                    const tier = tiers.find(t2 => t2.id === e.target.value);
                    setDraft({
                      ...draft,
                      tier_id: e.target.value || null,
                      ...(tier ? { budget_usd_limit: tier.budget_usd_limit_default, budget_period: tier.budget_period_default } : {}),
                    });
                  }}
                >
                  <option value="">No tier (custom)</option>
                  {tiers.map(tierOpt => (
                    <option key={tierOpt.id} value={tierOpt.id} disabled={!tierOpt.is_active}>
                      {tierOpt.name}{!tierOpt.is_active ? ' — inactive' : ''}
                    </option>
                  ))}
                </select>
              ))}
            </div>

            {field(t('model'), (
              <select className="form-input" value={draft.model} onChange={e => setDraft({ ...draft, model: e.target.value })}>
                {[{ id: draft.model, name: draft.model }, ...models].filter((m, idx, arr) => m.id && arr.findIndex(x => x.id === m.id) === idx).map(model => (
                  <option key={model.id} value={model.id}>{model.name || model.id}</option>
                ))}
              </select>
            ))}

            {field(t('skills'), <input className="form-input" value={draft.skills || ''} onChange={e => setDraft({ ...draft, skills: e.target.value })} placeholder="web_search,python_sandbox" />)}
            {field(t('instructions'), <textarea className="form-input" rows={8} required value={draft.system_prompt} onChange={e => setDraft({ ...draft, system_prompt: e.target.value })} />)}
            {field('model_params JSON', <textarea className="form-input admin-mono-textarea" rows={5} value={paramsText} onChange={e => setParamsText(e.target.value)} />)}

            <label style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: '0.9rem' }}>
              <input type="checkbox" checked={!!draft.is_enabled} onChange={e => setDraft({ ...draft, is_enabled: e.target.checked })} />
              {t('enabled')}
            </label>

            <div className="admin-form-section">
              <h4>Budget (external API spend)</h4>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {field('Limit, USD (empty = unlimited)', (
                  <input
                    className="form-input" type="number" min={0} step="0.01"
                    value={draft.budget_usd_limit ?? ''}
                    onChange={e => setDraft({ ...draft, budget_usd_limit: e.target.value === '' ? null : Number(e.target.value) })}
                    placeholder="unlimited"
                  />
                ))}
                {field('Period', (
                  <select className="form-input" value={draft.budget_period || 'monthly'} onChange={e => setDraft({ ...draft, budget_period: e.target.value })}>
                    <option value="monthly">Monthly (resets each calendar month)</option>
                    <option value="lifetime">Lifetime (never resets)</option>
                  </select>
                ))}
              </div>
              {selected && budgetStatus && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.78rem', color: 'var(--text-dim)', marginBottom: 4 }}>
                    <span>${budgetStatus.used_usd.toFixed(4)} spent{budgetStatus.budget_usd_limit != null ? ` / $${budgetStatus.budget_usd_limit.toFixed(2)}` : ' (no limit)'}</span>
                    {budgetStatus.exceeded && <span style={{ color: 'var(--danger)', fontWeight: 700 }}>LIMIT REACHED</span>}
                  </div>
                  {budgetStatus.budget_usd_limit != null && (
                    <div style={{ height: 6, borderRadius: 3, background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
                      <div style={{
                        height: '100%',
                        width: `${Math.min(100, (budgetStatus.used_usd / Math.max(budgetStatus.budget_usd_limit, 0.0001)) * 100)}%`,
                        background: budgetStatus.exceeded ? 'var(--danger)' : 'var(--success)',
                      }} />
                    </div>
                  )}
                </div>
              )}
            </div>

            {selected && (
              <div className="admin-form-section">
                <h4>Telegram bot</h4>
                <p className="admin-form-section-hint">
                  Give this agent its own bot (from @BotFather) — anyone who messages it reaches only this agent.
                  Needs one Telegram <code>/approve</code> before it goes live.
                </p>
                {telegramBindings.length > 0 ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {telegramBindings.map(binding => (
                      <div key={binding.id} className="admin-item-row">
                        <div className="admin-item-main">
                          <div className="admin-item-name">@{binding.bot_username}</div>
                          <div className="admin-item-meta">
                            {binding.allowed_chat_ids.length > 0 ? `${binding.allowed_chat_ids.length} allowed chat(s)` : 'unrestricted'}
                          </div>
                        </div>
                        <div className="admin-item-actions">
                          <span className={`admin-status-chip ${statusChipClass(binding.status)}`}><i className="dot" />{binding.status}</span>
                          <button type="button" className="icon-btn danger" title="Disconnect" onClick={() => deleteTelegramBinding(binding)}><Trash2 size={14} /></button>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="admin-add-grid">
                    {field('Bot token from @BotFather', (
                      <input className="form-input" type="password" autoComplete="off" value={telegramForm.bot_token}
                        onChange={e => setTelegramForm({ ...telegramForm, bot_token: e.target.value })} placeholder="123456:AA..." />
                    ))}
                    {field('Allowed chat IDs (optional, comma-separated)', (
                      <input className="form-input" value={telegramForm.allowed_chat_ids}
                        onChange={e => setTelegramForm({ ...telegramForm, allowed_chat_ids: e.target.value })} placeholder="unrestricted if empty" />
                    ))}
                    <div style={{ display: 'flex', alignItems: 'flex-end' }}>
                      <button type="button" className="btn-primary" onClick={addTelegramBinding} disabled={telegramSaving || !telegramForm.bot_token}>
                        <Send size={14} />
                        <span>{telegramSaving ? '...' : 'Connect'}</span>
                      </button>
                    </div>
                  </div>
                )}
                {telegramError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 8 }}>{telegramError}</div>}
                {telegramNotice && <div style={{ color: 'var(--success)', fontSize: '0.85rem', marginTop: 8 }}>{telegramNotice}</div>}
              </div>
            )}

            {error && <div style={{ color: 'var(--danger)', fontSize: '0.85rem' }}>{error}</div>}

            <button className="btn-primary" type="submit" disabled={saving || !draft.id || !draft.name || !draft.system_prompt} style={{ alignSelf: 'flex-start' }}>
              <CheckCircle2 size={16} />
              <span>{saving ? 'Saving...' : t('saveAgent')}</span>
            </button>
          </form>
        </div>
      )}

      {section === 'providers' && (
        <div className="glass-panel" style={{ padding: 20 }}>
          <div className="admin-section-head">
            <div>
              <h3>{t('adminNavProviders')}</h3>
              <p>
                Bind agents to an external, OpenAI-compatible API (e.g. DeepSeek). Sensitive
                data is redacted automatically before anything is sent externally. New
                bindings need one Telegram <code>/approve</code> before agents can use them.
              </p>
            </div>
            {!showProviderForm && (
              <button type="button" className="btn-primary" onClick={() => setShowProviderForm(true)}>
                <Plus size={14} />
                <span>Add provider</span>
              </button>
            )}
          </div>

          {providers.length > 0 ? (
            <div className="admin-item-list">
              {providers.map(binding => (
                <div key={binding.id} className="admin-item-row">
                  <div className="admin-item-main">
                    <div className="admin-item-name">{binding.name}</div>
                    <div className="admin-item-meta">{binding.provider_type} · {binding.api_base}</div>
                  </div>
                  <div className="admin-item-actions">
                    <span className={`admin-status-chip ${statusChipClass(binding.status)}`}><i className="dot" />{binding.status}</span>
                    <button type="button" className="icon-btn danger" title="Revoke" onClick={() => deleteProvider(binding)}><Trash2 size={14} /></button>
                  </div>
                </div>
              ))}
            </div>
          ) : (!showProviderForm && <div className="admin-empty">No external providers bound yet.</div>)}

          {showProviderForm && (
            <form onSubmit={addProvider} className="admin-add-card">
              <div className="admin-add-grid">
                {field('Name', <input className="form-input" required value={providerForm.name} onChange={e => setProviderForm({ ...providerForm, name: e.target.value })} placeholder="deepseek" />)}
                {field('Type', (
                  <select className="form-input" value={providerForm.provider_type} onChange={e => setProviderForm({ ...providerForm, provider_type: e.target.value })}>
                    <option value="openai_compatible">OpenAI-compatible</option>
                  </select>
                ))}
                {field('API base', <input className="form-input" required type="url" value={providerForm.api_base} onChange={e => setProviderForm({ ...providerForm, api_base: e.target.value })} placeholder="https://api.deepseek.com/v1" />)}
                {field('API key', <input className="form-input" required type="password" value={providerForm.api_key} onChange={e => setProviderForm({ ...providerForm, api_key: e.target.value })} placeholder="sk-..." autoComplete="off" />)}
              </div>
              <div className="admin-add-actions">
                <button type="button" className="icon-btn" title="Cancel" onClick={cancelProviderForm}><X size={14} /></button>
                <button className="btn-primary" type="submit" disabled={providerSaving || !providerForm.name || !providerForm.api_base || !providerForm.api_key}>
                  <Plus size={14} />
                  <span>{providerSaving ? '...' : 'Add'}</span>
                </button>
              </div>
            </form>
          )}
          {providerError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 12 }}>{providerError}</div>}
          {providerNotice && <div style={{ color: 'var(--success)', fontSize: '0.85rem', marginTop: 12 }}>{providerNotice}</div>}
        </div>
      )}

      {section === 'tiers' && (
        <div className="glass-panel" style={{ padding: 20 }}>
          <div className="admin-section-head">
            <div>
              <h3>{t('adminNavTiers')}</h3>
              <p>
                Named presets of defaults an agent can be assigned to (default budget, whether
                external providers / a Telegram bot are allowed at all). Local config only —
                no approval needed. An inactive tier can't be newly assigned but doesn't change
                agents already on it.
              </p>
            </div>
            {!showTierForm && (
              <button type="button" className="btn-primary" onClick={() => setShowTierForm(true)}>
                <Plus size={14} />
                <span>Add tier</span>
              </button>
            )}
          </div>

          {tiers.length > 0 ? (
            <div className="admin-item-list">
              {tiers.map(tier => (
                <div key={tier.id} className="admin-item-row">
                  <div className="admin-item-main">
                    <div className="admin-item-name">{tier.name}</div>
                    <div className="admin-item-meta">
                      {tier.budget_usd_limit_default != null ? `$${tier.budget_usd_limit_default}/${tier.budget_period_default}` : 'no default limit'}
                      {' · '}{tier.allow_external_provider ? 'external providers ✓' : 'external providers ✗'}
                      {' · '}{tier.allow_messenger ? 'messenger ✓' : 'messenger ✗'}
                    </div>
                  </div>
                  <div className="admin-item-actions">
                    <span
                      className={`admin-status-chip clickable ${tier.is_active ? 'is-active' : 'is-inactive'}`}
                      onClick={() => toggleTierActive(tier)}
                      title="Toggle active"
                    >
                      <i className="dot" />{tier.is_active ? 'active' : 'inactive'}
                    </span>
                    <button type="button" className="icon-btn danger" title="Delete" onClick={() => deleteTier(tier)}><Trash2 size={14} /></button>
                  </div>
                </div>
              ))}
            </div>
          ) : (!showTierForm && <div className="admin-empty">No tiers defined yet.</div>)}

          {showTierForm && (
            <form onSubmit={addTier} className="admin-add-card">
              <div className="admin-add-grid">
                {field('Name', <input className="form-input" required value={tierForm.name} onChange={e => setTierForm({ ...tierForm, name: e.target.value })} placeholder="Basic" />)}
                {field('Default limit, USD', <input className="form-input" type="number" min={0} step="0.01" value={tierForm.budget_usd_limit_default} onChange={e => setTierForm({ ...tierForm, budget_usd_limit_default: e.target.value })} placeholder="unlimited" />)}
                {field('Period', (
                  <select className="form-input" value={tierForm.budget_period_default} onChange={e => setTierForm({ ...tierForm, budget_period_default: e.target.value })}>
                    <option value="monthly">Monthly</option>
                    <option value="lifetime">Lifetime</option>
                  </select>
                ))}
              </div>
              <div className="admin-checkbox-row">
                <label>
                  <input type="checkbox" checked={tierForm.allow_external_provider} onChange={e => setTierForm({ ...tierForm, allow_external_provider: e.target.checked })} />
                  External APIs
                </label>
                <label>
                  <input type="checkbox" checked={tierForm.allow_messenger} onChange={e => setTierForm({ ...tierForm, allow_messenger: e.target.checked })} />
                  Messenger
                </label>
              </div>
              <div className="admin-add-actions">
                <button type="button" className="icon-btn" title="Cancel" onClick={cancelTierForm}><X size={14} /></button>
                <button className="btn-primary" type="submit" disabled={tierSaving || !tierForm.name}>
                  <Plus size={14} />
                  <span>{tierSaving ? '...' : 'Add'}</span>
                </button>
              </div>
            </form>
          )}
          {tierError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 12 }}>{tierError}</div>}
        </div>
      )}
    </div>
  );
}
