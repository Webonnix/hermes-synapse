import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, Bell, Building2, Check, ChevronLeft, ChevronRight, ExternalLink, ListChecks,
  Phone, Plus, Target, Users as MeetingIcon,
} from 'lucide-react';
import { api } from '../currency/currencyApi';
import {
  CALENDAR_STATUS_REASON_TEXT, EVENT_TYPE_COLOR, EVENT_TYPE_LABELS, EVENT_TYPE_ORDER, formatDate,
} from './clientLabels';
import { CalendarEventDrawer } from './CalendarEventDrawer';
import type { CalendarEvent, CalendarEventType, CalendarStatus, ClientTask, Deal } from './clientTypes';

/**
 * «Календарь» — everything with a date on it, in one month grid: real Google
 * Calendar events, an open CRM task's due date, and an open deal's expected
 * close date.
 *
 * The events layer is genuinely live, not a read-only mirror: creating,
 * moving or deleting an event here goes straight to Google Calendar through
 * `/api/calendar/*` (backend/main.py), which is a thin wrapper over the exact
 * same functions the chat agent's `add_calendar_event`/`update_calendar_event`/
 * `delete_calendar_event` tools call. An event added from a chat message
 * ("напомни мне позвонить Ивану завтра в 15:00") shows up here with no
 * separate sync step, because there is only the one calendar — the owner's
 * real one — behind both surfaces.
 *
 * "Напоминание" has two forms: this page, and a daily Telegram digest of
 * what's overdue/due today (`client_tasks.daily_digest_text`, wired into
 * `main.py`'s lifespan) — an event itself still relies on Google's own
 * per-event reminder, which already reaches the owner's phone.
 */

const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const MONTH_NAMES = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
];

const EVENT_TYPE_ICON: Record<CalendarEventType, typeof Target> = {
  MEETING: MeetingIcon, TASK: ListChecks, CALL: Phone, DEADLINE: AlertTriangle, TRAINING: Target, OTHER: Target,
};

function toIso(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

/** Monday-first 6-week grid covering the given month, padded with the
 *  trailing days of the previous/next month so every week is full. */
function buildMonthGrid(year: number, month: number): Date[] {
  const first = new Date(year, month, 1);
  // getDay(): 0 = Sunday. Shift so Monday is the start of the week.
  const leadingDays = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - leadingDays);
  return Array.from({ length: 42 }, (_, index) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + index));
}

