import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CurrencySettingsTab } from './CurrencySettingsTab';
import { invalidateCurrencySettings } from './currency/useCurrencySettings';

const settings = {
  baseCurrency: 'USD',
  displayCurrency: 'USD',
  rates: { USD: '1', BYN: '3.27', KZT: '515.5', CNY: '7.24' },
  currencies: [
    { code: 'USD', nameRu: 'Доллар США', nameEn: 'US Dollar', symbol: '$', decimals: 2, isBase: true },
    { code: 'BYN', nameRu: 'Белорусский рубль', nameEn: 'Belarusian Ruble', symbol: 'BYN', decimals: 2, isBase: false },
    { code: 'KZT', nameRu: 'Казахстанский тенге', nameEn: 'Tenge', symbol: '₸', decimals: 2, isBase: false },
    { code: 'CNY', nameRu: 'Китайский юань', nameEn: 'Yuan', symbol: '¥', decimals: 2, isBase: false },
  ],
  updatedAt: null,
  updatedBy: null,
};

function stubFetch(onPatch?: (body: Record<string, unknown>) => unknown) {
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === '/api/settings/currency' && init?.method === 'PATCH') {
      const body = JSON.parse((init.body as string) || '{}');
      const result = onPatch?.(body);
      if (result === 'error') {
        return { ok: false, status: 400, json: async () => ({ detail: 'rate for BYN must be greater than 0' }) };
      }
      return { ok: true, json: async () => ({ ...settings, ...body, rates: { ...settings.rates, ...(body.rates || {}) } }) };
    }
    if (path.startsWith('/api/settings/currency/history')) return { ok: true, json: async () => [] };
    if (path.startsWith('/api/settings/currency')) return { ok: true, json: async () => settings };
    return { ok: true, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', handler);
  return handler;
}

beforeEach(() => { invalidateCurrencySettings(); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('CurrencySettingsTab', () => {
  it('shows USD as a read-only base currency', async () => {
    stubFetch();
    render(<CurrencySettingsTab />);
    const base = await screen.findByLabelText('Базовая валюта');
    expect(base).toBeDisabled();
    expect(base).toHaveValue('USD — US Dollar');
    expect(screen.getByText(/USD используется как базовая валюта/)).toBeInTheDocument();
  });

  it('renders one editable rate per non-base currency', async () => {
    stubFetch();
    render(<CurrencySettingsTab />);
    expect(await screen.findByLabelText('Курс USD к BYN')).toHaveValue('3.2700');
    expect(screen.getByLabelText('Курс USD к KZT')).toHaveValue('515.5000');
    expect(screen.getByLabelText('Курс USD к CNY')).toHaveValue('7.2400');
    // USD has no input: 1 USD = 1 USD is an identity, not a setting.
    expect(screen.queryByLabelText('Курс USD к USD')).not.toBeInTheDocument();
  });

  it('saves rates as strings so no float rounding happens on the way out', async () => {
    const received: Record<string, unknown>[] = [];
    stubFetch(body => { received.push(body); return undefined; });
    render(<CurrencySettingsTab />);

    fireEvent.change(await screen.findByLabelText('Курс USD к BYN'), { target: { value: '3.3100' } });
    fireEvent.click(screen.getByText('Сохранить курсы'));

    await waitFor(() => expect(received).toHaveLength(1));
    expect((received[0].rates as Record<string, string>).BYN).toBe('3.3100');
    expect(typeof (received[0].rates as Record<string, string>).BYN).toBe('string');
    expect(await screen.findByText('Курсы валют обновлены')).toBeInTheDocument();
  });

  it('refuses a non-positive rate before it reaches the server', async () => {
    const handler = stubFetch();
    render(<CurrencySettingsTab />);
    fireEvent.change(await screen.findByLabelText('Курс USD к BYN'), { target: { value: '0' } });
    fireEvent.click(screen.getByText('Сохранить курсы'));

    expect(await screen.findByText('Введите корректный курс больше 0')).toBeInTheDocument();
    expect(handler.mock.calls.some(([, init]) => (init as RequestInit)?.method === 'PATCH')).toBe(false);
  });

  it('surfaces a server-side rejection', async () => {
    stubFetch(() => 'error');
    render(<CurrencySettingsTab />);
    fireEvent.change(await screen.findByLabelText('Курс USD к BYN'), { target: { value: '3.5' } });
    fireEvent.click(screen.getByText('Сохранить курсы'));
    expect(await screen.findByText(/must be greater than 0/)).toBeInTheDocument();
  });

  it('saves the display currency separately from the rates', async () => {
    const received: Record<string, unknown>[] = [];
    stubFetch(body => { received.push(body); return undefined; });
    render(<CurrencySettingsTab />);

    fireEvent.change(await screen.findByLabelText('Валюта отображения'), { target: { value: 'BYN' } });
    fireEvent.click(screen.getByText('Сохранить'));

    await waitFor(() => expect(received).toHaveLength(1));
    expect(received[0].displayCurrency).toBe('BYN');
    expect(received[0].rates).toBeUndefined();
    expect(await screen.findByText('Валюта отображения сохранена')).toBeInTheDocument();
  });

  it('disables the display save until something changes', async () => {
    stubFetch();
    render(<CurrencySettingsTab />);
    await screen.findByLabelText('Валюта отображения');
    expect(screen.getByText('Сохранить')).toBeDisabled();
  });
});
