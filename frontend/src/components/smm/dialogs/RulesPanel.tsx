import { useCallback, useEffect, useState } from 'react';
import { Clock, Plus, Trash2 } from 'lucide-react';
import type { BindingSettings, InboxBinding, InboxRule } from './inboxTypes';
import { PLATFORM_LABELS, inboxApi } from './inboxApi';

const ACTION_LABELS: Record<string, string> = {
  pass: 'Передать человеку', block: 'Блокировать', ai: 'Отвечает ИИ',
};

export function RulesPanel({ bindings, canEdit }: { bindings: InboxBinding[]; canEdit: boolean }) {
  const [rules, setRules] = useState<InboxRule[]>([]);
  const [error, setError] = useState('');
  const [form, setForm] = useState({ pattern: '', match: 'exact', action: 'block', binding_id: '', note: '' });
  const [busy, setBusy] = useState(false);
  const [hoursBinding, setHoursBinding] = useState('');
  const [settings, setSettings] = useState<BindingSettings | null>(null);
  const [hours, setHours] = useState({ business_hours: '', timezone: '' });
  const [hoursMessage, setHoursMessage] = useState('');

  const load = useCallback(async () => {
    try {
      setRules(await inboxApi.rules());
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!hoursBinding && bindings.length > 0) setHoursBinding(bindings[0].id);
  }, [bindings, hoursBinding]);

  useEffect(() => {
    if (!hoursBinding) return;
    let cancelled = false;
    setHoursMessage('');
    inboxApi.bindingSettings(hoursBinding)
      .then(result => {
        if (cancelled) return;
        setSettings(result);
        setHours({ business_hours: result.business_hours || '', timezone: result.timezone || '' });
      })
      .catch(exc => { if (!cancelled) setHoursMessage((exc as Error).message); });
    return () => { cancelled = true; };
  }, [hoursBinding]);

  const bindingLabel = (id: string | null) => {
    if (!id) return 'Все каналы';
    const binding = bindings.find(b => b.id === id);
    return binding ? `${PLATFORM_LABELS[binding.platform] || binding.platform} · ${binding.bot_username || binding.id}` : id;
  };

  const create = async () => {
    setBusy(true);
    try {
      await inboxApi.createRule({ ...form, binding_id: form.binding_id || null });
      setForm({ ...form, pattern: '', note: '' });
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveHours = async () => {
    setBusy(true);
    setHoursMessage('');
    try {
      const saved = await inboxApi.saveBindingSettings(hoursBinding, hours);
      setSettings(saved);
      setHoursMessage('Сохранено');
    } catch (exc) {
      setHoursMessage((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ibx-rules">
      <section className="glass-panel cl-settings-block">
        <div className="cl-settings-head">
          <div>
            <h3>Правила маршрутизации</h3>
            <p className="cl-muted">
              Правило пишется про человека: номер, @ник, email или id чата — формат номера не важен.
              Точное совпадение важнее префикса, правило канала — важнее общего. «Передать человеку»
              действует только в рабочие часы, «Блокировать» — всегда.
            </p>
          </div>
        </div>
        {error && <div className="cl-error-banner" role="alert">{error}</div>}
        {canEdit && (
          <div className="ibx-rule-form">
            <input className="form-input" aria-label="Шаблон" placeholder="+7 900 123-45-67, @nick, a@b.ru"
                   value={form.pattern} onChange={e => setForm({ ...form, pattern: e.target.value })} />
            <select className="form-input" aria-label="Совпадение" value={form.match} onChange={e => setForm({ ...form, match: e.target.value })}>
              <option value="exact">точно</option>
              <option value="prefix">начинается с</option>
            </select>
            <select className="form-input" aria-label="Действие" value={form.action} onChange={e => setForm({ ...form, action: e.target.value })}>
              {Object.entries(ACTION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <select className="form-input" aria-label="Канал правила" value={form.binding_id} onChange={e => setForm({ ...form, binding_id: e.target.value })}>
              <option value="">Все каналы</option>
              {bindings.map(binding => <option key={binding.id} value={binding.id}>{bindingLabel(binding.id)}</option>)}
            </select>
            <input className="form-input" aria-label="Заметка" placeholder="Заметка" value={form.note}
                   onChange={e => setForm({ ...form, note: e.target.value })} />
            <button type="button" className="btn-primary" disabled={busy || !form.pattern.trim()} onClick={() => void create()}>
              <Plus size={14} /><span>Добавить</span>
            </button>
          </div>
        )}
        <div className="admin-item-list">
          {rules.length === 0 && <div className="admin-empty">Правил нет — все отправители идут к агенту канала.</div>}
          {rules.map(rule => (
            <div key={rule.id} className="admin-item-row ibx-rule-row">
              <span className="ibx-mono">{rule.match === 'prefix' ? `${rule.pattern}…` : rule.pattern}</span>
              <span className={`admin-status-chip ibx-action is-${rule.action}`}>{ACTION_LABELS[rule.action] || rule.action}</span>
              <span className="cl-muted">{bindingLabel(rule.binding_id)}</span>
              <span className="cl-muted">{rule.note}</span>
              {canEdit && (
                <button type="button" className="icon-btn danger" aria-label={`Удалить правило ${rule.pattern}`}
                        onClick={async () => {
                          try {
                            await inboxApi.deleteRule(rule.id);
                            await load();
                          } catch (exc) {
                            setError((exc as Error).message);
                          }
                        }}>
                  <Trash2 size={14} />
                </button>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="glass-panel cl-settings-block">
        <div className="cl-settings-head">
          <div>
            <h3><Clock size={15} /> Рабочие часы</h3>
            <p className="cl-muted">Формат: <code>mo-fr 09:00-18:00; sa 10:00-14:00</code>. Пусто — часы не ограничены.</p>
          </div>
        </div>
        {bindings.length === 0 ? (
          <div className="admin-empty">Нет подключённых каналов.</div>
        ) : (
          <div className="ibx-hours-form">
            <select className="form-input" aria-label="Канал для часов" value={hoursBinding} onChange={e => setHoursBinding(e.target.value)}>
              {bindings.map(binding => <option key={binding.id} value={binding.id}>{bindingLabel(binding.id)}</option>)}
            </select>
            <input className="form-input" aria-label="Рабочие часы" value={hours.business_hours} disabled={!canEdit}
                   placeholder="mo-fr 09:00-18:00" onChange={e => setHours({ ...hours, business_hours: e.target.value })} />
            <input className="form-input" aria-label="Часовой пояс" value={hours.timezone} disabled={!canEdit}
                   placeholder="Asia/Jerusalem" onChange={e => setHours({ ...hours, timezone: e.target.value })} />
            {canEdit && (
              <button type="button" className="btn-primary" disabled={busy} onClick={() => void saveHours()}>Сохранить</button>
            )}
            {settings && typeof settings.within_hours_now === 'boolean' && (
              <span className={`admin-status-chip ${settings.within_hours_now ? 'is-active' : ''}`}>
                <i className="dot" />{settings.within_hours_now ? 'сейчас рабочее время' : 'сейчас нерабочее время'}
              </span>
            )}
            {hoursMessage && <span className="cl-muted" role="status">{hoursMessage}</span>}
          </div>
        )}
      </section>
    </div>
  );
}