function eventTime(event: CalendarEvent): string {
  if (event.allDay) return 'весь день';
  const start = new Date(event.start);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(start.getHours())}:${pad(start.getMinutes())}`;
}

interface DayItems {
  tasks: ClientTask[];
  deals: Deal[];
  events: CalendarEvent[];
}

export function ClientsCalendarPane({
  tasks, deals, loading, onCompleteTask, onOpenDeal, onCreateTask,
}: {
  tasks: ClientTask[];
  deals: Deal[];
  loading: boolean;
  onCompleteTask: (task: ClientTask) => void;
  onOpenDeal: (deal: Deal) => void;
  onCreateTask: (payload: { title: string; dueAt: string; priority: 'NORMAL' }) => Promise<void>;
}) {
  const today = useMemo(() => new Date(), []);
  const [cursor, setCursor] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));
  const [selected, setSelected] = useState(() => toIso(today));
  const [quickTitle, setQuickTitle] = useState('');
  const [adding, setAdding] = useState(false);

  const [status, setStatus] = useState<CalendarStatus | null>(null);
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingEvent, setEditingEvent] = useState<CalendarEvent | null>(null);
  const [toast, setToast] = useState('');

  const grid = useMemo(() => buildMonthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor]);
  const gridStartIso = toIso(grid[0]);
  const gridEndIso = toIso(grid[grid.length - 1]);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(''), 2600);
    return () => clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    api<CalendarStatus>('/api/calendar/status')
      .then(setStatus)
      .catch(exc => {
        // A role holding clients.tasks.view without clients.calendar.view
        // (only reachable through a hand-configured custom role — every
        // built-in one grants both together) would otherwise see this as an
        // ordinary "can't reach Google" failure, which is the wrong story.
        const message = (exc as Error).message || '';
        setStatus({
          connected: false,
          reason: message.includes('403') || message.toLowerCase().includes('прав') ? 'forbidden' : 'auth_error',
        });
      });
  }, []);

  const loadEvents = useCallback(async () => {
    if (!status?.connected) return;
    setEventsLoading(true);
    setEventsError('');
    try {
      const params = new URLSearchParams({ dateFrom: gridStartIso, dateTo: gridEndIso });
      const list = await api<CalendarEvent[]>(`/api/calendar/events?${params}`);
      setEvents(Array.isArray(list) ? list : []);
    } catch (exc) {
      setEventsError((exc as Error).message);
    } finally {
      setEventsLoading(false);
    }
  }, [status?.connected, gridStartIso, gridEndIso]);

  useEffect(() => { void loadEvents(); }, [loadEvents]);

  const itemsByDate = useMemo(() => {
    const map = new Map<string, DayItems>();
    const ensure = (iso: string) => {
      let entry = map.get(iso);
      if (!entry) { entry = { tasks: [], deals: [], events: [] }; map.set(iso, entry); }
      return entry;
    };
    tasks.forEach(task => {
      if (task.status === 'OPEN' && task.dueAt) ensure(task.dueAt.slice(0, 10)).tasks.push(task);
    });
    deals.forEach(deal => {
      if (deal.stage !== 'WON' && deal.stage !== 'LOST' && deal.expectedCloseDate) {
        ensure(deal.expectedCloseDate.slice(0, 10)).deals.push(deal);
      }
    });
    events.forEach(event => {
      if (event.start) ensure(event.start.slice(0, 10)).events.push(event);
    });
    return map;
  }, [tasks, deals, events]);

  const upcomingEvents = useMemo(() => {
    const todayIso = toIso(today);
    return events
      .filter(event => event.start.slice(0, 10) >= todayIso)
      .sort((a, b) => a.start.localeCompare(b.start))
      .slice(0, 6);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events]);

  const todayIso = toIso(today);
  const selectedItems = itemsByDate.get(selected);

  const changeMonth = (delta: number) => {
    setCursor(current => new Date(current.getFullYear(), current.getMonth() + delta, 1));
  };

  const jumpTo = (iso: string) => {
    const [year, month] = iso.split('-').map(Number);
    setCursor(new Date(year, month - 1, 1));
    setSelected(iso);
  };

  const addQuickTask = async () => {
    if (!quickTitle.trim()) return;
    setAdding(true);
    try {
      await onCreateTask({ title: quickTitle.trim(), dueAt: selected, priority: 'NORMAL' });
      setQuickTitle('');
    } finally {
      setAdding(false);
    }
  };

  const submitEvent = useCallback(async (payload: {
    title: string; date: string; time: string; endTime: string; description: string;
    eventType: CalendarEventType; location: string;
  }, eventId?: string) => {
    const body = {
      title: payload.title, date: payload.date, time: payload.time,
      endTime: payload.endTime || undefined, description: payload.description,
      eventType: payload.eventType, location: payload.location,
    };
    if (eventId) {
      await api(`/api/calendar/events/${eventId}`, { method: 'PATCH', body: JSON.stringify(body) });
      setToast('Событие обновлено');
    } else {
      await api('/api/calendar/events', { method: 'POST', body: JSON.stringify(body) });
      setToast('Событие добавлено в Google Календарь');
    }
    await loadEvents();
  }, [loadEvents]);

  const deleteEvent = useCallback(async (event: CalendarEvent) => {
    await api(`/api/calendar/events/${event.id}`, { method: 'DELETE' });
    setToast('Событие удалено');
    await loadEvents();
  }, [loadEvents]);

  const openCreateDrawer = () => { setEditingEvent(null); setDrawerOpen(true); };
  const openEditDrawer = (event: CalendarEvent) => { setEditingEvent(event); setDrawerOpen(true); };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {toast && <div className="cl-toast" role="status">{toast}</div>}

      {status && !status.connected && (
        <div className="glass-panel cl-cal-status-banner">
          <AlertTriangle size={15} />
          <div>
            <strong>{status.reason === 'forbidden' ? 'Нет доступа к календарю Google.' : 'Google Календарь не подключен.'}</strong>
            <p>
              {CALENDAR_STATUS_REASON_TEXT[status.reason ?? 'auth_error']}
              {status.reason !== 'forbidden' && (
                <>
                  {' '}Задачи и сделки на календаре видны по-прежнему; события Google появятся после того, как
                  администратор один раз выполнит вход (<code>python backend/google_auth.py</code>).
                </>
              )}
            </p>
          </div>
        </div>
      )}

      <div className="glass-panel cl-cal-reminder-note">
        <Bell size={15} />
        <span>
          Каждый день бот присылает в Telegram сводку по просроченным и сегодняшним задачам, а
          новое событие можно поставить прямо в чате — «напомни мне позвонить Ивану завтра в 15:00».
        </span>
      </div>

      <div className="cl-cal-layout">
        <section className="glass-panel cl-cal-board" aria-busy={loading || eventsLoading}>
          <header className="cl-cal-header">
            <strong>{MONTH_NAMES[cursor.getMonth()]} {cursor.getFullYear()}</strong>
            <div className="cl-cal-nav">
              <button type="button" className="icon-btn" aria-label="Предыдущий месяц" onClick={() => changeMonth(-1)}>
                <ChevronLeft size={15} />
              </button>
              <button type="button" className="btn-ghost" onClick={() => { setCursor(new Date(today.getFullYear(), today.getMonth(), 1)); setSelected(todayIso); }}>
                Сегодня
              </button>
              <button type="button" className="icon-btn" aria-label="Следующий месяц" onClick={() => changeMonth(1)}>
                <ChevronRight size={15} />
              </button>
            </div>
            {status?.connected && (
              <button type="button" className="btn-primary" style={{ marginLeft: 'auto' }} onClick={openCreateDrawer}>
                <Plus size={14} /><span>Создать событие</span>
              </button>
            )}
          </header>

          {eventsError && (
            <p className="cl-cal-status-banner is-inline">
              <AlertTriangle size={13} /><span>Не удалось загрузить события: {eventsError}</span>
            </p>
          )}

          <div className="cl-cal-weekdays" aria-hidden="true">
            {WEEKDAYS.map(day => <span key={day}>{day}</span>)}
          </div>

          <div className="cl-cal-grid" role="grid" aria-label="Календарь событий, задач и сделок">
            {grid.map(date => {
              const iso = toIso(date);
              const items = itemsByDate.get(iso);
              const outside = date.getMonth() !== cursor.getMonth();
              const overdueCount = items?.tasks.filter(task => task.overdue).length ?? 0;
              const chips: Array<{ id: string; label: string; kind: string; color?: string }> = [
                ...(items?.events ?? []).map(event => ({
                  id: event.id, label: event.title, kind: `google is-${event.eventType.toLowerCase()}`,
                  color: EVENT_TYPE_COLOR[event.eventType],
                })),
                ...(items?.tasks ?? []).map(task => ({
                  id: task.id, label: task.title, kind: task.overdue ? 'overdue' : 'task',
                })),
                ...(items?.deals ?? []).map(deal => ({ id: deal.id, label: deal.title, kind: 'deal' })),
              ];
              const visibleChips = chips.slice(0, 3);
              const hiddenCount = chips.length - visibleChips.length;
              return (
                <button
                  key={iso}
                  type="button"
                  role="gridcell"
                  className={`cl-cal-cell${outside ? ' is-outside' : ''}${iso === todayIso ? ' is-today' : ''}${iso === selected ? ' is-selected' : ''}`}
                  aria-label={`${formatDate(iso)}${chips.length ? `, ${chips.length} событий` : ''}`}
                  aria-current={iso === todayIso ? 'date' : undefined}
                  onClick={() => setSelected(iso)}
                >
                  <span className="cl-cal-cell-top">
                    <span className="cl-cal-cell-date">{date.getDate()}</span>
                    {overdueCount > 0 && <AlertTriangle size={11} className="cl-cal-overdue-flag" />}
                  </span>
                  <span className="cl-cal-chips">
                    {visibleChips.map(chip => (
                      <span key={chip.id} className={`cl-cal-chip is-${chip.kind}`}
                            style={chip.color ? { background: `${chip.color}2e`, color: chip.color } : undefined}>
                        {chip.label}
                      </span>
                    ))}
                    {hiddenCount > 0 && <span className="cl-cal-chip-more">+{hiddenCount}</span>}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="cl-cal-legend">
            {EVENT_TYPE_ORDER.map(type => (
              <span key={type}><i className="cl-cal-dot" style={{ background: EVENT_TYPE_COLOR[type] }} />{EVENT_TYPE_LABELS[type]}</span>
            ))}
            <span><i className="cl-cal-dot is-task" />Задача CRM</span>
            <span><i className="cl-cal-dot is-overdue" />Просрочена</span>
            <span><i className="cl-cal-dot is-deal" />Сделка</span>
          </div>
        </section>

        <div className="cl-cal-side">
          <aside className="glass-panel cl-cal-agenda" aria-label={`События: ${formatDate(selected)}`}>
            <div className="cl-cal-agenda-head">
              <strong>{formatDate(selected)}{selected === todayIso ? ' · сегодня' : ''}</strong>
            </div>

            {!selectedItems || (selectedItems.tasks.length === 0 && selectedItems.deals.length === 0 && selectedItems.events.length === 0) ? (
              <p className="cl-muted" style={{ margin: '10px 0' }}>На этот день ничего не запланировано.</p>
            ) : (
              <ul className="cl-cal-agenda-list">
                {(selectedItems.events ?? []).map(event => {
                  const Icon = EVENT_TYPE_ICON[event.eventType];
                  return (
                    <li key={event.id} className="cl-cal-agenda-item">
                      <button type="button" className="cl-cal-agenda-link" onClick={() => openEditDrawer(event)}>
                        <span className="cl-cal-agenda-icon" style={{ color: EVENT_TYPE_COLOR[event.eventType] }}><Icon size={13} /></span>
                        <span>{eventTime(event)} · {event.title}</span>
                      </button>
                      {event.link && (
                        <a className="cl-cal-agenda-sub" href={event.link} target="_blank" rel="noreferrer" onClick={ev => ev.stopPropagation()}>
                          <ExternalLink size={11} />Google Календарь
                        </a>
                      )}
                    </li>
                  );
                })}
                {selectedItems.tasks.map(task => (
                  <li key={task.id} className={`cl-cal-agenda-item${task.overdue ? ' is-overdue' : ''}`}>
                    <button type="button" className="cl-task-check" aria-label={`Отметить выполненной: ${task.title}`}
                            onClick={() => onCompleteTask(task)}>
                      <Check size={12} />
                    </button>
                    <span className="cl-cal-agenda-icon"><ListChecks size={13} /></span>
                    <span>{task.title}</span>
                  </li>
                ))}
                {selectedItems.deals.map(deal => (
                  <li key={deal.id} className="cl-cal-agenda-item">
                    <button type="button" className="cl-cal-agenda-link" onClick={() => onOpenDeal(deal)}>
                      <span className="cl-cal-agenda-icon"><Target size={13} /></span>
                      <span>{deal.title}</span>
                    </button>
                    {deal.companyName && <span className="cl-cal-agenda-sub"><Building2 size={11} />{deal.companyName}</span>}
                  </li>
                ))}
              </ul>
            )}

            <div className="cl-cal-quick-add">
              <input
                className="form-input" placeholder="Добавить задачу на этот день…" value={quickTitle}
                onChange={ev => setQuickTitle(ev.target.value)}
                onKeyDown={ev => { if (ev.key === 'Enter') void addQuickTask(); }}
              />
              <button type="button" className="icon-btn" aria-label="Добавить задачу" disabled={!quickTitle.trim() || adding}
                      onClick={() => void addQuickTask()}>
                <Plus size={15} />
              </button>
            </div>
          </aside>

          {status?.connected && (
            <aside className="glass-panel cl-cal-upcoming" aria-label="Предстоящие события">
              <strong>Предстоящие события</strong>
              {upcomingEvents.length === 0 ? (
                <p className="cl-muted" style={{ margin: '10px 0' }}>Ничего не запланировано.</p>
              ) : (
                <ul className="cl-cal-upcoming-list">
                  {upcomingEvents.map(event => (
                    <li key={event.id}>
                      <button type="button" onClick={() => jumpTo(event.start.slice(0, 10))}>
                        <span className="cl-cal-upcoming-date">{formatDate(event.start)}</span>
                        <span className="cl-cal-upcoming-title">{eventTime(event)} · {event.title}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </aside>
          )}
        </div>
      </div>

      <CalendarEventDrawer
        open={drawerOpen}
        event={editingEvent}
        defaultDate={selected}
        onClose={() => setDrawerOpen(false)}
        onSubmit={submitEvent}
        onDelete={deleteEvent}
      />
    </div>
  );
}

export default ClientsCalendarPane;
