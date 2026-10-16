import { test, expect } from '@playwright/test';

/**
 * The blank-screen guard.
 *
 * The dashboard has twice gone fully blank in production while every unit test
 * passed — once because a 401 storm put an error object where a list belonged
 * and `.map` unmounted the tree, once because the module script never loaded.
 * Both looked the same: a valid 200, correct HTML, and an empty `#root`.
 *
 * No backend is running here on purpose. The app must still mount and render
 * its unauthenticated state; a failure to do that is a real defect, not a
 * missing dependency.
 */

const BASE_URL = process.env.SMOKE_BASE_URL || 'https://localhost:8443';

test('the app mounts and renders something', async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on('pageerror', error => consoleErrors.push(String(error)));

  const response = await page.goto(BASE_URL, { waitUntil: 'load' });
  expect(response?.status()).toBe(200);

  // #root having children is the whole assertion: the bundle parsed, React
  // mounted, and the first render did not throw.
  await expect.poll(
    () => page.locator('#root > *').count(),
    { timeout: 15000, message: '#root stayed empty — the app never mounted' },
  ).toBeGreaterThan(0);

  const text = (await page.locator('#root').innerText()).trim();
  expect(text.length).toBeGreaterThan(0);

  // An uncaught exception during the first render is what unmounts the tree.
  expect(consoleErrors, `uncaught errors during first render:\n${consoleErrors.join('\n')}`).toHaveLength(0);
});

test('the page survives a reload without losing the app', async ({ page }) => {
  await page.goto(BASE_URL, { waitUntil: 'load' });
  await expect.poll(() => page.locator('#root > *').count(), { timeout: 15000 }).toBeGreaterThan(0);

  // A second load goes through the service worker. If it ever started serving a
  // cached shell, this is where a stale or wrong document would appear.
  await page.reload({ waitUntil: 'load' });
  await expect.poll(() => page.locator('#root > *').count(), { timeout: 15000 }).toBeGreaterThan(0);
});

test('the service worker registers without taking over navigation', async ({ page }) => {
  await page.goto(BASE_URL, { waitUntil: 'load' });
  const swSupported = await page.evaluate(() => 'serviceWorker' in navigator);
  expect(swSupported).toBe(true);

  // Navigations must always reach the network: a cached app shell is how a
  // stale bundle outlives a deploy.
  const source = await (await page.request.get(`${BASE_URL}/sw.js`)).text();
  expect(source).toContain("request.mode === 'navigate'");
  expect(source).not.toContain('caches.match(request).then((hit) => hit || fetch(request))');
});
