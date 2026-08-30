import { useEffect, useState, type ReactNode } from 'react';
import { Bot, Check, ChevronDown, ChevronUp, Copy, Eraser, Hand, MessageCircle, MessageSquare, Plus, Power, PowerOff, RefreshCw, Send, Trash2, Users, X } from 'lucide-react';
import { styles } from '../styles';
import type { AccessPlan, AgentModel, ChannelActivityEvent, ConversationMessage, ConversationState, MessengerBinding, PendingChannelReply, SocialPlatformSpec, SocialWebhookDetails } from '../types';
import {
  ACCESS_MODE_LABELS, CREDENTIAL_FIELDS, HANDLING_MODE_LABELS, PLATFORM_LABELS, RESPONSE_MODE_LABELS,
  STATUS_LABELS, authHeaders, isSocialPlatform,
  type AccessMode, type Platform, type ResponseMode,
} from './smm/platforms';

const field = (label: string, child: ReactNode) => (
  <label style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
    {label}
    {child}
  </label>
);

/** The connect/reconnect fields for a social platform, rendered from the
 *  backend's own declaration of what that platform needs (GET /api/social-platforms).
 *  `verify_token` is offered as an optional extra rather than a required field:
 *  leaving it empty makes the backend generate one, which is what an owner
 *  registering a fresh webhook actually wants. */
