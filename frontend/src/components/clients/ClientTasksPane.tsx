import { useState } from 'react';
import { AlertTriangle, Check, ListChecks, Plus, RotateCcw, Trash2 } from 'lucide-react';
import { TASK_PRIORITY_LABELS, formatDueDate } from './clientLabels';
import type { ClientTask, TaskPriority } from './clientTypes';

type Filter = 'open' | 'overdue' | 'today' | 'done' | 'all';

const FILTERS: Array<{ id: Filter; label: string }> = [
  { id: 'open', label: 'Открытые' },
  { id: 'overdue', label: 'Просрочено' },
  { id: 'today', label: 'Сегодня' },
  { id: 'done', label: 'Выполнено' },
  { id: 'all', label: 'Все' },
];

function matches(task: ClientTask, filter: Filter): boolean {
  if (filter === 'all') return true;
  if (filter === 'open') return task.status === 'OPEN';
  if (filter === 'done') return task.status === 'DONE';
  if (filter === 'overdue') return task.overdue;
  if (filter === 'today') return task.status === 'OPEN' && Boolean(task.dueAt) && task.dueAt!.slice(0, 10) === new Date().toISOString().slice(0, 10);
  return true;
}

/**
 * «Задачи» — follow-ups, standalone as its own pane and embedded inside a
 * client's card (§CRM). The same component either way: a task tied to one
 * client scrolls the same list a global view does, just pre-filtered, so
 * there is one row layout and one set of actions instead of two.
 */
export function ClientTasksPane({
  tasks, loading, error, onCreate, onComplete, onReopen, onDelete, onRetry, embedded = false,
  clientId, dealId,
}: {
  tasks: ClientTask[];
  loading: boolean;
  error: string;
  onCreate: (payload: { title: string; dueAt: string; priority: TaskPriority; clientId?: string; dealId?: string }) => Promise<void>;
  onComplete: (task: ClientTask) => void;
  onReopen: (task: ClientTask) => void;
  onDelete: (task: ClientTask) => void;
  onRetry: () => void;
  /** Compact chrome for use inside ClientDetailsPane — no filter tabs, no
   *  client/deal reference on each row (the pane is already scoped to one). */
  embedded?: boolean;
  clientId?: string;
  dealId?: string;
}) {
  const [filter, setFilter] = useState<Filter>('open');
  const [title, setTitle] = useState('');
  const [dueAt, setDueAt] = useState('');
  const [priority, setPriority] = useState<TaskPriority>('NORMAL');
  const [busy, setBusy] = useState('');
  const [adding, setAdding] = useState(false);

  const addTask = async () => {
    if (!title.trim()) return;
    setAdding(true);
    try {
      await onCreate({ title: title.trim(), dueAt, priority, clientId, dealId });
      setTitle('');
      setDueAt('');
      setPriority('NORMAL');
    } finally {
      setAdding(false);
    }
  };

  const run = async (id: string, action: () => void) => {
    setBusy(id);
    try {
      action();
    } finally {
      setBusy('');
    }
  };

  const visible = embedded ? tasks : tasks.filter(task => matches(task, filter));

  if (error) {
    return (
      <div className="cl-error-banner">
        <span>Не удалось загрузить задачи</span>
        <button type="button" className="btn-ghost" onClick={onRetry}>Повторить</button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="cl-task-add">
        <input
          className="form-input" value={title} placeholder="Новая задача…"
          onChange={event => setTitle(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter') void addTask(); }}
        />
        <input
          className="form-input" type="date" value={dueAt} aria-label="Срок"
          onChange={event => setDueAt(event.target.value)}
        />
        <select className="form-input" value={priority} aria-label="Приоритет"
                onChange={event => setPriority(event.target.value as TaskPriority)}>
          {Object.entries(TASK_PRIORITY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <button type="button" className="btn-primary" disabled={!title.trim() || adding} onClick={() => void addTask()}>
          <Plus size={14} /><span>Добавить</span>
        </button>
      </div>

      {!embedded && (
        <nav className="admin-pane-tabs" aria-label="Фильтр задач">
          {FILTERS.map(item => (
            <button key={item.id} type="button" className={filter === item.id ? 'is-active' : ''}
                    onClick={() => setFilter(item.id)}>
              <span>{item.label}</span>
              <span className="admin-pane-tabs-count">
                {tasks.filter(task => matches(task, item.id)).length}
              </span>
            </button>
          ))}
        </nav>
      )}

      {loading ? (
        <div className="admin-table-wrap">
          {[0, 1, 2].map(index => <div key={index} className="cl-skeleton cl-skeleton-row" />)}
        </div>
      ) : visible.length === 0 ? (
        <div className="admin-empty-cta">
          <ListChecks size={22} />
          <strong>{embedded ? 'Задач пока нет' : 'Здесь пусто'}</strong>
          <span>Добавьте первую задачу выше — например, «Позвонить и уточнить детали».</span>
        </div>
      ) : (
        <ul className="cl-task-list">
          {visible.map(task => (
            <li key={task.id} className={`cl-task-row${task.status !== 'OPEN' ? ' is-done' : ''}${task.overdue ? ' is-overdue' : ''}`}>
              <button
                type="button" className="cl-task-check" disabled={busy === task.id}
                aria-pressed={task.status === 'DONE'}
                aria-label={task.status === 'DONE' ? `Вернуть в работу: ${task.title}` : `Отметить выполненной: ${task.title}`}
                onClick={() => void run(task.id, () => (task.status === 'DONE' ? onReopen(task) : onComplete(task)))}
              >
                {task.status === 'DONE' ? <RotateCcw size={13} /> : <Check size={13} />}
              </button>

              <div className="cl-task-body">
                <span className="cl-task-title">{task.title}</span>
                <span className="cl-task-meta">
                  {task.priority !== 'NORMAL' && <span className={`cl-task-priority is-${task.priority.toLowerCase()}`}>{TASK_PRIORITY_LABELS[task.priority]}</span>}
                  <span className={task.overdue ? 'cl-outstanding' : 'cl-muted'}>
                    {task.overdue && <AlertTriangle size={11} style={{ verticalAlign: -1, marginRight: 3 }} />}
                    {formatDueDate(task.dueAt)}
                  </span>
                  {task.assignee && <span className="cl-muted">· {task.assignee}</span>}
                </span>
              </div>

              <button type="button" className="icon-btn danger" title="Удалить задачу"
                      aria-label={`Удалить задачу: ${task.title}`}
                      onClick={() => void run(task.id, () => onDelete(task))}>
                <Trash2 size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default ClientTasksPane;
