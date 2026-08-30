import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle, Bell, Building2, Check, ChevronLeft, ChevronRight, ExternalLink, ListChecks,
  MapPin, MoreHorizontal, Pencil, Phone, Plus, Target, Trash2, Users as MeetingIcon,
} from 'lucide-react';
import { api } from '../currency/currencyApi';
import {
  CALENDAR_STATUS_REASON_TEXT, EVENT_TYPE_COLOR, EVENT_TYPE_LABELS, EVENT_TYPE_ORDER, formatDate,
} from './clientLabels';
import { CalendarEventDrawer } from './CalendarEventDrawer';
import type { CalendarEvent, CalendarEventType, CalendarStatus, ClientTask, Deal } from './clientTypes';

/**
 * «Календарь» — everything with a date on it, in one month grid plus a
 * sidebar: a mini-calendar, the selected day's agenda, and an upcoming-events
 * list — real Google Calendar events, an open CRM task's due date, and an
 * open deal's expected close date.
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
const UPCOMING_WINDOW_DAYS = 60;
const UPCOMING_COLLAPSED_COUNT = 6;
const UPCOMING_EXPANDED_COUNT = 30;

const EVENT_TYPE_ICON: Record<CalendarEventType, typeof Target> = {
  MEETING: MeetingIcon, TASK: ListChecks, CALL: Phone, DEADLINE: AlertTriangle, TRAINING: Target, OTHER: Target,
};

function toIso(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function addDays(date: Date, days: number): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
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

/** Start–end, e.g. "10:00–11:30" — the agenda card's headline, richer than
 *  the grid's own single-time line because there's room for it. */
function eventTimeRange(event: CalendarEvent): string {
  if (event.allDay) return 'Весь день';
  const pad = (n: number) => String(n).padStart(2, '0');
  const start = new Date(event.start);
  const startLabel = `${pad(start.getHours())}:${pad(start.getMinutes())}`;
  if (!event.end) return startLabel;
  const end = new Date(event.end);
  if (Number.isNaN(end.getTime())) return startLabel;
  const endLabel = `${pad(end.getHours())}:${pad(end.getMinutes())}`;
  return endLabel === startLabel ? startLabel : `${startLabel}–${endLabel}`;
}

interface DayItems {
  tasks: ClientTask[];
  deals: Deal[];
  events: CalendarEvent[];
}

/** One "HH:MM Title" (or bare-title, for a task/deal) line rendered inside a
 *  day cell. Deliberately a single leaf node with only text as direct
 *  children — a wrapper around it would never itself match a text query
 *  (testing-library only looks at a node's own direct text children), but
 *  keeping it flat also keeps the DOM here as light as 42 cells × a few
 *  lines each deserves. */
interface DayLine {
  id: string;
  text: string;
  className: string;
  color?: string;
}

/** Row overflow menu for one Google event in the agenda panel: Изменить /
 *  Удалить. Rendered into a portal for the same reason SmmPublicationsPanel's
 *  RowMenu is — `.cl-cal-side` doesn't clip, but a `position: fixed` popover
 *  anchored to a button inside a `position: sticky` sidebar is one scroll
 *  event away from drifting off its anchor otherwise. */
