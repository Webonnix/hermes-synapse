/**
 * Auth requests behind Cloudflare Access.
 *
 * The public domain sits behind Cloudflare Access. Once the Access session
 * expires, every request — including POST /api/auth/login — is answered with a
 * 302 to the Access login page on another origin. fetch cannot follow that
 * redirect (no CORS headers there) and throws a TypeError, which the login
 * screen used to report as "no connection to the server" while the backend was
 * perfectly healthy and never even saw the request.
 *
 * With `redirect: 'manual'` the redirect comes back as an `opaqueredirect`
 * response instead of an exception, so it can be told apart from a real network
 * failure. The auth endpoints never redirect on their own, so any redirect here
 * means the edge intercepted the request; reloading the page lets the browser
 * go through the Access login and come back.
 */
export class AccessSessionExpired extends Error {
  constructor() {
    super('Access session expired');
    this.name = 'AccessSessionExpired';
  }
}

export const ACCESS_SESSION_EXPIRED_MESSAGE = 'Access session expired. Reload the page to sign in again.';

export async function authEndpointFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(url, { ...init, redirect: 'manual' });
  if (res.type === 'opaqueredirect') throw new AccessSessionExpired();
  return res;
}

/** The copy key to show for a failed auth request. */
export function authErrorMessage(err: unknown, fallback: string): string {
  return err instanceof AccessSessionExpired ? ACCESS_SESSION_EXPIRED_MESSAGE : fallback;
}
