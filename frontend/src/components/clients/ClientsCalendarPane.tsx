import { useMemo, useState } from 'react';
import {
  AlertTriangle, Bell, Building2, Check, ChevronLeft, ChevronRight, ListChecks, Plus, Target,
} from 'lucide-react';
import { formatDate } from './clientLabels';
import type { ClientTask, Deal } from './clientTypes';

/**
 * «Календарь» — everything with a date on it, in one month grid: an open
 * task's due date and an open deal's expected close date. Read-mostly by
 * design — it aggregates data the Сделки and Задачи panes already fetch
 * (no new endpoint), and the one write it offers, a quick-add task for the
 * day you clicked, goes through the same `onCreateTask` every other task
 * composer in this section uses.
 *
 * "Напоминание" is not only visual: a task due today or overdue also drives
 * the daily Telegram digest the backend sends the owner
 * (`client_tasks.daily_digest_text`, wired into `main.py`'s lifespan) — this
 * page is where a person comes to see the same thing that message already
 * told them, with the context to act on it.
 */

const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const MONTH_NAMES = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
];

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

interface DayItems {
  tasks: ClientTask[];
  deals: Deal[];
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

  const itemsByDate = useMemo(() => {
    const map = new Map<string, DayItems>();
    const ensure = (iso: string) => {
      let entry = map.get(iso);
      if (!entry) { entry = { tasks: [], deals: [] }; map.set(iso, entry); }
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
    return map;
  }, [tasks, deals]);

  const grid = useMemo(() => buildMonthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor]);
  const todayIso = toIso(today);
  const selectedItems = itemsByDate.get(selected);

  const changeMonth = (delta: number) => {
    setCursor(current => new Date(current.getFullYear(), current.getMonth() + delta, 1));
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="glass-panel cl-cal-reminder-note">
        <Bell size={15} />
        <span>
          Каждый день бот присылает в Telegram сводку по просроченным и сегодняшним задачам —
          это тот же список, что показан здесь на выбранный день.
        </span>
      </div>

      <div className="cl-cal-layout">
        <section className="glass-panel cl-cal-board" aria-busy={loading}>
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

          <div className="cl-cal-weekdays" aria-hidden="true">
            {WEEKDAYS.map(day => <span key={day}>{day}</span>)}
          </div>

          <div className="cl-cal-grid" role="grid" aria-label="Календарь задач и сделок">
            {grid.map(date => {
              const iso = toIso(date);
              const items = itemsByDate.get(iso);
              const outside = date.getMonth() !== cursor.getMonth();
              const overdueCount = items?.tasks.filter(task => task.overdue).length ?? 0;
              const chips: Array<{ id: string; label: string; kind: 'task' | 'overdue' | 'deal' }> = [
                ...(items?.tasks ?? []).map(task => ({
                  id: task.id, label: task.title, kind: task.overdue ? 'overdue' as const : 'task' as const,
                })),
                ...(items?.deals ?? []).map(deal => ({ id: deal.id, label: deal.title, kind: 'deal' as const })),
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
                      <span key={chip.id} className={`cl-cal-chip is-${chip.kind}`}>{chip.label}</span>
                    ))}
                    {hiddenCount > 0 && <span className="cl-cal-chip-more">+{hiddenCount}</span>}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="cl-cal-legend">
            <span><i className="cl-cal-dot is-task" />Задача</span>
            <span><i className="cl-cal-dot is-overdue" />Просрочена</span>
            <span><i className="cl-cal-dot is-deal" />Ожидаемое закрытие сделки</span>
          </div>
        </section>

        <aside className="glass-panel cl-cal-agenda" aria-label={`События: ${formatDate(selected)}`}>
          <strong>{formatDate(selected)}{selected === todayIso ? ' · сегодня' : ''}</strong>

          {!selectedItems || (selectedItems.tasks.length === 0 && selectedItems.deals.length === 0) ? (
            <p className="cl-muted" style={{ margin: '10px 0' }}>На этот день ничего не запланировано.</p>
          ) : (
            <ul className="cl-cal-agenda-list">
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
              onChange={event => setQuickTitle(event.target.value)}
              onKeyDown={event => { if (event.key === 'Enter') void addQuickTask(); }}
            />
            <button type="button" className="icon-btn" aria-label="Добавить задачу" disabled={!quickTitle.trim() || adding}
                    onClick={() => void addQuickTask()}>
              <Plus size={15} />
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}

export default ClientsCalendarPane;
