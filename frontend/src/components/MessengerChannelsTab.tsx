import { useEffect, useState, type ReactNode } from 'react';
import { Check, ChevronDown, ChevronUp, MessageCircle, Plus, Power, PowerOff, Send, Trash2, X } from 'lucide-react';
import { styles } from '../styles';
import type { AgentModel, MessengerBinding, PendingChannelReply } from '../types';

const field = (label: string, child: ReactNode) => (
  <label style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
    {label}
    {child}
  </label>
);

type Platform = 'telegram' | 'matrix' | 'discord' | 'slack' | 'email';
type ResponseMode = 'draft' | 'auto_labeled';

const PLATFORM_LABELS: Record<Platform, string> = {
  telegram: 'Telegram',
  matrix: 'Element / Matrix',
  discord: 'Discord',
  slack: 'Slack',
  email: 'Email',
};

const STATUS_LABELS: Record<string, string> = {
  awaiting_approval: 'Ожидает подтверждения',
  approved: 'Подтверждено',
  active: 'Активен',
  failed: 'Ошибка',
  revoked: 'Отключён',
};

const RESPONSE_MODE_LABELS: Record<ResponseMode, string> = {
  draft: 'Черновик на подтверждение',
  auto_labeled: 'Автоответ с пометкой «ассистент»',
};

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

const emptyTelegramForm = { bot_token: '', allowed_chat_ids: '' };
const emptyMatrixForm = { homeserver_url: 'https://matrix.org', user_id: '', password: '', access_token: '', allowed_room_ids: '' };
const emptyDiscordForm = { bot_token: '', allowed_channel_ids: '' };
const emptySlackForm = { bot_token: '', app_token: '', allowed_channel_ids: '' };
const emptyEmailForm = { imap_host: '', imap_port: '993', smtp_host: '', smtp_port: '587', address: '', password: '', allowed_senders: '' };

