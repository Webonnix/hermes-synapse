/** The session is HttpOnly. Only its separate CSRF nonce is readable by JS. */
export function csrfToken(): string {
  const value = document.cookie.split('; ').find(item => item.startsWith('hermes_csrf='));
  try { return value ? decodeURIComponent(value.slice('hermes_csrf='.length)) : ''; }
  catch { return ''; }
}

let reauthPending: Promise<boolean> | null = null;
export function requestReauth(): Promise<boolean> {
  if (!reauthPending) {
    let complete!: (ok: boolean) => void;
    const pending = new Promise<boolean>(resolve => { complete = resolve; });
    reauthPending = pending;
    window.dispatchEvent(new CustomEvent('hermes-reauth', { detail: (approved: boolean) => {
      reauthPending = null; complete(approved);
    } }));
    return pending;
  }
  return reauthPending;
}
