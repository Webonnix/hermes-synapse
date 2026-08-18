import { Fragment, useEffect, useMemo, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Activity, CheckCircle2, ChevronRight, CreditCard, Edit3, ExternalLink, FolderKanban, KeyRound, Layers, Plus, RefreshCw, Send, Server, Shuffle, Trash2, Users, X, Zap } from 'lucide-react';
import { BotAccessTab } from './BotAccessTab';
import { BillingTab } from './BillingTab';
import type {
  AgentBudgetStatus, AgentModel, AgentTelegramBinding, AgentTier, Project, ProviderBinding, RouterStats,
  RouterSessionStatus, RouterCombosResponse, RouterConnection, RouterConnectionsResponse, RouterModel, RouterTier, RouterOverview,
} from '../types';
import { styles } from '../styles';

const emptyProviderForm = {
  name: '', provider_type: 'openai_compatible', api_base: '', api_key: '',
  cost_per_1m_input: '', cost_per_1m_output: '',
};
const emptyTelegramForm = { bot_token: '', allowed_chat_ids: '' };
const emptyTierForm = {
  name: '', description: '', budget_usd_limit_default: '', budget_period_default: 'monthly',
  allow_external_provider: true, allow_messenger: true, is_active: true,
};
const emptyProjectForm = { name: '', description: '', is_active: true };
// Fallback-chain tier form (backend/router_tiers.py) — deliberately named
// distinctly from emptyTierForm/tiers above, which power the unrelated
// "Тарифы" (agent budget/permission preset) tab.
const emptyRouterTierForm = {
  label: '', tier_rank: 1, kind: 'binding' as 'binding' | 'local',
  provider_binding_id: '', model_override: '', quota_limit: '', quota_window_hours: 24, is_active: true,
};

type AdminSection = 'agents' | 'router' | 'tiers' | 'projects' | 'access' | 'billing';
/** 'chain' = no pinned provider, escalate through the global router_tiers ladder;
 *  'pinned' = always this one provider binding + model. */
type RoutingMode = 'chain' | 'pinned';

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

function statusChipClass(status: string | undefined): string {
  if (status === 'active') return 'is-active';
  if (status === 'awaiting_approval') return 'is-pending';
  return 'is-revoked';
}

// provider_bindings.status is a raw governance-state enum (backend/provider_governance.py) —
// friendlier label for the one state a human actually needs to act on; other states are
// short enough in English to leave as-is next to the Russian copy around them.
function bindingStatusLabel(status: string | undefined): string {
  if (status === 'awaiting_approval') return 'ждёт /approve';
  return status || 'unknown';
}

// backend/router_usage.py's tier_quota_status().resets_at is the timestamp of
// the window's first successful request plus its length — this formats the
// remaining time client-side rather than a live countdown, refreshed whenever
// the tier list refetches.
function formatResetIn(resetsAt: string | null): string {
  if (!resetsAt) return '—';
  const diffMs = new Date(resetsAt).getTime() - Date.now();
  if (diffMs <= 0) return 'сейчас';
  const totalMin = Math.round(diffMs / 60000);
  const hours = Math.floor(totalMin / 60);
  const minutes = totalMin % 60;
  if (hours <= 0) return `${minutes}м`;
  return minutes ? `${hours}ч ${minutes}м` : `${hours}ч`;
}

