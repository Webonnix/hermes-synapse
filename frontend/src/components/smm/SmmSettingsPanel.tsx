/**
 * Section-wide settings for SMM Factory.
 *
 * Deliberately small. Almost everything about how a channel behaves — prompt,
 * draft vs. auto-labeled, who may write, escalation — belongs to that channel
 * and is edited on its own card, because two accounts on the same platform
 * routinely want different answers to those questions.
 *
 * What is genuinely section-wide is the one thing the backend cannot work out
 * for itself: the public address it is reachable at. Meta delivers messages by
 * POSTing to a URL, the backend sits behind a reverse proxy and has no reliable
 * way to know its own external name, and every webhook channel needs an exact
 * URL to paste into the Meta console. So the owner states it once here, and
 * each channel's card shows its own callback URL built from it.
 */

import { useEffect, useState } from 'react';
import { Check, Globe } from 'lucide-react';
import { styles } from '../../styles';
import type { MessengerBinding } from '../../types';
import { PLATFORM_LABELS, authHeaders, type Platform } from './platforms';

const WEBHOOK_PLATFORMS = ['instagram', 'facebook', 'whatsapp'];

export function SmmSettingsPanel() {
  const [baseUrl, setBaseUrl] = useState('');
  const [saved, setSaved] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [bindings, setBindings] = useState<MessengerBinding[]>([]);

  useEffect(() => {
    fetch('/api/smm/settings', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => { setBaseUrl(data.public_base_url || ''); setSaved(data.public_base_url || ''); })
      .catch(() => {});
    fetch('/api/messenger-bindings', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setBindings(Array.isArray(data) ? data : []))
      .catch(() => setBindings([]));
  }, []);

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      const response = await fetch('/api/smm/settings', {
        method: 'PUT', headers: authHeaders(), body: JSON.stringify({ public_base_url: baseUrl }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось сохранить');
      setSaved(data.public_base_url || '');
      setBaseUrl(data.public_base_url || '');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сохранить');
    } finally {
      setSaving(false);
    }
  };

  const webhookChannels = bindings.filter(b => WEBHOOK_PLATFORMS.includes(b.platform));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18, maxWidth: 780 }}>
      <div className="glass-panel" style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <strong style={{ fontSize: '0.9rem', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Globe size={15} />Публичный адрес для вебхуков
        </strong>
        <p style={{ ...styles.formHelp, margin: 0 }}>
          Адрес, по которому этот бэкенд доступен из интернета. Instagram, Facebook и WhatsApp присылают
          сообщения только POST-запросом — без такого адреса они физически не могут ничего доставить.
          VK, Telegram и остальные мессенджеры сюда не смотрят: они опрашиваются изнутри и работают без
          публичного адреса.
        </p>
        <input
          className="form-input"
          value={baseUrl}
          onChange={e => setBaseUrl(e.target.value)}
          placeholder="https://vexa.example.com"
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button type="button" className="btn-primary" disabled={saving || baseUrl === saved} onClick={() => void save()}>
            <Check size={13} /><span>{saving ? '...' : 'Сохранить'}</span>
          </button>
          {saved && (
            <code style={{ fontSize: '0.76rem', color: 'var(--text-dim, #8a94a6)' }}>
              {saved}/api/social/webhook/&lt;платформа&gt;
            </code>
          )}
        </div>
        {error && <span style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>{error}</span>}
      </div>

      <div className="glass-panel" style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <strong style={{ fontSize: '0.9rem' }}>Каналы, которым нужен вебхук</strong>
        {webhookChannels.length === 0 ? (
          <span style={{ fontSize: '0.8rem', color: 'var(--text-dim, #8a94a6)' }}>
            Пока нет ни одного подключения Instagram, Facebook или WhatsApp.
          </span>
        ) : (
          <>
            <p style={{ ...styles.formHelp, margin: 0 }}>
              Точный callback URL и verify token каждого канала — на его карточке в «Подключениях»
              (кнопка настроек). Здесь только список тех, кого это касается.
            </p>
            <div className="admin-item-list">
              {webhookChannels.map(binding => (
                <div key={binding.id} className="admin-item-row">
                  <div>
                    <strong style={{ fontSize: '0.85rem' }}>{binding.bot_username}</strong>
                    <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)' }}>
                      {PLATFORM_LABELS[binding.platform as Platform] || binding.platform} · {binding.agent_name}
                      {binding.external_account_id ? ` · id ${binding.external_account_id}` : ''}
                    </span>
                  </div>
                  <span className={`admin-status-chip ${binding.status === 'active' ? 'is-active' : ''}`}>
                    <i className="dot" />{binding.status === 'active' ? 'Активен' : binding.status}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default SmmSettingsPanel;
