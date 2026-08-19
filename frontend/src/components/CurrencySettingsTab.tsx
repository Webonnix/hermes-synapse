import { useEffect, useMemo, useState } from 'react';
import { Coins, History, Lock, RefreshCw } from 'lucide-react';
import { styles } from '../styles';
import { CurrencySelect } from './currency/CurrencySelect';
import { ExchangeRateInput } from './currency/ExchangeRateInput';
import { fetchRateHistory, type ExchangeRateHistoryEntry } from './currency/currencyApi';
import { useCurrencySettings } from './currency/useCurrencySettings';

/**
 * Настройки → Валюта и курсы.
 *
 * Two independent saves on purpose. Editing the rates restates every financial
 * aggregate in the system and belongs to its own deliberate action; switching
 * the display currency changes nothing but what you are looking at. Bundling
 * them behind one button would make a glance-level preference feel as heavy as
 * a books-level change — and vice versa.
 */

/** Mirrors backend validation (rate > 0, finite) so the operator hears about a
 *  typo before the round trip — the server still refuses it regardless. */
function validateRate(raw: string): string {
  const text = (raw || '').trim().replace(',', '.');
  if (!text) return 'Введите курс';
  const value = Number(text);
  if (!Number.isFinite(value)) return 'Введите корректный курс больше 0';
  if (value <= 0) return 'Введите корректный курс больше 0';
  return '';
}

/** The stored rate as an editable draft, at the 4-decimal precision the spec's
 *  examples use. Kept separate from `decimalToText` because this is a form
 *  value, not a stored figure: it renders even before the first `useEffect`
 *  pass has seeded the draft state. */
function storedRateToDraft(stored: string | undefined): string {
  return stored ? Number(stored).toFixed(4) : '';
}

