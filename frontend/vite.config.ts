import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { pwaPlugin } from './vite.pwa-plugin.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), pwaPlugin()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/setupTests.ts',
    // Above the 10s asyncUtilTimeout in setupTests, so a slow findBy fails with
    // its own message instead of the generic test timeout.
    testTimeout: 20000,
    // `tests/` holds checks that read the source tree itself, so they need node
    // types the app build does not have — they live outside `src` for that
    // reason and are typed by tsconfig.node.json.
    include: ['src/**/*.{test,spec}.{ts,tsx}', 'tests/**/*.{test,spec}.{ts,tsx}'],
    // tests/smoke/ is the Playwright suite: it drives a real browser against a
    // running container and is run by the browser-smoke workflow, not vitest.
    exclude: ['**/node_modules/**', '**/dist/**', 'tests/smoke/**'],
  },
})
