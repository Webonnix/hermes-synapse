import type {
  BillingFrequency, ClientStatus, ConnectionStatus, InvoiceStatus, PaymentStatus, ServiceType,
} from './clientTypes';

/** Russian labels and badge tones for every enum the module renders.
 *
 *  Centralised so a status added on the backend fails visibly in one place
 *  rather than rendering a raw SCREAMING_CASE token in six different tables. */

export const CLIENT_STATUS_LABELS: Record<ClientStatus, string> = {
  ACTIVE: 'Активен',
  PAUSED: 'Пауза',
  COMPLETED: 'Завершён',
  OVERDUE: 'Просрочен',
  ARCHIVED: 'В архиве',
};

export const SERVICE_TYPE_LABELS: Record<ServiceType, string> = {
  ONE_TIME: 'Разовая',
  RECURRING: 'Постоянная',
};

export const FREQUENCY_LABELS: Record<BillingFrequency, string> = {
  ONE_TIME: 'Единоразово',
  MONTHLY: 'Ежемесячно',
  QUARTERLY: 'Ежеквартально',
  SEMI_ANNUAL: 'Раз в полгода',
  YEARLY: 'Ежегодно',
  CUSTOM: 'Свой интервал',
};

export const CONNECTION_STATUS_LABELS: Record<ConnectionStatus, string> = {
  CONNECTED: 'online',
  NOT_CONNECTED: 'Не подключен',
  DISCONNECTED: 'отключен',
  OFFLINE: 'offline',
  ERROR: 'ошибка',
  PAUSED: 'пауза',
};

/** Longer form for the connections table, where the column is the status
 *  itself rather than an annotation under an agent name. */
export const CONNECTION_STATUS_FULL: Record<ConnectionStatus, string> = {
  CONNECTED: 'Подключен',
  NOT_CONNECTED: 'Не подключен',
  DISCONNECTED: 'Отключен',
  OFFLINE: 'Offline',
  ERROR: 'Ошибка',
  PAUSED: 'Пауза',
};

export const PAYMENT_STATUS_LABELS: Record<PaymentStatus, string> = {
  NO_INVOICE: 'Нет счёта',
  PLANNED: 'Запланирован',
  AWAITING_PAYMENT: 'Ожидает оплаты',
  PARTIALLY_PAID: 'Частично оплачено',
  PAID: 'Оплачено',
  OVERDUE: 'Просрочено',
};

export const INVOICE_STATUS_LABELS: Record<InvoiceStatus, string> = {
  PLANNED: 'Запланирован',
  ISSUED: 'Выставлен',
  PARTIALLY_PAID: 'Частично оплачен',
  PAID: 'Оплачен',
  OVERDUE: 'Просрочен',
  CANCELLED: 'Отменён',
};

type Tone = 'is-active' | 'is-pending' | 'is-revoked' | 'is-inactive';

export const CLIENT_STATUS_TONE: Record<ClientStatus, Tone> = {
  ACTIVE: 'is-active',
  PAUSED: 'is-pending',
  COMPLETED: 'is-inactive',
  OVERDUE: 'is-revoked',
  ARCHIVED: 'is-inactive',
};

export const CONNECTION_STATUS_TONE: Record<ConnectionStatus, Tone> = {
  CONNECTED: 'is-active',
  NOT_CONNECTED: 'is-inactive',
  DISCONNECTED: 'is-inactive',
  OFFLINE: 'is-pending',
  ERROR: 'is-revoked',
  PAUSED: 'is-pending',
};

export const PAYMENT_STATUS_TONE: Record<PaymentStatus, Tone> = {
  NO_INVOICE: 'is-inactive',
  PLANNED: 'is-inactive',
  AWAITING_PAYMENT: 'is-pending',
  PARTIALLY_PAID: 'is-pending',
  PAID: 'is-active',
  OVERDUE: 'is-revoked',
};

export const INVOICE_STATUS_TONE: Record<InvoiceStatus, Tone> = {
  PLANNED: 'is-inactive',
  ISSUED: 'is-pending',
  PARTIALLY_PAID: 'is-pending',
  PAID: 'is-active',
  OVERDUE: 'is-revoked',
  CANCELLED: 'is-inactive',
};

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/** "2 минуты назад" — the form the agents block uses for last activity. */
export function formatRelative(value: string | null | undefined): string {
  if (!value) return 'нет данных';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'нет данных';
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return 'только что';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин. назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч. назад`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} дн. назад`;
  return formatDate(value);
}
