import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  ArrowDown, ArrowUp, Activity, AlertTriangle, BadgeCheck, Ban, Copy, KeyRound,
  Pencil, Plus, RotateCcw, Send, Trash2, Users, Wallet, X, Zap,
} from 'lucide-react';
import { styles } from '../styles';
import type {
  AccessOverview, AccessPlan, AccessToken, AccessTokenDetail, AgentModel, BotSubscriber,
  MessengerBinding, SubscriberCard,
} from '../types';

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: authHeaders(), ...init });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Запрос не удался (${res.status})`);
  }
  return res.json() as Promise<T>;
}

const PERIOD_LABELS: Record<string, string> = {
  daily: 'В день',
  weekly: 'В неделю',
  monthly: 'В месяц',
  lifetime: 'Разово (без сброса)',
};

const TOKEN_STATUS_LABELS: Record<string, string> = {
  active: 'Активен',
  suspended: 'Приостановлен',
  revoked: 'Отозван',
};

const OPTIONAL_TOOLS = [
  { name: 'web_search', label: 'Поиск в интернете' },
  { name: 'get_current_time_israel', label: 'Текущее время' },
  { name: 'get_weather', label: 'Погода' },
  { name: 'get_market_prices', label: 'Котировки' },
  { name: 'get_rss_digest', label: 'RSS-дайджест' },
  { name: 'generate_image', label: 'Генерация изображений (платно)' },
  { name: 'browser_read', label: 'Чтение веб-страниц (платно)' },
];

const money = (value: number | null | undefined) =>
  value === null || value === undefined ? '∞' : `$${Number(value).toFixed(4)}`;
const count = (value: number | null | undefined) =>
  value === null || value === undefined ? '∞' : Number(value).toLocaleString('ru-RU');

const emptyPlanForm = {
  name: '', description: '', period: 'monthly',
  limit_usd: '', limit_tokens: '', limit_messages: '',
  rate_limit_per_min: '6', max_message_chars: '2000',
  system_prompt_suffix: '', welcome_message: '',
  allowed_tools: null as string[] | null,
  price_usd: '', is_purchasable: false, duration_days: '',
  subagent_id: '',
};

type StatTone = 'neutral' | 'success' | 'warning' | 'info' | 'dim';

function Stat({
  icon, label, value, hint, tone = 'neutral',
}: { icon: ReactNode; label: string; value: string; hint?: string; tone?: StatTone }) {
  return (
    <div className={`glass-panel admin-stat-card${tone !== 'neutral' ? ` is-${tone}` : ''}`}>
      <div className="admin-stat-icon">{icon}</div>
      <div className="admin-stat-body">
        <span className="admin-stat-label">{label}</span>
        <strong className="admin-stat-value">{value}</strong>
        {hint && <span className="admin-stat-hint">{hint}</span>}
      </div>
    </div>
  );
}

/** Shown once, right after issuing — the plaintext is never retrievable again. */
function IssuedTokens({ tokens, onClose }: { tokens: AccessToken[]; onClose: () => void }) {
  const [copied, setCopied] = useState('');
  const all = tokens.map(t => t.plaintext).join('\n');
  return (
    <div className="glass-panel" style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: 10, border: '1px solid rgba(77,222,180,.4)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <strong style={{ fontSize: '0.92rem' }}>
          <BadgeCheck size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />
          Выдано токенов: {tokens.length}
        </strong>
        <button type="button" className="icon-btn" onClick={onClose} title="Скрыть"><X size={14} /></button>
      </div>
      <span style={{ fontSize: '0.78rem', color: 'var(--warning)' }}>
        Скопируйте сейчас — в базе хранится только хэш, показать эти строки повторно невозможно.
      </span>
      <textarea className="form-input" readOnly rows={Math.min(tokens.length + 1, 10)} value={all} style={{ fontFamily: 'monospace', fontSize: '0.8rem' }} />
      <button
        type="button"
        className="btn-primary"
        style={{ alignSelf: 'flex-start' }}
        onClick={() => { navigator.clipboard?.writeText(all); setCopied('ok'); setTimeout(() => setCopied(''), 2000); }}
      >
        <Copy size={13} style={{ marginRight: 6 }} />{copied ? 'Скопировано' : 'Копировать все'}
      </button>
    </div>
  );
}

function PlansPane({
  plans, agents, reload,
}: { plans: AccessPlan[]; agents: AgentModel[]; reload: () => void }) {
  const agentName = (id: string | null) => (id && agents.find(a => a.id === id)?.name) || null;
  // Vexa is never reachable through a token in the first place (tool_permissions.
  // MAIN_AGENT_IDS on the backend) — offering it here would just bounce off a
  // validation error, so it's filtered out rather than left to fail on submit.
  const assignableAgents = useMemo(() => agents.filter(a => a.id !== 'jarvis' && a.id !== 'vexa'), [agents]);
  const [form, setForm] = useState(emptyPlanForm);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const reset = () => { setForm(emptyPlanForm); setEditingId(null); setError(''); };

  const submit = async () => {
    setBusy(true);
    setError('');
    const body = {
      name: form.name.trim(),
      description: form.description,
      period: form.period,
      limit_usd: form.limit_usd === '' ? null : Number(form.limit_usd),
      limit_tokens: form.limit_tokens === '' ? null : Number(form.limit_tokens),
      limit_messages: form.limit_messages === '' ? null : Number(form.limit_messages),
      rate_limit_per_min: Number(form.rate_limit_per_min || 0),
      max_message_chars: Number(form.max_message_chars || 2000),
      allowed_tools: form.allowed_tools,
      system_prompt_suffix: form.system_prompt_suffix,
      welcome_message: form.welcome_message,
      is_active: true,
      price_usd: form.price_usd === '' ? null : Number(form.price_usd),
      is_purchasable: form.is_purchasable,
      duration_days: form.duration_days === '' ? null : Number(form.duration_days),
      subagent_id: form.subagent_id || null,
    };
    try {
      await api(editingId ? `/api/access/plans/${editingId}` : '/api/access/plans', {
        method: editingId ? 'PUT' : 'POST',
        body: JSON.stringify(body),
      });
      reset();
      reload();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (plan: AccessPlan) => {
    setEditingId(plan.id);
    setForm({
      name: plan.name, description: plan.description || '', period: plan.period,
      limit_usd: plan.limit_usd == null ? '' : String(plan.limit_usd),
      limit_tokens: plan.limit_tokens == null ? '' : String(plan.limit_tokens),
      limit_messages: plan.limit_messages == null ? '' : String(plan.limit_messages),
      rate_limit_per_min: String(plan.rate_limit_per_min),
      max_message_chars: String(plan.max_message_chars),
      system_prompt_suffix: plan.system_prompt_suffix || '',
      welcome_message: plan.welcome_message || '',
      allowed_tools: plan.allowed_tools,
      price_usd: plan.price_usd == null ? '' : String(plan.price_usd),
      is_purchasable: plan.is_purchasable,
      duration_days: plan.duration_days == null ? '' : String(plan.duration_days),
      subagent_id: plan.subagent_id || '',
    });
  };

  const remove = async (plan: AccessPlan) => {
    if (!window.confirm(`Удалить тариф «${plan.name}»?`)) return;
    try {
      await api(`/api/access/plans/${plan.id}`, { method: 'DELETE' });
      reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const toggleTool = (name: string) => {
    const current = form.allowed_tools;
    if (current === null) { setForm({ ...form, allowed_tools: [name] }); return; }
    const next = current.includes(name) ? current.filter(t => t !== name) : [...current, name];
    setForm({ ...form, allowed_tools: next });
  };

  return (
    <div className="admin-plans-grid">
      <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <strong style={{ fontSize: '0.9rem' }}>{editingId ? 'Изменить тариф' : 'Новый тариф'}</strong>
        <input className="form-input" placeholder="Название (например «Базовая консультация»)" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
        <input className="form-input" placeholder="Описание" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
        <select className="form-input" value={form.subagent_id} onChange={e => setForm({ ...form, subagent_id: e.target.value })}>
          <option value="">Любой агент (общий тариф)</option>
          {assignableAgents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <span style={styles.formHelp}>
          Привязка к агенту — этот тариф можно будет выдать или продать только для него; для остальных агентов он не появится в списке.
        </span>
        <select className="form-input" value={form.period} onChange={e => setForm({ ...form, period: e.target.value })}>
          {Object.entries(PERIOD_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
          <input className="form-input" placeholder="$ лимит" value={form.limit_usd} onChange={e => setForm({ ...form, limit_usd: e.target.value })} />
          <input className="form-input" placeholder="Токенов" value={form.limit_tokens} onChange={e => setForm({ ...form, limit_tokens: e.target.value })} />
          <input className="form-input" placeholder="Сообщений" value={form.limit_messages} onChange={e => setForm({ ...form, limit_messages: e.target.value })} />
        </div>
        <span style={styles.formHelp}>Пустое поле — без ограничения по этому измерению.</span>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <input className="form-input" placeholder="Сообщ./мин" value={form.rate_limit_per_min} onChange={e => setForm({ ...form, rate_limit_per_min: e.target.value })} />
          <input className="form-input" placeholder="Макс. символов" value={form.max_message_chars} onChange={e => setForm({ ...form, max_message_chars: e.target.value })} />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 12px', borderRadius: 8, border: '1px solid rgba(255,255,255,.09)' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.82rem', fontWeight: 600 }}>
            <input type="checkbox" checked={form.is_purchasable} onChange={e => setForm({ ...form, is_purchasable: e.target.checked })} />
            Продавать как подписку
          </label>
          {form.is_purchasable && (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <input className="form-input" placeholder="Цена, $" value={form.price_usd} onChange={e => setForm({ ...form, price_usd: e.target.value })} />
                <input className="form-input" placeholder="Дней в периоде" value={form.duration_days} onChange={e => setForm({ ...form, duration_days: e.target.value })} />
              </div>
              <span style={styles.formHelp}>
                Пустой срок — по периоду тарифа (день/неделя/30 дней). Бот с этим тарифом по умолчанию
                сам предложит покупку тому, кто напишет без токена.
              </span>
            </>
          )}
        </div>
        <textarea className="form-input" rows={3} placeholder="Дополнение к системному промпту для этого тарифа" value={form.system_prompt_suffix} onChange={e => setForm({ ...form, system_prompt_suffix: e.target.value })} />
        <textarea className="form-input" rows={2} placeholder="Приветствие после активации токена" value={form.welcome_message} onChange={e => setForm({ ...form, welcome_message: e.target.value })} />

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.8rem' }}>
            <input type="checkbox" checked={form.allowed_tools === null} onChange={e => setForm({ ...form, allowed_tools: e.target.checked ? null : [] })} />
            Набор инструментов по умолчанию
          </label>
          {form.allowed_tools !== null && OPTIONAL_TOOLS.map(tool => (
            <label key={tool.name} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.78rem', paddingLeft: 16 }}>
              <input type="checkbox" checked={form.allowed_tools?.includes(tool.name) || false} onChange={() => toggleTool(tool.name)} />
              {tool.label}
            </label>
          ))}
          <span style={styles.formHelp}>
            Тариф может только сузить список: доступ к серверу, оболочке и личным данным владельца закрыт для всех ботов независимо от настроек.
          </span>
        </div>

        {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>⚠️ {error}</span>}
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" className="btn-primary" disabled={busy || !form.name.trim()} onClick={submit}>
            {editingId ? 'Сохранить' : 'Создать'}
          </button>
          {editingId && <button type="button" className="btn-ghost" onClick={reset}>Отмена</button>}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {plans.length === 0 ? (
          <div className="admin-empty-cta">
            <BadgeCheck size={22} />
            <span>Тарифов пока нет. Токен без тарифа работает без лимитов — сначала создайте тариф слева.</span>
          </div>
        ) : (
          <div className="admin-item-list" style={{ marginBottom: 0 }}>
            {plans.map(plan => (
              <div key={plan.id} className="admin-item-row" style={{ alignItems: 'flex-start' }}>
                <div className="admin-item-main">
                  <div className="admin-item-name">
                    {plan.name}
                    <span className="admin-status-chip is-inactive" style={{ marginLeft: 8, verticalAlign: 'middle' }}>
                      {agentName(plan.subagent_id) || 'любой агент'}
                    </span>
                  </div>
                  <div className="admin-item-meta">{plan.description || '—'}</div>
                  <div className="admin-item-meta">
                    {PERIOD_LABELS[plan.period]} · {money(plan.limit_usd)} · {count(plan.limit_tokens)} токенов · {count(plan.limit_messages)} сообщений ·
                    {' '}{plan.rate_limit_per_min || '∞'}/мин · до {plan.max_message_chars} символов
                  </div>
                  {plan.is_purchasable && plan.price_usd != null && (
                    <div className="admin-item-meta" style={{ color: 'var(--success)' }}>
                      Продаётся: ${Number(plan.price_usd).toFixed(2)} за {plan.duration_days ?? '—'} дн.
                    </div>
                  )}
                  <div className="admin-item-meta">
                    Инструменты: {plan.allowed_tools === null ? 'набор по умолчанию' : (plan.allowed_tools.length ? plan.allowed_tools.join(', ') : 'без инструментов')}
                  </div>
                </div>
                <div className="admin-item-actions">
                  <button type="button" className="icon-btn" onClick={() => startEdit(plan)} title="Изменить"><Pencil size={14} /></button>
                  <button type="button" className="icon-btn danger" onClick={() => remove(plan)} title="Удалить"><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function TokensPane({
  tokens, plans, bindings, reload,
}: { tokens: AccessToken[]; plans: AccessPlan[]; bindings: MessengerBinding[]; reload: () => void }) {
  const [bindingId, setBindingId] = useState('');
  const [planId, setPlanId] = useState('');
  const [label, setLabel] = useState('');
  const [maxChats, setMaxChats] = useState('1');
  const [expiresAt, setExpiresAt] = useState('');
  const [issueCount, setIssueCount] = useState('1');
  const [issued, setIssued] = useState<AccessToken[] | null>(null);
  const [detail, setDetail] = useState<AccessTokenDetail | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const tokenBindings = useMemo(() => bindings.filter(b => b.access_mode === 'token'), [bindings]);
  const binding = tokenBindings.find(b => b.id === bindingId);
  // A plan assigned to a specific agent can only be issued for that agent — the
  // backend refuses the mismatch outright, so the dropdown is filtered to match
  // rather than letting the owner pick something that will just bounce back.
  const availablePlans = useMemo(
    () => plans.filter(p => !p.subagent_id || p.subagent_id === binding?.subagent_id),
    [plans, binding]
  );

  useEffect(() => {
    if (!bindingId && tokenBindings.length) setBindingId(tokenBindings[0].id);
  }, [tokenBindings, bindingId]);

  useEffect(() => {
    if (planId && !availablePlans.some(p => p.id === planId)) setPlanId('');
  }, [availablePlans, planId]);

  const issue = async () => {
    if (!binding) return;
    setBusy(true);
    setError('');
    try {
      const result = await api<{ tokens: AccessToken[] }>('/api/access/tokens', {
        method: 'POST',
        body: JSON.stringify({
          binding_id: binding.id,
          subagent_id: binding.subagent_id,
          plan_id: planId || null,
          label,
          max_chats: Number(maxChats || 1),
          expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
          count: Number(issueCount || 1),
        }),
      });
      setIssued(result.tokens);
      setLabel('');
      reload();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const act = async (token: AccessToken, path: string, method = 'POST', body?: unknown) => {
    try {
      await api(`/api/access/tokens/${token.id}${path}`, { method, body: body ? JSON.stringify(body) : undefined });
      reload();
      if (detail?.token.id === token.id) openDetail(token);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const openDetail = async (token: AccessToken) => {
    try {
      setDetail(await api<AccessTokenDetail>(`/api/access/tokens/${token.id}`));
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <strong style={{ fontSize: '0.9rem' }}>Выдать токен</strong>
        {tokenBindings.length === 0 ? (
          <span style={{ display: 'flex', alignItems: 'flex-start', gap: 6, fontSize: '0.8rem', color: 'var(--warning)' }}>
            <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} />
            Нет ни одного канала в режиме доступа по токенам. Откройте «Каналы связи», выберите бота и переключите режим доступа.
          </span>
        ) : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
              <select className="form-input" value={bindingId} onChange={e => setBindingId(e.target.value)}>
                {tokenBindings.map(b => (
                  <option key={b.id} value={b.id}>{b.agent_name} · @{b.bot_username} ({b.platform})</option>
                ))}
              </select>
              <select className="form-input" value={planId} onChange={e => setPlanId(e.target.value)}>
                <option value="">Без тарифа (без лимитов)</option>
                {availablePlans.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <input className="form-input" placeholder="Метка: кому выдан" value={label} onChange={e => setLabel(e.target.value)} />
              <input className="form-input" placeholder="Чатов на токен" value={maxChats} onChange={e => setMaxChats(e.target.value)} />
              <input className="form-input" type="date" value={expiresAt} onChange={e => setExpiresAt(e.target.value)} />
              <input className="form-input" placeholder="Сколько выпустить" value={issueCount} onChange={e => setIssueCount(e.target.value)} />
            </div>
            {/* Disabled until a binding is actually selected — the select is
                populated by an effect, so the first render has no target yet
                and the click would otherwise do nothing at all. */}
            <button type="button" className="btn-primary" style={{ alignSelf: 'flex-start' }} disabled={busy || !binding} onClick={issue}>
              <Plus size={13} style={{ marginRight: 6 }} />Выдать
            </button>
          </>
        )}
        {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>⚠️ {error}</span>}
      </div>

      {issued && <IssuedTokens tokens={issued} onClose={() => setIssued(null)} />}

      {tokens.length === 0 ? (
        <div className="admin-empty-cta">
          <KeyRound size={22} />
          <span>Токенов пока нет.</span>
        </div>
      ) : (
        <div className="admin-item-list" style={{ marginBottom: 0 }}>
          {tokens.map(token => {
            const plan = plans.find(p => p.id === token.plan_id);
            const usedTokens = (token.used_tokens_in || 0) + (token.used_tokens_out || 0);
            const limitUsd = token.limit_usd ?? plan?.limit_usd ?? null;
            return (
              <div key={token.id} className="admin-item-row">
                <div className="admin-item-main">
                  <div className="admin-item-name">
                    <code style={{ fontSize: '0.85rem', fontWeight: 400 }}>{token.display}</code>
                    <span style={{ marginLeft: 8 }}>{token.label || 'без метки'}</span>
                  </div>
                  <div className="admin-item-meta">
                    {plan ? plan.name : 'без тарифа'} · выдан {new Date(token.created_at).toLocaleDateString('ru-RU')}
                    {token.expires_at ? ` · до ${new Date(token.expires_at).toLocaleDateString('ru-RU')}` : ''}
                  </div>
                  <div className="admin-item-meta">
                    Израсходовано: {money(token.used_usd)} из {money(limitUsd)} · {count(usedTokens)} токенов · {token.used_messages} сообщений
                  </div>
                </div>
                <div className="admin-item-actions">
                  <span className={`admin-status-chip ${token.status === 'active' ? 'is-active' : 'is-revoked'}`}>
                    <i className="dot" />{TOKEN_STATUS_LABELS[token.status] || token.status}
                  </span>
                  <button type="button" className="icon-btn" onClick={() => openDetail(token)} title="Расход">📊</button>
                  {token.status === 'active' && (
                    <button type="button" className="icon-btn" onClick={() => act(token, '', 'PATCH', { status: 'suspended' })} title="Приостановить">
                      <Ban size={14} />
                    </button>
                  )}
                  {token.status === 'suspended' && (
                    <button type="button" className="icon-btn" onClick={() => act(token, '', 'PATCH', { status: 'active' })} title="Возобновить">
                      <BadgeCheck size={14} />
                    </button>
                  )}
                  <button type="button" className="icon-btn" onClick={() => act(token, '/reset-usage')} title="Обнулить расход">
                    <RotateCcw size={14} />
                  </button>
                  {token.status !== 'revoked' && (
                    <button
                      type="button"
                      className="icon-btn danger"
                      title="Отозвать — чат сразу перестанет отвечать"
                      onClick={() => { if (window.confirm('Отозвать токен? Привязанные чаты будут заблокированы.')) act(token, '/revoke'); }}
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {detail && (
        <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <strong style={{ fontSize: '0.9rem' }}>Расход по токену {detail.token.display}</strong>
            <button type="button" className="icon-btn" onClick={() => setDetail(null)} title="Скрыть"><X size={14} /></button>
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <Stat icon={<Activity size={16} />} label="Обращений" value={String(detail.lifetime.turns)} />
            <Stat icon={<ArrowDown size={16} />} label="Токенов вход" value={count(detail.lifetime.tokens_in)} />
            <Stat icon={<ArrowUp size={16} />} label="Токенов выход" value={count(detail.lifetime.tokens_out)} />
            <Stat icon={<Wallet size={16} />} label="Стоимость" value={money(detail.lifetime.cost_usd)} />
          </div>
          <div style={{ maxHeight: 240, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 4 }}>
            {detail.recent.map(row => (
              <div key={row.id} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-muted)', gap: 8 }}>
                <span>{new Date(row.ts).toLocaleString('ru-RU')}</span>
                <span>{row.model || '—'}</span>
                <span>{row.prompt_tokens}/{row.completion_tokens}</span>
                <span>{money(row.cost_usd)}</span>
                <span style={{ color: row.status === 'ok' ? 'inherit' : 'var(--warning)' }}>{row.status === 'ok' ? '✓' : row.detail || row.status}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SubscribersPane({ subscribers, reload }: { subscribers: BotSubscriber[]; reload: () => void }) {
  const [card, setCard] = useState<SubscriberCard | null>(null);
  const [notes, setNotes] = useState('');
  const [error, setError] = useState('');

  const open = async (subscriber: BotSubscriber) => {
    try {
      const loaded = await api<SubscriberCard>(`/api/access/subscribers/${subscriber.id}`);
      setCard(loaded);
      setNotes(loaded.subscriber.notes || '');
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const patch = async (id: string, body: unknown) => {
    try {
      await api(`/api/access/subscribers/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
      reload();
      if (card?.subscriber.id === id) open(card.subscriber);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div style={{ display: 'grid', gridTemplateColumns: card ? 'minmax(280px, 360px) 1fr' : '1fr', gap: 18, alignItems: 'start' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>⚠️ {error}</span>}
        {subscribers.length === 0 ? (
          <div className="admin-empty-cta">
            <Users size={22} />
            <span>Пока никто не активировал токен.</span>
          </div>
        ) : (
          <div className="admin-item-list" style={{ marginBottom: 0 }}>
            {subscribers.map(subscriber => (
              <div
                key={subscriber.id}
                className="admin-item-row"
                style={{ cursor: 'pointer' }}
                onClick={() => open(subscriber)}
              >
                <div className="admin-item-main">
                  <div className="admin-item-name">{subscriber.display_name || subscriber.chat_id}</div>
                  <div className="admin-item-meta">
                    {subscriber.platform} · {subscriber.messages_count} сообщений · последний контакт {new Date(subscriber.last_seen_at).toLocaleString('ru-RU')}
                  </div>
                </div>
                <div className="admin-item-actions">
                  <span className={`admin-status-chip ${subscriber.status === 'active' ? 'is-active' : 'is-revoked'}`}>
                    <i className="dot" />{subscriber.status === 'active' ? 'Активен' : 'Заблокирован'}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {card && (
        <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <strong style={{ fontSize: '0.95rem' }}>{card.subscriber.display_name || card.subscriber.chat_id}</strong>
            <button type="button" className="icon-btn" onClick={() => setCard(null)} title="Скрыть"><X size={14} /></button>
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <Stat icon={<Activity size={16} />} label="Обращений" value={String(card.usage.turns)} />
            <Stat icon={<Zap size={16} />} label="Токенов" value={count((card.usage.tokens_in || 0) + (card.usage.tokens_out || 0))} />
            <Stat icon={<Wallet size={16} />} label="Стоимость" value={money(card.usage.cost_usd)} />
            <Stat icon={<BadgeCheck size={16} />} label="Тариф" value={card.plan?.name || '—'} hint={card.token?.display} />
          </div>

          <textarea
            className="form-input"
            rows={3}
            placeholder="Заметки администратора — попадают в карточку, которую видит агент"
            value={notes}
            onChange={e => setNotes(e.target.value)}
            onBlur={() => notes !== (card.subscriber.notes || '') && patch(card.subscriber.id, { notes })}
          />
          <button
            type="button"
            className={`btn-ghost${card.subscriber.status === 'active' ? ' danger' : ''}`}
            style={{ alignSelf: 'flex-start' }}
            onClick={() => patch(card.subscriber.id, { status: card.subscriber.status === 'active' ? 'blocked' : 'active' })}
          >
            {card.subscriber.status === 'active' ? 'Заблокировать' : 'Разблокировать'}
          </button>

          <strong style={{ fontSize: '0.85rem', marginTop: 6 }}>Переписка</strong>
          <div style={{ maxHeight: 380, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
            {card.conversation.length === 0 && <span style={{ fontSize: '0.78rem', color: 'var(--text-dim)' }}>Пока пусто.</span>}
            {card.conversation.map(message => (
              <div key={message.id} style={{
                alignSelf: message.role === 'user' ? 'flex-start' : 'flex-end',
                maxWidth: '85%', padding: '8px 10px', borderRadius: 8, fontSize: '0.8rem',
                background: message.role === 'user' ? 'rgba(255,255,255,.05)' : 'rgba(46,179,139,.10)',
              }}>
                {message.content}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

const PANES = [
  { value: 'tokens', label: 'Токены', icon: <KeyRound size={14} /> },
  { value: 'plans', label: 'Тарифы', icon: <BadgeCheck size={14} /> },
  { value: 'subscribers', label: 'Абоненты', icon: <Users size={14} /> },
] as const;

export function BotAccessTab({ agents }: { agents: AgentModel[] }) {
  const [pane, setPane] = useState<'tokens' | 'plans' | 'subscribers'>('tokens');
  const [overview, setOverview] = useState<AccessOverview | null>(null);
  const [plans, setPlans] = useState<AccessPlan[]>([]);
  const [tokens, setTokens] = useState<AccessToken[]>([]);
  const [subscribers, setSubscribers] = useState<BotSubscriber[]>([]);
  const [bindings, setBindings] = useState<MessengerBinding[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    Promise.all([
      api<AccessOverview>('/api/access/overview').then(setOverview).catch(() => {}),
      api<AccessPlan[]>('/api/access/plans').then(setPlans).catch(() => {}),
      api<AccessToken[]>('/api/access/tokens').then(setTokens).catch(() => {}),
      api<BotSubscriber[]>('/api/access/subscribers').then(setSubscribers).catch(() => {}),
      api<MessengerBinding[]>('/api/messenger-bindings').then(setBindings).catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <p style={styles.tabSubtitle}>
        Токены для внешних пользователей, тарифы с лимитами и картотека абонентов социальных ботов
      </p>

      {overview && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <Stat icon={<KeyRound size={16} />} label="Токенов" value={String(overview.tokens.total || 0)} hint={`активных ${overview.tokens.active || 0}`} tone="info" />
          <Stat icon={<Users size={16} />} label="Абонентов" value={String(overview.subscribers.total || 0)} hint={`заблокировано ${overview.subscribers.blocked || 0}`} />
          <Stat icon={<Send size={16} />} label="Сообщений" value={count(overview.tokens.used_messages)} hint={`отклонено ${overview.blocked_turns}`} />
          <Stat icon={<Zap size={16} />} label="Токенов LLM" value={count(overview.tokens.used_tokens)} />
          <Stat icon={<Wallet size={16} />} label="Расход" value={money(overview.tokens.used_usd)} />
        </div>
      )}

      <nav className="admin-pane-tabs">
        {PANES.map(({ value, label, icon }) => (
          <button
            key={value}
            type="button"
            className={pane === value ? 'is-active' : ''}
            onClick={() => setPane(value)}
          >
            {icon}{label}
          </button>
        ))}
      </nav>

      {loading ? (
        <p style={{ color: 'var(--text-dim)' }}>Загрузка...</p>
      ) : pane === 'plans' ? (
        <PlansPane plans={plans} agents={agents} reload={reload} />
      ) : pane === 'tokens' ? (
        <TokensPane tokens={tokens} plans={plans} bindings={bindings} reload={reload} />
      ) : (
        <SubscribersPane subscribers={subscribers} reload={reload} />
      )}

      <span style={styles.formHelp}>
        Токен показывается один раз при выпуске — в базе хранится только его хэш. Отзыв мгновенно блокирует все чаты,
        которые его активировали. Каждое обращение по токену учитывается в токенах и долларах, а лимиты проверяются
        до обращения к модели, поэтому исчерпанный токен ничего не стоит.
      </span>
    </div>
  );
}
