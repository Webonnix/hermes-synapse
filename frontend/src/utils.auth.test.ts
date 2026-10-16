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
    expect(localStorage.getItem('vexa_auth_token')).toBe('test-token');
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
    expect(options.headers.get('Authorization')).toBe('Bearer test-token');
    expect(localStorage.getItem('vexa_auth_token')).toBeNull();
  });
  it('leaves auth endpoint headers intact', async () => {
    const original = vi.fn().mockResolvedValue(new Response(''));
    vi.stubGlobal('fetch', original); initFetchInterceptor();
    const options = { headers: new Headers({ 'X-Test': 'preserved' }) };
    await fetch('/api/auth/login', options);
    expect(original.mock.calls[0][1]).toBe(options);
  });
});
