import type { ReactNode } from 'react';
import { AlertTriangle, Bot, Receipt, TrendingUp, Users } from 'lucide-react';
import { formatMoney, type CurrencySettings } from '../currency/currencyApi';
import type { ClientsDashboard } from './clientTypes';

/**
 * The KPI row (§4).
 *
 * Every money figure comes from the backend already converted, in both USD and
 * the display currency — this component picks the display one and formats it.
 * It does no arithmetic, which is why switching the display currency simply
 * refetches rather than recalculating anything here.
 */

type Tone = 'neutral' | 'success' | 'warning' | 'info' | 'dim';

function KpiCard({ icon, label, value, hint, tone = 'neutral' }: {
  icon: ReactNode; label: string; value: string; hint?: string; tone?: Tone;
}) {
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

export function ClientKpiSkeleton() {
  return (
    <div className="cl-kpi-row">
      {[0, 1, 2, 3, 4].map(index => (
        <div key={index} className="glass-panel admin-stat-card">
          <div className="cl-skeleton cl-skeleton-icon" />
          <div className="admin-stat-body" style={{ gap: 6, width: '100%' }}>
            <div className="cl-skeleton cl-skeleton-line is-short" />
            <div className="cl-skeleton cl-skeleton-line" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function ClientKpiCards({
  dashboard, settings,
}: { dashboard: ClientsDashboard; settings: CurrencySettings }) {
  const money = (value: string) => formatMoney(value, dashboard.currency, settings);

  return (
    <div className="cl-kpi-row">
      <KpiCard
        icon={<Users size={16} />}
        label="Всего клиентов"
        value={String(dashboard.totalClients)}
        hint={`Активных: ${dashboard.activeClients}`}
      />
      <KpiCard
        icon={<Bot size={16} />}
        label="Подключенные агенты"
        value={String(dashboard.connectedAgents)}
        hint={dashboard.agentsWithErrors ? `Ошибок: ${dashboard.agentsWithErrors}` : 'Ошибок нет'}
        tone={dashboard.agentsWithErrors ? 'warning' : 'info'}
      />
      <KpiCard
        icon={<TrendingUp size={16} />}
        label="MRR"
        value={money(dashboard.mrr.display)}
        // The USD figure stays visible even when displaying another currency:
        // USD is the unit the business is actually measured in.
        hint={dashboard.currency === dashboard.baseCurrency
          ? 'Ежемесячный доход'
          : `≈ ${formatMoney(dashboard.mrr.usd, dashboard.baseCurrency, settings)}`}
        tone="success"
      />
      <KpiCard
        icon={<Receipt size={16} />}
        label="К выставлению"
        value={money(dashboard.toInvoice.display)}
        hint={`${dashboard.toInvoice.count ?? 0} счетов · 30 дней`}
        tone="info"
      />
      <KpiCard
        icon={<AlertTriangle size={16} />}
        label="Просрочено"
        value={money(dashboard.overdue.display)}
        hint={`${dashboard.overdue.count ?? 0} счетов`}
        tone={Number(dashboard.overdue.usd) > 0 ? 'warning' : 'dim'}
      />
    </div>
  );
}