function EventKebabMenu({ event, onEdit, onDelete }: {
  event: CalendarEvent;
  onEdit: (event: CalendarEvent) => void;
  onDelete: (event: CalendarEvent) => Promise<void>;
}) {
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const open = anchor !== null;

  const place = useCallback(() => {
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) setAnchor({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const close = () => setAnchor(null);
    const onPointer = (event_: MouseEvent) => {
      const target = event_.target as Node;
      if (!buttonRef.current?.contains(target) && !menuRef.current?.contains(target)) close();
    };
    const onKey = (event_: KeyboardEvent) => {
      if (event_.key === 'Escape') { close(); buttonRef.current?.focus(); }
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [open]);

  const remove = async () => {
    setAnchor(null);
    if (!window.confirm(`Удалить событие «${event.title}» из Google Календаря?`)) return;
    await onDelete(event);
  };

  return (
    <>
      <button
        ref={buttonRef} type="button" className="icon-btn" aria-haspopup="menu" aria-expanded={open}
        aria-label={`Действия: ${event.title}`}
        onClick={ev => { ev.stopPropagation(); if (open) { setAnchor(null); } else { place(); } }}
      >
        <MoreHorizontal size={14} />
      </button>
      {open && createPortal(
        <div
          ref={menuRef} role="menu" className="glass-panel"
          style={{
            position: 'fixed', top: anchor.top, right: anchor.right, zIndex: 70, minWidth: 172,
            padding: 6, display: 'flex', flexDirection: 'column', gap: 2,
          }}
        >
          <button type="button" role="menuitem" className="btn-ghost" style={{ justifyContent: 'flex-start' }}
                  onClick={() => { setAnchor(null); onEdit(event); }}>
            <Pencil size={13} /><span>Изменить</span>
          </button>
          <button type="button" role="menuitem" className="btn-ghost danger" style={{ justifyContent: 'flex-start' }}
                  onClick={() => void remove()}>
            <Trash2 size={13} /><span>Удалить</span>
          </button>
        </div>,
        document.body,
      )}
    </>
  );
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
  const [upcoming, setUpcoming] = useState<CalendarEvent[]>([]);
  const [upcomingExpanded, setUpcomingExpanded] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingEvent, setEditingEvent] = useState<CalendarEvent | null>(null);
  const [toast, setToast] = useState('');

  const grid = useMemo(() => buildMonthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor]);
  const gridStartIso = toIso(grid[0]);
  const gridEndIso = toIso(grid[grid.length - 1]);
  // A fixed forward-looking window, independent of whatever month the grid
  // happens to be showing — the "Предстоящие события" panel is about what's
  // actually next, not "next in this particular month".
  const upcomingFromIso = toIso(today);
  const upcomingToIso = toIso(addDays(today, UPCOMING_WINDOW_DAYS));

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

  const loadUpcoming = useCallback(async () => {
    if (!status?.connected) return;
    try {
      const params = new URLSearchParams({ dateFrom: upcomingFromIso, dateTo: upcomingToIso });
      const list = await api<CalendarEvent[]>(`/api/calendar/events?${params}`);
      setUpcoming(Array.isArray(list) ? list : []);
    } catch {
      // A non-critical, secondary panel — the grid's own status/error banner
      // already covers "Google isn't reachable"; no need to say it twice.
    }
  }, [status?.connected, upcomingFromIso, upcomingToIso]);

  useEffect(() => { void loadEvents(); }, [loadEvents]);
  useEffect(() => { void loadUpcoming(); }, [loadUpcoming]);

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
    const sorted = upcoming
      .filter(event => event.start.slice(0, 10) >= todayIso)
      .sort((a, b) => a.start.localeCompare(b.start));
    return sorted.slice(0, upcomingExpanded ? UPCOMING_EXPANDED_COUNT : UPCOMING_COLLAPSED_COUNT);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upcoming, upcomingExpanded]);
  const upcomingTotal = useMemo(() => {
    const todayIso = toIso(today);
    return upcoming.filter(event => event.start.slice(0, 10) >= todayIso).length;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upcoming]);

  const todayIso = toIso(today);
  const selectedItems = itemsByDate.get(selected);
  const selectedCount = selectedItems ? selectedItems.events.length + selectedItems.tasks.length + selectedItems.deals.length : 0;

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
    await Promise.all([loadEvents(), loadUpcoming()]);
  }, [loadEvents, loadUpcoming]);

  const deleteEvent = useCallback(async (event: CalendarEvent) => {
    await api(`/api/calendar/events/${event.id}`, { method: 'DELETE' });
    setToast('Событие удалено');
    await Promise.all([loadEvents(), loadUpcoming()]);
  }, [loadEvents, loadUpcoming]);

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
              const lines: DayLine[] = [
                ...(items?.events ?? []).map(event => ({
                  id: event.id,
                  text: `${eventTime(event)} ${event.title}`,
                  className: `cl-cal-line is-google is-${event.eventType.toLowerCase()}`,
                  color: EVENT_TYPE_COLOR[event.eventType],
                })),
                ...(items?.tasks ?? []).map(task => ({
                  id: task.id, text: task.title,
                  className: `cl-cal-line ${task.overdue ? 'is-overdue' : 'is-task'}`,
                })),
                ...(items?.deals ?? []).map(deal => ({ id: deal.id, text: deal.title, className: 'cl-cal-line is-deal' })),
              ];
              const visibleLines = lines.slice(0, 3);
              const hiddenCount = lines.length - visibleLines.length;
              return (
                <button
                  key={iso}
                  type="button"
                  role="gridcell"
                  className={`cl-cal-cell${outside ? ' is-outside' : ''}${iso === todayIso ? ' is-today' : ''}${iso === selected ? ' is-selected' : ''}`}
                  aria-label={`${formatDate(iso)}${lines.length ? `, ${lines.length} событий` : ''}`}
                  aria-current={iso === todayIso ? 'date' : undefined}
                  onClick={() => setSelected(iso)}
                >
                  <span className="cl-cal-cell-top">
                    <span className="cl-cal-cell-date">{date.getDate()}</span>
                    {overdueCount > 0 && <AlertTriangle size={11} className="cl-cal-overdue-flag" />}
                  </span>
                  <span className="cl-cal-lines">
                    {visibleLines.map(line => (
                      <span key={line.id} className={line.className}
                            style={line.color ? { '--cal-line-color': line.color, color: line.color } as CSSProperties : undefined}>
                        {line.text}
                      </span>
                    ))}
                    {hiddenCount > 0 && <span className="cl-cal-line-more">+{hiddenCount} ещё</span>}
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
          <aside className="glass-panel cl-cal-mini" aria-label="Мини-календарь">
            <div className="cl-cal-mini-head">
              <button type="button" className="icon-btn" aria-label="Предыдущий месяц" onClick={() => changeMonth(-1)}>
                <ChevronLeft size={13} />
              </button>
              <strong>{MONTH_NAMES[cursor.getMonth()]} {cursor.getFullYear()}</strong>
              <button type="button" className="icon-btn" aria-label="Следующий месяц" onClick={() => changeMonth(1)}>
                <ChevronRight size={13} />
              </button>
            </div>
            <div className="cl-cal-mini-weekdays" aria-hidden="true">
              {WEEKDAYS.map(day => <span key={day}>{day[0]}</span>)}
            </div>
            <div className="cl-cal-mini-grid">
              {grid.map(date => {
                const iso = toIso(date);
                const outside = date.getMonth() !== cursor.getMonth();
                const hasItems = itemsByDate.has(iso);
                return (
                  <button
                    key={iso} type="button"
                    className={`cl-cal-mini-day${outside ? ' is-outside' : ''}${iso === todayIso ? ' is-today' : ''}${iso === selected ? ' is-selected' : ''}`}
                    aria-label={formatDate(iso)}
                    onClick={() => setSelected(iso)}
                  >
                    {date.getDate()}
                    {hasItems && <i className="cl-cal-mini-dot" />}
                  </button>
                );
              })}
            </div>
          </aside>

          <aside className="glass-panel cl-cal-agenda" aria-label={`События: ${formatDate(selected)}`}>
            <div className="cl-cal-agenda-head">
              <strong>{formatDate(selected)}{selected === todayIso ? ' · сегодня' : ''}</strong>
              {selectedCount > 0 && <span className="cl-cal-agenda-count">{selectedCount}</span>}
            </div>

            {!selectedItems || selectedCount === 0 ? (
              <p className="cl-muted" style={{ margin: '10px 0' }}>На этот день ничего не запланировано.</p>
            ) : (
              <ul className="cl-cal-agenda-list">
                {(selectedItems.events ?? []).map(event => {
                  const Icon = EVENT_TYPE_ICON[event.eventType];
                  return (
                    <li key={event.id} className="cl-cal-agenda-card" style={{ borderLeftColor: EVENT_TYPE_COLOR[event.eventType] }}>
                      <button type="button" className="cl-cal-agenda-card-main" onClick={() => openEditDrawer(event)}>
                        <span className="cl-cal-agenda-card-top">
                          <Icon size={12} style={{ color: EVENT_TYPE_COLOR[event.eventType] }} />
                          <span className="cl-cal-agenda-card-time">{eventTimeRange(event)}</span>
                        </span>
                        <span className="cl-cal-agenda-card-title">{event.title}</span>
                        {(event.location || event.attendeeCount > 0) && (
                          <span className="cl-cal-agenda-card-meta">
                            {event.location && <span className="cl-cal-agenda-card-loc"><MapPin size={10} />{event.location}</span>}
                            {event.attendeeCount > 0 && (
                              <span className="cl-cal-agenda-card-attendees"><MeetingIcon size={10} />{event.attendeeCount}</span>
                            )}
                          </span>
                        )}
                        {event.link && (
                          <a className="cl-cal-agenda-sub" href={event.link} target="_blank" rel="noreferrer" onClick={ev => ev.stopPropagation()}>
                            <ExternalLink size={11} />Google Календарь
                          </a>
                        )}
                      </button>
                      <EventKebabMenu event={event} onEdit={openEditDrawer} onDelete={deleteEvent} />
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

            {status?.connected && (
              <button type="button" className="btn-primary cl-cal-create-btn" onClick={openCreateDrawer}>
                <Plus size={14} /><span>Создать событие</span>
              </button>
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
                <>
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
                  {upcomingTotal > UPCOMING_COLLAPSED_COUNT && (
                    <button type="button" className="cl-cal-upcoming-toggle" onClick={() => setUpcomingExpanded(v => !v)}>
                      {upcomingExpanded ? 'Свернуть' : 'Смотреть все'}
                    </button>
                  )}
                </>
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
