/**
 * SMM FACTORY — every channel the agents talk to the outside world through,
 * and everything those channels put out.
 *
 * This section replaces the old "Настройки → Каналы связи" page. The move is
 * not cosmetic: connecting an account, watching the conversations it produces,
 * approving the drafts it queues and reading the gateway's log are four views
 * of one job, and only the first of them is a setting. Buried in Настройки the
 * other three effectively did not exist.
 *
 * "Публикации" and "Дизайнер" are the outbound half, added later: everything
 * else here answers people who wrote first, while these two are what the
 * accounts say on their own. They share one register
 * (backend/smm_posts.py) and one editor, because a draft, a scheduled post and
 * a published one are the same object at different stages.
 *
 * The rest are deliberately thin wrappers over what already worked:
 * "Подключения", "Черновики" and "Журнал" are the same MessengerChannelsTab
 * rendered one block at a time (see its `view` prop), so there is one
 * implementation of connecting and editing a channel, not two that drift.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  Activity, Link2, MessagesSquare, Palette, Plus, Send, SlidersHorizontal, Sparkles,
} from 'lucide-react';
import { styles } from '../styles';
import type { AgentModel } from '../types';
import { MessengerChannelsTab } from './MessengerChannelsTab';
import { SmmDialogsPanel } from './smm/SmmDialogsPanel';
import { SmmSettingsPanel } from './smm/SmmSettingsPanel';
import { SmmPublicationsPanel } from './smm/SmmPublicationsPanel';
import { SmmDesignerPanel } from './smm/SmmDesignerPanel';
import { authHeaders } from './smm/platforms';
import type { Publication } from './smm/publications';

type View = 'publications' | 'designer' | 'connections' | 'dialogs' | 'drafts' | 'activity' | 'settings';

const VIEWS: [View, string, typeof Link2][] = [
  ['publications', 'Публикации', Sparkles],
  ['designer', 'Дизайнер', Palette],
  ['connections', 'Подключения', Link2],
  ['dialogs', 'Диалоги', MessagesSquare],
  ['drafts', 'Черновики', Send],
  ['activity', 'Журнал', Activity],
  ['settings', 'Настройки', SlidersHorizontal],
];

const VIEW_KEYS = VIEWS.map(([key]) => key);

interface SMMFactoryTabProps {
  agents: AgentModel[];
}

export function SMMFactoryTab({ agents }: SMMFactoryTabProps) {
  const [view, setView] = useState<View>(() => {
    const stored = localStorage.getItem('smm_factory_view') as View | null;
    // A view name from an older build (or a hand-edited key) must not leave the
    // section rendering nothing at all.
    return stored && VIEW_KEYS.includes(stored) ? stored : 'publications';
  });
  /** The post loaded into the Дизайнер. `null` there means a new one. */
  const [editing, setEditing] = useState<Publication | null>(null);
  // A waiting draft is the one thing here that needs answering rather than
  // reading, so its count sits on the tab instead of only inside the panel.
  const [pendingDrafts, setPendingDrafts] = useState(0);

  useEffect(() => {
    localStorage.setItem('smm_factory_view', view);
  }, [view]);

  useEffect(() => {
    const load = () => {
      fetch('/api/channel-replies?status=pending', { headers: authHeaders() })
        .then(res => res.json())
        .then(data => setPendingDrafts(Array.isArray(data) ? data.length : 0))
        .catch(() => {});
    };
    load();
    const interval = setInterval(load, 20000);
    return () => clearInterval(interval);
  }, [view]);

  const openDesigner = useCallback((post: Publication | null) => {
    setEditing(post);
    setView('designer');
  }, []);

  const closeDesigner = useCallback(() => {
    setEditing(null);
    setView('publications');
  }, []);

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>SMM FACTORY</h2>
          <p style={styles.tabSubtitle}>
            Создавайте, публикуйте и отслеживайте посты в соцсетях — вместе с подключениями,
            живыми диалогами и черновиками ответов на подтверждение
          </p>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <nav className="admin-subnav is-wrap" aria-label="SMM Factory">
          {VIEWS.map(([key, label, Icon]) => (
            <button key={key} type="button" className={view === key ? 'is-active' : ''} onClick={() => setView(key)}>
              <Icon size={15} />
              <span>{label}</span>
              {key === 'drafts' && pendingDrafts > 0 && (
                <span className="admin-subnav-count">{pendingDrafts}</span>
              )}
            </button>
          ))}
        </nav>
        <div style={{ flex: 1 }} />
        <button type="button" className="btn-primary" onClick={() => openDesigner(null)}>
          <Plus size={14} /><span>Создать публикацию</span>
        </button>
      </div>

      {view === 'publications' && <SmmPublicationsPanel onEdit={openDesigner} />}
      {view === 'designer' && (
        <SmmDesignerPanel post={editing} onSaved={closeDesigner} onCancel={closeDesigner} />
      )}
      {view === 'connections' && <MessengerChannelsTab agents={agents} view="connections" />}
      {view === 'dialogs' && <SmmDialogsPanel />}
      {view === 'drafts' && <MessengerChannelsTab agents={agents} view="drafts" />}
      {view === 'activity' && <MessengerChannelsTab agents={agents} view="activity" />}
      {view === 'settings' && <SmmSettingsPanel />}
    </div>
  );
}

export default SMMFactoryTab;
