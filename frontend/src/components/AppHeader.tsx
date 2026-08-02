import { useCallback, useEffect, useState } from 'react';
import { BellRing, Eye, OctagonX, Play } from 'lucide-react';
import type { ControlPlaneSummary } from '../types';
import { translate } from '../i18n';

type Props = {
  language: 'ru' | 'en';
  onOpenProcesses: () => void;
  onOpenBrowserView: () => void;
  /** When the parent already polls /api/control-plane/summary, pass the data
   * here to reuse it; the header then skips its own polling. */
  summary?: ControlPlaneSummary | null;
};

const COPY = {
  ru: {
    approvals: 'ожидают подтверждения', killSwitch: 'Аварийная остановка', resume: 'Возобновить',
    stopConfirm: 'Немедленно остановить все действия агентов?', stopped: 'ОСТАНОВЛЕНО',
  },
  en: {
    approvals: 'awaiting approval', killSwitch: 'Emergency stop', resume: 'Resume',
    stopConfirm: 'Immediately stop all agent actions?', stopped: 'STOPPED',
  },
} as const;

export function AppHeader({ language, onOpenProcesses, onOpenBrowserView, summary: externalSummary }: Props) {
  const copy = COPY[language];
  const [ownSummary, setOwnSummary] = useState<ControlPlaneSummary | null>(null);
  const summary = externalSummary ?? ownSummary;
  const [browserActive, setBrowserActive] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/control-plane/summary?limit=50');
      if (!response.ok) return;
      setOwnSummary(await response.json() as ControlPlaneSummary);
    } catch {
      /* header is best-effort; the Processes tab shows errors */
    }
  }, []);

  useEffect(() => {
    if (externalSummary !== undefined && externalSummary !== null) return;
    void load();
    const interval = window.setInterval(() => void load(), 15000);
    return () => window.clearInterval(interval);
  }, [externalSummary, load]);

  useEffect(() => {
    const loadBrowserState = async () => {
      try {
        const response = await fetch('/api/browser/live-frame');
        if (!response.ok) return;
        const data = await response.json();
        setBrowserActive(Boolean(data?.active));
      } catch {
        /* header indicator is best-effort */
      }
    };
    void loadBrowserState();
    const interval = window.setInterval(() => void loadBrowserState(), 10000);
    return () => window.clearInterval(interval);
  }, []);

  const pending = summary?.counts?.awaiting_approval || 0;
  const stopped = Boolean(summary?.state?.kill_switch);

  const toggleKill = async () => {
    try {
      if (stopped) {
        await fetch('/api/control-plane/resume', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'Resumed from header' }),
        });
      } else {
        if (!window.confirm(copy.stopConfirm)) return;
        await fetch('/api/control-plane/kill', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'Emergency stop from header' }),
        });
      }
      await load();
    } catch {
      /* surfaced on the Processes tab */
    }
  };

  return (
    <header className="app-header" role="banner">
      <button
        type="button"
        className={`app-header-approvals${pending ? ' has-pending' : ''}`}
        onClick={onOpenProcesses}
        title={`${pending} ${copy.approvals}`}
        aria-label={`${pending} ${copy.approvals}`}
      >
        <BellRing size={15} />
        <span className="app-header-badge" data-testid="approvals-badge">{pending}</span>
        <span className="app-header-badge-label">{copy.approvals}</span>
      </button>

      <button
        type="button"
        className={`app-header-kill${stopped ? ' is-stopped' : ''}`}
        onClick={() => void toggleKill()}
        title={stopped ? copy.resume : copy.killSwitch}
        aria-label={stopped ? copy.resume : copy.killSwitch}
      >
        {stopped ? <><Play size={15} />{copy.resume}</> : <><OctagonX size={15} />{copy.killSwitch}</>}
        {stopped && <em>{copy.stopped}</em>}
      </button>

      <button
        type="button"
        className={`app-header-browser-view${browserActive ? ' is-active' : ''}`}
        onClick={onOpenBrowserView}
        title={translate(language, 'browserViewOpen')}
        aria-label={translate(language, 'browserViewOpen')}
      >
        <Eye size={15} />
        {browserActive && <span className="app-header-badge-dot" aria-hidden="true" />}
      </button>
    </header>
  );
}
