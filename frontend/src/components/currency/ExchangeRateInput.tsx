import type { CurrencyDefinition } from './currencyApi';

/**
 * One editable `1 USD = [ x ] BYN` row.
 *
 * The value is held as a string all the way to the server: parsing it into a
 * JS number here would round 515.5000 through binary floating point before the
 * backend's Decimal ever saw it, which is precisely what the money rules
 * forbid.
 */
export function ExchangeRateInput({
  definition, baseCurrency, value, onChange, disabled, error,
}: {
  definition: CurrencyDefinition;
  baseCurrency: string;
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  error?: string;
}) {
  const inputId = `rate-${definition.code}`;
  return (
    <div className={`cl-rate-card${error ? ' is-invalid' : ''}`}>
      <div className="cl-rate-head">
        <strong>{definition.nameRu}</strong>
        <span className="cl-rate-code">{definition.code}</span>
      </div>
      <label className="cl-rate-row" htmlFor={inputId}>
        <span>1 {baseCurrency} =</span>
        <input
          id={inputId}
          className="form-input cl-rate-input"
          inputMode="decimal"
          value={value}
          disabled={disabled}
          aria-label={`Курс ${baseCurrency} к ${definition.code}`}
          aria-invalid={Boolean(error)}
          onChange={event => onChange(event.target.value)}
        />
        <span>{definition.code}</span>
      </label>
      {error && <span className="cl-rate-error">{error}</span>}
    </div>
  );
}
