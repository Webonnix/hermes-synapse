/**
 * Service worker source. Built into `dist/sw.js` by the `pwa` Vite plugin with
 * `__BUILD_ID__` replaced by the build's identity.
 *
 * Caching policy, chosen around a failure this deployment has actually had —
 * a deploy that "changed nothing" because the browser kept serving an old
 * bundle:
 *
 *   - `index.html` and every navigation are **network-first with no cache
 *     fallback**. A cached shell is exactly how a stale bundle survives a
 *     deploy, and under password auth the cached response might be the login
 *     page instead of the app.
 *   - `/assets/*` are content-hashed by Vite and therefore immutable, so they
 *     are cache-first. A new build produces new filenames; nothing is ever
 *     served stale.
 *   - `/api/*` is never cached. The UI requires a live backend, and a cached
 *     API response would show yesterday's state as if it were current.
 *
 * Caches are keyed by build id and every other cache is deleted on install, so
 * an upgrade can never accumulate or resurrect old bytes.
 */
export const SERVICE_WORKER_SOURCE = String.raw`
const BUILD_ID = '__BUILD_ID__';
const CACHE_NAME = 'hermes-assets-' + BUILD_ID;

// Immutable, content-hashed build output: Vite emits '<name>-<hash>.<ext>'.
// A file without a hash in its name is NOT provably immutable and is left alone.
const IMMUTABLE_PATH = /^\/assets\/.+[-.][0-9a-zA-Z_-]{6,}\.(js|css|woff2?|ttf|png|svg|jpg|jpeg|webp)$/;

function isImmutable(url) {
  return IMMUTABLE_PATH.test(url.pathname);
}

self.addEventListener('install', (event) => {
  // Drop every older build's cache BEFORE taking over, so a quota-limited
  // browser is never holding two builds at once.
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event && event.data === 'hermes-skip-waiting') self.skipWaiting();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch (err) {
    return;
  }
  // Never touch cross-origin, the API, or the websocket.
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  // Navigations always go to the network. No offline fallback on purpose: a
  // cached app shell is how a stale bundle outlives a deploy, and it can also
  // be the login page rather than the app.
  if (request.mode === 'navigate') return;

  if (!isImmutable(url)) return;

  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        if (response && response.status === 200 && response.type === 'basic') {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      });
    })
  );
});
`;
