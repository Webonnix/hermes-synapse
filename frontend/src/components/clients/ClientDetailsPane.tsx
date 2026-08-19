import { useEffect, useState, type ReactNode } from 'react';
import {
  ArrowLeft, Bot, Building2, CalendarClock, ExternalLink, Link2Off, Mail, Phone,
  RefreshCw, Send, Stethoscope, User,
} from 'lucide-react';
import { CurrencyAmount } from '../currency/CurrencyAmount';
import { formatMoney, type CurrencySettings } from '../currency/currencyApi';
import { AgentConnectionBadge, ClientStatusBadge, ServiceTypeBadge } from './ClientBadges';
import { FREQUENCY_LABELS, formatDate, formatRelative } from './clientLabels';
import { ClientInvoicesPane } from './ClientInvoicesPane';
import { ClientPaymentsPane } from './ClientPaymentsPane';
import type {
  AgentConnection, Client, ClientActivityEntry, ClientBillingSummary, ClientInvoice, ClientPayment,
} from './clientTypes';

/** Client card (§46–§50): overview, services, agents, billing, documents. */

type Section = 'overview' | 'services' | 'agents' | 'billing' | 'invoices' | 'payments' | 'contacts' | 'activity';

const SECTIONS: Array<{ id: Section; label: string }> = [
  { id: 'overview', label: 'Обзор' },
  { id: 'services', label: 'Услуги' },
  { id: 'agents', label: 'Агенты' },
  { id: 'billing', label: 'Billing' },
  { id: 'invoices', label: 'Счета' },
  { id: 'payments', label: 'Платежи' },
  { id: 'contacts', label: 'Контакты' },
  { id: 'activity', label: 'Активность' },
];

function InfoRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="cl-info-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function ClientDetailsPane({
  client, billing, invoices, payments, activity, settings, canSeeFinancials,
  onBack, onCheck, onReconnect, onDisconnect, onOpenAgent,
  onIssueInvoice, onMarkPaid, onCancelInvoice, onCreateInvoice, onReload,
}: {
  client: Client;
  billing: ClientBillingSummary | null;
  invoices: ClientInvoice[];
  payments: ClientPayment[];
  activity: ClientActivityEntry[];
  settings: CurrencySettings;
  canSeeFinancials: boolean;
  onBack: () => void;
  onCheck: (connection: AgentConnection) => void;
  onReconnect: (connection: AgentConnection) => void;
  onDisconnect: (connection: AgentConnection) => void;
  onOpenAgent: (agentId: string) => void;
  onIssueInvoice: (invoice: ClientInvoice) => void;
  onMarkPaid: (invoice: ClientInvoice) => void;
  onCancelInvoice: (invoice: ClientInvoice) => void;
  onCreateInvoice: () => void;
  onReload: () => void;
}) {
  const [section, setSection] = useState<Section>('overview');

  // A different client in the same pane starts at its overview rather than
  // whatever tab happened to be open for the previous one.
  useEffect(() => { setSection('overview'); }, [client.id]);

  const visibleSections = SECTIONS.filter(item =>
    canSeeFinancials || !['billing', 'invoices', 'payments'].includes(item.id));

  return (
    <div className="cl-details">
      <div className="cl-details-head">
        <button type="button" className="btn-ghost" onClick={onBack}>
          <ArrowLeft size={14} /><span>К списку</span>
        </button>
        <div className="cl-details-title">
          <span className="cl-avatar is-large" aria-hidden>
            {client.type === 'PERSON' ? <User size={16} /> : <Building2 size={16} />}
          </span>
          <div>
            <h3>{client.name}</h3>
            <small>{client.projectName || '—'}</small>
          </div>
          <ClientStatusBadge status={client.status} />
        </div>
        <button type="button" className="btn-ghost" onClick={onReload}>
          <RefreshCw size={14} /><span>Обновить</span>
        </button>
      </div>

      <nav className="admin-subnav">
        {visibleSections.map(item => (
          <button key={item.id} type="button"
                  className={section === item.id ? 'is-active' : ''}
                  onClick={() => setSection(item.id)}>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      {section === 'overview' && (
        <section className="glass-panel cl-settings-block">
          <InfoRow label="Название" value={client.name} />
          <InfoRow label="Проект" value={client.projectName || '—'} />
          <InfoRow label="Тип" value={client.type === 'PERSON' ? 'Физическое лицо' : 'Компания'} />
          <InfoRow label="Статус" value={<ClientStatusBadge status={client.status} />} />
          <InfoRow label="Ответственный" value={client.responsibleUserId || '—'} />
          <InfoRow label="Основной контакт" value={client.primaryContact?.name || '—'} />
          <InfoRow label="Дата создания" value={formatDate(client.createdAt)} />
          {client.description && <InfoRow label="Описание" value={client.description} />}
        </section>
      )}

      {section === 'services' && (
        <div className="cl-card-grid">
          {client.services.length === 0 && <div className="admin-empty">Услуг пока нет</div>}
          {client.services.map(service => (
            <article key={service.id} className="glass-panel cl-service-card">
              <header>
                <strong>{service.title || service.serviceName || 'Услуга'}</strong>
                <ServiceTypeBadge type={service.serviceType} />
              </header>
              {canSeeFinancials && service.amount !== undefined && (
                <div className="cl-service-price">
                  <CurrencyAmount
                    amount={service.amount}
                    currency={service.currency}
                    settings={settings}
                    suffix={service.billing?.frequency === 'MONTHLY' ? '/ месяц' : ''}
                  />
                </div>
              )}
              <InfoRow label="Периодичность"
                       value={service.billing ? FREQUENCY_LABELS[service.billing.frequency] : '—'} />
              <InfoRow label="Следующий счёт" value={formatDate(service.billing?.nextBillingDate)} />
              <InfoRow label="Статус" value={service.status} />
              {client.agentConnections
                .filter(connection => connection.clientServiceId === service.id)
                .map(connection => (
                  <div key={connection.id} className="cl-service-agent">
                    <AgentConnectionBadge
                      agentName={connection.agentName} agentId={connection.agentId}
                      status={connection.status} full
                    />
                  </div>
                ))}
            </article>
          ))}
        </div>
      )}

      {section === 'agents' && (
        <div className="cl-card-grid">
          {client.agentConnections.length === 0 && (
            <div className="admin-empty-cta">
              <Bot size={22} />
              <strong>Подключений пока нет</strong>
              <span>Клиент может работать и без агента — подключите его, когда потребуется.</span>
            </div>
          )}
          {client.agentConnections.map(connection => (
            <article key={connection.id} className="glass-panel cl-agent-card">
              <header>
                <strong>{connection.agentName || connection.agentId}</strong>
                <AgentConnectionBadge
                  agentName={connection.agentName} agentId={connection.agentId}
                  status={connection.status} full errorMessage={connection.errorMessage}
                />
              </header>
              <InfoRow label="Канал" value={connection.channel || connection.connectionType} />
              <InfoRow label="Последняя активность" value={formatRelative(connection.lastSeenAt)} />
              <InfoRow label="Проверка связи" value={formatRelative(connection.lastHealthCheckAt)} />
              {connection.errorMessage && (
                <div className="cl-error-note">{connection.errorCode}: {connection.errorMessage}</div>
              )}
              <div className="cl-agent-actions">
                <button type="button" className="btn-ghost" onClick={() => onOpenAgent(connection.agentId)}>
                  <ExternalLink size={13} /><span>Открыть агента</span>
                </button>
                <button type="button" className="btn-ghost" onClick={() => onCheck(connection)}>
                  <Stethoscope size={13} /><span>Проверить</span>
                </button>
                <button type="button" className="btn-ghost" onClick={() => onReconnect(connection)}>
                  <RefreshCw size={13} /><span>Переподключить</span>
                </button>
                <button type="button" className="btn-ghost danger" onClick={() => onDisconnect(connection)}>
                  <Link2Off size={13} /><span>Отключить</span>
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      {section === 'billing' && canSeeFinancials && (
        <section className="glass-panel cl-settings-block">
          {billing ? (
            <div className="cl-billing-grid">
              <div className="cl-billing-metric">
                <span>MRR</span>
                <strong>{formatMoney(billing.mrr.display, billing.currency, settings)}</strong>
                {billing.currency !== billing.baseCurrency && (
                  <small>≈ {formatMoney(billing.mrr.usd, billing.baseCurrency, settings)}</small>
                )}
              </div>
              <div className="cl-billing-metric">
                <span>Следующий счёт</span>
                <strong><CalendarClock size={14} /> {formatDate(billing.nextBillingDate)}</strong>
              </div>
              <div className="cl-billing-metric">
                <span>Задолженность</span>
                <strong className={Number(billing.outstanding.usd) > 0 ? 'cl-outstanding' : ''}>
                  {formatMoney(billing.outstanding.display, billing.currency, settings)}
                </strong>
                {billing.currency !== billing.baseCurrency && (
                  <small>≈ {formatMoney(billing.outstanding.usd, billing.baseCurrency, settings)}</small>
                )}
              </div>
              <div className="cl-billing-metric">
                <span>Оплачено</span>
                <strong>{formatMoney(billing.paid.display, billing.currency, settings)}</strong>
                {billing.currency !== billing.baseCurrency && (
                  <small>≈ {formatMoney(billing.paid.usd, billing.baseCurrency, settings)}</small>
                )}
              </div>
            </div>
          ) : <div className="admin-empty">Нет данных по биллингу</div>}
        </section>
      )}

      {section === 'invoices' && canSeeFinancials && (
        <>
          <div className="cl-pane-actions">
            <button type="button" className="btn-primary" onClick={onCreateInvoice}>
              <Send size={14} /><span>Выставить счёт</span>
            </button>
          </div>
          <ClientInvoicesPane
            invoices={invoices} loading={false} error="" settings={settings}
            onIssue={onIssueInvoice} onMarkPaid={onMarkPaid} onCancel={onCancelInvoice}
            onRetry={onReload}
            emptyHint="Выставьте счёт вручную или дождитесь автоматического по расписанию услуги."
          />
        </>
      )}

      {section === 'payments' && canSeeFinancials && (
        <ClientPaymentsPane
          payments={payments} loading={false} error="" settings={settings} onRetry={onReload}
        />
      )}

      {section === 'contacts' && (
        <div className="cl-card-grid">
          {client.contacts.length === 0 && <div className="admin-empty">Контактов пока нет</div>}
          {client.contacts.map(contact => (
            <article key={contact.id} className="glass-panel cl-contact-card">
              <header>
                <strong>{contact.name || 'Без имени'}</strong>
                {contact.isPrimary && <span className="admin-status-chip is-active"><i className="dot" />Основной</span>}
              </header>
              {contact.email && <div className="cl-info-row"><span><Mail size={12} /> Email</span><strong>{contact.email}</strong></div>}
              {contact.phone && <div className="cl-info-row"><span><Phone size={12} /> Телефон</span><strong>{contact.phone}</strong></div>}
              {contact.telegram && <div className="cl-info-row"><span>Telegram</span><strong>{contact.telegram}</strong></div>}
            </article>
          ))}
        </div>
      )}

      {section === 'activity' && (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr><th>Когда</th><th>Событие</th><th>Описание</th><th>Кто</th></tr>
            </thead>
            <tbody>
              {activity.length === 0 && (
                <tr><td colSpan={4} className="admin-empty">Событий пока нет</td></tr>
              )}
              {activity.map(entry => (
                <tr key={entry.id}>
                  <td>{new Date(entry.created_at).toLocaleString('ru-RU')}</td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: '0.72rem' }}>{entry.event_type}</td>
                  <td>{entry.summary}</td>
                  <td>{entry.actor_id || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
