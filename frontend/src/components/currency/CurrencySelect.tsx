import { ChevronDown } from 'lucide-react';
import type { CurrencyCode, CurrencySettings } from './currencyApi';

/** Currency picker, driven by whatever the backend says it supports — adding a
 *  currency server-side makes it appear here with no frontend change. */
export function CurrencySelect({
  value, onChange, settings, disabled, id, ariaLabel = 'Валюта', withNames = false,
}: {
  value: CurrencyCode;
  onChange: (next: CurrencyCode) => void;
  settings: CurrencySettings;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
  withNames?: boolean;
}) {
  return (
    <div className="cl-select-wrap">
      <select
        id={id}
        className="form-input"
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={event => onChange(event.target.value)}
      >
        {settings.currencies.map(item => (
          <option key={item.code} value={item.code}>
            {withNames ? `${item.code} — ${item.nameRu}` : item.code}
          </option>
        ))}
      </select>
      <ChevronDown size={14} aria-hidden />
    </div>
  );
}
