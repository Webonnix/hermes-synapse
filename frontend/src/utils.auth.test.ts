import { beforeEach, describe, expect, it, vi } from 'vitest';
import { initFetchInterceptor } from './utils';

describe('API authorization boundary', () => {
  beforeEach(() => { localStorage.clear(); localStorage.setItem('vexa_auth_token', 'test-token'); });
  it('does not attach credentials to external URLs or sign out on their 401', async () => {
    const original = vi.fn().mockResolvedValue(new Response('', { status: 401 }));
    vi.stubGlobal('fetch', original);
    initFetchInterceptor();
    await fetch('https://external.example/api/items');
    expect(original).toHaveBeenCalledWith('https://external.example/api/items', undefined);
    expect(localStorage.getItem('vexa_auth_token')).toBeNull();
  });
  it('preserves Headers and Request headers, handles 401, and installs once', async () => {
    const original = vi.fn().mockResolvedValue(new Response('', { status: 401 }));
    vi.stubGlobal('fetch', original);
    initFetchInterceptor();
    const installed = window.fetch;
    initFetchInterceptor();
    expect(window.fetch).toBe(installed);
    const request = new Request(`${location.origin}/api/items`, { headers: { 'Content-Type': 'application/json' } });
    await fetch(request);
    const options = original.mock.calls[0][1];
    expect(options.headers.get('Content-Type')).toBe('application/json');
    expect(options.headers.has('Authorization')).toBe(false);
    expect(options.credentials).toBe('same-origin');
    expect(localStorage.getItem('vexa_auth_token')).toBeNull();
  });
  it('leaves auth endpoint headers intact', async () => {
    const original = vi.fn().mockResolvedValue(new Response(''));
    vi.stubGlobal('fetch', original); initFetchInterceptor();
    const options = { headers: new Headers({ 'X-Test': 'preserved' }) };
    await fetch('/api/auth/login', options);
    expect(original.mock.calls[0][1].headers.get('X-Test')).toBe('preserved');
  });
  it('adds the readable CSRF nonce only to same-origin API mutations', async () => {
    document.cookie = 'hermes_csrf=csrf-nonce; path=/';
    const original = vi.fn().mockResolvedValue(new Response(''));
    vi.stubGlobal('fetch', original); initFetchInterceptor();
    await fetch('/api/items', { method: 'POST', body: '{}' });
    expect(original.mock.calls[0][1].headers.get('X-CSRF-Token')).toBe('csrf-nonce');
    await fetch('https://external.example/api/items', { method: 'POST', body: '{}' });
    expect(original.mock.calls[1][1].headers).toBeUndefined();
  });
  it('retries a cloned Request only after fresh authentication succeeds', async () => {
    const original = vi.fn().mockResolvedValueOnce(new Response('', { status: 428 })).mockResolvedValueOnce(new Response('ok'));
    vi.stubGlobal('fetch', original); initFetchInterceptor();
    const approve = (event: Event) => (event as CustomEvent<(ok: boolean) => void>).detail(true);
    window.addEventListener('hermes-reauth', approve, { once: true });
    const request = new Request(`${location.origin}/api/action`, { method: 'POST', body: '{"id":1}' });
    expect((await fetch(request)).status).toBe(200);
    expect(original).toHaveBeenCalledTimes(2);
    expect(original.mock.calls[1][0]).not.toBe(request);
    expect(await original.mock.calls[1][0].text()).toBe('{"id":1}');
  });
});
