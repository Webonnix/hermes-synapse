import { useEffect, useState, useRef } from 'react';

export function notify(message: string) {
  window.dispatchEvent(new CustomEvent('hermes-notification', { detail: message }));
}

type Confirmation = { message: string; resolve: (accepted: boolean) => void };
export function confirmAction(message: string): Promise<boolean> {
  return new Promise(resolve => window.dispatchEvent(new CustomEvent('hermes-confirm', { detail: { message, resolve } })));
}

export function Notifications() {
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const pending = useRef<Confirmation | null>(null);
  const finish = (accepted: boolean) => {
    pending.current?.resolve(accepted);
    pending.current = null;
    dialog.current?.close();
    setConfirmation(null);
  };
  useEffect(() => {
    const receive = (event: Event) => {
      const next = (event as CustomEvent<Confirmation>).detail;
      if (pending.current) { next.resolve(false); return; }
      pending.current = next; setConfirmation(next);
    };
    window.addEventListener('hermes-confirm', receive);
    return () => { window.removeEventListener('hermes-confirm', receive); pending.current?.resolve(false); };
  }, []);
  useEffect(() => { if (confirmation) dialog.current?.showModal(); }, [confirmation]);
  const ru = localStorage.getItem('hermes_language') !== 'en';
  const [messages, setMessages] = useState<{ id: number; text: string }[]>([]);
  useEffect(() => {
    let sequence = 0;
    const receive = (event: Event) => {
      const text = String((event as CustomEvent).detail);
      setMessages(current => [...current.slice(-3), { id: ++sequence, text }]);
    };
    window.addEventListener('hermes-notification', receive);
    return () => window.removeEventListener('hermes-notification', receive);
  }, []);
  return <>
    <dialog ref={dialog} className="confirmation-dialog" aria-labelledby="confirmation-title" onCancel={event => { event.preventDefault(); finish(false); }}>
      <h2 id="confirmation-title">{ru ? 'Подтвердите удаление' : 'Confirm deletion'}</h2>
      <p>{confirmation?.message}</p>
      <div><button type="button" autoFocus onClick={() => finish(false)}>{ru ? 'Отмена' : 'Cancel'}</button>
      <button type="button" onClick={() => finish(true)}>{ru ? 'Удалить' : 'Delete'}</button></div>
    </dialog>
    <div className="notifications" aria-live="polite" aria-relevant="additions">
    {messages.map(message => <div className="notification" key={message.id}>
      <span>{message.text}</span>
      <button type="button" aria-label={localStorage.getItem('hermes_language') === 'ru' ? 'Закрыть уведомление' : 'Dismiss notification'} onClick={() => setMessages(current => current.filter(item => item.id !== message.id))}>×</button>
    </div>)}
  </div></>;
}
