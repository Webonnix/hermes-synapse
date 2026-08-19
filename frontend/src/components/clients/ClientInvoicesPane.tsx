import { useState } from 'react';
import { Ban, CheckCircle2, FileText, Send } from 'lucide-react';
import { formatMoney, type CurrencySettings } from '../currency/currencyApi';
import { CurrencyAmount } from '../currency/CurrencyAmount';
import { InvoiceStatusBadge } from './ClientBadges';
import { formatDate } from './clientLabels';
import type { ClientInvoice } from './clientTypes';

/**
 * «Счета».
 *
 * The amount column shows the invoice's *frozen* display amount, not a live
 * conversion: an issued invoice is a record of what was billed at the rate that
 * applied then, and re-converting it today would quietly restate history.
 */
export function ClientInvoicesPane({
  invoices, loading, error, settings, onIssue, onMarkPaid, onCancel, onRetry, emptyHint,
}: {
  invoices: ClientInvoice[];
  loading: boolean;
  error: string;
  settings: CurrencySettings;
  onIssue: (invoice: ClientInvoice) => void;
  onMarkPaid: (invoice: ClientInvoice) => void;
  onCancel: (invoice: ClientInvoice) => void;
  onRetry: () => void;
  emptyHint?: string;
}) {
  const [busy, setBusy] = useState('');

  const run = async (id: string, action: () => void) => {
    setBusy(id);
    try {
      await action();
    } finally {
      setBusy('');
    }
  };

  if (error) {
    return (
      <div className="cl-error-banner">
        <span>Не удалось загрузить счета</span>
        <button type="button" className="btn-ghost" onClick={onRetry}>Повторить</button>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="admin-table-wrap">
        {[0, 1, 2, 3].map(index => <div key={index} className="cl-skeleton cl-skeleton-row" />)}
      </div>
    );
  }

  if (invoices.length === 0) {
    return (
      <div className="admin-empty-cta">
        <FileText size={22} />
        <strong>Счетов пока нет</strong>
        <span>{emptyHint || 'Счета появятся автоматически по расписанию постоянных услуг.'}</span>
      </div>
    );
  }

  return (
    <div className="admin-table-wrap">
      <table className="admin-table">
        <thead>
          <tr>
            <th>Счёт</th><th>Клиент</th><th>Период</th><th>Сумма</th><th>Оплачено</th>
            <th>Остаток</th><th>Дата</th><th>Срок оплаты</th><th>Статус</th><th aria-label="Действия" />
          </tr>
        </thead>
        <tbody>
          {invoices.map(invoice => (
            <tr key={invoice.id}>
              <td>
                <strong style={{ fontFamily: 'var(--font-mono)', fontSize: '0.78rem' }}>
                  {invoice.invoiceNumber}
                </strong>
                {invoice.origin === 'scheduler' && <small className="cl-origin-chip">авто</small>}
              </td>
              <td>
                <div className="cl-client-names">
                  <strong>{invoice.clientName || '—'}</strong>
                  <small>{invoice.serviceTitle || invoice.projectName || ''}</small>
                </div>
              </td>
              <td>{invoice.billingPeriod || '—'}</td>
              <td>
                <CurrencyAmount
                  amount={invoice.sourceAmount}
                  currency={invoice.sourceCurrency}
                  displayCurrency={invoice.displayCurrency ?? undefined}
                  displayAmount={invoice.displayAmount}
                  settings={settings}
                />
              </td>
              <td>{formatMoney(invoice.paidAmount, invoice.sourceCurrency, settings)}</td>
              <td>
                <span className={Number(invoice.outstandingAmount) > 0 ? 'cl-outstanding' : 'cl-muted'}>
                  {formatMoney(invoice.outstandingAmount, invoice.sourceCurrency, settings)}
                </span>
              </td>
              <td>{formatDate(invoice.invoiceDate)}</td>
              <td>{formatDate(invoice.dueDate)}</td>
              <td><InvoiceStatusBadge status={invoice.status} /></td>
              <td>
                <div className="cl-row-actions">
                  {invoice.status === 'PLANNED' && (
                    <button type="button" className="icon-btn" title="Выставить"
                            aria-label={`Выставить счёт ${invoice.invoiceNumber}`}
                            disabled={busy === invoice.id}
                            onClick={() => run(invoice.id, () => onIssue(invoice))}>
                      <Send size={14} />
                    </button>
                  )}
                  {invoice.status !== 'PAID' && invoice.status !== 'CANCELLED' && (
                    <button type="button" className="icon-btn" title="Отметить оплаченным"
                            aria-label={`Отметить оплаченным ${invoice.invoiceNumber}`}
                            disabled={busy === invoice.id}
                            onClick={() => run(invoice.id, () => onMarkPaid(invoice))}>
                      <CheckCircle2 size={14} />
                    </button>
                  )}
                  {invoice.status !== 'PAID' && invoice.status !== 'CANCELLED' && (
                    <button type="button" className="icon-btn danger" title="Отменить"
                            aria-label={`Отменить счёт ${invoice.invoiceNumber}`}
                            disabled={busy === invoice.id}
                            onClick={() => run(invoice.id, () => onCancel(invoice))}>
                      <Ban size={14} />
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
