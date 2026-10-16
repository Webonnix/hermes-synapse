import { useEffect, useRef, useState } from 'react';
import { Bot, Hand, Lock, Pencil, Play, RotateCcw, Send, Sparkles, Trash2, Unlock, X } from 'lucide-react';
import type { InboxAttachment, InboxConversation, InboxMessage, InboxMeta } from './inboxTypes';
import { HANDLING_LABELS, PLATFORM_LABELS, formatWhen, inboxApi } from './inboxApi';

const SPEAKER_LABELS: Record<string, string> = {
  customer: 'Клиент', agent: 'Агент', human: 'Оператор', whisper: 'Подсказка агенту', system: 'Система',
};
const DELIVERY_LABELS: Record<string, string> = { draft: 'черновик', failed: 'не доставлено', discarded: 'отклонён', sent: '' };

function AttachmentAudio({ attachment }: { attachment: InboxAttachment }) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  if (!attachment.mime.startsWith('audio/')) {
    return <span className="ibx-attachment">{attachment.kind} · {Math.round(attachment.size_bytes / 1024)} КБ</span>;
  }
  if (url) return <audio className="ibx-audio" controls src={url} />;
  return (
    <button type="button" className="btn-ghost ibx-attachment" onClick={async () => {
      try {
        setUrl(await inboxApi.attachmentUrl(attachment.id));
      } catch (exc) {
        setError((exc as Error).message);
      }
    }}>
      <Play size={12} /><span>{error || `Аудио (${Math.round(attachment.size_bytes / 1024)} КБ)`}</span>
    </button>
  );
}

function MessageBubble({ message, canReply, onSendDraft, onDiscardDraft }: {
  message: InboxMessage;
  canReply: boolean;
  onSendDraft: (message: InboxMessage, text?: string) => Promise<void>;
  onDiscardDraft: (message: InboxMessage) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.text);
  const [busy, setBusy] = useState(false);
  const isDraft = message.delivery === 'draft' && Boolean(message.pending_reply_id);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className={`ibx-msg is-${message.speaker} is-${message.delivery}`}>
      <header>
        <span>{SPEAKER_LABELS[message.speaker] || message.speaker}</span>
        {DELIVERY_LABELS[message.delivery] && <em className={`ibx-delivery is-${message.delivery}`}>{DELIVERY_LABELS[message.delivery]}</em>}
        {message.speaker === 'whisper' && <em className="ibx-delivery">{message.consumed_at ? 'учтена' : 'ждёт ответа агента'}</em>}
        <time>{formatWhen(message.created_at)}</time>
      </header>
      {editing ? (
        <textarea className="form-input ibx-edit" value={draft} onChange={e => setDraft(e.target.value)} rows={4} aria-label="Текст черновика" />
      ) : (
        <p>{message.text}</p>
      )}
      {message.attachments.length > 0 && (
        <div className="ibx-attachments">
          {message.attachments.map(attachment => <AttachmentAudio key={attachment.id} attachment={attachment} />)}
        </div>
      )}
      {isDraft && canReply && (
        <div className="ibx-draft-actions">
          {editing ? (
            <>
              <button type="button" className="btn-primary" disabled={busy || !draft.trim()} onClick={() => run(() => onSendDraft(message, draft))}>
                <Send size={13} /><span>Отправить</span>
              </button>
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => { setEditing(false); setDraft(message.text); }}>
                <X size={13} /><span>Отмена</span>
              </button>
            </>
          ) : (
            <>
              <button type="button" className="btn-primary" disabled={busy} onClick={() => run(() => onSendDraft(message))}>
                <Send size={13} /><span>Отправить</span>
              </button>
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => setEditing(true)}>
                <Pencil size={13} /><span>Изменить</span>
              </button>
              <button type="button" className="btn-ghost danger" disabled={busy} onClick={() => run(() => onDiscardDraft(message))}>
                <Trash2 size={13} /><span>Отклонить</span>
              </button>
            </>
          )}
        </div>
      )}
    </article>
  );
}

