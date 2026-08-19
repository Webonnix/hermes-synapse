import { useEffect, useState, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { styles } from '../../styles';
import { CurrencySelect } from '../currency/CurrencySelect';
import type { CurrencySettings } from '../currency/currencyApi';
import { FREQUENCY_LABELS } from './clientLabels';
import type { CatalogueService, Client, PickerAgent } from './clientTypes';

/**
 * Right-side drawer for creating (and editing) a client (§51).
 *
 * One submit creates the client, its primary contact, its first service, that
 * service's billing schedule and — optionally — an agent connection. That is a
 * single backend transaction, so the drawer cannot leave a client half-built
 * behind it.
 */

interface DraftState {
  type: 'COMPANY' | 'PERSON';
  name: string;
  projectName: string;
  description: string;
  responsibleUserId: string;
  contactName: string;
  contactEmail: string;
  contactPhone: string;
  contactTelegram: string;
  serviceId: string;
  serviceName: string;
  serviceTitle: string;
  serviceType: 'ONE_TIME' | 'RECURRING';
  amount: string;
  currency: string;
  frequency: string;
  billingDay: string;
  nextBillingDate: string;
  customIntervalDays: string;
  agentId: string;
  connectionType: string;
}

function emptyDraft(baseCurrency: string): DraftState {
  return {
    type: 'COMPANY', name: '', projectName: '', description: '', responsibleUserId: '',
    contactName: '', contactEmail: '', contactPhone: '', contactTelegram: '',
    serviceId: '', serviceName: '', serviceTitle: '', serviceType: 'RECURRING',
    // USD by default: the base currency is the safe assumption, and anything
    // else is an explicit decision the operator makes per client.
    amount: '', currency: baseCurrency,
    frequency: 'MONTHLY', billingDay: '1', nextBillingDate: '', customIntervalDays: '',
    agentId: '', connectionType: 'INTERNAL',
  };
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="cl-field">
      <span className="cl-field-label">{label}</span>
      {children}
      {hint && <small style={styles.formHelp}>{hint}</small>}
    </label>
  );
}