// 9Router's own GET /api/providers response uses `provider`/`testStatus`/
// `isActive`, not the `type`/`status` fields this file's own ProviderBinding
// uses — read both shapes so a real connection (kimi, deepseek, ...) renders
// correctly regardless of which field name a given 9Router version sends.
function connectionType(conn: RouterConnection): string {
  return String(conn.provider ?? conn.type ?? '');
}
function connectionStatus(conn: RouterConnection): string {
  if (conn.isActive === false) return 'revoked';
  const raw = conn.testStatus ?? conn.status;
  return raw ? String(raw) : 'active';
}
function connectionAuthLabel(conn: RouterConnection): string {
  if (conn.authType === 'oauth') return 'OAuth';
  if (conn.authType === 'apikey') return 'API-ключ';
  return conn.authType ? String(conn.authType) : '—';
}
// OAuth device-code sessions (see backend/router_session.py's own JWT-exp
// tracking for the same pattern) carry a short TTL — surface it so a soon-
// expiring connection doesn't look identical to a stable API-key one.
function connectionExpiryHint(conn: RouterConnection): string | null {
  if (conn.authType !== 'oauth' || typeof conn.expiresIn !== 'number') return null;
  if (conn.expiresIn <= 0) return 'истёк';
  const minutes = Math.round(conn.expiresIn / 60);
  if (minutes < 60) return `истекает через ${minutes}м`;
  return `истекает через ${Math.round(minutes / 60)}ч`;
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
  allowed_provider_ids: [],
  budget_fallback_to_local: false,
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

  const [routerStats, setRouterStats] = useState<RouterStats | null>(null);
  const [routerLoading, setRouterLoading] = useState(false);

  const [routerSession, setRouterSession] = useState<RouterSessionStatus | null>(null);
  const [routerCombos, setRouterCombos] = useState<RouterCombosResponse | null>(null);
  const [routerConnections, setRouterConnections] = useState<RouterConnectionsResponse | null>(null);
  const [showRouterSessionForm, setShowRouterSessionForm] = useState(false);
  const [routerSessionPassword, setRouterSessionPassword] = useState('');
  const [routerSessionSaving, setRouterSessionSaving] = useState(false);
  const [routerSessionError, setRouterSessionError] = useState('');
  const [routerSessionNotice, setRouterSessionNotice] = useState('');
  const [routerBindSaving, setRouterBindSaving] = useState(false);
  const [routerBindError, setRouterBindError] = useState('');
  const [routerBindNotice, setRouterBindNotice] = useState('');

  const [tiers, setTiers] = useState<AgentTier[]>([]);
  const [tiersLoaded, setTiersLoaded] = useState(false);
  const [showTierForm, setShowTierForm] = useState(false);
  const [tierForm, setTierForm] = useState(emptyTierForm);
  const [tierSaving, setTierSaving] = useState(false);
  const [tierError, setTierError] = useState('');

  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [showProjectForm, setShowProjectForm] = useState(false);
  const [projectForm, setProjectForm] = useState(emptyProjectForm);
  const [projectSaving, setProjectSaving] = useState(false);
  const [projectError, setProjectError] = useState('');

  const [routerOverview, setRouterOverview] = useState<RouterOverview | null>(null);
  const [routerTierList, setRouterTierList] = useState<RouterTier[]>([]);
  const [routerTiersLoaded, setRouterTiersLoaded] = useState(false);
  const [showRouterTierForm, setShowRouterTierForm] = useState(false);
  const [routerTierForm, setRouterTierForm] = useState(emptyRouterTierForm);
  const [routerTierSaving, setRouterTierSaving] = useState(false);
  const [routerTierError, setRouterTierError] = useState('');
  const [routerModels, setRouterModels] = useState<RouterModel[]>([]);
  const [routerModelsLoaded, setRouterModelsLoaded] = useState(false);

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

  const fetchProjects = () => {
    fetch('/api/projects', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(data => { setProjects(data); setProjectsLoaded(true); })
      .catch(() => { setProjects([]); setProjectsLoaded(true); });
  };

  const fetchRouterStats = () => {
    setRouterLoading(true);
    fetch('/api/router/stats', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : null))
      .then(data => setRouterStats(data))
      .catch(() => setRouterStats(null))
      .finally(() => setRouterLoading(false));
  };

  const fetchRouterSession = () => {
    fetch('/api/router/session-status', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : null))
      .then((data: RouterSessionStatus | null) => {
        setRouterSession(data);
        if (data?.configured) {
          fetch('/api/router/combos', { headers: authHeaders() })
            .then(r => (r.ok ? r.json() : null)).then(setRouterCombos).catch(() => setRouterCombos(null));
          fetch('/api/router/connections', { headers: authHeaders() })
            .then(r => (r.ok ? r.json() : null)).then(setRouterConnections).catch(() => setRouterConnections(null));
        }
      })
      .catch(() => setRouterSession(null));
  };

  const proposeRouterSessionPassword = async (event: FormEvent) => {
    event.preventDefault();
    setRouterSessionSaving(true);
    setRouterSessionError('');
    setRouterSessionNotice('');
    try {
      const response = await fetch('/api/router/session-credential', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ password: routerSessionPassword }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setRouterSessionNotice('Ожидает подтверждения — один Telegram /approve, затем данные подтянутся сами.');
      setRouterSessionPassword('');
      setShowRouterSessionForm(false);
      fetchRouterSession();
    } catch (err) {
      setRouterSessionError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setRouterSessionSaving(false);
    }
  };

  const revokeRouterSession = async () => {
    if (!window.confirm('Отозвать доступ бэкенда к сессии 9Router? Комбо и подключённые аккаунты перестанут отображаться здесь.')) return;
    await fetch('/api/router/session-credential', { method: 'DELETE', headers: authHeaders() });
    setRouterSession({ configured: false, pending_task_id: null });
    setRouterCombos(null);
    setRouterConnections(null);
  };

  // Reuses the already-configured ROUTER_API_KEY server-side so connecting
  // an account inside 9Router's own dashboard (visible above via the session
  // proxy) can become an actual fallback-chain tier without retyping that
  // key into the generic "Add provider" form — still one Telegram /approve,
  // same governance as any other binding.
  const bindRouterSelf = async () => {
    setRouterBindSaving(true);
    setRouterBindError('');
    setRouterBindNotice('');
    try {
      const response = await fetch('/api/router/bind-self', { method: 'POST', headers: authHeaders() });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setRouterBindNotice(`Отправлено на подтверждение — один Telegram /approve ${data.task_id}, затем 9Router станет доступен как провайдер для тиров.`);
      fetchProviders();
    } catch (err) {
      setRouterBindError(err instanceof Error ? err.message : 'Failed to create binding');
    } finally {
      setRouterBindSaving(false);
    }
  };

  const fetchRouterOverview = () => {
    fetch('/api/router/overview', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : null))
      .then(setRouterOverview)
      .catch(() => setRouterOverview(null));
  };

  const fetchRouterTierList = () => {
    fetch('/api/router/tiers', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : []))
      .then(data => { setRouterTierList(data); setRouterTiersLoaded(true); })
      .catch(() => { setRouterTierList([]); setRouterTiersLoaded(true); });
  };

  const fetchRouterModels = () => {
    fetch('/api/router/models', { headers: authHeaders() })
      .then(response => (response.ok ? response.json() : null))
      .then((data: { models?: RouterModel[] } | null) => { setRouterModels(data?.models || []); setRouterModelsLoaded(true); })
      .catch(() => { setRouterModels([]); setRouterModelsLoaded(true); });
  };

  const cancelRouterTierForm = () => {
    setRouterTierForm(emptyRouterTierForm);
    setRouterTierError('');
    setShowRouterTierForm(false);
  };

  const addRouterTier = async (event: FormEvent) => {
    event.preventDefault();
    setRouterTierSaving(true);
    setRouterTierError('');
    try {
      const response = await fetch('/api/router/tiers', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          label: routerTierForm.label,
          tier_rank: Number(routerTierForm.tier_rank),
          kind: routerTierForm.kind,
          provider_binding_id: routerTierForm.kind === 'binding' ? routerTierForm.provider_binding_id : null,
          model_override: routerTierForm.model_override,
          quota_limit: routerTierForm.quota_limit ? Number(routerTierForm.quota_limit) : null,
          quota_window_hours: Number(routerTierForm.quota_window_hours) || 24,
          is_active: routerTierForm.is_active,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      cancelRouterTierForm();
      fetchRouterTierList();
      fetchRouterOverview();
    } catch (err) {
      setRouterTierError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setRouterTierSaving(false);
    }
  };

  const deleteRouterTier = async (tier: RouterTier) => {
    if (!window.confirm(`Удалить тир "${tier.label}" из цепочки фолбэка?`)) return;
    await fetch(`/api/router/tiers/${tier.id}`, { method: 'DELETE', headers: authHeaders() });
    fetchRouterTierList();
    fetchRouterOverview();
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
    fetchProjects();
  }, []);

  // Lazy — an unreachable 9Router sidecar shouldn't add a 5s stall to every
  // admin-panel load, only to the one visit that opens this tab.
  useEffect(() => {
    if (section === 'router' && !routerStats && !routerLoading) fetchRouterStats();
    if (section === 'router' && !routerSession) fetchRouterSession();
    if (section === 'router' && !routerOverview) fetchRouterOverview();
    // The tier chain and the model catalog are also what the agent form's
    // routing picker and model dropdown are built from, so they load on the
    // agents section too — both are cheap local calls, unlike the sidecar
    // stats/session probes above.
    if ((section === 'router' || section === 'agents') && !routerTiersLoaded) fetchRouterTierList();
    if ((section === 'router' || section === 'agents') && !routerModelsLoaded) fetchRouterModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);

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
    if (projectsLoaded && projects.length === 0) setShowProjectForm(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectsLoaded]);

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
        body: JSON.stringify({
          ...providerForm,
          cost_per_1m_input: providerForm.cost_per_1m_input === '' ? null : Number(providerForm.cost_per_1m_input),
          cost_per_1m_output: providerForm.cost_per_1m_output === '' ? null : Number(providerForm.cost_per_1m_output),
        }),
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

  const updateProviderPricing = async (binding: ProviderBinding, field: 'cost_per_1m_input' | 'cost_per_1m_output', raw: string) => {
    const value = raw.trim() === '' ? null : Number(raw);
    if (value !== null && !Number.isFinite(value)) return;
    setProviders(prev => prev.map(p => (p.id === binding.id ? { ...p, [field]: value } : p)));
    await fetch(`/api/providers/${binding.id}/pricing`, {
      method: 'PUT',
      headers: authHeaders(),
      body: JSON.stringify({
        cost_per_1m_input: field === 'cost_per_1m_input' ? value : binding.cost_per_1m_input ?? null,
        cost_per_1m_output: field === 'cost_per_1m_output' ? value : binding.cost_per_1m_output ?? null,
      }),
    }).catch(() => fetchProviders());
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

  const addProject = async (event: FormEvent) => {
    event.preventDefault();
    setProjectSaving(true);
    setProjectError('');
    try {
      const response = await fetch('/api/projects', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          name: projectForm.name,
          description: projectForm.description,
          is_active: projectForm.is_active,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setProjectForm(emptyProjectForm);
      setShowProjectForm(false);
      fetchProjects();
    } catch (err) {
      setProjectError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setProjectSaving(false);
    }
  };

  const cancelProjectForm = () => {
    setProjectForm(emptyProjectForm);
    setProjectError('');
    setShowProjectForm(false);
  };

  const toggleProjectActive = async (project: Project) => {
    await fetch(`/api/projects/${project.id}`, {
      method: 'PUT',
      headers: authHeaders(),
      body: JSON.stringify({ ...project, is_active: !project.is_active }),
    });
    fetchProjects();
  };

  const deleteProject = async (project: Project) => {
    if (!window.confirm(`Удалить проект "${project.name}"? У агентов и диалогов, привязанных к нему, привязка просто снимется.`)) return;
    await fetch(`/api/projects/${project.id}`, { method: 'DELETE', headers: authHeaders() });
    fetchProjects();
  };

  // The "Модель" field on the tier form needs a free-text input for a direct
  // provider binding (model-id conventions vary per API), but 9Router's own
  // catalog is namespaced (e.g. 'ds/deepseek-chat') and easy to mistype by
  // hand — swap to a select sourced from the live catalog once the chosen
  // binding actually routes through the 9Router sidecar.
  const selectedTierBinding = providers.find(p => p.id === routerTierForm.provider_binding_id);
  const selectedTierBindingIsRouter = routerTierForm.kind === 'binding' && !!selectedTierBinding?.api_base.includes('9router');
  const routerModelsByOwner = useMemo(() => {
    const grouped: Record<string, RouterModel[]> = {};
    for (const model of routerModels) {
      const owner = model.owned_by || 'other';
      (grouped[owner] ||= []).push(model);
    }
    return grouped;
  }, [routerModels]);

  // A tier assigned to this agent is a ceiling, not a suggestion: if it
  // forbids external providers, the primary provider AND the allowed-
  // providers checklist below are locked to local regardless of what was
  // picked before the tier was applied (backend/agent_provider_access.py
  // enforces the same ceiling again server-side, at save time and call time).
  // Same reasoning as the tier form above, applied to the agent itself: once an
  // agent is pointed at the 9Router binding, its model must come from that
  // catalog ('kimi/kimi-k3', 'ds/deepseek-chat'), not from the local Ollama
  // list — a leftover Ollama model id there makes every external call fail with
  // model_not_found.
  const draftProviderBinding = providers.find(p => p.id === draft.model_provider);
  const draftUsesRouter = !!draftProviderBinding?.api_base.includes('9router');

  // The two routing modes the backend has always had, finally named. 'ollama'
  // in model_provider is not "use Ollama", it means "no pinned provider" — the
  // request starts local and escalates through the global router_tiers chain
  // (agent_provider_access.allowed_binding_ids_for_chain). Anything else pins
  // the agent to that one binding and skips the chain entirely.
  const routingMode: RoutingMode = (draft.model_provider || 'ollama') === 'ollama' ? 'chain' : 'pinned';
  const routerBinding = providers.find(p => p.status === 'active' && p.api_base.includes('9router'));
  const applyRoutingMode = (mode: RoutingMode) => {
    if (mode === routingMode) return;
    if (mode === 'chain') {
      setDraft({ ...draft, model_provider: 'ollama', model_type: 'local' });
      return;
    }
    const target = routerBinding || providers.find(p => p.status === 'active');
    if (!target) return;
    setDraft({ ...draft, model_provider: target.id, model_type: 'external' });
  };
  // The settings panel is built from what the catalog says this model can do,
  // so it changes shape with the model instead of offering fields the provider
  // will reject.
  const selectedModelCapabilities = routerModels.find(m => m.id === draft.model)?.capabilities;
  /** model_params is edited both as JSON (the escape hatch) and through the
   *  controls above, so the textarea stays the single source of truth. */
  const modelParam = (key: string): unknown => {
    try {
      return (JSON.parse(paramsText || '{}') as Record<string, unknown>)[key];
    } catch {
      return undefined;
    }
  };
  const setModelParam = (key: string, value: unknown) => {
    let current: Record<string, unknown> = {};
    try {
      current = JSON.parse(paramsText || '{}') as Record<string, unknown>;
    } catch {
      current = {};
    }
    if (value === null || value === '') delete current[key];
    else current[key] = value;
    setParamsText(JSON.stringify(current, null, 2));
  };
  const chainSummary = !routerTiersLoaded
    ? 'загружается…'
    : [
        'локальная',
        ...[...routerTierList].sort((a, b) => a.tier_rank - b.tier_rank)
          .filter(tier => tier.is_active).map(tier => tier.model_override || tier.label),
      ].join(' → ');
  const draftTier = tiers.find(t => t.id === draft.tier_id);
  const externalProvidersLocked = !!draftTier && !draftTier.allow_external_provider;
  const activeProviders = providers.filter(p => p.status === 'active');
  const providerLabel = (providerId: string) => {
    if (providerId === 'ollama') return 'Ollama (локально)';
    return providers.find(p => p.id === providerId)?.name || providerId;
  };
  const toggleAllowedProvider = (providerId: string) => {
    const current = draft.allowed_provider_ids || [];
    setDraft({
      ...draft,
      allowed_provider_ids: current.includes(providerId)
        ? current.filter(id => id !== providerId)
        : [...current, providerId],
    });
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
        <button type="button" className={section === 'router' ? 'is-active' : ''} onClick={() => setSection('router')}>
          <Shuffle size={15} />
          <span>{t('adminNavRouter')}</span>
          <em className="admin-subnav-count">{providers.length}</em>
        </button>
        <button type="button" className={section === 'tiers' ? 'is-active' : ''} onClick={() => setSection('tiers')}>
          <Layers size={15} />
          <span>{t('adminNavTiers')}</span>
          <em className="admin-subnav-count">{tiers.length}</em>
        </button>
        <button type="button" className={section === 'projects' ? 'is-active' : ''} onClick={() => setSection('projects')}>
          <FolderKanban size={15} />
          <span>{t('adminNavProjects')}</span>
          <em className="admin-subnav-count">{projects.length}</em>
        </button>
        <button type="button" className={section === 'access' ? 'is-active' : ''} onClick={() => setSection('access')}>
          <KeyRound size={15} />
          <span>{t('adminNavAccess')}</span>
        </button>
        <button type="button" className={section === 'billing' ? 'is-active' : ''} onClick={() => setSection('billing')}>
          <CreditCard size={15} />
          <span>{t('adminNavBilling')}</span>
        </button>
      </nav>

      {section === 'access' && <BotAccessTab agents={agents} />}

      {section === 'billing' && <BillingTab />}

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
                <select
                  className="form-input"
                  value={draft.model_provider || 'ollama'}
                  disabled={externalProvidersLocked}
                  onChange={e => setDraft({ ...draft, model_provider: e.target.value })}
                >
                  <option value="ollama">Ollama ({t('local')})</option>
                  {activeProviders.map(p => (
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
                    const tierLocksExternal = !!tier && !tier.allow_external_provider;
                    setDraft({
                      ...draft,
                      tier_id: e.target.value || null,
                      ...(tier ? { budget_usd_limit: tier.budget_usd_limit_default, budget_period: tier.budget_period_default } : {}),
                      // The tier is a ceiling: forbidding external providers
                      // clears whatever was picked before, same as the server
                      // does on save (see save_subagent_api).
                      ...(tierLocksExternal ? { model_provider: 'ollama', allowed_provider_ids: [] } : {}),
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
              {field(t('project'), (
                <select
                  className="form-input"
                  value={draft.project_id || ''}
                  onChange={e => setDraft({ ...draft, project_id: e.target.value || null })}
                >
                  <option value="">{t('unassignedProject')}</option>
                  {projects.map(projectOpt => (
                    <option key={projectOpt.id} value={projectOpt.id} disabled={!projectOpt.is_active}>
                      {projectOpt.name}{!projectOpt.is_active ? ' — inactive' : ''}
                    </option>
                  ))}
                </select>
              ))}
            </div>

            <div className="admin-form-section">
              <h4>Маршрутизация запросов</h4>
              <p className="admin-form-section-hint">
                Как этот агент выбирает модель. Оба режима уже работали в бэкенде, но задавались
                россыпью полей — здесь это один явный выключатель.
              </p>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {(['chain', 'pinned'] as RoutingMode[]).map(mode => {
                  const disabled = mode === 'pinned' && (externalProvidersLocked || activeProviders.length === 0);
                  return (
                    <label key={mode} style={{
                      display: 'flex', alignItems: 'flex-start', gap: 10, padding: '10px 12px', borderRadius: 8,
                      border: `1px solid ${routingMode === mode ? 'rgba(77,222,180,.4)' : 'rgba(255,255,255,.09)'}`,
                      background: routingMode === mode ? 'rgba(46,179,139,.08)' : 'rgba(255,255,255,.02)',
                      cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1,
                    }}>
                      <input
                        type="radio" name="routing_mode" checked={routingMode === mode} disabled={disabled}
                        onChange={() => applyRoutingMode(mode)} style={{ marginTop: 3 }}
                      />
                      <span>
                        <strong style={{ display: 'block', fontSize: '0.85rem' }}>
                          {mode === 'chain' ? 'Иерархия по умолчанию (AI Router)' : 'Своя модель из каталога 9Router'}
                        </strong>
                        <span style={{ display: 'block', fontSize: '0.76rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2 }}>
                          {mode === 'chain'
                            ? 'Начинает с локальной модели и поднимается по общей цепочке тиров, когда задача её перерастает. Отметки ниже сужают, до каких провайдеров разрешено подниматься именно этому агенту.'
                            : 'Все запросы агента идут в одну выбранную модель, минуя иерархию. Нужен, когда важна конкретная модель, а не «подешевле, если хватит».'}
                        </span>
                        {mode === 'chain' && (
                          <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-muted)', marginTop: 6 }}>
                            Цепочка сейчас: {chainSummary}
                          </span>
                        )}
                        {mode === 'pinned' && disabled && (
                          <span style={{ display: 'block', fontSize: '0.74rem', color: '#ffb454', marginTop: 6 }}>
                            {externalProvidersLocked
                              ? 'Тир агента запрещает внешние провайдеры.'
                              : 'Нет активных провайдеров — добавьте их во вкладке «AI Router».'}
                          </span>
                        )}
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="admin-form-section">
              <h4>Разрешённые провайдеры</h4>
              <p className="admin-form-section-hint">
                {externalProvidersLocked
                  ? 'Тир этого агента запрещает внешние провайдеры — только локальная модель.'
                  : 'Кроме основного провайдера выше, агент может при необходимости переключаться на любой ' +
                    'отмеченный здесь (например, если основной отозван или исчерпал квоту). Ничего не отмечено ' +
                    '— ограничений нет: используется основной провайдер как раньше, а для локального агента ' +
                    'работает полная общая цепочка фолбэка из вкладки "Router".'}
              </p>
              {!externalProvidersLocked && activeProviders.length === 0 && (
                <p className="admin-form-section-hint">Нет активных провайдеров — добавьте их во вкладке "Router".</p>
              )}
              {!externalProvidersLocked && activeProviders.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {activeProviders.map(p => (
                    <label key={p.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                      <input
                        type="checkbox"
                        checked={(draft.allowed_provider_ids || []).includes(p.id)}
                        onChange={() => toggleAllowedProvider(p.id)}
                      />
                      {p.name} ({p.provider_type}){p.id === draft.model_provider ? ' — основной' : ''}
                    </label>
                  ))}
                </div>
              )}
            </div>

            {field(t('model'), (
              <select className="form-input" value={draft.model} onChange={e => setDraft({ ...draft, model: e.target.value })}>
                {draftUsesRouter ? (
                  <>
                    {!routerModelsLoaded && <option value={draft.model}>Загрузка каталога…</option>}
                    {Object.entries(routerModelsByOwner).map(([owner, ownerModels]) => (
                      <optgroup key={owner} label={owner}>
                        {ownerModels.map(m => <option key={m.id} value={m.id}>{m.id}</option>)}
                      </optgroup>
                    ))}
                    {/* Keeps a model the catalog no longer offers visible instead of
                        silently snapping the agent onto a different one on save. */}
                    {draft.model && routerModelsLoaded && !routerModels.some(m => m.id === draft.model) && (
                      <optgroup label="настроено сейчас (нет в каталоге)">
                        <option value={draft.model}>{draft.model}</option>
                      </optgroup>
                    )}
                  </>
                ) : (
                  [{ id: draft.model, name: draft.model }, ...models].filter((m, idx, arr) => m.id && arr.findIndex(x => x.id === m.id) === idx).map(model => (
                    <option key={model.id} value={model.id}>{model.name || model.id}</option>
                  ))
                )}
              </select>
            ))}
            {draftUsesRouter && routerModelsLoaded && !routerModels.some(m => m.id === draft.model) && (
              <span style={{ color: '#ffb454', fontSize: '0.75rem', marginTop: -4 }}>
                Провайдер не отдаёт такую модель — запросы к нему будут падать с model_not_found. Выберите из списка.
              </span>
            )}

            <div className="admin-form-section">
              <h4>Настройки модели</h4>
              <p className="admin-form-section-hint">
                {selectedModelCapabilities
                  ? 'Набор параметров зависит от выбранной модели — берётся из её capabilities в каталоге 9Router.'
                  : 'Каталог не отдаёт характеристик для этой модели, поэтому показаны только универсальные параметры.'}
              </p>
              {selectedModelCapabilities && (
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
                  {selectedModelCapabilities.contextWindow ? (
                    <span className="admin-status-chip">контекст {Math.round(selectedModelCapabilities.contextWindow / 1000)}k</span>
                  ) : null}
                  {selectedModelCapabilities.maxOutput ? (
                    <span className="admin-status-chip">ответ до {Math.round(selectedModelCapabilities.maxOutput / 1000)}k</span>
                  ) : null}
                  {selectedModelCapabilities.tools && <span className="admin-status-chip">инструменты</span>}
                  {selectedModelCapabilities.vision && <span className="admin-status-chip">изображения</span>}
                  {selectedModelCapabilities.reasoning && <span className="admin-status-chip">рассуждения</span>}
                </div>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {field(`Температура: ${Number(draft.temperature ?? 0.7).toFixed(2)}`, (
                  <input
                    className="form-input" type="range" min={0} max={2} step={0.05}
                    value={Number(draft.temperature ?? 0.7)}
                    onChange={e => setDraft({ ...draft, temperature: Number(e.target.value) })}
                  />
                ))}
                {field(
                  selectedModelCapabilities?.maxOutput
                    ? `Максимум токенов ответа (до ${selectedModelCapabilities.maxOutput})`
                    : 'Максимум токенов ответа',
                  (
                    <input
                      className="form-input" type="number" min={1}
                      max={selectedModelCapabilities?.maxOutput || undefined}
                      value={String(modelParam('max_tokens') ?? '')}
                      placeholder="по умолчанию"
                      onChange={e => setModelParam('max_tokens', e.target.value ? Number(e.target.value) : null)}
                    />
                  ),
                )}
              </div>
              {/* Only offered where the model says it can be turned off: sending
                  the switch to a model that always reasons is a wasted field at
                  best and a 400 at worst. */}
              {selectedModelCapabilities?.reasoning && selectedModelCapabilities?.thinkingCanDisable && (
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: 10 }}>
                  <input
                    type="checkbox"
                    checked={modelParam('enable_thinking') !== false}
                    onChange={e => setModelParam('enable_thinking', e.target.checked ? null : false)}
                  />
                  Рассуждать перед ответом (медленнее и дороже, но точнее)
                </label>
              )}
              <p className="admin-form-section-hint" style={{ marginTop: 10 }}>
                Если модель откажется от какого-то параметра (kimi, например, принимает только температуру 1),
                бэкенд уберёт именно его и повторит запрос — ответ вы всё равно получите.
              </p>
              <details style={{ marginTop: 8 }}>
                <summary style={{ cursor: 'pointer', color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
                  Расширенные параметры (JSON)
                </summary>
                <textarea className="form-input admin-mono-textarea" rows={5} value={paramsText}
                  onChange={e => setParamsText(e.target.value)} style={{ marginTop: 8 }} />
                <span style={{ fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)' }}>
                  Уходят в тело запроса: top_p, top_k, presence_penalty, frequency_penalty, seed, stop,
                  reasoning_effort, thinking, reasoning. Остальные ключи игнорируются.
                </span>
              </details>
            </div>

            {field(t('skills'), <input className="form-input" value={draft.skills || ''} onChange={e => setDraft({ ...draft, skills: e.target.value })} placeholder="web_search,python_sandbox" />)}
            {field(t('instructions'), <textarea className="form-input" rows={8} required value={draft.system_prompt} onChange={e => setDraft({ ...draft, system_prompt: e.target.value })} />)}

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
              <label style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)', fontSize: '0.85rem', marginTop: 10 }}>
                <input
                  type="checkbox"
                  checked={!!draft.budget_fallback_to_local}
                  onChange={e => setDraft({ ...draft, budget_fallback_to_local: e.target.checked })}
                />
                При исчерпании бюджета переключаться на локальную модель вместо отказа
              </label>
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
                  {budgetStatus.by_provider.length > 0 && (
                    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <span style={{ fontSize: '0.72rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.03em' }}>По провайдерам</span>
                      {budgetStatus.by_provider.map(row => (
                        <div key={row.provider_id} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                          <span>{providerLabel(row.provider_id)} · {row.calls} вызов(ов)</span>
                          <span>${row.used_usd.toFixed(4)}</span>
                        </div>
                      ))}
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

      {section === 'router' && (
        <div className="glass-panel" style={{ padding: 20 }}>
          <div className="admin-section-head">
            <div>
              <h3>
                {t('adminNavRouter')}{' '}
                <span className={`admin-status-chip ${routerTiersLoaded ? 'is-active' : 'is-revoked'}`} style={{ marginLeft: 8, verticalAlign: 'middle' }}>
                  <i className="dot" />{routerTierList.length > 0 || routerStats?.reachable ? 'connected' : 'local only'}
                </span>
              </h3>
              <p>
                3-уровневый фолбэк между вашими провайдерами и авто-сжатие вывода инструментов.
                Локальная модель сама решает по каждому запросу — ответить самой или передать наверх
                по цепочке; чувствительные данные редактируются перед отправкой наружу, постоянная
                память шифруется на диске. Работает поверх обычных биндингов ниже.
              </p>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button" className="icon-btn" title="Обновить"
                onClick={() => { fetchRouterOverview(); fetchRouterTierList(); fetchRouterStats(); fetchProviders(); fetchRouterModels(); }}
                disabled={routerLoading}
              >
                <RefreshCw size={14} />
              </button>
              {routerStats?.dashboard_url && (
                <a className="btn-ghost" href={routerStats.dashboard_url} target="_blank" rel="noreferrer" style={{ textDecoration: 'none' }}>
                  <ExternalLink size={14} />
                  <span>Открыть 9Router</span>
                </a>
              )}
            </div>
          </div>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 20 }}>
            <div className="glass-panel admin-stat-card is-info">
              <div className="admin-stat-icon"><Shuffle size={16} /></div>
              <div className="admin-stat-body">
                <span className="admin-stat-label">Активный комбо</span>
                <strong className="admin-stat-value" style={{ fontSize: '1rem' }}>{routerOverview?.active_combo_label ?? 'Локальная модель'}</strong>
                <span className="admin-stat-hint">{routerOverview?.active_combo_tier_count ?? 1} тир{routerOverview && routerOverview.active_combo_tier_count !== 1 ? 'а' : ''} настроено</span>
              </div>
            </div>
            <div className="glass-panel admin-stat-card is-success">
              <div className="admin-stat-icon"><Zap size={16} /></div>
              <div className="admin-stat-body">
                <span className="admin-stat-label">Экономия токенов</span>
                <strong className="admin-stat-value">{routerOverview?.token_savings_pct != null ? `-${routerOverview.token_savings_pct}%` : '—'}</strong>
                <span className="admin-stat-hint">за счёт локальной модели и дешёвых тиров, 24ч</span>
              </div>
            </div>
            <div className="glass-panel admin-stat-card">
              <div className="admin-stat-icon"><Activity size={16} /></div>
              <div className="admin-stat-body">
                <span className="admin-stat-label">Запросов, 24ч</span>
                <strong className="admin-stat-value">{routerOverview?.requests_24h ?? '—'}</strong>
                <span className="admin-stat-hint">через все агенты</span>
              </div>
            </div>
            <div className="glass-panel admin-stat-card">
              <div className="admin-stat-icon"><Server size={16} /></div>
              <div className="admin-stat-body">
                <span className="admin-stat-label">Провайдеры</span>
                <strong className="admin-stat-value">{routerOverview ? `${routerOverview.providers_healthy}/${routerOverview.providers_total}` : '—'}</strong>
                <span className="admin-stat-hint">в норме</span>
              </div>
            </div>
          </div>

          <div className="admin-section-head">
            <div>
              <h3 style={{ fontSize: '1rem' }}>Фолбэк-цепочка</h3>
              <p>Локальная модель — всегда первая и бесплатная ступень. Дальше — тиры, которые вы настроите ниже, в порядке ранга; при отказе или исчерпании квоты запрос уходит на следующий, а если откажут все — снова на локальную модель.</p>
            </div>
            {!showRouterTierForm && (
              <button type="button" className="btn-primary" onClick={() => setShowRouterTierForm(true)}>
                <Plus size={14} />
                <span>Добавить тир</span>
              </button>
            )}
          </div>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 18 }}>
            <div className="glass-panel admin-tier-card">
              <div className="admin-tier-eyebrow">TIER 0 · ЛОКАЛЬНАЯ</div>
              <span className="admin-status-chip is-active" style={{ marginBottom: 8 }}><i className="dot" />active</span>
              <div className="admin-tier-model">Локальная модель</div>
              <div className="admin-item-meta">всегда доступна, бесплатно</div>
            </div>
            {[...routerTierList].sort((a, b) => a.tier_rank - b.tier_rank).map(tier => {
              const status = tier.quota.exhausted ? 'exhausted' : tier.quota.used > 0 ? 'active' : 'standby';
              const binding = providers.find(p => p.id === tier.provider_binding_id);
              return (
                <Fragment key={tier.id}>
                  <ChevronRight size={16} className="admin-tier-arrow" />
                  <div className="glass-panel admin-tier-card">
                    <div className="admin-tier-eyebrow">
                      TIER {tier.tier_rank} · {tier.kind === 'local' ? 'ЛОКАЛЬНАЯ' : 'ВНЕШНЯЯ'}
                      <button type="button" className="icon-btn danger" title="Удалить тир" onClick={() => deleteRouterTier(tier)} style={{ float: 'right', padding: 2 }}>
                        <Trash2 size={12} />
                      </button>
                    </div>
                    <span className={`admin-status-chip ${status === 'exhausted' ? 'is-revoked' : status === 'active' ? 'is-active' : 'is-pending'}`} style={{ marginBottom: 8 }}>
                      <i className="dot" />{status}
                    </span>
                    <div className="admin-tier-model">{tier.model_override || binding?.name || tier.label}</div>
                    <div className="admin-item-meta" style={{ fontFamily: 'var(--font-mono)' }}>
                      {tier.label}{binding ? ` · ${binding.name}` : ''}
                    </div>
                    {tier.quota_limit ? (
                      <>
                        <div className="admin-quota-bar">
                          <div className={`admin-quota-fill is-${status}`} style={{ width: `${tier.quota.pct ?? 0}%` }} />
                        </div>
                        <div className="admin-item-meta" style={{ display: 'flex', justifyContent: 'space-between' }}>
                          <span>{tier.quota.pct ?? 0}% квоты</span>
                          <span>сброс {formatResetIn(tier.quota.resets_at)}</span>
                        </div>
                      </>
                    ) : tier.quota.used > 0 ? (
                      <div className="admin-item-meta">{tier.quota.used} запросов, 24ч · без лимита</div>
                    ) : (
                      <div className="admin-item-meta">не использован —</div>
                    )}
                  </div>
                </Fragment>
              );
            })}
          </div>

          {showRouterTierForm && (
            <form onSubmit={addRouterTier} className="admin-add-card">
              <div className="admin-add-grid">
                {field('Название', <input className="form-input" required value={routerTierForm.label} onChange={e => setRouterTierForm({ ...routerTierForm, label: e.target.value })} placeholder="Подписка" />)}
                {field('Ранг', <input className="form-input" required type="number" min={1} value={routerTierForm.tier_rank} onChange={e => setRouterTierForm({ ...routerTierForm, tier_rank: Number(e.target.value) })} />)}
                {field('Тип', (
                  <select className="form-input" value={routerTierForm.kind} onChange={e => setRouterTierForm({ ...routerTierForm, kind: e.target.value as 'binding' | 'local' })}>
                    <option value="binding">Внешний провайдер</option>
                    <option value="local">Локальная модель</option>
                  </select>
                ))}
                {routerTierForm.kind === 'binding' && field('Провайдер', (
                  <select
                    className="form-input" required value={routerTierForm.provider_binding_id}
                    onChange={e => {
                      const nextId = e.target.value;
                      const prevIsRouter = !!selectedTierBinding?.api_base.includes('9router');
                      const nextIsRouter = !!providers.find(p => p.id === nextId)?.api_base.includes('9router');
                      // Only clear a typed/picked model when crossing between the
                      // free-text world (a direct binding) and the catalog-select
                      // world (9Router) — its value format isn't compatible across
                      // that boundary. Picking a provider for the first time, or
                      // switching between two direct bindings, leaves it alone so
                      // typing the model before the provider (a normal order) still
                      // works.
                      setRouterTierForm({
                        ...routerTierForm, provider_binding_id: nextId,
                        model_override: prevIsRouter === nextIsRouter ? routerTierForm.model_override : '',
                      });
                    }}
                  >
                    <option value="">— выберите —</option>
                    {providers.filter(p => p.status === 'active').map(p => (
                      <option key={p.id} value={p.id}>{p.name} ({p.api_base})</option>
                    ))}
                  </select>
                ))}
                {field('Модель', selectedTierBindingIsRouter ? (
                  <select
                    className="form-input" required value={routerTierForm.model_override}
                    onChange={e => setRouterTierForm({ ...routerTierForm, model_override: e.target.value })}
                  >
                    <option value="">{routerModelsLoaded ? '— выберите модель —' : 'Загрузка каталога…'}</option>
                    {Object.entries(routerModelsByOwner).map(([owner, models]) => (
                      <optgroup key={owner} label={owner}>
                        {models.map(m => <option key={m.id} value={m.id}>{m.id}</option>)}
                      </optgroup>
                    ))}
                  </select>
                ) : (
                  <input className="form-input" required value={routerTierForm.model_override} onChange={e => setRouterTierForm({ ...routerTierForm, model_override: e.target.value })} placeholder="deepseek-chat" />
                ))}
                {field('Квота (запросов/окно, пусто = без лимита)', <input className="form-input" type="number" min={1} value={routerTierForm.quota_limit} onChange={e => setRouterTierForm({ ...routerTierForm, quota_limit: e.target.value })} placeholder="200" />)}
                {field('Окно квоты, часов', <input className="form-input" type="number" min={0.5} step={0.5} value={routerTierForm.quota_window_hours} onChange={e => setRouterTierForm({ ...routerTierForm, quota_window_hours: Number(e.target.value) })} />)}
              </div>
              <div className="admin-add-actions">
                <button type="button" className="icon-btn" title="Cancel" onClick={cancelRouterTierForm}><X size={14} /></button>
                <button className="btn-primary" type="submit" disabled={routerTierSaving || !routerTierForm.label || (routerTierForm.kind === 'binding' && !routerTierForm.provider_binding_id) || !routerTierForm.model_override}>
                  <Plus size={14} />
                  <span>{routerTierSaving ? '...' : 'Добавить'}</span>
                </button>
              </div>
            </form>
          )}
          {routerTierError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 12 }}>{routerTierError}</div>}

          <div style={{ borderTop: '1px solid rgba(255,255,255,.08)', margin: '22px 0 18px' }} />

          <div className="admin-section-head">
            <div>
              <h3 style={{ fontSize: '1rem' }}>Подключённые провайдеры</h3>
              <p>
                Обычные OpenAI-compatible биндинги (например DeepSeek), плюс опционально сам 9Router
                как один из провайдеров (<code>http://9router:20128/v1</code>). Чувствительные данные
                редактируются перед отправкой наружу, новые биндинги требуют одного Telegram{' '}
                <code>/approve</code>.
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
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr><th>Имя</th><th>API Base</th><th>Тип</th><th>Статус</th><th>Цена вход/1M</th><th>Цена выход/1M</th><th /></tr>
                </thead>
                <tbody>
                  {providers.map(binding => (
                    <tr key={binding.id}>
                      <td>
                        {binding.name}
                        <div className="admin-item-meta">
                          {binding.api_base.includes('9router') ? 'через governance-биндинг' : 'прямой биндинг'}
                        </div>
                      </td>
                      <td style={{ fontFamily: 'var(--font-mono)', fontSize: '0.78rem' }}>{binding.api_base}</td>
                      <td>{binding.provider_type}</td>
                      <td><span className={`admin-status-chip ${statusChipClass(binding.status)}`}><i className="dot" />{bindingStatusLabel(binding.status)}</span></td>
                      <td>
                        <input
                          className="form-input" type="number" min={0} step="0.001" style={{ width: 90 }}
                          defaultValue={binding.cost_per_1m_input ?? ''} placeholder="авто"
                          onBlur={e => updateProviderPricing(binding, 'cost_per_1m_input', e.target.value)}
                        />
                      </td>
                      <td>
                        <input
                          className="form-input" type="number" min={0} step="0.001" style={{ width: 90 }}
                          defaultValue={binding.cost_per_1m_output ?? ''} placeholder="авто"
                          onBlur={e => updateProviderPricing(binding, 'cost_per_1m_output', e.target.value)}
                        />
                      </td>
                      <td><button type="button" className="icon-btn danger" title="Revoke" onClick={() => deleteProvider(binding)}><Trash2 size={14} /></button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
                {field('Цена вход, $/1M токенов (опц.)', <input className="form-input" type="number" min={0} step="0.001" value={providerForm.cost_per_1m_input} onChange={e => setProviderForm({ ...providerForm, cost_per_1m_input: e.target.value })} placeholder="по умолчанию — по названию модели" />)}
                {field('Цена выход, $/1M токенов (опц.)', <input className="form-input" type="number" min={0} step="0.001" value={providerForm.cost_per_1m_output} onChange={e => setProviderForm({ ...providerForm, cost_per_1m_output: e.target.value })} placeholder="по умолчанию — по названию модели" />)}
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

          <div style={{ borderTop: '1px solid rgba(255,255,255,.08)', margin: '22px 0 18px' }} />

          <div className="admin-section-head">
            <div>
              <h3 style={{ fontSize: '1rem' }}>Аккаунты 9Router</h3>
              <p>
                Учётные записи, подключённые напрямую в дашборде самого 9Router (Providers) — например
                после логина через device code или API-ключ. Отдельно от таблицы выше: сами по себе они
                ещё не доступны фолбэк-цепочке, пока 9Router не добавлен как обычный провайдер ниже.
              </p>
            </div>
          </div>

          {!routerSession?.configured ? (
            <div className="admin-empty">
              Сессия дашборда 9Router не настроена — разверните диагностический раздел ниже, чтобы
              подключить её и увидеть аккаунты, добавленные там.
            </div>
          ) : routerConnections?.connections && routerConnections.connections.length > 0 ? (
            <>
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead>
                    <tr><th>Имя</th><th>Провайдер</th><th>Авторизация</th><th>Статус</th></tr>
                  </thead>
                  <tbody>
                    {routerConnections.connections.map((conn, idx) => {
                      const expiry = connectionExpiryHint(conn);
                      return (
                        <tr key={conn.id || conn.name || idx}>
                          <td>
                            {conn.name || `account-${idx + 1}`}
                            {conn.lastError && <div className="admin-item-meta" style={{ color: 'var(--danger)' }}>{conn.lastError}</div>}
                          </td>
                          <td style={{ fontFamily: 'var(--font-mono)', fontSize: '0.78rem' }}>{connectionType(conn) || '—'}</td>
                          <td>
                            {connectionAuthLabel(conn)}
                            {expiry && <div className="admin-item-meta">{expiry}</div>}
                          </td>
                          <td><span className={`admin-status-chip ${statusChipClass(connectionStatus(conn))}`}><i className="dot" />{connectionStatus(conn)}</span></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {providers.some(p => p.api_base.includes('9router')) ? (
                <div className="admin-item-meta" style={{ marginTop: 10 }}>9Router уже добавлен как провайдер выше — выберите нужную модель из его каталога при создании тира.</div>
              ) : (
                <div style={{ marginTop: 12 }}>
                  <button type="button" className="btn-primary" onClick={bindRouterSelf} disabled={routerBindSaving}>
                    <Plus size={14} />
                    <span>{routerBindSaving ? '...' : 'Быстро подключить 9Router как провайдера'}</span>
                  </button>
                  <div className="admin-item-meta" style={{ marginTop: 6 }}>
                    Использует уже сохранённый ROUTER_API_KEY — один Telegram /approve, и модели этих
                    аккаунтов станут выбираемы при добавлении тира выше.
                  </div>
                </div>
              )}
              {routerBindError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 10 }}>{routerBindError}</div>}
              {routerBindNotice && <div style={{ color: 'var(--success)', fontSize: '0.85rem', marginTop: 10 }}>{routerBindNotice}</div>}
            </>
          ) : (
            <div className="admin-empty">
              {routerConnections?.error || 'Аккаунты ещё не подключены — сделайте это в 9Router (Dashboard → Providers).'}
            </div>
          )}

          <details style={{ marginTop: 22 }}>
            <summary className="control-eyebrow" style={{ cursor: 'pointer' }}>Необработанные данные из дашборда 9Router (диагностика)</summary>
            <div style={{ marginTop: 14 }}>
              {routerLoading && !routerStats && <div className="admin-empty">Загрузка…</div>}

              {!routerLoading && !routerStats && (
                <div className="admin-empty">
                  Не удалось загрузить статус AI Router.{' '}
                  <button
                    type="button" onClick={fetchRouterStats}
                    style={{ background: 'none', border: 'none', padding: 0, color: 'var(--accent-cyan)', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}
                  >
                    Повторить
                  </button>
                </div>
              )}

              {!routerLoading && routerStats?.available === false && (
                <div className="admin-empty">
                  9Router не настроен. Укажите ROUTER_API_KEY в Настройках → API Keys.
                  {routerStats.error && <div style={{ marginTop: 8, color: 'var(--text-dim)', fontSize: '0.78rem' }}>{routerStats.error}</div>}
                </div>
              )}

              {!routerLoading && routerStats?.available && !routerStats.reachable && (
                <div className="admin-empty">
                  Ключ настроен, но контейнер 9router не отвечает
                  (<code>docker compose up -d 9router</code>).
                </div>
              )}

              {!routerLoading && routerStats?.reachable && (
                <>
                  <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
                    <div className="glass-panel admin-stat-card is-info">
                      <div className="admin-stat-icon"><Shuffle size={16} /></div>
                      <div className="admin-stat-body">
                        <span className="admin-stat-label">Моделей в каталоге</span>
                        <strong className="admin-stat-value">{routerStats.model_count ?? '—'}</strong>
                      </div>
                    </div>
                    <div className="glass-panel admin-stat-card is-success">
                      <div className="admin-stat-icon"><Layers size={16} /></div>
                      <div className="admin-stat-body">
                        <span className="admin-stat-label">Провайдеров подключено</span>
                        <strong className="admin-stat-value">{routerStats.provider_count ?? '—'}</strong>
                      </div>
                    </div>
                  </div>
                  {routerStats.sample_models.length > 0 && (
                    <div className="admin-item-list" style={{ marginBottom: 14 }}>
                      {routerStats.sample_models.map(id => (
                        <div key={id} className="admin-item-row">
                          <div className="admin-item-name" style={{ fontFamily: 'var(--font-mono)', fontSize: '0.78rem' }}>{id}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}

              <div className="admin-section-head">
                <div><h3 style={{ fontSize: '0.9rem' }}>Сессия дашборда 9Router</h3></div>
                {routerSession?.configured && (
                  <button type="button" className="icon-btn danger" title="Отозвать доступ" onClick={revokeRouterSession}><Trash2 size={14} /></button>
                )}
              </div>

              {!routerSession?.configured && !routerSession?.pending_task_id && !showRouterSessionForm && (
                <div className="admin-empty">
                  Сессия дашборда не настроена.{' '}
                  <button type="button" onClick={() => setShowRouterSessionForm(true)} style={{ background: 'none', border: 'none', padding: 0, color: 'var(--accent-cyan)', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}>
                    Подключить
                  </button>
                </div>
              )}

              {routerSession?.pending_task_id && !routerSession.configured && (
                <div className="admin-empty">Пароль отправлен на подтверждение — один Telegram <code>/approve</code>.</div>
              )}

              {showRouterSessionForm && (
                <form onSubmit={proposeRouterSessionPassword} className="admin-add-card">
                  <div className="admin-add-grid">
                    {field('Пароль дашборда 9Router', (
                      <input className="form-input" required type="password" autoComplete="off" value={routerSessionPassword} onChange={e => setRouterSessionPassword(e.target.value)} placeholder="пароль из Dashboard 9Router" />
                    ))}
                  </div>
                  <div className="admin-add-actions">
                    <button type="button" className="icon-btn" title="Cancel" onClick={() => { setShowRouterSessionForm(false); setRouterSessionPassword(''); }}><X size={14} /></button>
                    <button className="btn-primary" type="submit" disabled={routerSessionSaving || !routerSessionPassword}>
                      <Plus size={14} />
                      <span>{routerSessionSaving ? '...' : 'Подключить'}</span>
                    </button>
                  </div>
                </form>
              )}
              {routerSessionError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 12 }}>{routerSessionError}</div>}
              {routerSessionNotice && <div style={{ color: 'var(--success)', fontSize: '0.85rem', marginTop: 12 }}>{routerSessionNotice}</div>}

              {routerSession?.configured && (
                <>
                  <p className="control-eyebrow" style={{ marginTop: 16, marginBottom: 10 }}>Комбо</p>
                  {routerCombos?.combos && routerCombos.combos.length > 0 ? (
                    <div className="admin-item-list" style={{ marginBottom: 18 }}>
                      {routerCombos.combos.map((combo, idx) => {
                        const tierLabels = (combo.tiers || combo.models || [])
                          .map(tier => (typeof tier === 'string' ? tier : tier?.model || tier?.provider || ''))
                          .filter(Boolean);
                        return (
                          <div key={combo.id || combo.name || idx} className="admin-item-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
                            <div className="admin-item-name">{combo.name || `combo-${idx + 1}`}</div>
                            {tierLabels.length > 0 && (
                              <div className="admin-item-meta" style={{ fontFamily: 'var(--font-mono)' }}>{tierLabels.join(' → ')}</div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="admin-empty">
                      {routerCombos?.error || 'Комбо ещё не создано — соберите его в самом 9Router (Dashboard → Combos).'}
                    </div>
                  )}
                  <div className="admin-item-meta" style={{ marginTop: 10 }}>
                    Подключённые аккаунты теперь показаны в «Аккаунты 9Router» выше, вне диагностики.
                  </div>
                </>
              )}
            </div>
          </details>
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

      {section === 'projects' && (
        <div className="glass-panel" style={{ padding: 20 }}>
          <div className="admin-section-head">
            <div>
              <h3>{t('adminNavProjects')}</h3>
              <p>
                Именованные проекты, к которым можно привязать агентов (вкладка "Проект" в форме
                агента) и диалоги (селектор PROJECT в чате) — чтобы видеть и переключать, над каким
                проектом идёт общение и какой агент за него отвечает. Локальная настройка, без
                согласований.
              </p>
            </div>
            {!showProjectForm && (
              <button type="button" className="btn-primary" onClick={() => setShowProjectForm(true)}>
                <Plus size={14} />
                <span>Add project</span>
              </button>
            )}
          </div>

          {projects.length > 0 ? (
            <div className="admin-item-list">
              {projects.map(project => (
                <div key={project.id} className="admin-item-row">
                  <div className="admin-item-main">
                    <div className="admin-item-name">{project.name}</div>
                    <div className="admin-item-meta">
                      {project.description || 'без описания'}
                      {' · '}{agents.filter(a => a.project_id === project.id).length} агент(ов)
                    </div>
                  </div>
                  <div className="admin-item-actions">
                    <span
                      className={`admin-status-chip clickable ${project.is_active ? 'is-active' : 'is-inactive'}`}
                      onClick={() => toggleProjectActive(project)}
                      title="Toggle active"
                    >
                      <i className="dot" />{project.is_active ? 'active' : 'inactive'}
                    </span>
                    <button type="button" className="icon-btn danger" title="Delete" onClick={() => deleteProject(project)}><Trash2 size={14} /></button>
                  </div>
                </div>
              ))}
            </div>
          ) : (!showProjectForm && <div className="admin-empty">Проектов пока нет.</div>)}

          {showProjectForm && (
            <form onSubmit={addProject} className="admin-add-card">
              <div className="admin-add-grid">
                {field('Name', <input className="form-input" required value={projectForm.name} onChange={e => setProjectForm({ ...projectForm, name: e.target.value })} placeholder="Сайт клиента X" />)}
                {field('Description', <input className="form-input" value={projectForm.description} onChange={e => setProjectForm({ ...projectForm, description: e.target.value })} placeholder="необязательно" />)}
              </div>
              <div className="admin-add-actions">
                <button type="button" className="icon-btn" title="Cancel" onClick={cancelProjectForm}><X size={14} /></button>
                <button className="btn-primary" type="submit" disabled={projectSaving || !projectForm.name}>
                  <Plus size={14} />
                  <span>{projectSaving ? '...' : 'Add'}</span>
                </button>
              </div>
            </form>
          )}
          {projectError && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', marginTop: 12 }}>{projectError}</div>}
        </div>
      )}
    </div>
  );
}