function SocialCredentialFields({ spec, values, onChange }: {
  spec: SocialPlatformSpec;
  values: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  const fields = [...spec.required, ...(spec.delivery === 'webhook' ? ['verify_token'] : [])];
  return (
    <div className="admin-add-grid">
      {fields.map(name => {
        const meta = CREDENTIAL_FIELDS[name] || { label: name };
        return (
          <label key={name} style={{ display: 'flex', flexDirection: 'column', gap: 6, color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
            {meta.label}
            <input
              className="form-input"
              type={meta.secret ? 'password' : 'text'}
              autoComplete="off"
              placeholder={meta.placeholder || ''}
              value={values[name] || ''}
              onChange={e => onChange({ ...values, [name]: e.target.value })}
            />
            {meta.hint && <span style={styles.formHelp}>{meta.hint}</span>}
          </label>
        );
      })}
    </div>
  );
}

/** Switching a bot to token mode is what turns it from a private line into a
 *  social bot anyone can start, so it gets its own explicit control rather than
 *  hiding inside the prompt/mode form. */
function AccessModePicker({ value, onChange }: { value: AccessMode; onChange: (mode: AccessMode) => void }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>Кто может писать боту</span>
      {(['owner_only', 'token'] as AccessMode[]).map(mode => (
        <label key={mode} style={{
          display: 'flex', alignItems: 'flex-start', gap: 10, padding: '10px 12px', borderRadius: 8,
          border: `1px solid ${value === mode ? 'rgba(77,222,180,.4)' : 'rgba(255,255,255,.09)'}`,
          background: value === mode ? 'rgba(46,179,139,.08)' : 'rgba(255,255,255,.02)', cursor: 'pointer',
        }}>
          <input type="radio" name="access_mode" checked={value === mode} onChange={() => onChange(mode)} style={{ marginTop: 3 }} />
          <span>
            <strong style={{ display: 'block', fontSize: '0.83rem' }}>{ACCESS_MODE_LABELS[mode]}</strong>
            <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2 }}>
              {mode === 'owner_only'
                ? 'Бот отвечает только чатам из белого списка. Все остальные сообщения игнорируются.'
                : 'Бот отвечает любому, кто пришлёт действующий токен из раздела «Доступ к ботам». Каждому токену заводится своя учётка, лимиты и учёт расхода; режим ответа переключается на автоответ с пометкой.'}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}

const emptyTelegramForm = { bot_token: '', allowed_chat_ids: '' };
const emptyMatrixForm = { homeserver_url: 'https://matrix.org', user_id: '', password: '', access_token: '', refresh_token: '', device_flow_id: '', allowed_room_ids: '' };
const emptyDiscordForm = { bot_token: '', allowed_channel_ids: '' };
const emptySlackForm = { bot_token: '', app_token: '', allowed_channel_ids: '' };
const emptyEmailForm = { imap_host: '', imap_port: '993', smtp_host: '', smtp_port: '587', address: '', password: '', allowed_senders: '' };

const emptyReconnectForm = {
  bot_token: '', app_token: '',
  homeserver_url: 'https://matrix.org', user_id: '', password: '', access_token: '', refresh_token: '',
  device_flow_id: '',
  imap_host: '', imap_port: '993', smtp_host: '', smtp_port: '587', address: '',
};

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
                ? 'Ничего не уходит собеседнику автоматически — Vexa готовит ответ, вы проверяете и отправляете сами на вкладке «Черновики».'
                : 'Отвечает сразу, но каждое сообщение помечено как ответ ассистента — собеседник всегда знает, что говорит не Альберт лично.'}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}

/** Browser (device-code) login for MAS-backed homeservers — matrix.senla.eu and
 *  every Element-hosted server. The access token Element shows under Advanced is
 *  deliberately short-lived there, so a pasted one drops the channel within
 *  minutes; this flow returns a renewable credential instead. The tokens never
 *  reach the browser — the backend parks them and hands back only a flow id. */
function MatrixDeviceLogin({ homeserverUrl, onComplete }: {
  homeserverUrl: string;
  onComplete: (flowId: string, userId: string) => void;
}) {
  const [flow, setFlow] = useState<{ flow_id: string; user_code: string; verification_uri: string; verification_uri_complete: string; interval: number; expires_in?: number } | null>(null);
  const [status, setStatus] = useState<'idle' | 'waiting' | 'done'>('idle');
  const [userId, setUserId] = useState('');
  const [loginError, setLoginError] = useState('');

  const start = async () => {
    setLoginError('');
    setUserId('');
    setStatus('idle');
    try {
      const response = await fetch('/api/matrix/device-login/start', {
        method: 'POST', headers: authHeaders(), body: JSON.stringify({ homeserver_url: homeserverUrl }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось начать вход');
      setFlow(data);
      setStatus('waiting');
      if (data.verification_uri_complete) window.open(data.verification_uri_complete, '_blank', 'noopener');
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : 'Не удалось начать вход');
    }
  };

  useEffect(() => {
    if (status !== 'waiting' || !flow) return;
    const poll = async () => {
      try {
        const response = await fetch(`/api/matrix/device-login/${flow.flow_id}/poll`, {
          method: 'POST', headers: authHeaders(),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Вход не завершён');
        if (data.status === 'complete') {
          setStatus('done');
          setUserId(data.user_id);
          onComplete(flow.flow_id, data.user_id);
        } else if (data.user_code) {
          // Server is the source of truth for the code and its remaining life.
          setFlow(current => (current ? { ...current, ...data } : current));
        }
      } catch (err) {
        setStatus('idle');
        setLoginError(err instanceof Error ? err.message : 'Вход не завершён');
      }
    };
    const interval = setInterval(poll, Math.max(3, flow.interval || 5) * 1000);
    // Confirming the code happens in another tab, and browsers throttle timers
    // in background ones — so poll the moment this tab is looked at again
    // instead of leaving a finished login hanging.
    const pollIfVisible = () => { if (!document.hidden) void poll(); };
    document.addEventListener('visibilitychange', pollIfVisible);
    window.addEventListener('focus', pollIfVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', pollIfVisible);
      window.removeEventListener('focus', pollIfVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, flow]);

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 8, padding: '12px', borderRadius: 8,
      border: '1px solid rgba(77,222,180,.25)', background: 'rgba(46,179,139,.06)',
    }}>
      {status === 'done' ? (
        <span style={{ fontSize: '0.82rem', color: 'var(--accent-green, #4ddeb4)' }}>
          <Check size={13} style={{ verticalAlign: '-2px' }} /> Вход выполнен: {userId} — токен будет продлеваться автоматически.
        </span>
      ) : (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <button type="button" className="btn-primary" onClick={start} disabled={!homeserverUrl}>
              {status === 'waiting' ? 'Ожидаю подтверждения...' : 'Войти через браузер (рекомендуется)'}
            </button>
          </div>
          {status === 'waiting' && flow && (
            <div style={{
              display: 'flex', flexDirection: 'column', gap: 8, padding: '14px 16px', borderRadius: 10,
              border: '1px solid rgba(77,222,180,.45)', background: 'rgba(46,179,139,.12)',
            }}>
              <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                Введите этот код на странице «Link a new device»:
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <strong style={{ fontSize: '1.9rem', letterSpacing: '8px', fontFamily: 'monospace', lineHeight: 1.1 }}>
                  {flow.user_code}
                </strong>
                <button type="button" className="btn-ghost" onClick={() => navigator.clipboard?.writeText(flow.user_code)}>
                  Скопировать
                </button>
                <a href={flow.verification_uri_complete || flow.verification_uri} target="_blank" rel="noopener noreferrer">
                  открыть страницу подтверждения
                </a>
              </div>
              <span style={{ fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)' }}>
                Код действует ещё ~{Math.max(0, Math.round((flow.expires_in || 0) / 60))} мин. Не закрывайте эту вкладку —
                подключение завершится само, как только вы нажмёте Continue.
              </span>
            </div>
          )}
          <span style={{ fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)' }}>
            Для серверов с Matrix Authentication Service (matrix.senla.eu, Element-хостинг) это единственный
            способ подключиться надолго: вставленный из Element access token там живёт минуты и канал постоянно отваливается.
          </span>
        </>
      )}
      {loginError && <span style={{ fontSize: '0.78rem', color: '#ff6b81' }}>{loginError}</span>}
    </div>
  );
}

/** Which panel of the SMM Factory this render is. The three views are one
 *  component because they share the channel list they describe: a draft names
 *  the channel it came in on, a log line names the channel that received it. */
export type ChannelsView = 'connections' | 'drafts' | 'activity';

interface MessengerChannelsTabProps {
  agents: AgentModel[];
  view: ChannelsView;
}

export function MessengerChannelsTab({ agents, view }: MessengerChannelsTabProps) {
  const [bindings, setBindings] = useState<MessengerBinding[]>([]);
  const [plans, setPlans] = useState<AccessPlan[]>([]);
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
  const [editAccessMode, setEditAccessMode] = useState<AccessMode>('owner_only');
  const [editOpenToEveryone, setEditOpenToEveryone] = useState(false);
  const [editAllowedChatIds, setEditAllowedChatIds] = useState('');
  const [editWelcome, setEditWelcome] = useState('');
  const [editDefaultPlanId, setEditDefaultPlanId] = useState('');
  const [editDisclosure, setEditDisclosure] = useState('');
  const [editPauseMinutes, setEditPauseMinutes] = useState(120);
  const [editEscalation, setEditEscalation] = useState(false);
  const [editHandlingMenu, setEditHandlingMenu] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  /** Binding id currently mid enable/disable/delete, so its row can disable its own buttons. */
  const [busyBindingId, setBusyBindingId] = useState('');

  /** Binding id whose "Диалоги" panel (per-chat AI/owner handling state) is open. */
  const [dialogsBindingId, setDialogsBindingId] = useState('');
  const [conversations, setConversations] = useState<ConversationState[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  /** `${platform}|${chat_id}` currently mid action, so only that row disables its buttons. */
  const [busyConversationKey, setBusyConversationKey] = useState('');
  const [conversationSearch, setConversationSearch] = useState('');
  /** Conversation whose stored transcript is expanded, plus its messages. */
  const [openHistoryKey, setOpenHistoryKey] = useState('');
  const [historyMessages, setHistoryMessages] = useState<ConversationMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const [replies, setReplies] = useState<PendingChannelReply[]>([]);
  const [repliesLoading, setRepliesLoading] = useState(true);
  const [editedReplyText, setEditedReplyText] = useState<Record<string, string>>({});
  const [busyReplyId, setBusyReplyId] = useState('');

  const [activity, setActivity] = useState<ChannelActivityEvent[]>([]);

  // Social platforms describe their own credential fields (GET /api/social-platforms),
  // so the connect form is built from the backend's declaration instead of a
  // second hard-coded copy that could drift from what it validates.
  const [socialSpecs, setSocialSpecs] = useState<SocialPlatformSpec[]>([]);
  const [socialForm, setSocialForm] = useState<Record<string, string>>({});
  const [socialAllowed, setSocialAllowed] = useState('');
  const [webhookDetails, setWebhookDetails] = useState<Record<string, SocialWebhookDetails>>({});

  const [reconnectingId, setReconnectingId] = useState('');
  const [reconnectForm, setReconnectForm] = useState(emptyReconnectForm);
  const [reconnecting, setReconnecting] = useState(false);

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

  const fetchPlans = () => {
    fetch('/api/access/plans', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setPlans(Array.isArray(data) ? data : []))
      .catch(() => setPlans([]));
  };

  const fetchActivity = () => {
    fetch('/api/messenger-bindings/activity', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setActivity(Array.isArray(data) ? data : []))
      .catch(() => {});
  };

  useEffect(() => {
    fetchBindings();
    fetchReplies();
    fetchPlans();
    fetch('/api/social-platforms', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setSocialSpecs(Array.isArray(data) ? data : []))
      .catch(() => setSocialSpecs([]));
    const interval = setInterval(fetchReplies, 20000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (view !== 'activity') return;
    fetchActivity();
    const interval = setInterval(fetchActivity, 3000);
    return () => clearInterval(interval);
  }, [view]);

  const resetForm = () => {
    setShowForm(false);
    setAgentId('');
    setTelegramForm(emptyTelegramForm);
    setMatrixForm(emptyMatrixForm);
    setDiscordForm(emptyDiscordForm);
    setSlackForm(emptySlackForm);
    setEmailForm(emptyEmailForm);
    setSocialForm({});
    setSocialAllowed('');
    setSystemPrompt('');
    setResponseMode('draft');
    setError('');
  };

  /** All four social platforms share one endpoint — they differ only in which
   *  credential fields they declare, which the backend already knows. */
  const PLATFORM_PATHS: Record<Platform, string> = {
    telegram: 'telegram', matrix: 'matrix', discord: 'discord', slack: 'slack', email: 'email',
    vk: 'social', instagram: 'social', facebook: 'social', whatsapp: 'social',
  };

  const socialSpec = socialSpecs.find(spec => spec.id === platform);

  const addSocialBinding = async () => {
    const response = await fetch(`/api/agents/${agentId}/social`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({
        platform,
        credentials: socialForm,
        allowed_chat_ids: socialAllowed.split(',').map(s => s.trim()).filter(Boolean),
        system_prompt: systemPrompt,
        response_mode: responseMode,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось подключить канал');
    return data;
  };

  const addBinding = async () => {
    if (!agentId) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      if (isSocialPlatform(platform)) {
        const data = await addSocialBinding();
        setNotice(`${data.account} ожидает подтверждения — выполните /approve ${data.task_id} в Telegram.`);
        resetForm();
        fetchBindings();
        return;
      }
      const path = `/api/agents/${agentId}/${PLATFORM_PATHS[platform]}`;
      const bodyByPlatform: Partial<Record<Platform, object>> = {
        telegram: {
          bot_token: telegramForm.bot_token,
          allowed_chat_ids: telegramForm.allowed_chat_ids.split(',').map(s => s.trim()).filter(Boolean),
        },
        matrix: {
          homeserver_url: matrixForm.homeserver_url,
          user_id: matrixForm.user_id,
          password: matrixForm.password,
          access_token: matrixForm.access_token,
          refresh_token: matrixForm.refresh_token,
          device_flow_id: matrixForm.device_flow_id,
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

  const openReconnect = (binding: MessengerBinding) => {
    if (reconnectingId === binding.id) {
      setReconnectingId('');
      return;
    }
    setExpandedId('');
    setReconnectingId(binding.id);
    setSocialForm({});
    // Prefill the Matrix server from the account's own domain — the backend
    // follows .well-known delegation, so "senla.eu" resolves to matrix.senla.eu.
    const matrixDomain = binding.platform === 'matrix' ? (binding.bot_username || '').split(':')[1] : '';
    setReconnectForm(matrixDomain
      ? { ...emptyReconnectForm, homeserver_url: `https://${matrixDomain}`, user_id: binding.bot_username || '' }
      : emptyReconnectForm);
    setError('');
  };

  /** `overrides` exists for the browser login, which finishes inside a child
   *  component: it must submit with the credentials it just obtained, not with
   *  whatever setReconnectForm has managed to flush into state by then. */
  const submitReconnect = async (binding: MessengerBinding, overrides?: Partial<typeof emptyReconnectForm>) => {
    const form = { ...reconnectForm, ...(overrides || {}) };
    setReconnecting(true);
    setError('');
    try {
      const bodyByPlatform: Record<string, object> = {
        telegram: { bot_token: form.bot_token },
        discord: { bot_token: form.bot_token },
        matrix: {
          homeserver_url: form.homeserver_url, user_id: form.user_id,
          password: form.password, access_token: form.access_token,
          refresh_token: form.refresh_token, device_flow_id: form.device_flow_id,
        },
        slack: { bot_token: form.bot_token, app_token: form.app_token },
        email: {
          imap_host: form.imap_host, imap_port: Number(form.imap_port) || 993,
          smtp_host: form.smtp_host, smtp_port: Number(form.smtp_port) || 587,
          address: form.address, password: form.password,
        },
      };
      const response = await fetch(`/api/messenger-bindings/${binding.id}/reconnect`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(
          isSocialPlatform(binding.platform) ? { credentials: socialForm } : (bodyByPlatform[binding.platform] || {}),
        ),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Не удалось переподключить канал');
      setReconnectingId('');
      setSocialForm({});
      setNotice('Канал переподключён с новыми учётными данными.');
      fetchBindings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось переподключить канал');
    } finally {
      setReconnecting(false);
    }
  };

  /** Callback URL + verify token for a webhook-delivered channel. Fetched only
   *  when its card is opened: the verify token is a credential and has its own
   *  endpoint rather than riding along in the binding list. */
  const loadWebhookDetails = (binding: MessengerBinding) => {
    fetch(`/api/messenger-bindings/${binding.id}/webhook`, { headers: authHeaders() })
      .then(res => (res.ok ? res.json() : null))
      .then(data => { if (data) setWebhookDetails(prev => ({ ...prev, [binding.id]: data })); })
      .catch(() => {});
  };

  const openEdit = (binding: MessengerBinding) => {
    if (expandedId === binding.id) {
      setExpandedId('');
      return;
    }
    setReconnectingId('');
    setExpandedId(binding.id);
    if (socialSpecs.find(spec => spec.id === binding.platform)?.delivery === 'webhook') {
      loadWebhookDetails(binding);
    }
    setEditPrompt(binding.system_prompt_override || '');
    setEditMode((binding.response_mode as ResponseMode) || 'draft');
    setEditAccessMode((binding.access_mode as AccessMode) || 'owner_only');
    const allowed = binding.allowed_chat_ids || [];
    setEditOpenToEveryone(allowed.length === 0);
    setEditAllowedChatIds(allowed.join(', '));
    setEditWelcome(binding.welcome_message || '');
    setEditDefaultPlanId(binding.default_plan_id || '');
    setEditDisclosure(binding.auto_reply_disclosure || '');
    setEditPauseMinutes(binding.human_takeover_pause_minutes ?? 120);
    setEditEscalation(Boolean(binding.escalation_enabled));
    setEditHandlingMenu(Boolean(binding.handling_menu_enabled));
  };

  const saveEdit = async (binding: MessengerBinding) => {
    setSavingEdit(true);
    try {
      const body: Record<string, unknown> = {
        system_prompt: editPrompt,
        response_mode: editMode,
        access_mode: editAccessMode,
        welcome_message: editWelcome,
        default_plan_id: editDefaultPlanId || null,
        auto_reply_disclosure: editDisclosure,
        ...(binding.platform === 'matrix' ? { human_takeover_pause_minutes: editPauseMinutes } : {}),
        escalation_enabled: editEscalation,
        handling_menu_enabled: editHandlingMenu,
      };
      if (editAccessMode === 'owner_only') {
        body.allowed_chat_ids = editOpenToEveryone
          ? []
          : editAllowedChatIds.split(',').map(s => s.trim()).filter(Boolean);
      }
      const response = await fetch(`/api/messenger-bindings/${binding.id}`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify(body),
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

  const fetchConversations = (bindingId: string) => {
    setConversationsLoading(true);
    fetch(`/api/messenger-bindings/${bindingId}/conversations`, { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setConversations(Array.isArray(data) ? data : []))
      .catch(() => setConversations([]))
      .finally(() => setConversationsLoading(false));
  };

  const toggleDialogs = (binding: MessengerBinding) => {
    if (dialogsBindingId === binding.id) {
      setDialogsBindingId('');
      return;
    }
    setExpandedId('');
    setReconnectingId('');
    setDialogsBindingId(binding.id);
    setConversationSearch('');
    setOpenHistoryKey('');
    fetchConversations(binding.id);
  };

  const conversationUrl = (bindingId: string, conversation: ConversationState, suffix: string) =>
    `/api/messenger-bindings/${bindingId}/conversations/${conversation.platform}/${encodeURIComponent(conversation.chat_id)}/${suffix}`;

  const setConversationMode = async (
    binding: MessengerBinding, conversation: ConversationState, action: 'take_over' | 'enable_ai' | 'wait',
  ) => {
    const key = `${conversation.platform}|${conversation.chat_id}`;
    setBusyConversationKey(key);
    setError('');
    try {
      const response = await fetch(conversationUrl(binding.id, conversation, 'takeover'), {
        method: 'POST', headers: authHeaders(), body: JSON.stringify({ action }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Не удалось переключить диалог');
      setConversations(current => current.map(item => (item.id === conversation.id ? { ...item, handling_mode: data.handling_mode } : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось переключить диалог');
    } finally {
      setBusyConversationKey('');
    }
  };

  const toggleHistory = async (binding: MessengerBinding, conversation: ConversationState) => {
    const key = `${conversation.platform}|${conversation.chat_id}`;
    if (openHistoryKey === key) {
      setOpenHistoryKey('');
      return;
    }
    setOpenHistoryKey(key);
    setHistoryMessages([]);
    setHistoryLoading(true);
    try {
      const response = await fetch(conversationUrl(binding.id, conversation, 'history?limit=200'), { headers: authHeaders() });
      const data = await response.json();
      setHistoryMessages(Array.isArray(data.messages) ? data.messages : []);
    } catch {
      setHistoryMessages([]);
    } finally {
      setHistoryLoading(false);
    }
  };

  const clearConversationHistory = async (binding: MessengerBinding, conversation: ConversationState) => {
    const who = conversation.display_name || conversation.chat_id;
    if (!window.confirm(`Очистить переписку с «${who}»? Агент забудет весь контекст этого диалога — восстановить нельзя. Режим ответа (кто отвечает) не изменится.`)) return;
    const key = `${conversation.platform}|${conversation.chat_id}`;
    setBusyConversationKey(key);
    setError('');
    try {
      const response = await fetch(conversationUrl(binding.id, conversation, 'history'), {
        method: 'DELETE', headers: authHeaders(),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Не удалось очистить переписку');
      }
      setHistoryMessages([]);
      setConversations(current => current.map(item => (item.id === conversation.id
        ? { ...item, messages_count: 0, last_message_preview: '', last_message_role: '', last_message_at: '' }
        : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось очистить переписку');
    } finally {
      setBusyConversationKey('');
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
    // A social platform is ready when every field the backend declared required
    // has a value — no per-platform list to keep in step here.
    isSocialPlatform(platform) ? Boolean(socialSpec && socialSpec.required.every(f => (socialForm[f] || '').trim())) :
    platform === 'telegram' ? Boolean(telegramForm.bot_token) :
    platform === 'matrix' ? Boolean(matrixForm.device_flow_id || (matrixForm.user_id && (matrixForm.password || matrixForm.access_token))) :
    platform === 'discord' ? Boolean(discordForm.bot_token) :
    platform === 'slack' ? Boolean(slackForm.bot_token && slackForm.app_token) :
    Boolean(emailForm.imap_host && emailForm.smtp_host && emailForm.address && emailForm.password)
  );

  return (
    <div>
      {view === 'connections' && !showForm && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 14 }}>
          <button type="button" className="btn-primary" onClick={() => setShowForm(true)}>
            <Plus size={14} /><span>Подключить канал</span>
          </button>
        </div>
      )}

      {view === 'activity' && (
        <div className="glass-panel" style={{ padding: '16px', marginBottom: '18px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <strong style={{ fontSize: '0.9rem' }}>Журнал сообщений (все каналы)</strong>
            <span style={{ fontSize: '0.72rem', color: 'var(--text-dim, #8a94a6)' }}>Обновляется каждые 3 с · последние {activity.length} событий</span>
          </div>
          <p style={{ fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', margin: 0 }}>
            Показывает каждое входящее сообщение и что с ним сделал шлюз доступа: доставлено агенту, отвечено без LLM,
            проигнорировано или упало с ошибкой. Не сохраняется — сбрасывается при перезапуске сервера.
          </p>
          <div style={{ maxHeight: 360, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
            {activity.length === 0 ? (
              <span style={{ fontSize: '0.8rem', color: 'var(--text-dim, #8a94a6)' }}>Пока ничего не приходило.</span>
            ) : (
              activity.map((event, i) => {
                const binding = bindings.find(b => b.id === event.binding_id);
                const kindMeta: Record<string, { label: string; color: string }> = {
                  proceed: { label: 'доставлено агенту', color: '#4ddeb4' },
                  reply: { label: 'ответ без LLM', color: '#63b3ed' },
                  ignore: { label: 'проигнорировано', color: '#8a94a6' },
                  invite: { label: 'приглашение', color: '#b794f4' },
                  error: { label: 'ошибка', color: '#f76b6b' },
                };
                const meta = kindMeta[event.kind] || { label: event.kind, color: '#8a94a6' };
                return (
                  <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', fontSize: '0.78rem', padding: '6px 8px', borderRadius: 6, background: 'rgba(255,255,255,.02)' }}>
                    <span style={{ color: 'var(--text-dim, #8a94a6)', whiteSpace: 'nowrap' }}>{new Date(event.ts * 1000).toLocaleTimeString()}</span>
                    <span style={{ whiteSpace: 'nowrap' }}>{PLATFORM_LABELS[event.platform as Platform] || event.platform}</span>
                    <span style={{ whiteSpace: 'nowrap', color: 'var(--text-dim, #8a94a6)' }}>{binding?.agent_name || event.binding_id}</span>
                    <span style={{ whiteSpace: 'nowrap', color: meta.color, fontWeight: 600 }}>{meta.label}</span>
                    <span style={{ color: 'var(--text-dim, #8a94a6)', whiteSpace: 'nowrap' }}>{event.sender || event.chat_id}</span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{event.detail}</span>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {view === 'drafts' && replies.length > 0 && (
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
      {view === 'drafts' && !repliesLoading && replies.length === 0 && (
        <p style={{ color: 'var(--text-dim, #8a94a6)', fontSize: '0.8rem', marginBottom: '14px' }}>Черновиков на подтверждение сейчас нет.</p>
      )}

      {view === 'connections' && showForm && (
        <div className="glass-panel" style={{ padding: '16px', marginBottom: '18px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <strong style={{ fontSize: '0.9rem' }}>Новое подключение</strong>
            <button type="button" className="icon-btn" onClick={resetForm} aria-label="Закрыть" title="Закрыть"><X size={14} /></button>
          </div>

          <div className="admin-add-grid">
            {field('Платформа', (
              <select className="form-input" value={platform} onChange={e => setPlatform(e.target.value as Platform)}>
                <option value="telegram">Telegram</option>
                <option value="matrix">Element / Matrix</option>
                <option value="discord">Discord</option>
                <option value="slack">Slack</option>
                <option value="email">Email</option>
                {socialSpecs.map(spec => <option key={spec.id} value={spec.id}>{spec.label}</option>)}
              </select>
            ))}
            {field('Агент', (
              <select className="form-input" value={agentId} onChange={e => setAgentId(e.target.value)}>
                <option value="">Выберите агента...</option>
                {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            ))}
          </div>

          {platform === 'telegram' && (
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
          )}

          {platform === 'matrix' && (
            <>
              <div className="admin-add-grid">
                {field('Homeserver URL', (
                  <input className="form-input" value={matrixForm.homeserver_url}
                    onChange={e => setMatrixForm({ ...matrixForm, homeserver_url: e.target.value, device_flow_id: '' })} placeholder="https://matrix.org" />
                ))}
                {field('Разрешённые room ID (через запятую)', (
                  <input className="form-input" value={matrixForm.allowed_room_ids}
                    onChange={e => setMatrixForm({ ...matrixForm, allowed_room_ids: e.target.value })} placeholder="без ограничений, если пусто" />
                ))}
              </div>
              <MatrixDeviceLogin
                homeserverUrl={matrixForm.homeserver_url}
                onComplete={(flowId, userId) => setMatrixForm(form => ({ ...form, device_flow_id: flowId, user_id: userId }))}
              />
              <details>
                <summary style={{ cursor: 'pointer', color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
                  Войти вручную (пароль или access token)
                </summary>
                <div className="admin-add-grid" style={{ marginTop: 10 }}>
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
                  {field('Refresh token (опционально, для авто-обновления)', (
                    <input className="form-input" type="password" autoComplete="off" value={matrixForm.refresh_token}
                      onChange={e => setMatrixForm({ ...matrixForm, refresh_token: e.target.value })}
                      placeholder="нужен, если access token живёт недолго (MAS)" />
                  ))}
                </div>
              </details>
            </>
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

          {socialSpec && (
            <>
              <SocialCredentialFields spec={socialSpec} values={socialForm} onChange={setSocialForm} />
              {field('Разрешённые id собеседников (через запятую)', (
                <input className="form-input" value={socialAllowed}
                  onChange={e => setSocialAllowed(e.target.value)} placeholder="без ограничений, если пусто" />
              ))}
              <p style={{ ...styles.formHelp, margin: 0 }}>
                {socialSpec.delivery === 'webhook'
                  ? 'Сообщения приходят вебхуком: после подтверждения канал покажет адрес и verify token, которые нужно зарегистрировать в приложении Meta. Пока этот бэкенд не доступен из интернета, входящих не будет — учётные данные при этом рабочие.'
                  : 'Сообщения забираются опросом (Bots Long Poll) — ничего публиковать в интернет не нужно.'}
              </p>
            </>
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

      {view !== 'connections' ? null : loading ? (
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
                      {PLATFORM_LABELS[binding.platform as Platform] || binding.platform} · {binding.bot_username} · {RESPONSE_MODE_LABELS[(binding.response_mode as ResponseMode) || 'draft']} · {ACCESS_MODE_LABELS[(binding.access_mode as AccessMode) || 'owner_only']}
                    </span>
                    {binding.status === 'failed' && binding.last_error && (
                      <span style={{ display: 'block', fontSize: '0.74rem', color: '#f76b6b', marginTop: 2 }}>{binding.last_error}</span>
                    )}
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span className={`admin-status-chip ${binding.status === 'active' ? 'is-active' : binding.status === 'failed' || binding.status === 'revoked' ? 'is-revoked' : ''}`} title={binding.last_error || ''}>
                    <i className="dot" />{STATUS_LABELS[binding.status] || binding.status}
                  </span>
                  {binding.status === 'active' && (
                    <button type="button" className="icon-btn" title="Настроить промпт/режим" onClick={() => openEdit(binding)}>
                      {expandedId === binding.id ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                    </button>
                  )}
                  {binding.status === 'active' && (
                    <button type="button" className="icon-btn" title="Диалоги — кто с кем сейчас говорит, взять диалог на себя" onClick={() => toggleDialogs(binding)}>
                      <Users size={14} />
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
                  {(binding.status === 'revoked' || binding.status === 'failed') && (
                    <button
                      type="button"
                      className="icon-btn"
                      title={binding.status === 'failed' ? 'Попробовать снова с тем же токеном' : 'Включить'}
                      disabled={busyBindingId === binding.id}
                      onClick={() => void enableBinding(binding)}
                    >
                      <PowerOff size={14} />
                    </button>
                  )}
                  {(binding.status === 'active' || binding.status === 'failed') && (
                    <button
                      type="button"
                      className="icon-btn"
                      title="Переподключить — вставить свежие учётные данные без пересоздания канала"
                      onClick={() => openReconnect(binding)}
                    >
                      <RefreshCw size={14} />
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

              {reconnectingId === binding.id && (
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,.07)', display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>
                    Вставьте новые учётные данные для этого же канала — промпт, режим ответа и белый список останутся как есть.
                    Проверяются перед сохранением, как при первом подключении.
                  </span>
                  {binding.platform === 'telegram' && field('Bot token from @BotFather', (
                    <input className="form-input" type="password" autoComplete="off" value={reconnectForm.bot_token}
                      onChange={e => setReconnectForm({ ...reconnectForm, bot_token: e.target.value })} placeholder="123456:AA..." />
                  ))}
                  {binding.platform === 'discord' && field('Bot token (Discord Developer Portal → Bot)', (
                    <input className="form-input" type="password" autoComplete="off" value={reconnectForm.bot_token}
                      onChange={e => setReconnectForm({ ...reconnectForm, bot_token: e.target.value })} placeholder="MTIz..." />
                  ))}
                  {binding.platform === 'matrix' && (
                    <>
                      {field('Homeserver URL', (
                        <input className="form-input" value={reconnectForm.homeserver_url}
                          onChange={e => setReconnectForm({ ...reconnectForm, homeserver_url: e.target.value, device_flow_id: '' })} placeholder="https://matrix.org" />
                      ))}
                      <MatrixDeviceLogin
                        homeserverUrl={reconnectForm.homeserver_url}
                        onComplete={(flowId, userId) => {
                          setReconnectForm(form => ({ ...form, device_flow_id: flowId, user_id: userId }));
                          // Finishing the login IS the reconnect — making the owner
                          // find and press a second button afterwards just left the
                          // channel sitting in 'failed' with a perfectly good login
                          // already done.
                          void submitReconnect(binding, { device_flow_id: flowId, user_id: userId });
                        }}
                      />
                      <details>
                        <summary style={{ cursor: 'pointer', color: 'var(--text-muted)', fontSize: '0.8rem', fontWeight: 600 }}>
                          Войти вручную (пароль или access token)
                        </summary>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 10 }}>
                          {field('Matrix user ID', (
                            <input className="form-input" value={reconnectForm.user_id}
                              onChange={e => setReconnectForm({ ...reconnectForm, user_id: e.target.value })} placeholder="@agent:matrix.org" />
                          ))}
                          {field('Пароль (или access token ниже)', (
                            <input className="form-input" type="password" autoComplete="off" value={reconnectForm.password}
                              onChange={e => setReconnectForm({ ...reconnectForm, password: e.target.value })} />
                          ))}
                          {field('Access token (вместо пароля)', (
                            <input className="form-input" type="password" autoComplete="off" value={reconnectForm.access_token}
                              onChange={e => setReconnectForm({ ...reconnectForm, access_token: e.target.value })} />
                          ))}
                          {field('Refresh token (опционально, для авто-обновления)', (
                            <input className="form-input" type="password" autoComplete="off" value={reconnectForm.refresh_token}
                              onChange={e => setReconnectForm({ ...reconnectForm, refresh_token: e.target.value })}
                              placeholder="нужен, если access token живёт недолго (MAS)" />
                          ))}
                        </div>
                      </details>
                    </>
                  )}
                  {binding.platform === 'slack' && (
                    <>
                      {field('Bot User OAuth Token (xoxb-...)', (
                        <input className="form-input" type="password" autoComplete="off" value={reconnectForm.bot_token}
                          onChange={e => setReconnectForm({ ...reconnectForm, bot_token: e.target.value })} placeholder="xoxb-..." />
                      ))}
                      {field('App-Level Token (xapp-...)', (
                        <input className="form-input" type="password" autoComplete="off" value={reconnectForm.app_token}
                          onChange={e => setReconnectForm({ ...reconnectForm, app_token: e.target.value })} placeholder="xapp-..." />
                      ))}
                    </>
                  )}
                  {binding.platform === 'email' && (
                    <>
                      {field('IMAP host', (
                        <input className="form-input" value={reconnectForm.imap_host}
                          onChange={e => setReconnectForm({ ...reconnectForm, imap_host: e.target.value })} placeholder="imap.gmail.com" />
                      ))}
                      {field('IMAP порт', (
                        <input className="form-input" type="number" value={reconnectForm.imap_port}
                          onChange={e => setReconnectForm({ ...reconnectForm, imap_port: e.target.value })} />
                      ))}
                      {field('SMTP host', (
                        <input className="form-input" value={reconnectForm.smtp_host}
                          onChange={e => setReconnectForm({ ...reconnectForm, smtp_host: e.target.value })} placeholder="smtp.gmail.com" />
                      ))}
                      {field('SMTP порт', (
                        <input className="form-input" type="number" value={reconnectForm.smtp_port}
                          onChange={e => setReconnectForm({ ...reconnectForm, smtp_port: e.target.value })} />
                      ))}
                      {field('Email адрес', (
                        <input className="form-input" value={reconnectForm.address}
                          onChange={e => setReconnectForm({ ...reconnectForm, address: e.target.value })} placeholder="agent@example.com" />
                      ))}
                      {field('Пароль', (
                        <input className="form-input" type="password" autoComplete="off" value={reconnectForm.password}
                          onChange={e => setReconnectForm({ ...reconnectForm, password: e.target.value })} />
                      ))}
                    </>
                  )}
                  {isSocialPlatform(binding.platform) && (() => {
                    const spec = socialSpecs.find(item => item.id === binding.platform);
                    return spec ? (
                      <>
                        <SocialCredentialFields spec={spec} values={socialForm} onChange={setSocialForm} />
                        {spec.delivery === 'webhook' && (
                          <p style={{ ...styles.formHelp, margin: 0 }}>
                            Verify token можно не заполнять — уже зарегистрированный в Meta останется прежним,
                            чтобы вебхук не пришлось подтверждать заново.
                          </p>
                        )}
                      </>
                    ) : null;
                  })()}
                  <button type="button" className="btn-primary" disabled={reconnecting} onClick={() => void submitReconnect(binding)} style={{ alignSelf: 'flex-start' }}>
                    <RefreshCw size={13} /><span>{reconnecting ? '...' : 'Переподключить'}</span>
                  </button>
                </div>
              )}

              {expandedId === binding.id && (
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,.07)', display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {webhookDetails[binding.id] && (() => {
                    const details = webhookDetails[binding.id];
                    return (
                      <div style={{ padding: '10px 12px', borderRadius: 8, border: '1px solid rgba(255,255,255,.09)', background: 'rgba(255,255,255,.02)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <strong style={{ fontSize: '0.82rem' }}>Вебхук в приложении Meta</strong>
                        <span style={styles.formHelp}>
                          Впишите это в Meta for Developers → приложение → Webhooks и подпишите аккаунт на события
                          сообщений. Пока это не сделано, канал активен, но входящих не получает.
                        </span>
                        {([
                          ['Callback URL', details.callback_url],
                          ['Verify token', details.verify_token],
                          ['ID аккаунта', details.account_id],
                        ] as [string, string][]).map(([label, value]) => (
                          <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.78rem' }}>
                            <span style={{ color: 'var(--text-dim, #8a94a6)', minWidth: 110 }}>{label}</span>
                            <code style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value || '—'}</code>
                            {value && (
                              <button type="button" className="icon-btn" title="Скопировать"
                                onClick={() => void navigator.clipboard?.writeText(value)}>
                                <Copy size={13} />
                              </button>
                            )}
                          </div>
                        ))}
                        {!details.base_url_configured && (
                          <span style={{ fontSize: '0.75rem', color: '#f0b23c' }}>
                            Публичный адрес не задан — укажите его в «SMM Factory → Настройки», иначе URL выше неполный.
                          </span>
                        )}
                      </div>
                    );
                  })()}
                  {field('Системный промпт для этого канала', (
                    <textarea className="form-input" rows={3} value={editPrompt} onChange={e => setEditPrompt(e.target.value)}
                      placeholder="Пусто — использовать промпт агента как есть" />
                  ))}
                  <ResponseModePicker value={editMode} onChange={setEditMode} />
                  <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: '0.85rem', color: 'var(--text-muted)', cursor: 'pointer' }}>
                    <input type="checkbox" checked={editEscalation} style={{ marginTop: 3 }}
                      onChange={e => setEditEscalation(e.target.checked)} />
                    <span>
                      Предлагать позвать Альберта в сложных вопросах
                      <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2 }}>
                        Если сообщение похоже на то, что должен решать лично Альберт, бот вместо обычного ответа
                        спросит «Вижу, что данное сообщение требует участие Альберта. Позвонить и сообщить ему?».
                        При согласии собеседника Альберту придёт уведомление в Telegram — реального звонка нет,
                        только уведомление. Это оценка модели, не гарантия: иногда сработает на пустом месте,
                        иногда пропустит то, что стоило бы эскалировать.
                      </span>
                    </span>
                  </label>
                  <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: '0.85rem', color: 'var(--text-muted)', cursor: 'pointer' }}>
                    <input type="checkbox" checked={editHandlingMenu} style={{ marginTop: 3 }}
                      onChange={e => setEditHandlingMenu(e.target.checked)} />
                    <span>
                      Предлагать выбор при первом сообщении: «ИИ сейчас», «подождать» или «срочно»
                      <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2 }}>
                        Новому собеседнику вместо тишины (в режиме «черновик») бот напишет, что вы загружены
                        другими задачами, и предложит три варианта: пообщаться с ИИ-ассистентом прямо сейчас
                        (с пометкой, что его обучает Альберт); просто подождать вас лично — тогда бот замолкает;
                        либо, если вопрос срочный, сразу прислать вам уведомление в Telegram через Vexa
                        (не чаще раза в 10 минут на диалог). Выбор запоминается для этого чата и сбрасывается сам,
                        если переписка надолго затихла. Взять диалог на себя в любой момент можно кнопкой
                        «Диалоги» — независимо от этой настройки.
                      </span>
                    </span>
                  </label>
                  {binding.platform === 'matrix' && field('Если я сам пишу в этом чате', (
                    <>
                      <select className="form-input" value={editPauseMinutes}
                        onChange={e => setEditPauseMinutes(Number(e.target.value))}>
                        <option value={0}>Не останавливать бота</option>
                        <option value={5}>Молчать 5 минут</option>
                        <option value={10}>Молчать 10 минут</option>
                        <option value={15}>Молчать 15 минут</option>
                        <option value={30}>Молчать 30 минут</option>
                        <option value={60}>Молчать 1 час</option>
                        <option value={120}>Молчать 2 часа</option>
                        <option value={240}>Молчать 4 часа</option>
                        <option value={480}>Молчать 8 часов</option>
                        <option value={1440}>Молчать сутки</option>
                      </select>
                      <span style={styles.formHelp}>
                        Этот бот работает через ваш собственный аккаунт Matrix — если вы сами ответите в чате
                        (из Element или другого устройства), Vexa увидит это и перестанет вмешиваться в разговор
                        на выбранное время. Каждый ваш новый ответ продлевает паузу.
                      </span>
                    </>
                  ))}
                  {editMode === 'auto_labeled' && field('Подпись под автоответом', (
                    <>
                      <input className="form-input" maxLength={200} value={editDisclosure}
                        onChange={e => setEditDisclosure(e.target.value)}
                        placeholder="— личный AI-ассистент, а не человек." />
                      <span style={styles.formHelp}>
                        Добавляется в конец каждого автоответа, чтобы собеседник видел, что пишет не Альберт лично —
                        это условие самого режима, полностью убрать подпись нельзя, только переформулировать.
                        Пусто — используется формулировка по умолчанию.
                      </span>
                    </>
                  ))}
                  <AccessModePicker value={editAccessMode} onChange={setEditAccessMode} />
                  {editAccessMode === 'owner_only' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingLeft: 4 }}>
                      <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: '0.8rem', cursor: 'pointer' }}>
                        <input type="checkbox" checked={editOpenToEveryone} style={{ marginTop: 3 }}
                          onChange={e => setEditOpenToEveryone(e.target.checked)} />
                        <span>
                          Отвечать всем, кто пишет мне в личку
                          <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2 }}>
                            Без списка — бот отвечает любому, кто напишет ему напрямую, без токенов и оплаты.
                          </span>
                        </span>
                      </label>
                      {!editOpenToEveryone && field('Разрешённые чаты (через запятую)', (
                        <input className="form-input" value={editAllowedChatIds}
                          onChange={e => setEditAllowedChatIds(e.target.value)} placeholder="ID чатов через запятую" />
                      ))}
                    </div>
                  )}
                  {editAccessMode === 'token' && field('Приветствие для тех, кто ещё не прислал токен', (
                    <textarea className="form-input" rows={2} value={editWelcome} onChange={e => setEditWelcome(e.target.value)}
                      placeholder="Пусто — стандартный запрос токена" />
                  ))}
                  {editAccessMode === 'token' && (() => {
                    const sellable = plans.filter(p => p.is_purchasable && p.price_usd != null
                      && (!p.subagent_id || p.subagent_id === binding.subagent_id));
                    return field('Что предложить купить тому, кто напишет без токена', (
                      <>
                        <select className="form-input" value={editDefaultPlanId} onChange={e => setEditDefaultPlanId(e.target.value)}>
                          <option value="">Не продавать — только просить токен</option>
                          {sellable.map(p => (
                            <option key={p.id} value={p.id}>{p.name} — ${Number(p.price_usd).toFixed(2)}</option>
                          ))}
                        </select>
                        <span style={styles.formHelp}>
                          Показаны только тарифы этого агента (или общие, без привязки) с ценой и статусом «на продажу» —
                          настраиваются в «Админка агентов» → «Доступ к ботам» → «Тарифы».
                        </span>
                      </>
                    ));
                  })()}
                  <button type="button" className="btn-primary" disabled={savingEdit} onClick={() => void saveEdit(binding)} style={{ alignSelf: 'flex-start' }}>
                    <Check size={13} /><span>{savingEdit ? '...' : 'Сохранить'}</span>
                  </button>
                </div>
              )}

              {dialogsBindingId === binding.id && (
                <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,.07)', display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <span style={styles.formHelp}>
                    Кто пишет в этот канал и кто ему отвечает. 🤖 — подключить ИИ к этому человеку сразу,
                    не дожидаясь его выбора; ✋ — взять диалог на себя, бот замолчит; 💬 — показать переписку,
                    которую помнит агент; 🧽 — очистить её. Переписка сохраняется всегда, даже пока бот молчит,
                    так что ИИ продолжит разговор с того же места.
                  </span>
                  {conversations.length > 5 && (
                    <input
                      className="form-input"
                      value={conversationSearch}
                      onChange={e => setConversationSearch(e.target.value)}
                      placeholder={`Поиск среди ${conversations.length} диалогов — имя или id чата`}
                    />
                  )}
                  {conversationsLoading ? (
                    <span style={{ fontSize: '0.8rem', color: 'var(--text-dim, #8a94a6)' }}>Загрузка...</span>
                  ) : conversations.length === 0 ? (
                    <span style={{ fontSize: '0.8rem', color: 'var(--text-dim, #8a94a6)' }}>Пока никто не писал.</span>
                  ) : (() => {
                    const needle = conversationSearch.trim().toLowerCase();
                    const shown = needle
                      ? conversations.filter(c => `${c.display_name} ${c.chat_id}`.toLowerCase().includes(needle))
                      : conversations;
                    if (shown.length === 0) {
                      return <span style={{ fontSize: '0.8rem', color: 'var(--text-dim, #8a94a6)' }}>Никто не найден.</span>;
                    }
                    return (
                      // Capped height so a binding with hundreds of people can't
                      // push the rest of the channel list off the screen.
                      <div className="admin-item-list" style={{ maxHeight: 460, overflowY: 'auto' }}>
                        {shown.map(conversation => {
                          const key = `${conversation.platform}|${conversation.chat_id}`;
                          const busy = busyConversationKey === key;
                          const takenOver = conversation.handling_mode === 'owner_takeover';
                          const aiOn = conversation.handling_mode === 'ai';
                          return (
                            <div key={conversation.id} className="admin-item-row" style={{ display: 'block' }}>
                              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
                                <div style={{ minWidth: 0 }}>
                                  <strong style={{ fontSize: '0.85rem' }}>{conversation.display_name || conversation.chat_id}</strong>
                                  <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)' }}>
                                    {PLATFORM_LABELS[conversation.platform as Platform] || conversation.platform} · {conversation.chat_id}
                                  </span>
                                  <span style={{ display: 'block', fontSize: '0.74rem', color: 'var(--text-dim, #8a94a6)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    {conversation.messages_count > 0
                                      ? `${conversation.messages_count} сообщ.${conversation.last_message_preview ? ` · ${conversation.last_message_role === 'assistant' ? 'бот' : 'он'}: ${conversation.last_message_preview}` : ''}`
                                      : 'переписка пуста'}
                                  </span>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                                  <span className={`admin-status-chip ${takenOver ? 'is-active' : ''}`}>
                                    {HANDLING_MODE_LABELS[conversation.handling_mode] || conversation.handling_mode}
                                  </span>
                                  {!aiOn && (
                                    <button type="button" className="icon-btn" title="Подключить ИИ — пусть агент отвечает этому человеку" disabled={busy}
                                      onClick={() => void setConversationMode(binding, conversation, 'enable_ai')}>
                                      <Bot size={14} />
                                    </button>
                                  )}
                                  {!takenOver && (
                                    <button type="button" className="icon-btn" title="Взять диалог на себя — бот замолчит" disabled={busy}
                                      onClick={() => void setConversationMode(binding, conversation, 'take_over')}>
                                      <Hand size={14} />
                                    </button>
                                  )}
                                  <button type="button" className="icon-btn" title="Показать переписку, которую помнит агент" disabled={busy}
                                    onClick={() => void toggleHistory(binding, conversation)}>
                                    <MessageSquare size={14} />
                                  </button>
                                  <button type="button" className="icon-btn danger" title="Очистить переписку — агент забудет контекст" disabled={busy || conversation.messages_count === 0}
                                    onClick={() => void clearConversationHistory(binding, conversation)}>
                                    <Eraser size={14} />
                                  </button>
                                </div>
                              </div>

                              {openHistoryKey === key && (
                                <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid rgba(255,255,255,.07)' }}>
                                  {historyLoading ? (
                                    <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>Загрузка переписки...</span>
                                  ) : historyMessages.length === 0 ? (
                                    <span style={{ fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)' }}>
                                      Пусто — агент ничего не помнит об этом человеке.
                                    </span>
                                  ) : (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 320, overflowY: 'auto' }}>
                                      {historyMessages.map(message => (
                                        <div key={message.id} style={{
                                          alignSelf: message.role === 'assistant' ? 'flex-end' : 'flex-start',
                                          maxWidth: '82%', padding: '7px 10px', borderRadius: 10, fontSize: '0.8rem',
                                          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                                          background: message.role === 'assistant' ? 'rgba(46,179,139,.10)' : 'rgba(255,255,255,.04)',
                                          border: `1px solid ${message.role === 'assistant' ? 'rgba(77,222,180,.22)' : 'rgba(255,255,255,.08)'}`,
                                        }}>
                                          <span style={{ display: 'block', fontSize: '0.68rem', color: 'var(--text-dim, #8a94a6)', marginBottom: 2 }}>
                                            {message.role === 'assistant' ? 'Бот' : 'Собеседник'}
                                            {message.timestamp ? ` · ${message.timestamp.slice(0, 16).replace('T', ' ')}` : ''}
                                          </span>
                                          {message.content}
                                        </div>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })()}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