export function ClientDrawer({
  open, mode, client, settings, services, agents, onClose, onSubmit,
}: {
  open: boolean;
  mode: 'create' | 'edit';
  client: Client | null;
  settings: CurrencySettings;
  services: CatalogueService[];
  agents: PickerAgent[];
  onClose: () => void;
  onSubmit: (payload: Record<string, unknown>, mode: 'create' | 'edit', clientId?: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState<DraftState>(() => emptyDraft(settings.baseCurrency));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setError('');
    if (mode === 'edit' && client) {
      const service = client.primaryService;
      const contact = client.primaryContact;
      setDraft({
        ...emptyDraft(settings.baseCurrency),
        type: client.type,
        name: client.name,
        projectName: client.projectName,
        description: client.description,
        responsibleUserId: client.responsibleUserId ?? '',
        contactName: contact?.name ?? '',
        contactEmail: contact?.email ?? '',
        contactPhone: contact?.phone ?? '',
        contactTelegram: contact?.telegram ?? '',
        serviceId: service?.serviceId ?? '',
        serviceTitle: service?.title ?? '',
        serviceType: service?.serviceType ?? 'RECURRING',
        amount: service?.amount ?? '',
        currency: service?.currency ?? settings.baseCurrency,
        frequency: service?.billing?.frequency ?? 'MONTHLY',
        billingDay: service?.billing?.billingDay ? String(service.billing.billingDay) : '1',
        nextBillingDate: service?.billing?.nextBillingDate ?? '',
        customIntervalDays: service?.billing?.customIntervalDays
          ? String(service.billing.customIntervalDays) : '',
      });
    } else {
      setDraft(emptyDraft(settings.baseCurrency));
    }
  }, [open, mode, client, settings.baseCurrency]);

  if (!open) return null;

  const set = (patch: Partial<DraftState>) => setDraft(current => ({ ...current, ...patch }));
  const recurring = draft.serviceType === 'RECURRING';

  const submit = async () => {
    if (!draft.name.trim()) {
      setError('Укажите название клиента');
      return;
    }
    setSaving(true);
    setError('');
    try {
      if (mode === 'edit' && client) {
        // Editing touches the client record only. Services and their billing
        // have their own endpoints — an edit form that silently rewrote a
        // service's price would be rewriting what the client agreed to pay.
        await onSubmit({
          type: draft.type, name: draft.name.trim(), projectName: draft.projectName.trim(),
          description: draft.description.trim(),
          responsibleUserId: draft.responsibleUserId.trim(),
        }, 'edit', client.id);
      } else {
        const payload: Record<string, unknown> = {
          type: draft.type,
          name: draft.name.trim(),
          projectName: draft.projectName.trim(),
          description: draft.description.trim(),
          responsibleUserId: draft.responsibleUserId.trim(),
          contact: {
            name: draft.contactName.trim(), email: draft.contactEmail.trim(),
            phone: draft.contactPhone.trim(), telegram: draft.contactTelegram.trim(),
            isPrimary: true,
          },
        };
        if (draft.serviceId || draft.serviceName.trim()) {
          payload.service = {
            serviceId: draft.serviceId || undefined,
            serviceName: draft.serviceName.trim(),
            title: draft.serviceTitle.trim(),
            serviceType: draft.serviceType,
            // Sent as a string so the backend's Decimal sees exactly what was
            // typed, with no float in between.
            amount: draft.amount.trim() || '0',
            currency: draft.currency,
            frequency: recurring ? draft.frequency : 'ONE_TIME',
            billingDay: recurring && draft.billingDay ? Number(draft.billingDay) : undefined,
            nextBillingDate: recurring ? (draft.nextBillingDate || undefined) : undefined,
            customIntervalDays: draft.frequency === 'CUSTOM' && draft.customIntervalDays
              ? Number(draft.customIntervalDays) : undefined,
          };
        }
        if (draft.agentId) {
          payload.agent = { agentId: draft.agentId, connectionType: draft.connectionType };
        }
        await onSubmit(payload, 'create');
      }
      onClose();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="cl-drawer-backdrop" onClick={onClose} aria-hidden />
      <aside className="cl-drawer" role="dialog" aria-modal="true" aria-label={mode === 'edit' ? 'Редактирование клиента' : 'Новый клиент'}>
        <header>
          <span>{mode === 'edit' ? 'Редактировать клиента' : 'Добавить клиента'}</span>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <X size={15} />
          </button>
        </header>

        <div className="cl-drawer-body">
          <section className="cl-drawer-section">
            <h4>Основное</h4>
            <Field label="Тип клиента">
              <select className="form-input" value={draft.type}
                      onChange={event => set({ type: event.target.value as DraftState['type'] })}>
                <option value="COMPANY">Компания</option>
                <option value="PERSON">Физическое лицо</option>
              </select>
            </Field>
            <Field label="Название">
              <input className="form-input" value={draft.name} autoFocus
                     placeholder="Например, Midot Project"
                     onChange={event => set({ name: event.target.value })} />
            </Field>
            <Field label="Название проекта">
              <input className="form-input" value={draft.projectName}
                     placeholder="Веб-платформа"
                     onChange={event => set({ projectName: event.target.value })} />
            </Field>
            <Field label="Описание">
              <textarea className="form-input" rows={2} value={draft.description}
                        onChange={event => set({ description: event.target.value })} />
            </Field>
            <Field label="Ответственный">
              <input className="form-input" value={draft.responsibleUserId}
                     placeholder="Кто ведёт клиента"
                     onChange={event => set({ responsibleUserId: event.target.value })} />
            </Field>
          </section>

          {mode === 'create' && (
            <>
              <section className="cl-drawer-section">
                <h4>Контакты</h4>
                <Field label="ФИО">
                  <input className="form-input" value={draft.contactName}
                         onChange={event => set({ contactName: event.target.value })} />
                </Field>
                <Field label="Email">
                  <input className="form-input" type="email" value={draft.contactEmail}
                         onChange={event => set({ contactEmail: event.target.value })} />
                </Field>
                <Field label="Телефон">
                  <input className="form-input" value={draft.contactPhone}
                         onChange={event => set({ contactPhone: event.target.value })} />
                </Field>
                <Field label="Telegram">
                  <input className="form-input" value={draft.contactTelegram} placeholder="@username"
                         onChange={event => set({ contactTelegram: event.target.value })} />
                </Field>
              </section>

              <section className="cl-drawer-section">
                <h4>Услуга</h4>
                <Field label="Услуга из каталога">
                  <select className="form-input" value={draft.serviceId}
                          onChange={event => set({ serviceId: event.target.value })}>
                    <option value="">— новая услуга —</option>
                    {services.map(service => (
                      <option key={service.id} value={service.id}>{service.name}</option>
                    ))}
                  </select>
                </Field>
                {!draft.serviceId && (
                  <Field label="Название новой услуги"
                         hint="Услуга добавится в каталог и станет доступна другим клиентам.">
                    <input className="form-input" value={draft.serviceName}
                           placeholder="Разработка"
                           onChange={event => set({ serviceName: event.target.value })} />
                  </Field>
                )}
                <Field label="Описание услуги для клиента">
                  <input className="form-input" value={draft.serviceTitle}
                         placeholder="Разработка и поддержка"
                         onChange={event => set({ serviceTitle: event.target.value })} />
                </Field>
                <Field label="Тип">
                  <select className="form-input" value={draft.serviceType}
                          onChange={event => set({ serviceType: event.target.value as DraftState['serviceType'] })}>
                    <option value="RECURRING">Постоянная</option>
                    <option value="ONE_TIME">Разовая</option>
                  </select>
                </Field>
              </section>

              <section className="cl-drawer-section">
                <h4>Цена</h4>
                <div className="cl-price-row">
                  <Field label="Сумма">
                    <input className="form-input" inputMode="decimal" value={draft.amount}
                           placeholder="1500"
                           onChange={event => set({ amount: event.target.value })} />
                  </Field>
                  <Field label="Валюта">
                    <CurrencySelect
                      value={draft.currency}
                      onChange={next => set({ currency: next })}
                      settings={settings}
                    />
                  </Field>
                </div>
              </section>

              {recurring && (
                <section className="cl-drawer-section">
                  <h4>Billing</h4>
                  <Field label="Периодичность">
                    <select className="form-input" value={draft.frequency}
                            onChange={event => set({ frequency: event.target.value })}>
                      {Object.entries(FREQUENCY_LABELS)
                        .filter(([value]) => value !== 'ONE_TIME')
                        .map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </Field>
                  {draft.frequency === 'CUSTOM' ? (
                    <Field label="Интервал, дней">
                      <input className="form-input" inputMode="numeric" value={draft.customIntervalDays}
                             onChange={event => set({ customIntervalDays: event.target.value })} />
                    </Field>
                  ) : (
                    <Field label="День выставления"
                           hint="Если в месяце меньше дней, счёт выставится последним числом.">
                      <input className="form-input" inputMode="numeric"
                             value={draft.billingDay}
                             onChange={event => set({ billingDay: event.target.value })} />
                    </Field>
                  )}
                  <Field label="Следующая дата"
                         hint="Оставьте пустым — рассчитается автоматически.">
                    <input className="form-input" type="date" value={draft.nextBillingDate}
                           onChange={event => set({ nextBillingDate: event.target.value })} />
                  </Field>
                </section>
              )}

              <section className="cl-drawer-section">
                <h4>Агент</h4>
                <Field label="Подключить агента"
                       hint="Агенты берутся из «Админки агентов» — здесь они только назначаются клиенту.">
                  <select className="form-input" value={draft.agentId}
                          onChange={event => set({ agentId: event.target.value })}>
                    <option value="">Не подключать сейчас</option>
                    {agents.map(agent => (
                      <option key={agent.id} value={agent.id}>
                        {agent.name}{agent.isEnabled ? '' : ' (выключен)'}
                      </option>
                    ))}
                  </select>
                </Field>
                {draft.agentId && (
                  <Field label="Тип подключения">
                    <select className="form-input" value={draft.connectionType}
                            onChange={event => set({ connectionType: event.target.value })}>
                      {['INTERNAL', 'BOT', 'API', 'TOKEN', 'CHANNEL'].map(value => (
                        <option key={value} value={value}>{value}</option>
                      ))}
                    </select>
                  </Field>
                )}
              </section>
            </>
          )}

          {error && <div className="cl-error-banner">⚠️ {error}</div>}
        </div>

        <footer className="cl-drawer-footer">
          <button type="button" className="btn-ghost" onClick={onClose}>Отмена</button>
          <button type="button" className="btn-primary" disabled={saving} onClick={submit}>
            {saving ? 'Сохранение…' : (mode === 'edit' ? 'Сохранить' : 'Добавить клиента')}
          </button>
        </footer>
      </aside>
    </>
  );
}
