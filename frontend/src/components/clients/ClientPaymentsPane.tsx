import { Wallet } from 'lucide-react';
import { formatMoney, type CurrencySettings } from '../currency/currencyApi';
import { formatDate } from './clientLabels';
import type { ClientPayment } from './clientTypes';

/** «Платежи». Each payment shows what actually arrived, in the currency it
 *  arrived in, alongside the USD figure it was normalized to at that moment. */
export function ClientPaymentsPane({
  payments, loading, error, settings, onRetry,
}: {
  payments: ClientPayment[];
  loading: boolean;
  error: string;
  settings: CurrencySettings;
  onRetry: () => void;
}) {
  if (error) {
    return (
      <div className="cl-error-banner">
        <span>Не удалось загрузить платежи</span>
        <button type="button" className="btn-ghost" onClick={onRetry}>Повторить</button>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="admin-table-wrap">
        {[0, 1, 2].map(index => <div key={index} className="cl-skeleton cl-skeleton-row" />)}
      </div>
    );
  }

  if (payments.length === 0) {
    return (
      <div className="admin-empty-cta">
        <Wallet size={22} />
        <strong>Платежей пока нет</strong>
        <span>Зарегистрируйте оплату по счёту — она появится здесь.</span>
      </div>
    );
  }

  return (
    <div className="admin-table-wrap">
      <table className="admin-table">
        <thead>
          <tr>
            <th>Дата</th><th>Клиент</th><th>Счёт</th><th>Сумма</th>
            <th>В USD</th><th>Способ</th><th>Комментарий</th>
          </tr>
        </thead>
        <tbody>
          {payments.map(payment => (
            <tr key={payment.id}>
              <td>{formatDate(payment.paymentDate)}</td>
              <td><strong>{payment.clientName || '—'}</strong></td>
              <td style={{ fontFamily: 'var(--font-mono)', fontSize: '0.75rem' }}>
                {payment.invoiceNumber || '—'}
              </td>
              <td>{formatMoney(payment.amount, payment.currency, settings)}</td>
              <td className="cl-muted">
                {formatMoney(payment.normalizedUsdAmount, settings.baseCurrency, settings)}
              </td>
              <td>{payment.paymentMethod || '—'}</td>
              <td>{payment.comment || payment.reference || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
