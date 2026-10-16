import { defineConfig, devices } from '@playwright/test';

/**
 * Smoke-only Playwright config. It drives an already-running frontend container
 * (see .github/workflows/browser-smoke.yml); it does not start a dev server,
 * because the thing under test is the built image, not the source.
 */
export default defineConfig({
  testDir: './tests/smoke',
  timeout: 45_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  use: {
    baseURL: process.env.SMOKE_BASE_URL || 'https://localhost:8443',
    // The smoke container terminates TLS with a throwaway self-signed pair; the
    // test asserts what is served, not who signed it.
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
  ],
});
