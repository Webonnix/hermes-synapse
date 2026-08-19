import { convertForDisplay, formatMoney, type CurrencyCode, type CurrencySettings } from './currencyApi';
import { useCurrencySettings } from './useCurrencySettings';

/**
 * The one component that renders money.
 *
 * Every price, total and aggregate in the clients module goes through here, so
 * the conversion-and-formatting rule lives in exactly one place (§71). It shows
 * the amount in the display currency and, when that differs from the currency
 * the amount was actually agreed in, keeps the original visible underneath:
 *
 *     4 905,00 BYN
 *     ≈ 1 500,00 $
 *
 * Losing the source currency would make the row a lie — the client pays
 * $1,500, not 4 905 BYN, and the BYN figure moves whenever the rate does.
 */

export interface CurrencyAmountProps {
  amount: string | number | null | undefined;
  /** The currency the amount is stored in. */
  currency: CurrencyCode;
  /** Override the global display currency (rare — mostly for previews). */
  displayCurrency?: CurrencyCode;
  /** Show the original amount below when it differs from what is displayed. */
  showOriginal?: boolean;
  /** Appended to the primary line, e.g. "/ мес.". */
  suffix?: string;
  /** Pre-converted value from the backend; skips client-side conversion. */
  displayAmount?: string | number | null;
  settings?: CurrencySettings;
  className?: string;
  /** Rendered when there is no amount at all. */
  placeholder?: string;
}

export function CurrencyAmount({
  amount, currency, displayCurrency, showOriginal = true, suffix,
  displayAmount, settings: settingsOverride, className, placeholder = '—',
}: CurrencyAmountProps) {
  const hook = useCurrencySettings();
  const settings = settingsOverride ?? hook.settings;
  const target = displayCurrency ?? settings.displayCurrency;

  if (amount === null || amount === undefined || amount === '') {
    return <span className={className}>{placeholder}</span>;
  }

  // Prefer the backend's own converted figure when it sent one: it was computed
  // with Decimal precision, and for an invoice it is the frozen snapshot value
  // rather than anything today's rates would produce.
  const converted = displayAmount !== null && displayAmount !== undefined && displayAmount !== ''
    ? Number(displayAmount)
    : convertForDisplay(amount, currency, target, settings);

  if (converted === null || !Number.isFinite(converted)) {
    // No usable rate — show the honest original rather than a made-up number.
    return (
      <span className={className}>
        <span className="cl-amount-main">{formatMoney(amount, currency, settings)}{suffix ? ` ${suffix}` : ''}</span>
      </span>
    );
  }

  const sameCurrency = target === currency;
  return (
    <span className={`cl-amount${className ? ` ${className}` : ''}`}>
      <span className="cl-amount-main">
        {formatMoney(converted, target, settings)}{suffix ? ` ${suffix}` : ''}
      </span>
      {showOriginal && !sameCurrency && (
        <span className="cl-amount-original" title={`Исходная сумма: ${formatMoney(amount, currency, settings)}`}>
          ≈ {formatMoney(amount, currency, settings)}
        </span>
      )}
    </span>
  );
}
