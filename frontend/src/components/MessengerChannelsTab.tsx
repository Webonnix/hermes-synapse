import { useEffect, useState, type ReactNode } from 'react';
import { MessageCircle, Plus, Trash2, X } from 'lucide-react';
import { styles } from '../styles';
import type { AgentModel, MessengerBinding } from '../types';

const field = (label: string, child: ReactNode) => (
  <label style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
    {label}
    {child}
  </label>
);

type Platform = 'telegram' | 'matrix';

const PLATFORM_LABELS: Record<Platform, string> = {
  telegram: 'Telegram',
  matrix: 'Element / Matrix',
};

const STATUS_LABELS: Record<string, string> = {
  awaiting_approval: 'Ожидает подтверждения',
  approved: 'Подтверждено',
  active: 'Активен',
  failed: 'Ошибка',
  revoked: 'Отключён',
};

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

const emptyTelegramForm = { bot_token: '', allowed_chat_ids: '' };
const emptyMatrixForm = { homeserver_url: 'https://matrix.org', user_id: '', password: '', access_token: '', allowed_room_ids: '' };

interface MessengerChannelsTabProps {
  agents: AgentModel[];
}

export function MessengerChannelsTab({ agents }: MessengerChannelsTabProps) {
  const [bindings, setBindings] = useState<MessengerBinding[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [platform, setPlatform] = useState<Platform>('telegram');
  const [agentId, setAgentId] = useState('');
  const [telegramForm, setTelegramForm] = useState(emptyTelegramForm);
  const [matrixForm, setMatrixForm] = useState(emptyMatrixForm);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const fetchBindings = () => {
    fetch('/api/messenger-bindings', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setBindings(Array.isArray(data) ? data : []))
      .catch(() => setBindings([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchBindings(); }, []);

  const resetForm = () => {
    setShowForm(false);
    setAgentId('');
    setTelegramForm(emptyTelegramForm);
    setMatrixForm(emptyMatrixForm);
    setError('');
  };

  const addBinding = async () => {
    if (!agentId) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const path = platform === 'telegram' ? `/api/agents/${agentId}/telegram` : `/api/agents/${agentId}/matrix`;
      const body = platform === 'telegram'
        ? {
          bot_token: telegramForm.bot_token,
          allowed_chat_ids: telegramForm.allowed_chat_ids.split(',').map(s => s.trim()).filter(Boolean),
        }
        : {
          homeserver_url: matrixForm.homeserver_url,
          user_id: matrixForm.user_id,
          password: matrixForm.password,
          access_token: matrixForm.access_token,
          allowed_room_ids: matrixForm.allowed_room_ids.split(',').map(s => s.trim()).filter(Boolean),
        };
      const response = await fetch(path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось подключить канал');
      const identity = data.bot_username || data.matrix_user_id;
      setNotice(`${identity} ожидает подтверждения — выполните /approve ${data.task_id} в Telegram.`);
      resetForm();
      fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось подключить канал');
    } finally {
      setSaving(false);
    }
  };

  const deleteBinding = async (binding: MessengerBinding) => {
    if (!window.confirm(`Отключить канал ${PLATFORM_LABELS[binding.platform as Platform] || binding.platform} для агента "${binding.agent_name}"?`)) return;
    const path = binding.platform === 'matrix' ? `/api/agents/matrix/${binding.id}` : `/api/agents/telegram/${binding.id}`;
    await fetch(path, { method: 'DELETE', headers: authHeaders() });
    fetchBindings();
  };

  const canSubmit = platform === 'telegram'
    ? Boolean(agentId && telegramForm.bot_token)
    : Boolean(agentId && matrixForm.user_id && (matrixForm.password || matrixForm.access_token));

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>КАНАЛЫ СВЯЗИ</h2>
          <p style={styles.tabSubtitle}>Все подключения агентов к мессенджерам в одном месте — Telegram, Element/Matrix и другие по мере добавления</p>
        </div>
        {!showForm && (
          <button type="button" className="btn-primary" onClick={() => setShowForm(true)}>
            <Plus size={14} /><span>Подключить канал</span>
          </button>
        )}
      </div>

      {showForm && (
        <div className="glass-panel" style={{ padding: '16px', marginBottom: '18px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <strong style={{ fontSize: '0.9rem' }}>Новое подключение</strong>
            <button type="button" className="icon-btn" onClick={resetForm}><X size={14} /></button>
          </div>

          <div className="admin-add-grid">
            {field('Платформа', (
              <select className="form-input" value={platform} onChange={e => setPlatform(e.target.value as Platform)}>
                <option value="telegram">Telegram</option>
                <option value="matrix">Element / Matrix</option>
              </select>
            ))}
            {field('Агент', (
              <select className="form-input" value={agentId} onChange={e => setAgentId(e.target.value)}>
                <option value="">Выберите агента...</option>
                {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            ))}
          </div>

          {platform === 'telegram' ? (
            <div className="admin-add-grid">
              {field('Bot token from @BotFather', (
                <input className="form-input" type="password" autoComplete="off" value={telegramForm.bot_token}
                  onChange={e => setTelegramForm({ ...telegramForm, bot_token: e.target.value })} placeholder="123456:AA..." />
              ))}
              {field('Разрешённые chat ID (через запятую)', (
                <input className="form-input" value={telegramForm.allowed_chat_ids}
                  onChange={e => setTelegramForm({ ...telegramForm, allowed_chat_ids: e.target.value })} placeholder="без ограничений, если пусто" />
              ))}
            </div>
          ) : (
            <div className="admin-add-grid">
              {field('Homeserver URL', (
                <input className="form-input" value={matrixForm.homeserver_url}
                  onChange={e => setMatrixForm({ ...matrixForm, homeserver_url: e.target.value })} placeholder="https://matrix.org" />
              ))}
              {field('Matrix user ID', (
                <input className="form-input" value={matrixForm.user_id}
                  onChange={e => setMatrixForm({ ...matrixForm, user_id: e.target.value })} placeholder="@agent:matrix.org" />
              ))}
              {field('Пароль (или access token ниже)', (
                <input className="form-input" type="password" autoComplete="off" value={matrixForm.password}
                  onChange={e => setMatrixForm({ ...matrixForm, password: e.target.value })} />
              ))}
              {field('Access token (вместо пароля)', (
                <input className="form-input" type="password" autoComplete="off" value={matrixForm.access_token}
                  onChange={e => setMatrixForm({ ...matrixForm, access_token: e.target.value })} />
              ))}
              {field('Разрешённые room ID (через запятую)', (
                <input className="form-input" value={matrixForm.allowed_room_ids}
                  onChange={e => setMatrixForm({ ...matrixForm, allowed_room_ids: e.target.value })} placeholder="без ограничений, если пусто" />
              ))}
            </div>
          )}

          <button type="button" className="btn-primary" onClick={addBinding} disabled={saving || !canSubmit} style={{ alignSelf: 'flex-start' }}>
            <span>{saving ? '...' : 'Подключить'}</span>
          </button>
          {error && <div style={{ color: 'var(--danger)', fontSize: '0.85rem' }}>{error}</div>}
        </div>
      )}

      {notice && <div className="glass-panel" style={{ padding: '10px 14px', marginBottom: '14px', color: 'var(--success)', fontSize: '0.85rem' }}>{notice}</div>}

      {loading ? (
        <p style={{ color: 'var(--text-dim, #8a94a6)' }}>Загрузка...</p>
      ) : bindings.length === 0 ? (
        <p style={{ color: 'var(--text-dim, #8a94a6)' }}>Пока нет ни одного подключённого канала.</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {bindings.map(binding => (
            <div key={binding.id} className="glass-panel" style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <MessageCircle size={16} />
                <div>
                  <strong style={{ fontSize: '0.9rem' }}>{binding.agent_name}</strong>
                  <span style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>
                    {PLATFORM_LABELS[binding.platform as Platform] || binding.platform} · {binding.bot_username}
                  </span>
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <span className={`admin-status-chip ${binding.status === 'active' ? 'is-active' : binding.status === 'failed' || binding.status === 'revoked' ? 'is-revoked' : ''}`}>
                  <i className="dot" />{STATUS_LABELS[binding.status] || binding.status}
                </span>
                <button type="button" className="icon-btn danger" title="Отключить" onClick={() => deleteBinding(binding)}>
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
