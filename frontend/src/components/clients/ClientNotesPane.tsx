import { useState } from 'react';
import { Pencil, Pin, PinOff, StickyNote, Trash2, X } from 'lucide-react';
import { formatRelative } from './clientLabels';
import type { ClientNote } from './clientTypes';

/**
 * «Заметки» — what was said on a call, what was agreed verbally, why a
 * discount was given. Unlike the «Активность» tab beside it (a system-written,
 * append-only audit trail), these are written by a person about the
 * relationship and can be corrected or removed — see backend/client_notes.py.
 */
export function ClientNotesPane({
  notes, loading, error, onCreate, onUpdate, onDelete, onRetry,
}: {
  notes: ClientNote[];
  loading: boolean;
  error: string;
  onCreate: (body: string) => Promise<void>;
  onUpdate: (note: ClientNote, patch: { body?: string; pinned?: boolean }) => Promise<void>;
  onDelete: (note: ClientNote) => void;
  onRetry: () => void;
}) {
  const [draft, setDraft] = useState('');
  const [posting, setPosting] = useState(false);
  const [editingId, setEditingId] = useState('');
  const [editBody, setEditBody] = useState('');

  const post = async () => {
    if (!draft.trim()) return;
    setPosting(true);
    try {
      await onCreate(draft.trim());
      setDraft('');
    } finally {
      setPosting(false);
    }
  };

  const startEdit = (note: ClientNote) => { setEditingId(note.id); setEditBody(note.body); };
  const saveEdit = async (note: ClientNote) => {
    if (!editBody.trim()) return;
    await onUpdate(note, { body: editBody.trim() });
    setEditingId('');
  };

  if (error) {
    return (
      <div className="cl-error-banner">
        <span>Не удалось загрузить заметки</span>
        <button type="button" className="btn-ghost" onClick={onRetry}>Повторить</button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="cl-note-composer">
        <textarea
          className="form-input" rows={3} value={draft} placeholder="Что важно запомнить об этом клиенте?"
          onChange={event => setDraft(event.target.value)}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button type="button" className="btn-primary" disabled={!draft.trim() || posting} onClick={() => void post()}>
            {posting ? 'Сохранение…' : 'Добавить заметку'}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="admin-table-wrap">
          {[0, 1].map(index => <div key={index} className="cl-skeleton cl-skeleton-row" />)}
        </div>
      ) : notes.length === 0 ? (
        <div className="admin-empty">
          <StickyNote size={16} style={{ verticalAlign: -3, marginRight: 6 }} />
          Заметок пока нет
        </div>
      ) : (
        <ul className="cl-note-list">
          {notes.map(note => (
            <li key={note.id} className={`glass-panel cl-note-card${note.pinned ? ' is-pinned' : ''}`}>
              <header>
                <span className="cl-note-meta">
                  {note.author && <strong>{note.author}</strong>}
                  <span className="cl-muted">{formatRelative(note.createdAt)}</span>
                </span>
                <div className="cl-row-actions">
                  <button type="button" className="icon-btn" title={note.pinned ? 'Открепить' : 'Закрепить'}
                          aria-label={note.pinned ? `Открепить заметку` : `Закрепить заметку`}
                          onClick={() => void onUpdate(note, { pinned: !note.pinned })}>
                    {note.pinned ? <PinOff size={13} /> : <Pin size={13} />}
                  </button>
                  {editingId === note.id ? (
                    <button type="button" className="icon-btn" title="Отменить" aria-label="Отменить редактирование"
                            onClick={() => setEditingId('')}>
                      <X size={13} />
                    </button>
                  ) : (
                    <button type="button" className="icon-btn" title="Редактировать" aria-label="Редактировать заметку"
                            onClick={() => startEdit(note)}>
                      <Pencil size={13} />
                    </button>
                  )}
                  <button type="button" className="icon-btn danger" title="Удалить" aria-label="Удалить заметку"
                          onClick={() => onDelete(note)}>
                    <Trash2 size={13} />
                  </button>
                </div>
              </header>
              {editingId === note.id ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <textarea className="form-input" rows={3} value={editBody}
                            onChange={event => setEditBody(event.target.value)} />
                  <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                    <button type="button" className="btn-primary" disabled={!editBody.trim()}
                            onClick={() => void saveEdit(note)}>
                      Сохранить
                    </button>
                  </div>
                </div>
              ) : (
                <p className="cl-note-body">{note.body}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default ClientNotesPane;
