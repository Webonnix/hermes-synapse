import { ArrowDown, ArrowUp, ChevronDown, Eye, Pencil, UserPlus } from 'lucide-react';
import { CurrencyAmount } from '../currency/CurrencyAmount';
import type { CurrencySettings } from '../currency/currencyApi';
import { AgentConnectionBadge, PaymentStatusBadge, ServiceTypeBadge } from './ClientBadges';
import { FREQUENCY_LABELS, formatDate } from './clientLabels';
import type { Client } from './clientTypes';

/** Frequency shown under the amount ("/ мес."), so the price column reads as a
 *  rate rather than a one-off number. */
const FREQUENCY_SUFFIX: Record<string, string> = {
  MONTHLY: '/ мес.',
  QUARTERLY: '/ кв.',
  SEMI_ANNUAL: '/ полгода',
  YEARLY: '/ год',
  CUSTOM: '/ период',
  ONE_TIME: '',
};

export type SortKey = 'name' | 'amount' | 'nextBillingDate' | 'status' | 'createdAt';

function SortHeader({ label, column, sort, order, onSort }: {
  label: string;
  column: SortKey;
  sort: SortKey;
  order: 'asc' | 'desc';
  onSort: (column: SortKey) => void;
}) {
  const active = sort === column;
  return (
    <th>
      <button type="button" className={`cl-sort-btn${active ? ' is-active' : ''}`} onClick={() => onSort(column)}>
        <span>{label}</span>
        {active && (order === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
      </button>
    </th>
  );
}

export function ClientsTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="admin-table-wrap">
      <table className="admin-table">
        <tbody>
          {Array.from({ length: rows }, (_, index) => (
            <tr key={index}>
              <td><div className="cl-skeleton cl-skeleton-row" /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ClientsTable({
  clients, settings, sort, order, canSeeFinancials, onSort, onOpen, onEdit, onConnectAgent,
}: {
  clients: Client[];
  settings: CurrencySettings;
  sort: SortKey;
  order: 'asc' | 'desc';
  /** False for roles without `clients.financials.view` — the money columns are
   *  not rendered at all, and the backend did not send the numbers either. */
  canSeeFinancials: boolean;
  onSort: (column: SortKey) => void;
  onOpen: (client: Client) => void;
  onEdit: (client: Client) => void;
  onConnectAgent: (client: Client) => void;
}) {
  return (
    <div className="admin-table-wrap">
      <table className="admin-table cl-table">
        <thead>
          <tr>
            <SortHeader label="Клиент / Проект" column="name" sort={sort} order={order} onSort={onSort} />
            <th>Контакты</th>
            <th>Услуга</th>
            <th>Тип услуги</th>
            <th>Подключен агент</th>
            {canSeeFinancials && (
              <SortHeader label="Стоимость" column="amount" sort={sort} order={order} onSort={onSort} />
            )}
            <th>Выставляется</th>
            <SortHeader label="Следующий счёт" column="nextBillingDate" sort={sort} order={order} onSort={onSort} />
            {canSeeFinancials && <th>Оплата</th>}
            <th aria-label="Действия" />
          </tr>
        </thead>
        <tbody>
          {clients.map(client => {
            const service = client.primaryService;
            const connection = client.primaryConnection;
            const frequency = service?.billing?.frequency ?? client.billingFrequency ?? undefined;
            return (
              <tr key={client.id}>
                <td>
                  <button type="button" className="cl-client-cell" onClick={() => onOpen(client)}>
                    <span className="cl-avatar" aria-hidden>{(client.name || '?').charAt(0).toUpperCase()}</span>
                    <span className="cl-client-names">
                      <strong>{client.name}</strong>
                      <small>{client.projectName || '—'}</small>
                    </span>
                  </button>
                </td>

                <td>
                  {client.primaryContact ? (
                    <div className="cl-contact-cell">
                      <strong>{client.primaryContact.name || '—'}</strong>
                      {client.primaryContact.email && <small>{client.primaryContact.email}</small>}
                      {client.primaryContact.phone && <small>{client.primaryContact.phone}</small>}
                    </div>
                  ) : <span className="cl-muted">—</span>}
                </td>

                <td>
                  {service ? (
                    <div className="cl-service-cell">
                      <span className="cl-service-chip">{service.serviceName || 'Услуга'}</span>
                      <small>{service.title || service.description || ''}</small>
                    </div>
                  ) : <span className="cl-muted">Нет услуг</span>}
                </td>

                <td>{service ? <ServiceTypeBadge type={service.serviceType} /> : <span className="cl-muted">—</span>}</td>

                <td>
                  <AgentConnectionBadge
                    agentName={connection?.agentName}
                    agentId={connection?.agentId}
                    status={connection?.status}
                    errorMessage={connection?.errorMessage}
                  />
                </td>

                {canSeeFinancials && (
                  <td>
                    {service && service.amount !== undefined ? (
                      <CurrencyAmount
                        amount={service.amount}
                        currency={service.currency}
                        displayAmount={client.primaryAmountDisplay}
                        suffix={frequency ? FREQUENCY_SUFFIX[frequency] : ''}
                        settings={settings}
                      />
                    ) : <span className="cl-muted">—</span>}
                  </td>
                )}

                <td>
                  <span className="cl-frequency">
                    {frequency ? FREQUENCY_LABELS[frequency] : '—'}
                  </span>
                </td>

                <td>{formatDate(client.nextBillingDate)}</td>

                {canSeeFinancials && <td><PaymentStatusBadge status={client.paymentStatus} /></td>}

                <td>
                  <div className="cl-row-actions">
                    <button type="button" className="icon-btn" title="Открыть карточку" aria-label={`Открыть ${client.name}`} onClick={() => onOpen(client)}>
                      <Eye size={14} />
                    </button>
                    <button type="button" className="icon-btn" title="Редактировать" aria-label={`Редактировать ${client.name}`} onClick={() => onEdit(client)}>
                      <Pencil size={14} />
                    </button>
                    <button type="button" className="icon-btn" title="Подключить агента" aria-label={`Подключить агента для ${client.name}`} onClick={() => onConnectAgent(client)}>
                      <UserPlus size={14} />
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ClientsPagination({
  page, pages, limit, total, onPage, onLimit,
}: {
  page: number; pages: number; limit: number; total: number;
  onPage: (next: number) => void;
  onLimit: (next: number) => void;
}) {
  const first = total === 0 ? 0 : (page - 1) * limit + 1;
  const last = Math.min(page * limit, total);
  return (
    <div className="cl-pagination">
      <span className="cl-muted">Показано {first}–{last} из {total}</span>
      <div className="cl-pagination-controls">
        <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          Назад
        </button>
        <span className="cl-page-indicator">{page} / {Math.max(pages, 1)}</span>
        <button type="button" className="btn-ghost" disabled={page >= pages} onClick={() => onPage(page + 1)}>
          Вперёд
        </button>
        <div className="cl-select-wrap">
          <select
            className="form-input"
            value={limit}
            aria-label="Записей на странице"
            onChange={event => onLimit(Number(event.target.value))}
          >
            {[25, 50, 100].map(size => <option key={size} value={size}>{size} на странице</option>)}
          </select>
          <ChevronDown size={13} aria-hidden />
        </div>
      </div>
    </div>
  );
}
