import { useCallback, useEffect, useState } from 'react';
import { BarChart3, Bell, Maximize, MessageSquare, Minimize, Package, PanelLeft, PanelRight, Settings, Sparkles, Users, Zap } from 'lucide-react';
import type { VexaCopy } from './vexaCopy';
import type { GlobalSystemState } from './vexaDashboardTypes';

/** Which side column is currently pulled open as a drawer on a narrow viewport. */
export type SidePanel = 'system' | 'metrics' | 'right';

interface Props {
  copy: VexaCopy;
  state: GlobalSystemState;
  stale: boolean;
  runtimeLabel: string;
  runtimeTone: 'normal' | 'warn' | 'error';
  pendingConfirmations: number;
  /** Leaves the dashboard for the minimal chat workspace; omitted when unavailable. */
  onSwitchToSimpleView?: () => void;
  onOpenAnalytics: () => void;
  onOpenAgents: () => void;
  onOpenProcesses: () => void;
  onOpenConfirmations: () => void;
  onOpenSettings: () => void;
  openPanel: SidePanel | null;
  onTogglePanel: (panel: SidePanel) => void;
}

function useFullscreen() {
  const [active, setActive] = useState(() => Boolean(document.fullscreenElement));

  useEffect(() => {
    const onChange = () => setActive(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggle = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void document.documentElement.requestFullscreen().catch(() => {
        // Some embedders (iframes without allow="fullscreen", certain browsers) reject
        // the request — the button just stays a no-op rather than throwing.
      });
    }
  }, []);

  return [active, toggle] as const;
}

function VexaLogo() {
  return (
    <span className="vx-brand-mark" aria-hidden="true">
      <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
        <defs>
          <linearGradient id="vx-logo-gradient" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#1bdcff" />
            <stop offset="52%" stopColor="#2684ff" />
            <stop offset="100%" stopColor="#8958ff" />
          </linearGradient>
        </defs>
        <path d="M14 2 L26 7 L14 26 L2 7 Z" stroke="url(#vx-logo-gradient)" strokeWidth="1.6" fill="rgba(27,220,255,.08)" strokeLinejoin="round" />
        <path d="M8.5 9 L14 19.5 L19.5 9" stroke="url(#vx-logo-gradient)" strokeWidth="1.8" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

export function VexaTopHeader({
  copy, state, stale, runtimeLabel, runtimeTone,
  pendingConfirmations,
  onSwitchToSimpleView,
  onOpenAnalytics, onOpenAgents, onOpenProcesses,
  onOpenConfirmations, onOpenSettings,
  openPanel, onTogglePanel,
}: Props) {

  const [isFullscreen, toggleFullscreen] = useFullscreen();

  const statusText: Record<GlobalSystemState, string> = {
    online: copy.systemOnline,
    degraded: copy.systemDegraded,
    maintenance: copy.systemMaintenance,
    offline: copy.systemOffline,
    emergency_stopped: copy.systemEmergency,
  };

  const statusHint = stale
    ? copy.systemStaleHint
    : state === 'online' ? copy.systemOnlineHint
      : state === 'offline' ? copy.systemOfflineHint
        : state === 'emergency_stopped' ? copy.emergencyActive
          : copy.systemDegradedHint;

  return (
    <header className="vx-header" role="banner">
      <div className="vx-brand">
        <VexaLogo />
        <span className="vx-brand-name">{copy.brand}</span>
        <span className="vx-brand-sub">{copy.brandSub}</span>
      </div>

      <div className={`vx-global-status is-${state}`} role="status" aria-live="polite">
        <i aria-hidden="true" />
        <strong>{statusText[state]}</strong>
        <em>{statusHint}</em>
      </div>

      <div className="vx-quick-actions">
        {/* Below 1280px the side columns collapse into drawers; these are their only way
            back on screen, so they live in the header rather than inside a column. */}
        <button
          type="button"
          className={`vx-quick-btn vx-panel-toggle${openPanel === 'system' ? ' is-open' : ''}`}
          onClick={() => onTogglePanel('system')}
          aria-pressed={openPanel === 'system'}
          title={copy.systemStatus}
          aria-label={copy.systemStatus}
        >
          <PanelLeft size={17} />
        </button>
        <button
          type="button"
          className={`vx-quick-btn vx-panel-toggle${openPanel === 'metrics' ? ' is-open' : ''}`}
          onClick={() => onTogglePanel('metrics')}
          aria-pressed={openPanel === 'metrics'}
          title={copy.resources}
          aria-label={copy.resources}
        >
          <BarChart3 size={17} />
        </button>
        <button
          type="button"
          className={`vx-quick-btn vx-panel-toggle${openPanel === 'right' ? ' is-open' : ''}`}
          onClick={() => onTogglePanel('right')}
          aria-pressed={openPanel === 'right'}
          title={copy.agentCircuit}
          aria-label={copy.agentCircuit}
        >
          <PanelRight size={17} />
        </button>

        <button type="button" className="vx-quick-btn vx-quick-nav" onClick={onOpenAnalytics} title={copy.quickTelemetry} aria-label={copy.quickTelemetry}>
          <Sparkles size={17} />
        </button>
        <button type="button" className="vx-quick-btn vx-quick-nav" onClick={onOpenAgents} title={copy.quickAgents} aria-label={copy.quickAgents}>
          <Users size={17} />
        </button>
        <button type="button" className="vx-quick-btn vx-quick-nav" onClick={onOpenProcesses} title={copy.quickSecurity} aria-label={copy.quickSecurity}>
          <Package size={17} />
        </button>
        <button
          type="button"
          className={`vx-quick-btn${pendingConfirmations ? ' is-alert' : ''}`}
          onClick={onOpenConfirmations}
          title={`${copy.confirmations}: ${pendingConfirmations}`}
          aria-label={`${copy.confirmations}: ${pendingConfirmations}`}
        >
          <Bell size={17} />
          {pendingConfirmations > 0 && <span className="vx-quick-badge">{pendingConfirmations}</span>}
        </button>
        <button type="button" className="vx-quick-btn vx-quick-nav" onClick={onOpenSettings} title={copy.quickSettings} aria-label={copy.quickSettings}>
          <Settings size={17} />
        </button>
        <button
          type="button"
          className="vx-quick-btn vx-quick-nav"
          onClick={toggleFullscreen}
          aria-pressed={isFullscreen}
          title={isFullscreen ? copy.exitFullscreen : copy.fullscreen}
          aria-label={isFullscreen ? copy.exitFullscreen : copy.fullscreen}
        >
          {isFullscreen ? <Minimize size={17} /> : <Maximize size={17} />}
        </button>

        {onSwitchToSimpleView && (
          <button
            type="button"
            className="vx-pill"
            onClick={onSwitchToSimpleView}
            title={copy.simpleMode}
          >
            <MessageSquare size={15} />
            <span>{copy.simpleMode}</span>
          </button>
        )}

        <span
          className={`vx-pill is-state${runtimeTone === 'warn' ? ' is-warn' : runtimeTone === 'error' ? ' is-error' : ''}`}
          role="status"
          aria-live="polite"
        >
          <Zap size={15} />
          <span>{runtimeLabel}</span>
        </span>
      </div>
    </header>
  );
}