export function ConversationThread({
  conversation, messages, loading, error, meta, hasOlder,
  onLoadOlder, onTakeover, onResume, onClose, onReopen, onReply, onWhisper, onSendDraft, onDiscardDraft, onBack,
}: {
  conversation: InboxConversation | null;
  messages: InboxMessage[];
  loading: boolean;
  error: string;
  meta: InboxMeta | null;
  hasOlder: boolean;
  onLoadOlder: () => void;
  onTakeover: () => Promise<void>;
  onResume: () => Promise<void>;
  onClose: () => Promise<void>;
  onReopen: () => Promise<void>;
  onReply: (text: string) => Promise<void>;
  onWhisper: (text: string) => Promise<void>;
  onSendDraft: (message: InboxMessage, text?: string) => Promise<void>;
  onDiscardDraft: (message: InboxMessage) => Promise<void>;
  onBack: () => void;
}) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const lastId = messages.length ? messages[messages.length - 1].id : 0;

  useEffect(() => { setText(''); }, [conversation?.id]);
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [conversation?.id, lastId]);

  if (!conversation) {
    return (
      <section className="ibx-thread glass-panel ibx-thread-empty">
        <Sparkles size={22} />
        <strong>Выберите разговор</strong>
        <span>Слева — все сообщения клиентов из подключённых каналов.</span>
      </section>
    );
  }

  const canReply = Boolean(meta?.can_reply);
  const human = conversation.handling === 'human';
  const blocked = conversation.handling === 'blocked';

  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  };

  const submit = () => act(async () => {
    const value = text.trim();
    if (!value) return;
    if (human) await onReply(value);
    else await onWhisper(value);
    setText('');
  });

  return (
    <section className="ibx-thread glass-panel" aria-label="Переписка">
      <header className="ibx-thread-head">
        <button type="button" className="btn-ghost ibx-back" onClick={onBack} aria-label="К списку">←</button>
        <div className="ibx-thread-title">
          <strong>{conversation.client_name || conversation.display_name || conversation.chat_id}</strong>
          <small>
            {PLATFORM_LABELS[conversation.platform] || conversation.platform}
            {conversation.account ? ` · ${conversation.account}` : ''}
            {' · '}
            <span className={`ibx-handling is-${conversation.handling}`}>{HANDLING_LABELS[conversation.handling]}</span>
          </small>
        </div>
        {canReply && (
          <div className="ibx-thread-actions">
            {conversation.handling === 'ai' && (
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => act(onTakeover)}>
                <Hand size={14} /><span>Забрать диалог</span>
              </button>
            )}
            {human && conversation.state_mode && (
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => act(onResume)}>
                <Bot size={14} /><span>Вернуть агенту</span>
              </button>
            )}
            {conversation.status === 'open' ? (
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => act(onClose)}>
                <Lock size={14} /><span>Закрыть</span>
              </button>
            ) : (
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => act(onReopen)}>
                <Unlock size={14} /><span>Открыть</span>
              </button>
            )}
          </div>
        )}
      </header>

      {error && <div className="cl-error-banner" role="alert">{error}</div>}

      <div className="ibx-thread-scroll" ref={scrollRef}>
        {hasOlder && (
          <button type="button" className="btn-ghost ibx-more" onClick={onLoadOlder}><RotateCcw size={13} /><span>Раньше</span></button>
        )}
        {loading && messages.length === 0 && <div className="ibx-empty"><span>Загрузка переписки…</span></div>}
        {messages.map(message => (
          <MessageBubble key={message.id} message={message} canReply={canReply}
                         onSendDraft={onSendDraft} onDiscardDraft={onDiscardDraft} />
        ))}
      </div>

      {canReply && !blocked && (
        <footer className={`ibx-composer ${human ? 'is-reply' : 'is-whisper'}`}>
          <textarea
            className="form-input"
            value={text}
            rows={2}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void submit(); }}
            placeholder={human ? 'Ответ клиенту от вашего имени' : 'Подсказка агенту — клиент её не увидит'}
            aria-label={human ? 'Ответ клиенту' : 'Подсказка агенту'}
          />
          <button type="button" className="btn-primary" disabled={busy || !text.trim()} onClick={() => void submit()}>
            {human ? <><Send size={14} /><span>Ответить</span></> : <><Sparkles size={14} /><span>Подсказать</span></>}
          </button>
          {!human && <small className="ibx-composer-hint">Чтобы ответить самому — «Забрать диалог».</small>}
        </footer>
      )}
      {blocked && (
        <footer className="ibx-composer is-blocked">
          <small>Отправитель заблокирован правилом «{conversation.last_rule_pattern}». Снимите правило во вкладке «Правила», чтобы снова отвечать.</small>
        </footer>
      )}
    </section>
  );
}
