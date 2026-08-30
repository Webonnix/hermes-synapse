import { useEffect, useState, type ReactNode } from 'react';
import { ExternalLink, Trash2, X } from 'lucide-react';
import { styles } from '../../styles';
import { EVENT_TYPE_COLOR, EVENT_TYPE_LABELS, EVENT_TYPE_ORDER } from './clientLabels';
import type { CalendarEvent, CalendarEventType } from './clientTypes';

/**
 * Create/edit drawer for one Google Calendar event.
 *
 * Same object a person, the agent (via chat) or Google's own apps can all
 * produce — there is one event shape (backend/tools.py's `_event_to_dict`),
 * so this drawer edits exactly what `add_calendar_event`/`update_calendar_event`
 * write, no dashboard-only fields that would vanish the moment the agent
 * touches the same event.
 */

interface DraftState {
  title: string;
  eventType: CalendarEventType;
  date: string;
  time: string;
  endTime: string;
  location: string;
  description: string;
}

function emptyDraft(date: string): DraftState {
  return { title: '', eventType: 'OTHER', date, time: '10:00', endTime: '', location: '', description: '' };
}

function draftFromEvent(event: CalendarEvent): DraftState {
  const start = new Date(event.start);
  const end = event.end ? new Date(event.end) : null;
  const pad = (n: number) => String(n).padStart(2, '0');
  return {
    title: event.title,
    eventType: event.eventType,
    date: event.allDay ? event.start.slice(0, 10) : `${start.getFullYear()}-${pad(start.getMonth() + 1)}-${pad(start.getDate())}`,
    time: event.allDay ? '' : `${pad(start.getHours())}:${pad(start.getMinutes())}`,
    endTime: !event.allDay && end ? `${pad(end.getHours())}:${pad(end.getMinutes())}` : '',
    location: event.location,
    description: event.description,
  };
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="cl-field">
      <span className="cl-field-label">{label}</span>
      {children}
      {hint && <small style={styles.formHelp}>{hint}</small>}
    </label>
  );
}

export function CalendarEventDrawer({
  open, event, defaultDate, onClose, onSubmit, onDelete,
}: {
  open: boolean;
  /** `null` starts a new event, defaulting to `defaultDate`. */
  event: CalendarEvent | null;
  defaultDate: string;
  onClose: () => void;
  onSubmit: (payload: {
    title: string; date: string; time: string; endTime: string; description: string;
    eventType: CalendarEventType; location: string;
  }, eventId?: string) => Promise<void>;
  onDelete: (event: CalendarEvent) => Promise<void>;
}) {
  const [draft, setDraft] = useState<DraftState>(() => emptyDraft(defaultDate));
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setError('');
    setDraft(event ? draftFromEvent(event) : emptyDraft(defaultDate));
  }, [open, event, defaultDate]);

  if (!open) return null;

  const set = <K extends keyof DraftState>(key: K, value: DraftState[K]) =>
    setDraft(current => ({ ...current, [key]: value }));

  const submit = async () => {
    if (!draft.title.trim()) {
      setError('Укажите название события');
      return;
    }
    if (!draft.date) {
      setError('Укажите дату');
      return;
    }
    setSaving(true);
    setError('');
    try {
      await onSubmit({
        title: draft.title.trim(),
        date: draft.date,
        time: draft.time || '00:00',
        endTime: draft.endTime,
        description: draft.description.trim(),
        eventType: draft.eventType,
        location: draft.location.trim(),
      }, event?.id);
      onClose();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!event) return;
    if (!window.confirm(`Удалить событие «${event.title}» из Google Календаря?`)) return;
    setDeleting(true);
    try {
      await onDelete(event);
      onClose();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <>
      <div className="cl-drawer-backdrop" onClick={onClose} aria-hidden />
      <aside className="cl-drawer" role="dialog" aria-modal="true"
             aria-label={event ? 'Редактирование события' : 'Новое событие'}>
        <header>
          <span>{event ? 'Событие' : 'Новое событие'}</span>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <X size={15} />
          </button>
        </header>

        <div className="cl-drawer-body">
          <fieldset className="cl-field" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="cl-field-label" style={{ marginBottom: 5 }}>Тип</legend>
            <div className="cl-deal-stage-picker">
              {EVENT_TYPE_ORDER.map(type => (
                <button
                  key={type} type="button"
                  className={`cl-deal-stage-btn cl-event-type-btn${draft.eventType === type ? ' is-active' : ''}`}
                  onClick={() => set('eventType', type)}
                >
                  <i className="cl-cal-dot" style={{ background: EVENT_TYPE_COLOR[type] }} />
                  {EVENT_TYPE_LABELS[type]}
                </button>
              ))}
            </div>
          </fieldset>

          <Field label="Название">
            <input className="form-input" value={draft.title} autoFocus
                   placeholder="Например, Созвон с клиентом"
                   onChange={ev => set('title', ev.target.value)} />
          </Field>

          <div className="cl-field-row">
            <Field label="Дата">
              <input className="form-input" type="date" value={draft.date}
                     onChange={ev => set('date', ev.target.value)} />
            </Field>
            <Field label="Начало">
              <input className="form-input" type="time" value={draft.time}
                     onChange={ev => set('time', ev.target.value)} />
            </Field>
            <Field label="Окончание" hint="Необязательно — по умолчанию час.">
              <input className="form-input" type="time" value={draft.endTime}
                     onChange={ev => set('endTime', ev.target.value)} />
            </Field>
          </div>

          <Field label="Место" hint="Необязательно">
            <input className="form-input" value={draft.location}
                   onChange={ev => set('location', ev.target.value)} />
          </Field>

          <Field label="Описание" hint="Необязательно">
            <textarea className="form-input" rows={3} value={draft.description}
                      onChange={ev => set('description', ev.target.value)} />
          </Field>

          {event?.link && (
            <a className="cl-link-btn" href={event.link} target="_blank" rel="noreferrer">
              <ExternalLink size={13} /><span>Открыть в Google Календаре</span>
            </a>
          )}

          {error && <div className="cl-error-banner">⚠️ {error}</div>}
        </div>

        <footer className="cl-drawer-footer">
          {event && (
            <button type="button" className="btn-ghost danger" disabled={deleting || saving} onClick={() => void remove()}>
              <Trash2 size={13} /><span>Удалить</span>
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button type="button" className="btn-ghost" onClick={onClose}>Отмена</button>
          <button type="button" className="btn-primary" disabled={saving || deleting} onClick={() => void submit()}>
            {saving ? 'Сохранение…' : (event ? 'Сохранить' : 'Создать')}
          </button>
        </footer>
      </aside>
    </>
  );
}

export default CalendarEventDrawer;
