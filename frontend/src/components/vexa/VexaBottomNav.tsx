import { BarChart3, Settings, ShieldCheck, Terminal, Users } from 'lucide-react';
import type { VexaCopy } from './vexaCopy';
import type { DashboardRoute } from './vexaDashboardTypes';

/**
 * Bottom navigation.
 *
 * Hermes has no router — the workspace is driven by App.tsx's `activeTab`, so each entry
 * maps onto an existing tab rather than a URL. The route ids stay in the design's
 * vocabulary so a future router can adopt them without touching this component.
 */

interface Props {
  copy: VexaCopy;
  active: DashboardRoute;
  onNavigate: (route: DashboardRoute) => void;
  version: string;
  connected: boolean;
}

export function VexaBottomNav({ copy, active, onNavigate, version, connected }: Props) {
  const items: Array<{ id: DashboardRoute; label: string; icon: React.ReactNode }> = [
    { id: 'terminal', label: copy.navTerminal, icon: <Terminal size={15} /> },
    { id: 'analytics', label: copy.navAnalytics, icon: <BarChart3 size={15} /> },
    { id: 'agents', label: copy.navAgents, icon: <Users size={15} /> },
    { id: 'protocols', label: copy.navProtocols, icon: <ShieldCheck size={15} /> },
    { id: 'settings', label: copy.navSettings, icon: <Settings size={15} /> },
  ];

  return (
    <nav className="vx-bottom-nav" aria-label={copy.navTerminal}>
      {items.map(item => (
        <button
          type="button"
          key={item.id}
          className={`vx-nav-item${active === item.id ? ' is-active' : ''}`}
          onClick={() => onNavigate(item.id)}
          aria-current={active === item.id ? 'page' : undefined}
        >
          {item.icon}
          <span>{item.label}</span>
        </button>
      ))}
      <span className="vx-version">
        {copy.version} v{version}
        <i className={connected ? 'is-on' : ''} />
        <i />
        <i />
      </span>
    </nav>
  );
}
