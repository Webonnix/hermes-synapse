import type { ClientStatus, ConnectionStatus, InvoiceStatus, PaymentStatus, ServiceType } from './clientTypes';
import {
  CLIENT_STATUS_LABELS, CLIENT_STATUS_TONE, CONNECTION_STATUS_FULL, CONNECTION_STATUS_LABELS,
  CONNECTION_STATUS_TONE, INVOICE_STATUS_LABELS, INVOICE_STATUS_TONE, PAYMENT_STATUS_LABELS,
  PAYMENT_STATUS_TONE, SERVICE_TYPE_LABELS,
} from './clientLabels';

/** Status pills, all on the existing `.admin-status-chip` from the agents
 *  admin so the clients tables read as the same product. */

export function ClientStatusBadge({ status }: { status: ClientStatus }) {
  return (
    <span className={`admin-status-chip ${CLIENT_STATUS_TONE[status] ?? 'is-inactive'}`}>
      <i className="dot" />{CLIENT_STATUS_LABELS[status] ?? status}
    </span>
  );
}

export function ServiceTypeBadge({ type }: { type: ServiceType }) {
  return (
    <span className={`cl-type-chip${type === 'RECURRING' ? ' is-recurring' : ' is-onetime'}`}>
      {SERVICE_TYPE_LABELS[type] ?? type}
    </span>
  );
}

export function PaymentStatusBadge({ status }: { status?: PaymentStatus }) {
  if (!status) return <span className="cl-muted">—</span>;
  return (
    <span className={`admin-status-chip ${PAYMENT_STATUS_TONE[status] ?? 'is-inactive'}`}>
      <i className="dot" />{PAYMENT_STATUS_LABELS[status] ?? status}
    </span>
  );
}

export function InvoiceStatusBadge({ status }: { status: InvoiceStatus }) {
  return (
    <span className={`admin-status-chip ${INVOICE_STATUS_TONE[status] ?? 'is-inactive'}`}>
      <i className="dot" />{INVOICE_STATUS_LABELS[status] ?? status}
    </span>
  );
}

/**
 * The agent column (§20).
 *
 * Two lines — which agent, and what state it is in — because "Agent #12" alone
 * does not say whether anything is being delivered, and "online" alone does not
 * say by whom. With no connection at all it collapses to one honest line.
 */
export function AgentConnectionBadge({
  agentName, agentId, status, full = false, errorMessage,
}: {
  agentName?: string | null;
  agentId?: string | null;
  status?: ConnectionStatus;
  full?: boolean;
  errorMessage?: string | null;
}) {
  const effective: ConnectionStatus = status ?? 'NOT_CONNECTED';
  const tone = CONNECTION_STATUS_TONE[effective] ?? 'is-inactive';

  if (effective === 'NOT_CONNECTED' && !agentId) {
    return (
      <span className={`cl-agent-badge ${tone}`}>
        <i className="dot" />
        <span className="cl-agent-name">Не подключен</span>
      </span>
    );
  }

  return (
    <span className={`cl-agent-badge ${tone}`} title={errorMessage || undefined}>
      <i className="dot" />
      <span className="cl-agent-body">
        <span className="cl-agent-name">{agentName || agentId}</span>
        <small>{(full ? CONNECTION_STATUS_FULL : CONNECTION_STATUS_LABELS)[effective] ?? effective}</small>
      </span>
    </span>
  );
}
