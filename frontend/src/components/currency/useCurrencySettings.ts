import { useCallback, useEffect, useState } from 'react';
import {
  FALLBACK_SETTINGS, fetchCurrencySettings, saveCurrencySettings,
  type CurrencyCode, type CurrencySettings,
} from './currencyApi';

/**
 * One shared copy of the currency settings for the whole dashboard.
 *
 * Rates and the display currency are global state that many screens read at
 * once (the KPI row, every table row, the client card, the settings page). A
 * per-component fetch would mean N requests for one answer and — worse —
 * screens disagreeing about the rate mid-session.
 *
 * So: a module-level cache with subscribers. Changing the display currency
 * writes through this store, which notifies every mounted consumer, which is
 * what makes §60 work — the dashboard re-renders in the new currency without a
 * page reload.
 */

let cache: CurrencySettings | null = null;
let inflight: Promise<CurrencySettings> | null = null;
const listeners = new Set<(settings: CurrencySettings) => void>();

function publish(settings: CurrencySettings) {
  cache = settings;
  listeners.forEach(listener => listener(settings));
}

/** Drops the cache so the next consumer refetches. Used after a save, and by
 *  tests between cases. */
export function invalidateCurrencySettings() {
  cache = null;
  inflight = null;
}

export function getCachedCurrencySettings(): CurrencySettings | null {
  return cache;
}

async function load(force = false): Promise<CurrencySettings> {
  if (cache && !force) return cache;
  // Collapse concurrent first-paint requests: a dozen components mounting at
  // once must produce one request, not a dozen.
  if (!inflight || force) {
    inflight = fetchCurrencySettings()
      .then(settings => {
        publish(settings);
        return settings;
      })
      .finally(() => { inflight = null; });
  }
  return inflight;
}

export interface UseCurrencySettings {
  settings: CurrencySettings;
  /** True until the first successful load — screens show skeletons, not zeros. */
  loading: boolean;
  error: string;
  reload: () => Promise<void>;
  /** Saves and publishes, so every other screen updates with this one. */
  save: (payload: { displayCurrency?: CurrencyCode; rates?: Record<string, string> }) => Promise<CurrencySettings>;
}

export function useCurrencySettings(): UseCurrencySettings {
  const [settings, setSettings] = useState<CurrencySettings>(cache ?? FALLBACK_SETTINGS);
  const [loading, setLoading] = useState(!cache);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    const listener = (next: CurrencySettings) => {
      if (active) setSettings(next);
    };
    listeners.add(listener);
    if (!cache) {
      load()
        .then(() => { if (active) setError(''); })
        .catch(exc => { if (active) setError((exc as Error).message); })
        .finally(() => { if (active) setLoading(false); });
    }
    return () => {
      active = false;
      listeners.delete(listener);
    };
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      await load(true);
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  const save = useCallback(async (
    payload: { displayCurrency?: CurrencyCode; rates?: Record<string, string> },
  ) => {
    const next = await saveCurrencySettings(payload);
    publish(next);
    return next;
  }, []);

  return { settings, loading, error, reload, save };
}
