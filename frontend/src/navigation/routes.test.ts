import { describe, expect, it } from 'vitest';
import { activeZone, appTabs, normalizeTab, UTILITIES, ZONES } from './routes';

describe('domain navigation', () => {
  it('offers six primary workspaces and resolves every legacy tab', () => {
    expect(ZONES).toHaveLength(6);
    for (const tab of appTabs) expect(normalizeTab(tab)).toBe(tab);
    expect(normalizeTab('inbox')).toBe('smm');
    expect(normalizeTab('unknown')).toBe('vexa');
    for (const zone of [...ZONES, ...UTILITIES]) {
      expect(activeZone(zone.home).id).toBe(zone.id);
      for (const page of zone.pages) expect(activeZone(page.to).id).toBe(zone.id);
    }
  });
  it('separates approvals, runs, inbox and publishing while using canonical pages', () => {
    expect(activeZone({ tab: 'processes', filter: 'approval' }).id).toBe('operations');
    expect(activeZone({ tab: 'processes' }).id).toBe('work');
    expect(activeZone({ tab: 'smm', view: 'dialogs' }).id).toBe('clients');
    expect(activeZone({ tab: 'smm', view: 'publications' }).id).toBe('content');
    expect(activeZone({ tab: 'smm', view: 'connections' }).id).toBe('integrations');
    expect(activeZone({ tab: 'settings', section: 'logs' }).id).toBe('operations');
    expect(activeZone({ tab: 'settings', section: 'browser' }).id).toBe('integrations');
  });
});
