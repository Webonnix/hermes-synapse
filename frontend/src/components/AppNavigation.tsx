import { AudioWaveform, Briefcase, ChevronDown, Factory, FolderKanban, KeyRound, Plug, Settings, ShieldCheck, Users, ExternalLink } from 'lucide-react';
import { activeZone, matchesDestination, UTILITIES, ZONES, type Destination, type Zone } from '../navigation/routes';
import './appNavigation.css';

const ICONS = { vexa: AudioWaveform, agents: Users, work: FolderKanban, clients: Briefcase, content: Factory, operations: ShieldCheck, integrations: Plug, settings: Settings };
type Props = { language: 'ru' | 'en'; current: Destination; collapsed: boolean; overdue: number; unread: number; pendingApprovals?: number;
  onNavigate: (destination: Destination) => void; onSignOut: () => void; routerUrl: string | null };

export function AppNavigation({ language, current, collapsed, overdue, unread, pendingApprovals = 0, onNavigate, onSignOut, routerUrl }: Props) {
  const selected = activeZone(current);
  const renderZone = (zone: Zone) => {
    const Icon = ICONS[zone.id as keyof typeof ICONS];
    const active = selected.id === zone.id;
    const badge = zone.id === 'clients' ? overdue + unread : zone.id === 'operations' ? pendingApprovals : 0;
    return <div key={zone.id} className={`nav-zone${active ? ' is-active' : ''}`}>
      <button type="button" className="nav-zone-button" title={zone[language]}
        aria-current={active ? 'page' : undefined} aria-expanded={zone.pages.length ? active : undefined}
        onClick={() => onNavigate(zone.home)}>
        <Icon size={18} /><span>{zone[language]}</span>
        {badge > 0 && !collapsed && <small aria-label={zone.id === 'operations' ? (language === 'ru' ? `${badge} ждут подтверждения` : `${badge} awaiting approval`) : undefined}>{badge}</small>}
        {!!zone.pages.length && !collapsed && <ChevronDown size={13} className="nav-zone-chevron" />}
      </button>
      {active && !collapsed && zone.pages.length > 0 && <div className="nav-zone-pages" aria-label={zone[language]}>
        {zone.pages.map(page => <button key={page.en} type="button" title={page[language]}
          className={matchesDestination(current, page.to) ? 'is-active' : ''}
          aria-current={matchesDestination(current, page.to) ? 'page' : undefined}
          onClick={() => onNavigate(page.to)}>{page[language]}</button>)}
        {zone.id === 'integrations' && routerUrl && <a href={routerUrl} target="_blank" rel="noreferrer">OmniRoute <ExternalLink size={12} /></a>}
      </div>}
    </div>;
  };
  return <nav className={`domain-navigation${collapsed ? ' is-collapsed' : ''}`} aria-label={language === 'ru' ? 'Навигация' : 'Navigation'}>
    <div className="domain-primary" aria-label={language === 'ru' ? 'Рабочие зоны' : 'Workspaces'}>{ZONES.map(renderZone)}</div>
    <div className="domain-utilities">{UTILITIES.map(renderZone)}
      <button type="button" className="nav-zone-button" title={language === 'ru' ? 'Выйти' : 'Sign out'} onClick={onSignOut}>
        <KeyRound size={18} /><span>{language === 'ru' ? 'Выйти' : 'Sign out'}</span>
      </button>
    </div>
  </nav>;
}
