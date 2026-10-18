import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ACCESS_SESSION_EXPIRED_MESSAGE,
  AccessSessionExpired,
  authEndpointFetch,
  authErrorMessage,
} from './accessSession';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('authEndpointFetch', () => {
  it('asks fetch not to follow redirects', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ type: 'basic', ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);
    await authEndpointFetch('/api/auth/login', { method: 'POST' });
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/login', { method: 'POST', redirect: 'manual' });
  });

  it('turns an edge redirect into AccessSessionExpired, not a network error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ type: 'opaqueredirect', ok: false, status: 0 }));
    await expect(authEndpointFetch('/api/auth/login')).rejects.toBeInstanceOf(AccessSessionExpired);
  });

  it('passes ordinary responses through, including auth failures', async () => {
    const response = { type: 'basic', ok: false, status: 401 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
    await expect(authEndpointFetch('/api/auth/login')).resolves.toBe(response);
  });
});

describe('authErrorMessage', () => {
  it('names the expired Access session', () => {
    expect(authErrorMessage(new AccessSessionExpired(), 'fallback')).toBe(ACCESS_SESSION_EXPIRED_MESSAGE);
  });

  it('keeps the fallback for a real network failure', () => {
    expect(authErrorMessage(new TypeError('Failed to fetch'), 'fallback')).toBe('fallback');
  });
});