export function CurrencySettingsTab() {
  const { settings, loading, error, reload, save } = useCurrencySettings();

  const [draftRates, setDraftRates] = useState<Record<string, string>>({});
  const [draftDisplay, setDraftDisplay] = useState(settings.displayCurrency);
  const [rateErrors, setRateErrors] = useState<Record<string, string>>({});
  const [toast, setToast] = useState('');
  const [savingRates, setSavingRates] = useState(false);
  const [savingDisplay, setSavingDisplay] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [history, setHistory] = useState<ExchangeRateHistoryEntry[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);

  // Currencies the operator may set a rate for: everything except the base,
  // whose rate against itself is 1 by definition.
  const editable = useMemo(
    () => settings.currencies.filter(item => item.code !== settings.baseCurrency),
    [settings.currencies, settings.baseCurrency],
  );
  const base = useMemo(
    () => settings.currencies.find(item => item.code === settings.baseCurrency),
    [settings.currencies, settings.baseCurrency],
  );

  // Re-seed the form whenever the shared settings change (first load, or
  // another screen saving). Rates are shown at 4 decimals — the precision the
  // spec's examples use — while the stored value keeps whatever it was given.
  useEffect(() => {
    const next: Record<string, string> = {};
    settings.currencies.forEach(item => {
      if (item.code === settings.baseCurrency) return;
      next[item.code] = storedRateToDraft(settings.rates[item.code]);
    });
    setDraftRates(next);
    setDraftDisplay(settings.displayCurrency);
  }, [settings]);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(''), 2600);
    return () => clearTimeout(timer);
  }, [toast]);

  const loadHistory = async () => {
    setHistoryOpen(open => !open);
    if (history.length) return;
    try {
      setHistory(await fetchRateHistory());
    } catch (exc) {
      setSaveError((exc as Error).message);
    }
  };

  const saveRates = async () => {
    const errors: Record<string, string> = {};
    editable.forEach(item => {
      const message = validateRate(draftRates[item.code] ?? '');
      if (message) errors[item.code] = message;
    });
    setRateErrors(errors);
    if (Object.keys(errors).length) return;

    setSavingRates(true);
    setSaveError('');
    try {
      const payload: Record<string, string> = {};
      editable.forEach(item => {
        payload[item.code] = (draftRates[item.code] || '').trim().replace(',', '.');
      });
      await save({ rates: payload });
      // Rates moved, so every cached financial figure on other screens is now
      // stale — they read the same store and re-render from this publish.
      setHistory([]);
      setToast('Курсы валют обновлены');
    } catch (exc) {
      setSaveError((exc as Error).message);
    } finally {
      setSavingRates(false);
    }
  };

  const saveDisplay = async () => {
    setSavingDisplay(true);
    setSaveError('');
    try {
      await save({ displayCurrency: draftDisplay });
      setToast('Валюта отображения сохранена');
    } catch (exc) {
      setSaveError((exc as Error).message);
    } finally {
      setSavingDisplay(false);
    }
  };

  if (loading) {
    return (
      <div style={styles.tabWrapper}>
        <div style={styles.tabHeader}>
          <div>
            <h2 className="glow-text-cyan" style={styles.tabTitle}>Валюта и курсы</h2>
            <p style={styles.tabSubtitle}>Настройте базовую валюту, курсы конвертации и валюту отображения</p>
          </div>
        </div>
        <div className="cl-skeleton-grid">
          {[0, 1, 2].map(index => <div key={index} className="cl-skeleton cl-skeleton-card" />)}
        </div>
      </div>
    );
  }

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>Валюта и курсы</h2>
          <p style={styles.tabSubtitle}>
            Настройте базовую валюту, курсы конвертации и валюту отображения
          </p>
        </div>
        <button type="button" className="btn-ghost" onClick={reload} title="Обновить">
          <RefreshCw size={15} />
          <span>Обновить</span>
        </button>
      </div>

      {error && (
        <div className="cl-error-banner">
          <span>Не удалось загрузить настройки валют</span>
          <button type="button" className="btn-ghost" onClick={reload}>Повторить</button>
        </div>
      )}
      {saveError && <div className="cl-error-banner">⚠️ {saveError}</div>}
      {toast && <div className="cl-toast" role="status">{toast}</div>}

      {/* ── Базовая валюта ─────────────────────────────────────────────── */}
      <section className="glass-panel cl-settings-block">
        <header className="cl-settings-head">
          <Lock size={14} />
          <h3>Базовая валюта</h3>
        </header>
        <div className="cl-base-currency">
          <strong>{settings.baseCurrency}</strong>
          <span>{base ? `${base.nameEn} · ${base.nameRu}` : ''}</span>
        </div>
        <input
          className="form-input"
          value={`${settings.baseCurrency} — ${base?.nameEn ?? ''}`}
          readOnly
          disabled
          aria-label="Базовая валюта"
        />
        <p style={styles.formHelp}>
          USD используется как базовая валюта для финансовых расчетов и аналитики.
          Все суммы нормализуются через USD, поэтому изменить её нельзя.
        </p>
      </section>

      {/* ── Курсы валют ────────────────────────────────────────────────── */}
      <section className="glass-panel cl-settings-block">
        <header className="cl-settings-head">
          <Coins size={14} />
          <h3>Курсы валют</h3>
          <button type="button" className="cl-link-btn" onClick={loadHistory}>
            <History size={13} />
            <span>{historyOpen ? 'Скрыть историю' : 'История изменений'}</span>
          </button>
        </header>

        <div className="cl-rate-grid">
          {editable.map(item => (
            <ExchangeRateInput
              key={item.code}
              definition={item}
              baseCurrency={settings.baseCurrency}
              value={draftRates[item.code] ?? storedRateToDraft(settings.rates[item.code])}
              disabled={savingRates}
              error={rateErrors[item.code]}
              onChange={next => {
                setDraftRates(current => ({ ...current, [item.code]: next }));
                setRateErrors(current => ({ ...current, [item.code]: '' }));
              }}
            />
          ))}
        </div>

        <button
          type="button"
          className="btn-primary"
          style={{ alignSelf: 'flex-start' }}
          disabled={savingRates}
          onClick={saveRates}
        >
          {savingRates ? 'Сохранение…' : 'Сохранить курсы'}
        </button>

        {historyOpen && (
          <div className="admin-table-wrap" style={{ marginTop: 14 }}>
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Валюта</th><th>Курс</th><th>Источник</th><th>Действует с</th><th>Кто изменил</th>
                </tr>
              </thead>
              <tbody>
                {history.length === 0 && (
                  <tr><td colSpan={5} className="admin-empty">История пока пуста</td></tr>
                )}
                {history.map(entry => (
                  <tr key={entry.id}>
                    <td>{entry.baseCurrency}/{entry.targetCurrency}</td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{entry.rate}</td>
                    <td>{entry.rateSource}</td>
                    <td>{new Date(entry.validFrom).toLocaleString('ru-RU')}</td>
                    <td>{entry.createdBy || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p style={styles.formHelp}>
              Курсы не перезаписываются: каждое изменение добавляет новую запись, а уже
              выставленные счета сохраняют курс на момент выставления.
            </p>
          </div>
        )}
      </section>

      {/* ── Валюта отображения ─────────────────────────────────────────── */}
      <section className="glass-panel cl-settings-block">
        <header className="cl-settings-head">
          <Coins size={14} />
          <h3>Валюта отображения</h3>
        </header>
        <div className="cl-display-row">
          <CurrencySelect
            value={draftDisplay}
            onChange={setDraftDisplay}
            settings={settings}
            disabled={savingDisplay}
            ariaLabel="Валюта отображения"
            withNames
          />
          <button
            type="button"
            className="btn-primary"
            disabled={savingDisplay || draftDisplay === settings.displayCurrency}
            onClick={saveDisplay}
          >
            {savingDisplay ? 'Сохранение…' : 'Сохранить'}
          </button>
        </div>
        <p style={styles.formHelp}>
          Все финансовые показатели интерфейса будут отображаться в выбранной валюте.
          Исходные суммы услуг и зафиксированные суммы выставленных счетов не изменятся.
        </p>
      </section>
    </div>
  );
}
