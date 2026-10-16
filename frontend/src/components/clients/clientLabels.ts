import type {
  BillingFrequency, CalendarEventType, ClientStatus, ConnectionStatus, DealStage, InvoiceStatus,
  PaymentStatus, ServiceType, TaskPriority, TaskStatus,
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

// ── CRM: sales pipeline ──────────────────────────────────────────────────────

/** Kanban column order — the one place that order is decided; everything else
 *  (the board, the stage picker, the summary cards) iterates this. */
export const DEAL_STAGE_ORDER: DealStage[] = [
  'NEW', 'CONTACTED', 'QUALIFIED', 'PROPOSAL', 'NEGOTIATION', 'WON', 'LOST',
];

export const DEAL_STAGE_LABELS: Record<DealStage, string> = {
  NEW: 'Новый', CONTACTED: 'Связались', QUALIFIED: 'Квалификация', PROPOSAL: 'Предложение',
  NEGOTIATION: 'Переговоры', WON: 'Выигран', LOST: 'Проигран',
};

export const DEAL_STAGE_TONE: Record<DealStage, Tone> = {
  NEW: 'is-inactive', CONTACTED: 'is-pending', QUALIFIED: 'is-pending', PROPOSAL: 'is-pending',
  NEGOTIATION: 'is-pending', WON: 'is-active', LOST: 'is-revoked',
};

export const DEAL_SOURCES = ['Сайт', 'Реферал', 'Соцсети', 'Холодный контакт', 'Партнёр', 'Другое'];

// ── client country ───────────────────────────────────────────────────────────

/** Suggestions only — the field itself is free text, so a client based
 *  somewhere off this list is never blocked, just not autocompleted. */
export const COMMON_COUNTRIES = [
  'Беларусь', 'Россия', 'Казахстан', 'Украина', 'Узбекистан', 'Азербайджан',
  'Армения', 'Кыргызстан', 'Молдова', 'Грузия', 'Польша', 'Литва', 'Латвия',
  'Эстония', 'Германия', 'США', 'ОАЭ',
];

/** No country set — the group every unassigned client falls into, always
 *  sorted last regardless of locale ordering. */
export const NO_COUNTRY_LABEL = 'Без страны';

// ── Календарь (Google Calendar) ──────────────────────────────────────────────

/** Picker order — legend and the type selector in the event drawer both walk
 *  this array, so the two are never in a different order by accident. */
export const EVENT_TYPE_ORDER: CalendarEventType[] = ['MEETING', 'TASK', 'CALL', 'DEADLINE', 'TRAINING', 'OTHER'];

export const EVENT_TYPE_LABELS: Record<CalendarEventType, string> = {
  MEETING: 'Встреча', TASK: 'Задача', CALL: 'Звонок', DEADLINE: 'Дедлайн', TRAINING: 'Обучение', OTHER: 'Другое',
};

/** Matches backend/tools.py's CALENDAR_EVENT_COLORS mapping to Google's own
 *  colorId palette, so the dot shown here is the same colour the event has
 *  in the owner's actual Google Calendar app — not a second, unrelated
 *  colour scheme invented just for this dashboard. */
export const EVENT_TYPE_COLOR: Record<CalendarEventType, string> = {
  MEETING: '#9277ff', TASK: '#5ee1ff', CALL: '#35d49a', DEADLINE: '#f05d75', TRAINING: '#f6b84a', OTHER: '#8c94a3',
};

export const CALENDAR_STATUS_REASON_TEXT: Record<string, string> = {
  no_credentials: 'На сервере нет google_credentials.json — учётные данные приложения Google ещё не настроены.',
  no_token: 'Google не авторизован — токен доступа не найден.',
  auth_error: 'Авторизация не выполнена или истекла.',
  forbidden: 'Недостаточно прав для просмотра календаря Google.',
};

// ── CRM: tasks ───────────────────────────────────────────────────────────────

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  OPEN: 'Открыта', DONE: 'Выполнена', CANCELLED: 'Отменена',
};

export const TASK_PRIORITY_LABELS: Record<TaskPriority, string> = {
  LOW: 'Низкий', NORMAL: 'Обычный', HIGH: 'Высокий',
};

/** "Сегодня" / "Завтра" / "Просрочено на 2 дн." — a due date reads as a
 *  relation to today, not as a calendar date, everywhere a task shows it. */
export function formatDueDate(value: string | null | undefined): string {
  if (!value) return 'Без срока';
  const due = new Date(`${value.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(due.getTime())) return formatDate(value);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((due.getTime() - today.getTime()) / 86400000);
  if (days === 0) return 'Сегодня';
  if (days === 1) return 'Завтра';
  if (days === -1) return 'Вчера';
  if (days > 1 && days <= 6) return `Через ${days} дн.`;
  if (days < -1) return `Просрочено на ${Math.abs(days)} дн.`;
  return formatDate(value);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/** Whole days since a timestamp — the kanban card's "N дней в стадии" and the
 *  staleness flag it earns past a week share this one calculation. */
export function daysSince(value: string | null | undefined): number {
  if (!value) return 0;
  const then = new Date(value);
  if (Number.isNaN(then.getTime())) return 0;
  return Math.max(0, Math.floor((Date.now() - then.getTime()) / 86400000));
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
