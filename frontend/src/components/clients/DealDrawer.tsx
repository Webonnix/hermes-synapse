import { useEffect, useState, type ReactNode } from 'react';
import { AlertTriangle, ArrowRight, Trash2, X } from 'lucide-react';
import { styles } from '../../styles';
import { CurrencySelect } from '../currency/CurrencySelect';
import type { CurrencySettings } from '../currency/currencyApi';
import { DEAL_SOURCES, DEAL_STAGE_LABELS, DEAL_STAGE_ORDER } from './clientLabels';
import type { Deal, DealStage } from './clientTypes';

/**
 * Drawer for a deal's own fields and its stage (§CRM pipeline).
 *
 * Deliberately not the tool that turns a deal into a client — that reuses
 * ClientDrawer (see its `prefill` prop) so a converted lead goes through the
 * exact same contact/service/agent form any other new client does, instead of
 * a second, thinner copy of it living here. This drawer only ever writes to
 * the `deals` table; "Конвертировать в клиента" is a button that hands off to
 * the caller (`onConvert`), which is the one that opens that other drawer.
 */

interface DraftState {
  title: string;
  contactName: string;
  contactEmail: string;
  contactPhone: string;
  companyName: string;
  value: string;
  currency: string;
  source: string;
  responsibleUserId: string;
  expectedCloseDate: string;
}

