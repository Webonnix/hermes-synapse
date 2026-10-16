import { describe, it, expect, beforeEach, vi } from 'vitest';
import { SERVICE_WORKER_SOURCE } from './serviceWorkerTemplate';
import { MANIFEST, buildId } from '../../vite.pwa-plugin';

/**
 * The service worker ships as a generated string, so it is tested by actually
 * running it against a fake ServiceWorkerGlobalScope. Asserting on the source
 * text would pass for code that never executes.
 */
function loadWorker(buildIdValue = 'build1') {
  type Listener = (event: any) => void;
  const listeners: Record<string, Listener[]> = {};
  const deleted: string[] = [];
  const cacheKeys = ['hermes-assets-oldbuild', `hermes-assets-${buildIdValue}`];

  const self: any = {
    location: { origin: 'https://hermes.test' },
    addEventListener: (type: string, fn: Listener) => {
      (listeners[type] ||= []).push(fn);
    },
    skipWaiting: vi.fn(() => Promise.resolve()),
    clients: { claim: vi.fn(() => Promise.resolve()) },
  };
  const caches = {
    keys: () => Promise.resolve(cacheKeys),
    delete: (key: string) => { deleted.push(key); return Promise.resolve(true); },
    match: vi.fn(() => Promise.resolve(undefined)),
    open: vi.fn(() => Promise.resolve({ put: vi.fn(() => Promise.resolve()) })),
  };
  const fetchMock = vi.fn(() => Promise.resolve({ status: 200, type: 'basic', clone: () => ({}) }));

  const source = SERVICE_WORKER_SOURCE.replace(/__BUILD_ID__/g, buildIdValue);
  new Function('self', 'caches', 'fetch', 'URL', source)(self, caches, fetchMock, URL);

  const fire = (type: string, event: any) => (listeners[type] || []).forEach(fn => fn(event));
  return { fire, deleted, caches, fetchMock, self };
}

function fetchEvent(url: string, init: { mode?: string; method?: string } = {}) {
  let responded: any = undefined;
  return {
    request: { url, method: init.method || 'GET', mode: init.mode || 'no-cors' },
    respondWith: (value: any) => { responded = value; },
    get responded() { return responded; },
  };
}

describe('service worker caching policy', () => {
  let worker: ReturnType<typeof loadWorker>;
  beforeEach(() => { worker = loadWorker(); });

  it('never intercepts a navigation, so a deploy cannot be masked by a cached shell', () => {
    const event = fetchEvent('https://hermes.test/', { mode: 'navigate' });
    worker.fire('fetch', event);
    expect(event.responded).toBeUndefined();
  });

  it('never intercepts API traffic', () => {
    const event = fetchEvent('https://hermes.test/api/sessions');
    worker.fire('fetch', event);
    expect(event.responded).toBeUndefined();
  });

  it('never intercepts index.html itself', () => {
    const event = fetchEvent('https://hermes.test/index.html');
    worker.fire('fetch', event);
    expect(event.responded).toBeUndefined();
  });

  it('serves content-hashed build assets from cache', () => {
    const event = fetchEvent('https://hermes.test/assets/AgentsAdminTab-u4GY0Ahv.js');
    worker.fire('fetch', event);
    expect(event.responded).toBeDefined();
  });

  it('leaves an unhashed asset alone — it is not provably immutable', () => {
    const event = fetchEvent('https://hermes.test/assets/style.css');
    worker.fire('fetch', event);
    expect(event.responded).toBeUndefined();
  });

  it('ignores cross-origin and non-GET requests', () => {
    const cross = fetchEvent('https://cdn.example.com/assets/x-abcdef12.js');
    worker.fire('fetch', cross);
    expect(cross.responded).toBeUndefined();

    const post = fetchEvent('https://hermes.test/assets/x-abcdef12.js', { method: 'POST' });
    worker.fire('fetch', post);
    expect(post.responded).toBeUndefined();
  });

  it('deletes every other build cache on install', async () => {
    let work: any;
    worker.fire('install', { waitUntil: (p: any) => { work = p; } });
    await work;
    expect(worker.deleted).toEqual(['hermes-assets-oldbuild']);
    expect(worker.self.skipWaiting).toHaveBeenCalled();
  });
});

describe('build identity', () => {
  it('changes per build so a deploy invalidates the asset cache', () => {
    const id = buildId();
    expect(id).toBeTruthy();
    expect(id.length).toBeLessThanOrEqual(16);
  });

  it('ships a manifest that can actually be installed', () => {
    expect(MANIFEST.start_url).toBe('/');
    expect(MANIFEST.display).toBe('standalone');
    expect(MANIFEST.icons.length).toBeGreaterThan(0);
  });
});
