import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CurrencyAmount } from './CurrencyAmount';
import { convertForDisplay, formatMoney, type CurrencySettings } from './currencyApi';

const settings: CurrencySettings = {
  baseCurrency: 'USD',
  displayCurrency: 'BYN',
  rates: { USD: '1', BYN: '3.27', KZT: '515.5', CNY: '7.24' },
  currencies: [
    { code: 'USD', nameRu: 'Доллар США', nameEn: 'US Dollar', symbol: '$', decimals: 2, isBase: true },
    { code: 'BYN', nameRu: 'Белорусский рубль', nameEn: 'BYN', symbol: 'BYN', decimals: 2, isBase: false },
    { code: 'KZT', nameRu: 'Тенге', nameEn: 'KZT', symbol: '₸', decimals: 2, isBase: false },
    { code: 'CNY', nameRu: 'Юань', nameEn: 'CNY', symbol: '¥', decimals: 2, isBase: false },
  ],
  updatedAt: null,
  updatedBy: null,
};

/** Digits only — so the assertions do not depend on which space character or
 *  symbol placement the runtime's ICU data picks. */
const digits = (text: string) => text.replace(/[^\d,.]/g, '');

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('convertForDisplay', () => {
  it('multiplies from the base currency', () => {
    expect(convertForDisplay('1500', 'USD', 'BYN', settings)).toBeCloseTo(4905, 6);
  });

  it('divides into the base currency', () => {
    expect(convertForDisplay('327', 'BYN', 'USD', settings)).toBeCloseTo(100, 6);
  });

  it('pivots a cross rate through the base currency', () => {
    // 327 BYN = 100 USD = 51 550 KZT — never a stored BYN/KZT pair.
    expect(convertForDisplay('327', 'BYN', 'KZT', settings)).toBeCloseTo(51550, 4);
  });

  it('returns the amount untouched for the same currency', () => {
    expect(convertForDisplay('1234.56', 'BYN', 'BYN', settings)).toBeCloseTo(1234.56, 6);
  });

  it('refuses to invent a number when a rate is missing', () => {
    const partial = { ...settings, rates: { USD: '1' } };
    expect(convertForDisplay('100', 'BYN', 'USD', partial)).toBeNull();
  });

  it('refuses a zero or negative rate', () => {
    expect(convertForDisplay('100', 'BYN', 'USD', { ...settings, rates: { ...settings.rates, BYN: '0' } })).toBeNull();
  });
});

describe('formatMoney', () => {
  it('formats each currency with its own symbol', () => {
    expect(digits(formatMoney('1250', 'USD', settings))).toBe('1250,00');
    expect(formatMoney('1250', 'USD', settings)).toContain('$');
    expect(formatMoney('1250', 'BYN', settings)).toMatch(/BYN|Br/);
  });

  it('falls back to a plain number for a currency this runtime does not know', () => {
    const exotic = {
      ...settings,
      currencies: [...settings.currencies, { code: 'ZZZ', nameRu: '', nameEn: '', symbol: '', decimals: 2, isBase: false }],
    };
    expect(formatMoney('10', 'ZZZ', exotic)).toContain('10');
  });
});

describe('CurrencyAmount', () => {
  it('shows the converted amount with the original underneath', () => {
    const { container } = render(<CurrencyAmount amount="1500" currency="USD" settings={settings} />);
    // Primary line: 1500 USD × 3.27 = 4905 BYN.
    expect(digits(container.querySelector('.cl-amount-main')!.textContent || '')).toBe('4905,00');
    // Secondary line keeps the price the client actually agreed to.
    expect(digits(container.querySelector('.cl-amount-original')!.textContent || '')).toBe('1500,00');
  });

  it('omits the original when it is the same currency', () => {
    const { container } = render(<CurrencyAmount amount="800" currency="BYN" settings={settings} />);
    expect(container.querySelector('.cl-amount-original')).toBeNull();
  });

  it('prefers a backend-supplied display amount over converting locally', () => {
    // An invoice's frozen snapshot value: 3.27 was the rate then, whatever it
    // is now — the component must not recompute it.
    const { container } = render(
      <CurrencyAmount
        amount="1000" currency="USD" displayAmount="3270.00"
        settings={{ ...settings, rates: { ...settings.rates, BYN: '9.99' } }}
      />,
    );
    expect(digits(container.querySelector('.cl-amount-main')!.textContent || '')).toBe('3270,00');
  });

  it('renders a placeholder rather than a zero when there is no amount', () => {
    render(<CurrencyAmount amount={null} currency="USD" settings={settings} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('falls back to the original when no rate is available', () => {
    const { container } = render(
      <CurrencyAmount amount="500" currency="KZT" settings={{ ...settings, rates: { USD: '1' } }} />,
    );
    expect(digits(container.querySelector('.cl-amount-main')!.textContent || '')).toContain('500');
    expect(container.querySelector('.cl-amount-original')).toBeNull();
  });

  it('appends the billing period suffix', () => {
    const { container } = render(
      <CurrencyAmount amount="800" currency="BYN" suffix="/ мес." settings={settings} />,
    );
    expect(container.querySelector('.cl-amount-main')!.textContent).toContain('/ мес.');
  });
});
