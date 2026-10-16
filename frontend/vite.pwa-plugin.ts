import { execSync } from 'node:child_process';
import type { Plugin } from 'vite';
import { SERVICE_WORKER_SOURCE } from './src/pwa/serviceWorkerTemplate.ts';

/**
 * Emits `sw.js` and `manifest.json` into the build output.
 *
 * The service worker's cache key is the build id, so a deploy invalidates every
 * cached asset without anyone remembering to bump a constant — the class of
 * mistake that produces "the deploy changed nothing".
 */
export function buildId(): string {
  const fromEnv = process.env.HERMES_BUILD_ID || process.env.GIT_COMMIT;
  if (fromEnv) return fromEnv.slice(0, 16);
  try {
    return execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString().trim().slice(0, 16);
  } catch {
    // No git in the build container (the Docker image copies plain files) —
    // fall back to build time, which still changes on every build.
    return `t${Date.now().toString(36)}`;
  }
}

export const MANIFEST = {
  name: 'Hermes — Vexa System Console',
  short_name: 'Vexa',
  description: 'Hermes multi-agent console',
  start_url: '/',
  scope: '/',
  display: 'standalone',
  orientation: 'portrait-primary',
  background_color: '#0B0D12',
  theme_color: '#0B0D12',
  icons: [
    { src: '/favicon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' },
  ],
};

export function pwaPlugin(): Plugin {
  return {
    name: 'hermes-pwa',
    apply: 'build',
    generateBundle() {
      const id = buildId();
      this.emitFile({
        type: 'asset',
        fileName: 'sw.js',
        source: SERVICE_WORKER_SOURCE.replace(/__BUILD_ID__/g, id),
      });
      this.emitFile({
        type: 'asset',
        fileName: 'manifest.json',
        source: JSON.stringify(MANIFEST, null, 2),
      });
    },
  };
}
