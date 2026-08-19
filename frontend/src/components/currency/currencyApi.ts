/**
 * Currency settings, shared by every screen that shows money.
 *
 * The rule this file exists to enforce: **the frontend never does conversion
 * arithmetic.** The backend (backend/currency.py) is the only place that knows
 * how to turn one currency into another, and it already returns both the USD
 * and the display-currency value for every aggregate. What is left here is
 * formatting and a cache, which is what a browser is actually for.
 *
 * The one conversion helper below (`convertForDisplay`) is used for a single
 * job — rendering a *stored source price* (a client service's `750 USD`) in the
 * display currency inside a table cell, where round-tripping to the server per
 * row would be absurd. It applies the same rates the server sent, through the
 * same USD pivot, and is never used for anything that gets stored.
 */

export type CurrencyCode = string;

export interface CurrencyDefinition {
  code: CurrencyCode;
  nameRu: string;
  nameEn: string;
  symbol: string;
  decimals: number;
  isBase: boolean;
}

export interface CurrencySettings {
  baseCurrency: CurrencyCode;
  displayCurrency: CurrencyCode;
  /** `1 USD = rate <code>`, as exact decimal strings. */
  rates: Record<CurrencyCode, string>;
  currencies: CurrencyDefinition[];
  updatedAt: string | null;
  updatedBy: string | null;
}

export interface ExchangeRateHistoryEntry {
  id: string;
  baseCurrency: string;
  targetCurrency: string;
  rate: string;
  rateSource: string;
  validFrom: string;
  createdAt: string;
  createdBy: string | null;
}

export function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: authHeaders(), ...init });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Запрос не удался (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const FALLBACK_SETTINGS: CurrencySettings = {
  baseCurrency: 'USD',
  displayCurrency: 'USD',
  rates: { USD: '1' },
  currencies: [
    { code: 'USD', nameRu: 'Доллар США', nameEn: 'US Dollar', symbol: '$', decimals: 2, isBase: true },
  ],
  updatedAt: null,
  updatedBy: null,
};

export function fetchCurrencySettings(): Promise<CurrencySettings> {
  return api<CurrencySettings>('/api/settings/currency');
}

export function saveCurrencySettings(payload: {
  displayCurrency?: CurrencyCode;
  rates?: Record<CurrencyCode, string>;
}): Promise<CurrencySettings> {
  return api<CurrencySettings>('/api/settings/currency', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function fetchRateHistory(currency?: string): Promise<ExchangeRateHistoryEntry[]> {
  const query = currency ? `?currency_code=${encodeURIComponent(currency)}` : '';
  return api<ExchangeRateHistoryEntry[]>(`/api/settings/currency/history${query}`);
}

/** Decimals a currency shows; 2 for anything the server hasn't described. */
export function decimalsFor(settings: CurrencySettings, code: CurrencyCode): number {
  return settings.currencies.find(item => item.code === code)?.decimals ?? 2;
}

export function definitionFor(
  settings: CurrencySettings, code: CurrencyCode,
): CurrencyDefinition | undefined {
  return settings.currencies.find(item => item.code === code);
}

/**
 * Money → the string a person reads.
 *
 * Built with Intl.NumberFormat rather than string concatenation, so grouping,
 * the decimal mark and the currency's own placement follow the locale instead
 * of a hand-rolled guess. `ru-RU` matches the dashboard's default language;
 * pass a locale to follow a different one.
 */
export function formatMoney(
  amount: string | number,
  code: CurrencyCode,
  settings: CurrencySettings,
  locale = 'ru-RU',
): string {
  const value = typeof amount === 'number' ? amount : Number.parseFloat(amount || '0');
  if (!Number.isFinite(value)) return '—';
  const decimals = decimalsFor(settings, code);
  try {
    return new Intl.NumberFormat(locale, {
      style: 'currency',
      currency: code,
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(value);
  } catch {
    // An unknown ISO code (a currency added server-side that this browser's
    // ICU data predates) must still render a number, not blow up a table row.
    const number = new Intl.NumberFormat(locale, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(value);
    return `${number} ${code}`;
  }
}

/**
 * Converts a stored source amount into the display currency, using the rates
 * the server already sent, via USD — the same pivot and the same direction as
 * backend/currency.convert.
 *
 * Deliberately limited to presentation: the result is rendered next to the
 * original and never submitted anywhere. Anything that gets *stored* (an
 * invoice total, a payment) is converted on the backend, where the result is
 * frozen into a snapshot.
 */
export function convertForDisplay(
  amount: string | number,
  from: CurrencyCode,
  to: CurrencyCode,
  settings: CurrencySettings,
): number | null {
  const value = typeof amount === 'number' ? amount : Number.parseFloat(amount || '0');
  if (!Number.isFinite(value)) return null;
  if (from === to) return value;
  const fromRate = Number.parseFloat(settings.rates[from] ?? '');
  const toRate = Number.parseFloat(settings.rates[to] ?? '');
  if (!Number.isFinite(fromRate) || !Number.isFinite(toRate) || fromRate <= 0 || toRate <= 0) {
    return null;
  }
  const base = settings.baseCurrency;
  if (from === base) return value * toRate;
  const usd = value / fromRate;
  return to === base ? usd : usd * toRate;
}
