import { useEffect, useState } from 'react';
import { translate, type Language } from '../i18n';

type LiveFrame = {
  active: boolean;
  step?: number;
  url?: string | null;
  goal?: string;
  screenshot_b64?: string | null;
};

type Props = {
  language: Language;
};

const POLL_MS = 1500;

/**
 * Polls /api/browser/live-frame while mounted (i.e. while the floating window
 * is open) and shows the browser-runner sidecar's latest step screenshot.
 * One browser session runs at a time (browser_runner/server.py's
 * asyncio.Lock), so there is nothing to key this view by — it always shows
 * "whatever the browser agent is doing right now, if anything".
 */
export function BrowserLiveView({ language }: Props) {
  const t = (key: string) => translate(language, key);
  const [frame, setFrame] = useState<LiveFrame | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetch('/api/browser/live-frame');
        if (!response.ok || cancelled) return;
        setFrame(await response.json() as LiveFrame);
      } catch {
        /* best-effort — keep showing the last known frame */
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  if (!frame || !frame.active) {
    return (
      <div className="browser-live-view browser-live-view-idle">
        <p>{t('browserViewIdle')}</p>
      </div>
    );
  }

  return (
    <div className="browser-live-view">
      {frame.screenshot_b64 ? (
        <img
          className="browser-live-view-frame"
          src={`data:image/png;base64,${frame.screenshot_b64}`}
          alt={frame.goal || t('browserViewTitle')}
        />
      ) : (
        <div className="browser-live-view-placeholder">…</div>
      )}
      <div className="browser-live-view-caption">
        <span>{t('browserViewStep')} {frame.step ?? 0}</span>
        {frame.url && <span className="browser-live-view-url">{frame.url}</span>}
        {frame.goal && <span className="browser-live-view-goal">{frame.goal}</span>}
      </div>
    </div>
  );
}
