/**
 * The frontend talks to its own origin, and nginx proxies /api to the backend.
 *
 * Eight calls in App.tsx were addressed to `http://localhost:8000` — the port
 * the backend answers on *inside the server*. From a browser that is the user's
 * own machine, where nothing is listening, so every one of them failed. They
 * were fire-and-forget (`try { await … } catch { console.error }`), so nothing
 * surfaced: creating a chat silently lost its name, its folder and its agent,
 * and the agent picker in an open chat did nothing at all.
 *
 * A grep is a blunt test, but this is a class of bug no component test catches:
 * the calls have no rendered output to assert on, and a mocked `fetch` answers
 * any URL you give it.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const SOURCE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry) ? [full] : [];
  });
}

describe('backend URLs', () => {
  it('never hardcodes the backend host and port', () => {
    const offenders = sourceFiles(SOURCE_ROOT)
      .filter(file => /https?:\/\/(localhost|127\.0\.0\.1):8000/.test(readFileSync(file, 'utf8')))
      .map(file => file.slice(SOURCE_ROOT.length + 1));

    expect(offenders).toEqual([]);
  });
});
