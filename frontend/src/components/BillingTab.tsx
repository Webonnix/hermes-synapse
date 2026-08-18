import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  AlertTriangle, Ban, Clock, CreditCard, ExternalLink, Plus, Receipt,
  RefreshCw, TrendingUp, Users, Wallet, X,
} from 'lucide-react';
import { styles } from '../styles';
import type {
  AccessPlan, AccessSubscription, BillingConfig, BillingOverview, MessengerBinding, PaymentInvoice,
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

const money = (value: number | null | undefined) =>
  value === null || value === undefined ? '—' : `$${Number(value).toFixed(2)}`;

const SUBSCRIPTION_LABELS: Record<string, string> = {
  active: 'Активна',
  expired: 'Истекла',
  canceled: 'Отменена',
};

const INVOICE_LABELS: Record<string, string> = {
  pending: 'Ожидает оплаты',
  paid: 'Оплачен',
  expired: 'Просрочен',
  failed: 'Не оплачен',
};

const PROVIDER_LABELS: Record<string, string> = {
  manual: 'Вручную (свой кошелёк)',
  nowpayments: 'NOWPayments (крипта)',
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

function ConfigPanel({ config, reload }: { config: BillingConfig; reload: () => void }) {
  const [provider, setProvider] = useState(config.provider);
  const [baseUrl, setBaseUrl] = useState(config.public_base_url);
  const [successUrl, setSuccessUrl] = useState(config.success_url);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  const save = async () => {
    setError('');
    try {
      await api('/api/billing/config', {
        method: 'PUT',
        body: JSON.stringify({ provider, public_base_url: baseUrl, success_url: successUrl }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const secretsMissing = provider === 'nowpayments' && !(config.api_key_configured && config.ipn_secret_configured);

  return (
    <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 560 }}>
      <strong style={{ fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: 8 }}>
        <Wallet size={14} />Способ приёма оплаты
      </strong>
      <select className="form-input" value={provider} onChange={e => setProvider(e.target.value)}>
        {config.providers.map(name => (
          <option key={name} value={name}>{PROVIDER_LABELS[name] || name}</option>
        ))}
      </select>
      {provider === 'nowpayments' && (
        <>
          <input
            className="form-input"
            placeholder="Публичный адрес сервиса, напр. https://hermes.webonnix.net"
            value={baseUrl}
            onChange={e => setBaseUrl(e.target.value)}
          />
          <input
            className="form-input"
            placeholder="Куда вернуть покупателя после оплаты (необязательно)"
            value={successUrl}
            onChange={e => setSuccessUrl(e.target.value)}
          />
          <span style={styles.formHelp}>
            Платёжная система шлёт подтверждение на <code>{(baseUrl || '…') + '/api/payments/webhook'}</code> —
            адрес должен быть доступен извне. Этот путь намеренно открыт без сессии дашборда: он проверяет
            подпись HMAC каждого запроса и без неё ничего не выдаёт.
          </span>
          {secretsMissing && (
            <span style={{ display: 'flex', alignItems: 'flex-start', gap: 6, color: 'var(--warning)', fontSize: '0.8rem' }}>
              <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} />
              Не хватает ключей: задайте NOWPAYMENTS_API_KEY и NOWPAYMENTS_IPN_SECRET в разделе «Ключи API».
              Без IPN-секрета оплата не подтвердится.
            </span>
          )}
        </>
      )}
      {provider === 'manual' && (
        <span style={styles.formHelp}>
          Счета выставляются, но ссылку на оплату система не создаёт: вы получаете перевод на свой кошелёк
          и отмечаете счёт оплаченным — токен и подписка выпустятся тем же путём, что и по webhook.
        </span>
      )}
      {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>⚠️ {error}</span>}
      <button type="button" className="btn-primary" style={{ alignSelf: 'flex-start' }} onClick={save}>
        {saved ? 'Сохранено' : 'Сохранить'}
      </button>
    </div>
  );
}

function NewInvoicePanel({
  plans, bindings, reload,
}: { plans: AccessPlan[]; bindings: MessengerBinding[]; reload: () => void }) {
  const tokenBindings = useMemo(() => bindings.filter(b => b.access_mode === 'token'), [bindings]);
  const [planId, setPlanId] = useState('');
  const [bindingId, setBindingId] = useState('');
  const [customer, setCustomer] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const binding = tokenBindings.find(b => b.id === bindingId);
  // A plan assigned to a specific agent can only be sold for that agent — the
  // backend refuses the mismatch outright, so this only offers plans that
  // would actually be accepted for whichever bot is selected above.
  const sellable = useMemo(
    () => plans.filter(p => p.is_purchasable && p.price_usd != null
      && (!p.subagent_id || p.subagent_id === binding?.subagent_id)),
    [plans, binding]
  );

  useEffect(() => {
    if (!bindingId && tokenBindings.length) setBindingId(tokenBindings[0].id);
  }, [tokenBindings, bindingId]);

  useEffect(() => {
    if (!sellable.some(p => p.id === planId)) setPlanId(sellable[0]?.id || '');
  }, [sellable, planId]);

  const create = async () => {
    if (!binding || !planId) return;
    setBusy(true);
    setError('');
    try {
      await api('/api/billing/invoices', {
        method: 'POST',
        body: JSON.stringify({
          plan_id: planId,
          binding_id: binding.id,
          subagent_id: binding.subagent_id,
          customer_ref: customer,
        }),
      });
      setCustomer('');
      reload();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 10 }}>
      <strong style={{ fontSize: '0.9rem' }}>Выставить счёт</strong>
      {sellable.length === 0 || tokenBindings.length === 0 ? (
        <span style={{ display: 'flex', alignItems: 'flex-start', gap: 6, fontSize: '0.8rem', color: 'var(--warning)' }}>
          <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} />
          Нужен тариф с ценой («Доступ» → «Тарифы» → «Продавать как подписку») и бот в режиме доступа по токенам.
        </span>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
            <select className="form-input" value={bindingId} onChange={e => setBindingId(e.target.value)}>
              {tokenBindings.map(b => (
                <option key={b.id} value={b.id}>{b.agent_name} · @{b.bot_username}</option>
              ))}
            </select>
            <select className="form-input" value={planId} onChange={e => setPlanId(e.target.value)}>
              {sellable.map(p => (
                <option key={p.id} value={p.id}>{p.name} — ${Number(p.price_usd).toFixed(2)}</option>
              ))}
            </select>
            <input className="form-input" placeholder="Кому: имя или контакт" value={customer} onChange={e => setCustomer(e.target.value)} />
          </div>
          <button type="button" className="btn-primary" style={{ alignSelf: 'flex-start' }} disabled={busy || !binding} onClick={create}>
            <Plus size={13} style={{ marginRight: 6 }} />Создать счёт
          </button>
        </>
      )}
      {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>⚠️ {error}</span>}
    </div>
  );
}

/** The plaintext token minted by a payment the owner confirmed by hand — same
 *  one-shot handover as manual issuance, since nobody delivered it over chat. */
function IssuedAfterPayment({ plaintext, onClose }: { plaintext: string; onClose: () => void }) {
  return (
    <div className="glass-panel" style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 8, border: '1px solid rgba(77,222,180,.4)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <strong style={{ fontSize: '0.9rem' }}>Токен выпущен по оплате</strong>
        <button type="button" className="icon-btn" onClick={onClose} title="Скрыть"><X size={14} /></button>
      </div>
      <span style={{ fontSize: '0.78rem', color: 'var(--warning)' }}>
        Передайте покупателю — повторно эта строка не показывается.
      </span>
      <input className="form-input" readOnly value={plaintext} style={{ fontFamily: 'monospace' }} />
    </div>
  );
}

const PANES = [
  { value: 'subscriptions', label: 'Подписки', icon: <Users size={14} /> },
  { value: 'invoices', label: 'Счета', icon: <Receipt size={14} /> },
  { value: 'settings', label: 'Приём оплаты', icon: <Wallet size={14} /> },
] as const;

export function BillingTab() {
  const [pane, setPane] = useState<'subscriptions' | 'invoices' | 'settings'>('subscriptions');
  const [overview, setOverview] = useState<BillingOverview | null>(null);
  const [subscriptions, setSubscriptions] = useState<AccessSubscription[]>([]);
  const [invoices, setInvoices] = useState<PaymentInvoice[]>([]);
  const [plans, setPlans] = useState<AccessPlan[]>([]);
  const [bindings, setBindings] = useState<MessengerBinding[]>([]);
  const [issued, setIssued] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    Promise.all([
      api<BillingOverview>('/api/billing/overview').then(setOverview).catch(() => {}),
      api<AccessSubscription[]>('/api/billing/subscriptions').then(setSubscriptions).catch(() => {}),
      api<PaymentInvoice[]>('/api/billing/invoices').then(setInvoices).catch(() => {}),
      api<AccessPlan[]>('/api/access/plans').then(setPlans).catch(() => {}),
      api<MessengerBinding[]>('/api/messenger-bindings').then(setBindings).catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const act = async (path: string) => {
    setError('');
    try {
      const result = await api<{ plaintext?: string }>(path, { method: 'POST' });
      if (result && result.plaintext) setIssued(result.plaintext);
      reload();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const planName = (planId: string) => plans.find(p => p.id === planId)?.name || planId;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <p style={styles.tabSubtitle}>
        Подписки на конкретных ботов, криптосчета и автоматическая выдача доступа после оплаты
      </p>

      {overview && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <Stat
            icon={<Users size={16} />}
            label="Активных подписок"
            value={String(overview.subscriptions.active)}
            hint={`всего ${overview.subscriptions.total}`}
            tone="info"
          />
          <Stat
            icon={<TrendingUp size={16} />}
            label="MRR"
            value={money(overview.mrr_usd)}
            hint="в пересчёте на 30 дней"
            tone="success"
          />
          <Stat
            icon={<Wallet size={16} />}
            label="Получено"
            value={money(overview.invoices.revenue_usd)}
            hint={`оплачено счетов: ${overview.invoices.paid || 0}`}
            tone="success"
          />
          <Stat
            icon={<Clock size={16} />}
            label="Ждут оплаты"
            value={String(overview.invoices.pending || 0)}
            tone="warning"
          />
          <Stat
            icon={<Ban size={16} />}
            label="Истекло"
            value={String(overview.subscriptions.expired)}
            hint={`отменено ${overview.subscriptions.canceled}`}
            tone="dim"
          />
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

      {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>⚠️ {error}</span>}
      {issued && <IssuedAfterPayment plaintext={issued} onClose={() => setIssued(null)} />}

      {loading ? (
        <p style={{ color: 'var(--text-dim)' }}>Загрузка...</p>
      ) : pane === 'settings' ? (
        overview && <ConfigPanel config={overview.config} reload={reload} />
      ) : pane === 'invoices' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <NewInvoicePanel plans={plans} bindings={bindings} reload={reload} />
          {invoices.length === 0 ? (
            <div className="admin-empty-cta">
              <Receipt size={22} />
              <span>Счетов пока нет — выставьте первый выше.</span>
            </div>
          ) : (
            <div className="admin-item-list" style={{ marginBottom: 0 }}>
              {invoices.map(invoice => (
                <div key={invoice.id} className="admin-item-row">
                  <div className="admin-item-main">
                    <div className="admin-item-name">{money(invoice.amount_usd)} · {planName(invoice.plan_id)}</div>
                    <div className="admin-item-meta">
                      {invoice.customer_ref || 'без имени'} · {invoice.origin === 'bot' ? 'куплен в чате' : 'выставлен вручную'}
                      {invoice.purpose === 'renewal' ? ' · продление' : ''} · {new Date(invoice.created_at).toLocaleString('ru-RU')}
                    </div>
                    {invoice.last_status && (
                      <div className="admin-item-meta">Статус у платёжной системы: {invoice.last_status}</div>
                    )}
                  </div>
                  <div className="admin-item-actions">
                    <span className={`admin-status-chip ${invoice.status === 'paid' ? 'is-active' : invoice.status === 'pending' ? 'is-pending' : 'is-revoked'}`}>
                      <i className="dot" />{INVOICE_LABELS[invoice.status] || invoice.status}
                    </span>
                    {invoice.payment_url && (
                      <a className="icon-btn" href={invoice.payment_url} target="_blank" rel="noreferrer" title="Страница оплаты">
                        <ExternalLink size={14} />
                      </a>
                    )}
                    {invoice.status === 'pending' && (
                      <>
                        <button
                          type="button"
                          className="icon-btn"
                          title="Подтвердить оплату вручную — выпустит токен и подписку"
                          onClick={() => act(`/api/billing/invoices/${invoice.id}/mark-paid`)}
                        >
                          <CreditCard size={14} />
                        </button>
                        <button type="button" className="icon-btn danger" title="Отменить счёт" onClick={() => act(`/api/billing/invoices/${invoice.id}/cancel`)}>
                          <Ban size={14} />
                        </button>
                      </>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {subscriptions.length === 0 ? (
            <div className="admin-empty-cta">
              <Users size={22} />
              <span>Подписок пока нет.</span>
              <button type="button" className="btn-ghost" onClick={() => setPane('invoices')}>
                <Receipt size={13} />Выставить счёт
              </button>
            </div>
          ) : (
            <div className="admin-item-list" style={{ marginBottom: 0 }}>
              {subscriptions.map(subscription => (
                <div key={subscription.id} className="admin-item-row">
                  <div className="admin-item-main">
                    <div className="admin-item-name">{subscription.customer_ref || 'без имени'} · {planName(subscription.plan_id)}</div>
                    <div className="admin-item-meta">
                      {money(subscription.price_usd)} · агент {subscription.subagent_id} ·
                      {subscription.current_period_end
                        ? ` до ${new Date(subscription.current_period_end).toLocaleDateString('ru-RU')}`
                        : ' бессрочно'}
                    </div>
                  </div>
                  <div className="admin-item-actions">
                    <span className={`admin-status-chip ${subscription.status === 'active' ? 'is-active' : 'is-revoked'}`}>
                      <i className="dot" />{SUBSCRIPTION_LABELS[subscription.status] || subscription.status}
                    </span>
                    <button
                      type="button"
                      className="icon-btn"
                      title="Продлить период вручную, без оплаты"
                      onClick={() => act(`/api/billing/subscriptions/${subscription.id}/renew`)}
                    >
                      <RefreshCw size={14} />
                    </button>
                    {subscription.status === 'active' && (
                      <button
                        type="button"
                        className="icon-btn danger"
                        title="Отменить — токен сразу приостановится"
                        onClick={() => {
                          if (window.confirm('Отменить подписку? Доступ по её токену прекратится сразу.')) {
                            act(`/api/billing/subscriptions/${subscription.id}/cancel`);
                          }
                        }}
                      >
                        <Ban size={14} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <span style={styles.formHelp}>
        Оплата подтверждается подписанным callback-ом платёжной системы: токен и подписка выпускаются
        автоматически и ровно один раз, повторный callback ничего не дублирует. Если покупка началась в чате
        бота, токен сразу привязывается к этому чату — покупателю не нужно ничего вставлять.
      </span>
    </div>
  );
}