function ResponseModePicker({ value, onChange }: { value: ResponseMode; onChange: (mode: ResponseMode) => void }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>Режим ответа</span>
      {(['draft', 'auto_labeled'] as ResponseMode[]).map(mode => (
        <label key={mode} style={{
          display: 'flex', alignItems: 'flex-start', gap: 10, padding: '10px 12px', borderRadius: 8,
          border: `1px solid ${value === mode ? 'rgba(77,222,180,.4)' : 'rgba(255,255,255,.09)'}`,
          background: value === mode ? 'rgba(46,179,139,.08)' : 'rgba(255,255,255,.02)', cursor: 'pointer',
        }}>
          <input type="radio" name="response_mode" checked={value === mode} onChange={() => onChange(mode)} style={{ marginTop: 3 }} />
          <span>
            <strong style={{ display: 'block', fontSize: '0.83rem' }}>{RESPONSE_MODE_LABELS[mode]}</strong>
            <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2 }}>
              {mode === 'draft'
                ? 'Ничего не уходит собеседнику автоматически — Vexa готовит ответ, вы проверяете и отправляете сами из раздела «Черновики» ниже.'
                : 'Отвечает сразу, но каждое сообщение помечено как ответ ассистента — собеседник всегда знает, что говорит не Альберт лично.'}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}

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
  const [discordForm, setDiscordForm] = useState(emptyDiscordForm);
  const [slackForm, setSlackForm] = useState(emptySlackForm);
  const [emailForm, setEmailForm] = useState(emptyEmailForm);
  const [systemPrompt, setSystemPrompt] = useState('');
  const [responseMode, setResponseMode] = useState<ResponseMode>('draft');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const [expandedId, setExpandedId] = useState('');
  const [editPrompt, setEditPrompt] = useState('');
  const [editMode, setEditMode] = useState<ResponseMode>('draft');
  const [savingEdit, setSavingEdit] = useState(false);
  /** Binding id currently mid enable/disable/delete, so its row can disable its own buttons. */
  const [busyBindingId, setBusyBindingId] = useState('');

  const [replies, setReplies] = useState<PendingChannelReply[]>([]);
  const [repliesLoading, setRepliesLoading] = useState(true);
  const [editedReplyText, setEditedReplyText] = useState<Record<string, string>>({});
  const [busyReplyId, setBusyReplyId] = useState('');

  const fetchBindings = () => {
    fetch('/api/messenger-bindings', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setBindings(Array.isArray(data) ? data : []))
      .catch(() => setBindings([]))
      .finally(() => setLoading(false));
  };

  const fetchReplies = () => {
    fetch('/api/channel-replies?status=pending', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setReplies(Array.isArray(data) ? data : []))
      .catch(() => setReplies([]))
      .finally(() => setRepliesLoading(false));
  };

  useEffect(() => {
    fetchBindings();
    fetchReplies();
    const interval = setInterval(fetchReplies, 20000);
    return () => clearInterval(interval);
  }, []);

  const resetForm = () => {
    setShowForm(false);
    setAgentId('');
    setTelegramForm(emptyTelegramForm);
    setMatrixForm(emptyMatrixForm);
    setDiscordForm(emptyDiscordForm);
    setSlackForm(emptySlackForm);
    setEmailForm(emptyEmailForm);
    setSystemPrompt('');
    setResponseMode('draft');
    setError('');
  };

  const PLATFORM_PATHS: Record<Platform, string> = {
    telegram: 'telegram', matrix: 'matrix', discord: 'discord', slack: 'slack', email: 'email',
  };

  const addBinding = async () => {
    if (!agentId) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const path = `/api/agents/${agentId}/${PLATFORM_PATHS[platform]}`;
      const bodyByPlatform: Record<Platform, object> = {
        telegram: {
          bot_token: telegramForm.bot_token,
          allowed_chat_ids: telegramForm.allowed_chat_ids.split(',').map(s => s.trim()).filter(Boolean),
        },
        matrix: {
          homeserver_url: matrixForm.homeserver_url,
          user_id: matrixForm.user_id,
          password: matrixForm.password,
          access_token: matrixForm.access_token,
          allowed_room_ids: matrixForm.allowed_room_ids.split(',').map(s => s.trim()).filter(Boolean),
        },
        discord: {
          bot_token: discordForm.bot_token,
          allowed_channel_ids: discordForm.allowed_channel_ids.split(',').map(s => s.trim()).filter(Boolean),
        },
        slack: {
          bot_token: slackForm.bot_token,
          app_token: slackForm.app_token,
          allowed_channel_ids: slackForm.allowed_channel_ids.split(',').map(s => s.trim()).filter(Boolean),
        },
        email: {
          imap_host: emailForm.imap_host,
          imap_port: Number(emailForm.imap_port) || 993,
          smtp_host: emailForm.smtp_host,
          smtp_port: Number(emailForm.smtp_port) || 587,
          address: emailForm.address,
          password: emailForm.password,
          allowed_senders: emailForm.allowed_senders.split(',').map(s => s.trim()).filter(Boolean),
        },
      };
      const body = { ...bodyByPlatform[platform], system_prompt: systemPrompt, response_mode: responseMode };
      const response = await fetch(path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось подключить канал');
      const identity = data.bot_username || data.matrix_user_id || data.slack_identity || data.mailbox;
      setNotice(`${identity} ожидает подтверждения — выполните /approve ${data.task_id} в Telegram.`);
      resetForm();
      fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось подключить канал');
    } finally {
      setSaving(false);
    }
  };

  const disableBinding = async (binding: MessengerBinding) => {
    if (!window.confirm(`Выключить канал ${PLATFORM_LABELS[binding.platform as Platform] || binding.platform} для агента "${binding.agent_name}"? Бот перестанет отвечать, но подключение можно будет включить обратно.`)) return;
    setBusyBindingId(binding.id);
    setError('');
    try {
      const response = await fetch(`/api/messenger-bindings/${binding.id}/disable`, { method: 'POST', headers: authHeaders() });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Не удалось выключить канал');
      }
      await fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось выключить канал');
    } finally {
      setBusyBindingId('');
    }
  };

  const enableBinding = async (binding: MessengerBinding) => {
    setBusyBindingId(binding.id);
    setError('');
    try {
      const response = await fetch(`/api/messenger-bindings/${binding.id}/enable`, { method: 'POST', headers: authHeaders() });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Не удалось включить канал');
      }
      await fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось включить канал');
    } finally {
      setBusyBindingId('');
    }
  };

  const deleteBindingPermanently = async (binding: MessengerBinding) => {
    if (!window.confirm(`Удалить канал ${PLATFORM_LABELS[binding.platform as Platform] || binding.platform} для агента "${binding.agent_name}" НАВСЕГДА? Учётные данные будут уничтожены — подключение придётся настраивать заново.`)) return;
    setBusyBindingId(binding.id);
    setError('');
    try {
      const response = await fetch(`/api/messenger-bindings/${binding.id}`, { method: 'DELETE', headers: authHeaders() });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Не удалось удалить канал');
      }
      await fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось удалить канал');
    } finally {
      setBusyBindingId('');
    }
  };

  const openEdit = (binding: MessengerBinding) => {
    if (expandedId === binding.id) {
      setExpandedId('');
      return;
    }
    setExpandedId(binding.id);
    setEditPrompt(binding.system_prompt_override || '');
    setEditMode((binding.response_mode as ResponseMode) || 'draft');
  };

  const saveEdit = async (binding: MessengerBinding) => {
    setSavingEdit(true);
    try {
      const response = await fetch(`/api/messenger-bindings/${binding.id}`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify({ system_prompt: editPrompt, response_mode: editMode }),
      });
      if (!response.ok) throw new Error('Не удалось сохранить изменения');
      setExpandedId('');
      fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сохранить изменения');
    } finally {
      setSavingEdit(false);
    }
  };

  const sendReply = async (reply: PendingChannelReply) => {
    setBusyReplyId(reply.id);
    try {
      const editedText = editedReplyText[reply.id];
      const response = await fetch(`/api/channel-replies/${reply.id}/send`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ edited_text: editedText !== undefined ? editedText : null }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || 'Не удалось отправить ответ');
      }
      fetchReplies();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось отправить ответ');
    } finally {
      setBusyReplyId('');
    }
  };

  const discardReply = async (reply: PendingChannelReply) => {
    setBusyReplyId(reply.id);
    try {
      await fetch(`/api/channel-replies/${reply.id}/discard`, { method: 'POST', headers: authHeaders() });
      fetchReplies();
    } finally {
      setBusyReplyId('');
    }
  };

  const canSubmit = !agentId ? false : (
    platform === 'telegram' ? Boolean(telegramForm.bot_token) :
    platform === 'matrix' ? Boolean(matrixForm.user_id && (matrixForm.password || matrixForm.access_token)) :
    platform === 'discord' ? Boolean(discordForm.bot_token) :
    platform === 'slack' ? Boolean(slackForm.bot_token && slackForm.app_token) :
    Boolean(emailForm.imap_host && emailForm.smtp_host && emailForm.address && emailForm.password)
  );

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

      {replies.length > 0 && (
        <div className="glass-panel" style={{ padding: '16px', marginBottom: '18px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <strong style={{ fontSize: '0.9rem' }}>Черновики, ожидающие отправки ({replies.length})</strong>
          {replies.map(reply => (
            <div key={reply.id} style={{ padding: '12px', borderRadius: 8, border: '1px solid rgba(255,255,255,.08)', background: 'rgba(255,255,255,.02)', display: 'flex', flexDirection: 'column', gap: 8 }}>
              <span style={{ fontSize: '0.75rem', color: 'var(--text-dim, #8a94a6)' }}>
                {PLATFORM_LABELS[reply.platform as Platform] || reply.platform} · от {reply.incoming_from || 'неизвестно'}
              </span>
              <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>«{reply.incoming_text}»</span>
              <textarea
                className="form-input"
                rows={3}
                value={editedReplyText[reply.id] !== undefined ? editedReplyText[reply.id] : reply.drafted_reply}
                onChange={e => setEditedReplyText(prev => ({ ...prev, [reply.id]: e.target.value }))}
              />
              <div style={{ display: 'flex', gap: 8 }}>
                <button type="button" className="btn-primary" disabled={busyReplyId === reply.id} onClick={() => void sendReply(reply)}>
                  <Send size={13} /><span>Отправить</span>
                </button>
                <button type="button" className="icon-btn danger" disabled={busyReplyId === reply.id} onClick={() => void discardReply(reply)} title="Отклонить черновик">
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {!repliesLoading && replies.length === 0 && (
        <p style={{ color: 'var(--text-dim, #8a94a6)', fontSize: '0.8rem', marginBottom: '14px' }}>Черновиков на подтверждение сейчас нет.</p>
      )}

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
                <option value="discord">Discord</option>
                <option value="slack">Slack</option>
                <option value="email">Email</option>
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

          {platform === 'discord' && (
            <div className="admin-add-grid">
              {field('Bot token (Discord Developer Portal → Bot)', (
                <input className="form-input" type="password" autoComplete="off" value={discordForm.bot_token}
                  onChange={e => setDiscordForm({ ...discordForm, bot_token: e.target.value })} placeholder="MTIz..." />
              ))}
              {field('Разрешённые channel/user ID (через запятую)', (
                <input className="form-input" value={discordForm.allowed_channel_ids}
                  onChange={e => setDiscordForm({ ...discordForm, allowed_channel_ids: e.target.value })} placeholder="без ограничений, если пусто" />
              ))}
            </div>
          )}

          {platform === 'slack' && (
            <div className="admin-add-grid">
              {field('Bot User OAuth Token (xoxb-...)', (
                <input className="form-input" type="password" autoComplete="off" value={slackForm.bot_token}
                  onChange={e => setSlackForm({ ...slackForm, bot_token: e.target.value })} placeholder="xoxb-..." />
              ))}
              {field('App-Level Token (xapp-..., нужен scope connections:write)', (
                <input className="form-input" type="password" autoComplete="off" value={slackForm.app_token}
                  onChange={e => setSlackForm({ ...slackForm, app_token: e.target.value })} placeholder="xapp-..." />
              ))}
              {field('Разрешённые channel/user ID (через запятую)', (
                <input className="form-input" value={slackForm.allowed_channel_ids}
                  onChange={e => setSlackForm({ ...slackForm, allowed_channel_ids: e.target.value })} placeholder="без ограничений, если пусто" />
              ))}
            </div>
          )}

          {platform === 'email' && (
            <div className="admin-add-grid">
              {field('IMAP host', (
                <input className="form-input" value={emailForm.imap_host}
                  onChange={e => setEmailForm({ ...emailForm, imap_host: e.target.value })} placeholder="imap.gmail.com" />
              ))}
              {field('IMAP порт', (
                <input className="form-input" type="number" value={emailForm.imap_port}
                  onChange={e => setEmailForm({ ...emailForm, imap_port: e.target.value })} />
              ))}
              {field('SMTP host', (
                <input className="form-input" value={emailForm.smtp_host}
                  onChange={e => setEmailForm({ ...emailForm, smtp_host: e.target.value })} placeholder="smtp.gmail.com" />
              ))}
              {field('SMTP порт', (
                <input className="form-input" type="number" value={emailForm.smtp_port}
                  onChange={e => setEmailForm({ ...emailForm, smtp_port: e.target.value })} />
              ))}
              {field('Email адрес', (
                <input className="form-input" value={emailForm.address}
                  onChange={e => setEmailForm({ ...emailForm, address: e.target.value })} placeholder="agent@example.com" />
              ))}
              {field('Пароль (app password для Gmail/Yandex и т.п.)', (
                <input className="form-input" type="password" autoComplete="off" value={emailForm.password}
                  onChange={e => setEmailForm({ ...emailForm, password: e.target.value })} />
              ))}
              {field('Разрешённые отправители (через запятую) — настоятельно рекомендуется', (
                <input className="form-input" value={emailForm.allowed_senders}
                  onChange={e => setEmailForm({ ...emailForm, allowed_senders: e.target.value })} placeholder="friend@example.com, boss@company.com" />
              ))}
            </div>
          )}

          {field('Системный промпт для этого канала (необязательно)', (
            <textarea className="form-input" rows={3} value={systemPrompt} onChange={e => setSystemPrompt(e.target.value)}
              placeholder="Оставьте пустым, чтобы использовать промпт выбранного агента без изменений. Здесь можно задать особый тон/роль именно для этого канала." />
          ))}

          <ResponseModePicker value={responseMode} onChange={setResponseMode} />

          <button type="button" className="btn-primary" onClick={addBinding} disabled={saving || !canSubmit} style={{ alignSelf: 'flex-start' }}>
            <span>{saving ? '...' : 'Подключить'}</span>
          </button>
        </div>
      )}

      {/* Rendered outside the add-binding form so it also surfaces failures from
          enable/disable/delete on the list below, which can happen with the form closed. */}
      {error && <div className="glass-panel" style={{ padding: '10px 14px', marginBottom: '14px', color: 'var(--danger)', fontSize: '0.85rem' }}>{error}</div>}
      {notice && <div className="glass-panel" style={{ padding: '10px 14px', marginBottom: '14px', color: 'var(--success)', fontSize: '0.85rem' }}>{notice}</div>}

      {loading ? (
        <p style={{ color: 'var(--text-dim, #8a94a6)' }}>Загрузка...</p>
      ) : bindings.length === 0 ? (
        <p style={{ color: 'var(--text-dim, #8a94a6)' }}>Пока нет ни одного подключённого канала.</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {bindings.map(binding => (
            <div key={binding.id} className="glass-panel" style={{ padding: '12px 16px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <MessageCircle size={16} />
                  <div>
                    <strong style={{ fontSize: '0.9rem' }}>{binding.agent_name}</strong>
                    <span style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>
                      {PLATFORM_LABELS[binding.platform as Platform] || binding.platform} · {binding.bot_username} · {RESPONSE_MODE_LABELS[(binding.response_mode as ResponseMode) || 'draft']}
                    </span>
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span className={`admin-status-chip ${binding.status === 'active' ? 'is-active' : binding.status === 'failed' || binding.status === 'revoked' ? 'is-revoked' : ''}`}>
                    <i className="dot" />{STATUS_LABELS[binding.status] || binding.status}
                  </span>
                  {binding.status === 'active' && (
                    <button type="button" className="icon-btn" title="Настроить промпт/режим" onClick={() => openEdit(binding)}>
                      {expandedId === binding.id ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                    </button>
                  )}
                  {binding.status === 'active' && (
                    <button
                      type="button"
                      className="icon-btn"
                      title="Выключить (можно будет включить обратно)"
                      disabled={busyBindingId === binding.id}
                      onClick={() => void disableBinding(binding)}
                    >
                      <Power size={14} />
                    </button>
                  )}
                  {binding.status === 'revoked' && (
                    <button
                      type="button"
                      className="icon-btn"
                      title="Включить"
                      disabled={busyBindingId === binding.id}
                      onClick={() => void enableBinding(binding)}
                    >
                      <PowerOff size={14} />
                    </button>
                  )}
                  <button
                    type="button"
                    className="icon-btn danger"
                    title="Удалить навсегда"
                    disabled={busyBindingId === binding.id}
                    onClick={() => void deleteBindingPermanently(binding)}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>

              {expandedId === binding.id && (
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,.07)', display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {field('Системный промпт для этого канала', (
                    <textarea className="form-input" rows={3} value={editPrompt} onChange={e => setEditPrompt(e.target.value)}
                      placeholder="Пусто — использовать промпт агента как есть" />
                  ))}
                  <ResponseModePicker value={editMode} onChange={setEditMode} />
                  <button type="button" className="btn-primary" disabled={savingEdit} onClick={() => void saveEdit(binding)} style={{ alignSelf: 'flex-start' }}>
                    <Check size={13} /><span>{savingEdit ? '...' : 'Сохранить'}</span>
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
