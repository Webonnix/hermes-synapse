import { ChevronDown, RotateCcw, Search } from 'lucide-react';
import type { CatalogueService } from './clientTypes';
import { CONNECTION_STATUS_FULL, PAYMENT_STATUS_LABELS, SERVICE_TYPE_LABELS } from './clientLabels';

export interface ClientFilters {
  search: string;
  status: string;
  serviceType: string;
  serviceId: string;
  agentStatus: string;
  paymentStatus: string;
}

export const EMPTY_FILTERS: ClientFilters = {
  search: '', status: '', serviceType: '', serviceId: '', agentStatus: '', paymentStatus: '',
};

function Select({ label, value, onChange, options }: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <div className="cl-select-wrap">
      <select
        className="form-input"
        value={value}
        aria-label={label}
        onChange={event => onChange(event.target.value)}
      >
        <option value="">{label}</option>
        {options.map(option => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
      <ChevronDown size={14} aria-hidden />
    </div>
  );
}

/** Filter bar (§78). Every control maps to a query parameter the backend
 *  applies across the whole book, not to a client-side filter of the current
 *  page — otherwise the result count would only ever describe 50 rows. */
export function ClientsToolbar({
  filters, onChange, services, disabled,
}: {
  filters: ClientFilters;
  onChange: (next: ClientFilters) => void;
  services: CatalogueService[];
  disabled?: boolean;
}) {
  const set = (patch: Partial<ClientFilters>) => onChange({ ...filters, ...patch });
  const dirty = Object.entries(filters).some(([, value]) => value !== '');

  return (
    <div className="cl-toolbar">
      <label className="cl-search">
        <Search size={15} aria-hidden />
        <input
          className="form-input"
          type="search"
          placeholder="Поиск по названию, контакту или проекту…"
          aria-label="Поиск клиентов"
          value={filters.search}
          disabled={disabled}
          onChange={event => set({ search: event.target.value })}
        />
      </label>

      <Select
        label="Все услуги"
        value={filters.serviceId}
        onChange={serviceId => set({ serviceId })}
        options={services.map(service => ({ value: service.id, label: service.name }))}
      />
      <Select
        label="Все типы"
        value={filters.serviceType}
        onChange={serviceType => set({ serviceType })}
        options={Object.entries(SERVICE_TYPE_LABELS).map(([value, label]) => ({ value, label }))}
      />
      <Select
        label="Все статусы"
        value={filters.status}
        onChange={status => set({ status })}
        options={[
          { value: 'ACTIVE', label: 'Активен' },
          { value: 'PAUSED', label: 'Пауза' },
          { value: 'COMPLETED', label: 'Завершён' },
          { value: 'ARCHIVED', label: 'В архиве' },
        ]}
      />
      <Select
        label="Подключение агента"
        value={filters.agentStatus}
        onChange={agentStatus => set({ agentStatus })}
        options={Object.entries(CONNECTION_STATUS_FULL).map(([value, label]) => ({ value, label }))}
      />
      <Select
        label="Статус оплаты"
        value={filters.paymentStatus}
        onChange={paymentStatus => set({ paymentStatus })}
        options={Object.entries(PAYMENT_STATUS_LABELS).map(([value, label]) => ({ value, label }))}
      />

      <button
        type="button"
        className="btn-ghost"
        disabled={!dirty}
        onClick={() => onChange({ ...EMPTY_FILTERS })}
      >
        <RotateCcw size={14} />
        <span>Сбросить</span>
      </button>
    </div>
  );
}