function emptyDraft(baseCurrency: string): DraftState {
  return {
    title: '', contactName: '', contactEmail: '', contactPhone: '', companyName: '',
    value: '', currency: baseCurrency, source: '', responsibleUserId: '', expectedCloseDate: '',
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

export function DealDrawer({
  open, deal, settings, onClose, onSubmit, onDelete, onConvert,
}: {
  open: boolean;
  /** `null` starts a new deal. */
  deal: Deal | null;
  settings: CurrencySettings;
  onClose: () => void;
  onSubmit: (payload: Record<string, unknown>, dealId?: string) => Promise<void>;
  onDelete: (deal: Deal) => Promise<void>;
  onConvert: (deal: Deal) => void;
}) {
  const [draft, setDraft] = useState<DraftState>(() => emptyDraft(settings.baseCurrency));
  const [stage, setStage] = useState<DealStage>('NEW');
  const [lostReason, setLostReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setError('');
    setLostReason('');
    if (deal) {
      setDraft({
        title: deal.title, contactName: deal.contactName, contactEmail: deal.contactEmail,
        contactPhone: deal.contactPhone, companyName: deal.companyName,
        value: deal.value ?? '', currency: deal.currency, source: deal.source,
        responsibleUserId: deal.responsibleUserId ?? '', expectedCloseDate: deal.expectedCloseDate ?? '',
      });
      setStage(deal.stage);
    } else {
      setDraft(emptyDraft(settings.baseCurrency));
      setStage('NEW');
    }
  }, [open, deal, settings.baseCurrency]);

  if (!open) return null;

  const set = (patch: Partial<DraftState>) => setDraft(current => ({ ...current, ...patch }));
  const converted = Boolean(deal?.clientId);

  const stageChanged = Boolean(deal) && !converted && stage !== deal?.stage;

  const submit = async () => {
    if (!draft.title.trim()) {
      setError('Укажите название сделки');
      return;
    }
    if (stageChanged && stage === 'LOST' && !lostReason.trim()) {
      setError('Укажите причину проигрыша');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const payload: Record<string, unknown> = {
        title: draft.title.trim(), contactName: draft.contactName.trim(),
        contactEmail: draft.contactEmail.trim(), contactPhone: draft.contactPhone.trim(),
        companyName: draft.companyName.trim(), value: draft.value.trim() || '',
        currency: draft.currency, source: draft.source.trim(),
        responsibleUserId: draft.responsibleUserId.trim(),
        expectedCloseDate: draft.expectedCloseDate || '',
      };
      if (stageChanged) {
        payload.stage = stage;
        if (stage === 'LOST') payload.lostReason = lostReason.trim();
      }
      await onSubmit(payload, deal?.id);
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
      <aside className="cl-drawer" role="dialog" aria-modal="true"
             aria-label={deal ? 'Редактирование сделки' : 'Новая сделка'}>
        <header>
          <span>{deal ? 'Сделка' : 'Новая сделка'}</span>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <X size={15} />
          </button>
        </header>

        <div className="cl-drawer-body">
          {converted && (
            <p className="cl-hint" style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
              <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 2 }} />
              <span>Сделка уже стала клиентом — стадия зафиксирована как «Выигран». Можно править остальные поля.</span>
            </p>
          )}

          {deal && !converted && (
            <section className="cl-drawer-section">
              <h4>Стадия</h4>
              <div className="cl-deal-stage-picker">
                {DEAL_STAGE_ORDER.map(value => (
                  <button
                    key={value} type="button"
                    className={`cl-deal-stage-btn${stage === value ? ' is-active' : ''}`}
                    onClick={() => { setStage(value); if (value !== 'LOST') setLostReason(''); }}
                  >
                    {DEAL_STAGE_LABELS[value]}
                  </button>
                ))}
              </div>
              {stage === 'LOST' && (
                <Field label="Причина проигрыша">
                  <input className="form-input" value={lostReason}
                         placeholder="Например: выбрали конкурента"
                         onChange={event => setLostReason(event.target.value)} />
                </Field>
              )}
            </section>
          )}

          <section className="cl-drawer-section">
            <h4>Основное</h4>
            <Field label="Название">
              <input className="form-input" value={draft.title} autoFocus
                     placeholder="Например, Сайт для Zenith"
                     onChange={event => set({ title: event.target.value })} />
            </Field>
            <Field label="Компания">
              <input className="form-input" value={draft.companyName}
                     onChange={event => set({ companyName: event.target.value })} />
            </Field>
            <Field label="Контакт">
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
          </section>

          <section className="cl-drawer-section">
            <h4>Оценка сделки</h4>
            <div className="cl-price-row">
              <Field label="Сумма" hint="Необязательно — оставьте пустым, если ещё не оценивали.">
                <input className="form-input" inputMode="decimal" value={draft.value}
                       placeholder="1500" onChange={event => set({ value: event.target.value })} />
              </Field>
              <Field label="Валюта">
                <CurrencySelect value={draft.currency} onChange={next => set({ currency: next })} settings={settings} />
              </Field>
            </div>
            <Field label="Источник">
              <input className="form-input" list="deal-sources" value={draft.source}
                     placeholder="Откуда пришёл лид"
                     onChange={event => set({ source: event.target.value })} />
              <datalist id="deal-sources">
                {DEAL_SOURCES.map(source => <option key={source} value={source} />)}
              </datalist>
            </Field>
            <Field label="Ответственный">
              <input className="form-input" value={draft.responsibleUserId}
                     onChange={event => set({ responsibleUserId: event.target.value })} />
            </Field>
            <Field label="Ожидаемая дата закрытия">
              <input className="form-input" type="date" value={draft.expectedCloseDate}
                     onChange={event => set({ expectedCloseDate: event.target.value })} />
            </Field>
          </section>

          {error && <div className="cl-error-banner">⚠️ {error}</div>}
        </div>

        <footer className="cl-drawer-footer">
          {deal && !converted && (
            <button type="button" className="btn-ghost" onClick={() => onConvert(deal)}
                    title="Заводит клиента и помечает сделку выигранной">
              <ArrowRight size={13} /><span>Конвертировать в клиента</span>
            </button>
          )}
          {deal && !converted && (
            <button type="button" className="btn-ghost danger" disabled={saving}
                    onClick={() => void onDelete(deal).then(onClose)}>
              <Trash2 size={13} /><span>Удалить</span>
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button type="button" className="btn-ghost" onClick={onClose}>Отмена</button>
          <button type="button" className="btn-primary" disabled={saving} onClick={submit}>
            {saving ? 'Сохранение…' : (deal ? 'Сохранить' : 'Создать сделку')}
          </button>
        </footer>
      </aside>
    </>
  );
}

export default DealDrawer;
