/**
 * Service-worker registration.
 *
 * Registering is opt-out (`VITE_DISABLE_PWA`) and never throws: a browser with
 * service workers disabled, a non-secure origin, or a failed registration must
 * leave the app working exactly as before.
 */
export function registerServiceWorker(): void {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator)) return;
  if (import.meta.env?.DEV) return; // dev server serves unhashed modules
  if (import.meta.env?.VITE_DISABLE_PWA === 'true') return;

  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').then(registration => {
      // A build deployed while the tab was open leaves an installed worker in
      // "waiting". Telling it to activate immediately is what keeps the next
      // reload from being served by the previous build.
      if (registration.waiting) {
        registration.waiting.postMessage('hermes-skip-waiting');
      }
      registration.addEventListener('updatefound', () => {
        const installing = registration.installing;
        if (!installing) return;
        installing.addEventListener('statechange', () => {
          if (installing.state === 'installed' && navigator.serviceWorker.controller) {
            installing.postMessage('hermes-skip-waiting');
          }
        });
      });
    }).catch(err => {
      console.warn('Service worker registration failed (the app still works):', err);
    });
  });
}
