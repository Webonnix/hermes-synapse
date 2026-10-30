import { csrfToken } from '../../../sessionAuth';
import { useEffect, useState } from 'react';
import type {
  AvailableNumber, BindingSettings, InboxBinding, InboxConversation, InboxMessage, InboxMeta, InboxRule, OwnedNumber,
  PhoneCallRecord, PhoneLine, PhoneOverview, RouteState, RouteTarget, SpeechConfig, VoiceInfo, VoiceProvider,
  VoiceProviderKind, VoiceTarget,
} from './inboxTypes';

/** Fired on window for every `inbox_update` WebSocket frame (App.tsx forwards it). */
export const INBOX_UPDATE_EVENT = 'vexa-inbox-update';
/** Fired to jump to one conversation from elsewhere (the client card). */
export const OPEN_INBOX_EVENT = 'vexa-open-inbox';
export const OPEN_INBOX_KEY = 'inbox_open_conversation';

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** Every call goes to a relative /api path (tests/urls.test.ts guards that) and every
 *  payload is shape-checked before it reaches state: an error body is an object, and a
 *  `.map` over it would blank the whole dashboard. */
async function request<T>(url: string, init: RequestInit | undefined, guard: (data: unknown) => data is T): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...(init?.headers || {}) },
  });
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? String((data as { detail: unknown }).detail) : '';
    throw new ApiError(detail || `Ошибка ${response.status}`, response.status);
  }
  if (!guard(data)) throw new ApiError('Сервер вернул неожиданный ответ', 500);
  return data;
}

const isObject = (data: unknown): data is Record<string, unknown> => Boolean(data) && typeof data === 'object' && !Array.isArray(data);
const isArrayOf = <T,>() => (data: unknown): data is T[] => Array.isArray(data);
const isObj = <T,>() => (data: unknown): data is T => isObject(data);
const isPage = <T,>() => (data: unknown): data is { items: T[]; next_cursor: string | null } =>
  isObject(data) && Array.isArray(data.items);

const post = (body?: unknown): RequestInit => ({ method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });

export interface ConversationFilters {
  search?: string;
  handling?: string;
  status?: string;
  platform?: string;
  binding_id?: string;
  unread?: boolean;
  cursor?: string | null;
}

export const inboxApi = {
  meta: () => request('/api/inbox/meta', undefined, isObj<InboxMeta>()),
  bindings: () => request('/api/inbox/bindings', undefined, isArrayOf<InboxBinding>()),
  conversations: (filters: ConversationFilters) => {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '' || value === false) return;
      params.set(key, String(value));
    });
    return request(`/api/inbox/conversations?${params.toString()}`, undefined, isPage<InboxConversation>());
  },
  conversation: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}`, undefined, isObj<InboxConversation>()),
  messages: (id: string, cursor?: string | null) =>
    request(`/api/inbox/conversations/${encodeURIComponent(id)}/messages${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`,
      undefined, isPage<InboxMessage>()),
  markRead: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/read`, post(), isObj<InboxConversation>()),
  close: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/close`, post(), isObj<InboxConversation>()),
  reopen: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/reopen`, post(), isObj<InboxConversation>()),
  takeover: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/takeover`, post(), isObj<InboxConversation>()),
  resume: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/resume`, post(), isObj<InboxConversation>()),
  reply: (id: string, text: string) =>
    request(`/api/inbox/conversations/${encodeURIComponent(id)}/reply`, post({ text }), isObj<{ status: string }>()),
  whisper: (id: string, text: string) =>
    request(`/api/inbox/conversations/${encodeURIComponent(id)}/whisper`, post({ text }), isObj<{ status: string }>()),
  block: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/block`, post({}), isObj<InboxRule>()),
  linkClient: (id: string, body: { client_id?: string; create?: Record<string, unknown> }) =>
    request(`/api/inbox/conversations/${encodeURIComponent(id)}/link-client`, post(body), isObj<InboxConversation>()),
  route: (id: string) => request(`/api/inbox/conversations/${encodeURIComponent(id)}/route`, undefined, isObj<RouteState>()),
  pinRoute: (id: string, target: { target_type: string; target_id: string }) =>
    request(`/api/inbox/conversations/${encodeURIComponent(id)}/route`, post(target), isObj<RouteState>()),
  unpinRoute: (id: string) =>
    request(`/api/inbox/conversations/${encodeURIComponent(id)}/route`, { method: 'DELETE' }, isObj<RouteState>()),
  targets: () => request('/api/inbox/route-targets', undefined, isArrayOf<RouteTarget>()),
  sendDraft: (replyId: string, editedText?: string) =>
    request(`/api/channel-replies/${encodeURIComponent(replyId)}/send`, post({ edited_text: editedText ?? null }), isObj<Record<string, unknown>>()),
  discardDraft: (replyId: string) =>
    request(`/api/channel-replies/${encodeURIComponent(replyId)}/discard`, post(), isObj<Record<string, unknown>>()),
  rules: (bindingId?: string) =>
    request(`/api/inbox/rules${bindingId ? `?binding_id=${encodeURIComponent(bindingId)}` : ''}`, undefined, isArrayOf<InboxRule>()),
  createRule: (rule: { pattern: string; action: string; match: string; binding_id?: string | null; note?: string }) =>
    request('/api/inbox/rules', post(rule), isObj<InboxRule>()),
  deleteRule: (id: string) => request(`/api/inbox/rules/${encodeURIComponent(id)}`, { method: 'DELETE' }, isObj<{ status: string }>()),
  bindingSettings: (bindingId: string) =>
    request(`/api/inbox/bindings/${encodeURIComponent(bindingId)}/settings`, undefined, isObj<BindingSettings>()),
  saveBindingSettings: (bindingId: string, body: { business_hours: string; timezone: string }) =>
    request(`/api/inbox/bindings/${encodeURIComponent(bindingId)}/settings`, { method: 'PUT', body: JSON.stringify(body) }, isObj<BindingSettings>()),
  clientConversations: (clientId: string) =>
    request(`/api/clients/${encodeURIComponent(clientId)}/conversations`, undefined, isArrayOf<InboxConversation>()),
  searchClients: (search: string) =>
    request(`/api/clients?search=${encodeURIComponent(search)}&limit=8`, undefined,
      (data: unknown): data is { items: Array<{ id: string; name: string }> } => isObject(data) && Array.isArray(data.items)),
  clientTasks: (clientId: string) =>
    request(`/api/client-tasks?clientId=${encodeURIComponent(clientId)}`, undefined,
      isArrayOf<{ id: string; title: string; status: string; dueAt: string | null; overdue?: boolean }>()),
  attachmentUrl: async (attachmentId: string) => {
    const response = await fetch(`/api/inbox/attachments/${encodeURIComponent(attachmentId)}`);
    if (!response.ok) throw new ApiError(`Ошибка ${response.status}`, response.status);
    return URL.createObjectURL(await response.blob());
  },
};

export const voiceApi = {
  providers: () => request('/api/voice/providers', undefined,
    (data: unknown): data is { kinds: Record<string, VoiceProviderKind>; failure_policies: string[]; items: VoiceProvider[] } =>
      isObject(data) && Array.isArray(data.items) && isObject(data.kinds)),
  createProvider: (body: Record<string, unknown>) => request('/api/voice/providers', post(body), isObj<VoiceProvider>()),
  updateProvider: (id: string, body: Record<string, unknown>) =>
    request(`/api/voice/providers/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }, isObj<VoiceProvider>()),
  deleteProvider: (id: string) => request(`/api/voice/providers/${encodeURIComponent(id)}`, { method: 'DELETE' }, isObj<{ status: string }>()),
  testProvider: (id: string) =>
    request(`/api/voice/providers/${encodeURIComponent(id)}/test`, post(), isObj<{ ok: boolean; error?: string; latency_ms?: number }>()),
  voices: (id: string) => request(`/api/voice/providers/${encodeURIComponent(id)}/voices`, undefined, isArrayOf<VoiceInfo>()),
  rotateWebhookSecret: (id: string) =>
    request(`/api/voice/providers/${encodeURIComponent(id)}/webhook-secret`, post(), isObj<{ webhook_secret: string; webhook_path: string }>()),
  targets: () => request('/api/voice/targets', undefined, isArrayOf<VoiceTarget>()),
  createTarget: (body: Record<string, unknown>) => request('/api/voice/targets', post(body), isObj<VoiceTarget>()),
  deleteTarget: (id: string) => request(`/api/voice/targets/${encodeURIComponent(id)}`, { method: 'DELETE' }, isObj<{ status: string }>()),
};

const isOverview = (data: unknown): data is PhoneOverview =>
  isObject(data) && Array.isArray(data.lines) && isObject(data.providers) && isObject(data.status) && isObject(data.speech);

const enc = encodeURIComponent;

export const phoneApi = {
  overview: () => request('/api/phone/overview', undefined, isOverview),
  saveSettings: (body: { public_base_url?: string; max_concurrent_calls?: number }) =>
    request('/api/phone/settings', { method: 'PUT', body: JSON.stringify(body) }, isObj<{ public_base_url: string; max_concurrent_calls: number }>()),
  createLine: (body: Record<string, unknown>) => request('/api/phone/lines', post(body), isObj<PhoneLine>()),
  updateLine: (id: string, body: Record<string, unknown>) =>
    request(`/api/phone/lines/${enc(id)}`, { method: 'PATCH', body: JSON.stringify(body) }, isObj<PhoneLine>()),
  deleteLine: (id: string) => request(`/api/phone/lines/${enc(id)}`, { method: 'DELETE' }, isObj<{ status: string }>()),
  checkLine: (id: string) => request(`/api/phone/lines/${enc(id)}/check`, post(), isObj<{ ok: boolean; error?: string; state?: string }>()),
  searchNumbers: (id: string, country: string, kind: string, contains: string) =>
    request(`/api/phone/lines/${enc(id)}/numbers/search?country=${enc(country)}&kind=${enc(kind)}&contains=${enc(contains)}`,
      undefined, isArrayOf<AvailableNumber>()),
  ownedNumbers: (id: string) => request(`/api/phone/lines/${enc(id)}/numbers/owned`, undefined, isArrayOf<OwnedNumber>()),
  buyNumber: (id: string, phoneNumber: string) =>
    request(`/api/phone/lines/${enc(id)}/numbers/buy`, post({ phone_number: phoneNumber, confirm: true }), isObj<Record<string, unknown>>()),
  attachNumber: (id: string, phoneNumber: string) =>
    request(`/api/phone/lines/${enc(id)}/numbers/attach`, post({ phone_number: phoneNumber }), isObj<PhoneLine>()),
  saveSpeech: (body: Partial<SpeechConfig>) =>
    request('/api/phone/speech', { method: 'PUT', body: JSON.stringify(body) }, isObj<SpeechConfig>()),
  setSpeechKey: (provider: string, apiKey: string) =>
    request(`/api/phone/speech/keys/${enc(provider)}`, { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) }, isObj<{ has_secret: boolean }>()),
  clearSpeechKey: (provider: string) =>
    request(`/api/phone/speech/keys/${enc(provider)}`, { method: 'DELETE' }, isObj<{ has_secret: boolean }>()),
  testSpeech: () =>
    request('/api/phone/speech/test', post(), isObj<{ ok: boolean; engine?: string; first_audio_ms?: number; error?: string; degraded?: boolean }>()),
  selftest: () => request('/api/phone/selftest', post(), isObj<{ ok: boolean; audio_ms?: number; error?: string }>()),
  calls: (params: { line_id?: string; conversation_id?: string; limit?: number }) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== '') query.set(key, String(value)); });
    return request(`/api/phone/calls?${query.toString()}`, undefined, isArrayOf<PhoneCallRecord>());
  },
  startCall: (body: { line_id: string; to: string; purpose?: string; client_id?: string | null }) =>
    request('/api/phone/calls', post(body), isObj<PhoneCallRecord>()),
  hangupCall: (id: string) => request(`/api/phone/calls/${enc(id)}/hangup`, post(), isObj<{ status: string }>()),
  transferCall: (id: string, number = '') =>
    request(`/api/phone/calls/${enc(id)}/transfer`, post({ number }), isObj<{ status: string }>()),
};

export const CALL_STATUS_LABELS: Record<string, string> = {
  ringing: 'дозвон', active: 'идёт разговор', ended: 'завершён', failed: 'не состоялся', rejected: 'отклонён',
};

export const CALL_END_LABELS: Record<string, string> = {
  remote_hangup: 'положил трубку', agent_hangup: 'агент завершил', transferred: 'переведён на владельца',
  silence: 'тишина на линии', max_duration: 'лимит длительности', stt_unavailable: 'нет распознавания',
  no_answer: 'не ответили', busy: 'занято', rule: 'заблокирован правилом', capacity: 'линии заняты',
  line_disabled: 'линия выключена', direct_transfer: 'сразу владельцу', backend_restart: 'перезапуск сервера',
  operator: 'завершил оператор',
};

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m} мин ${s} с` : `${s} с`;
}

export function openInboxConversation(conversationId: string) {
  try {
    localStorage.setItem(OPEN_INBOX_KEY, conversationId);
  } catch {
    /* storage unavailable — the event below still carries the id */
  }
  window.dispatchEvent(new CustomEvent(OPEN_INBOX_EVENT, { detail: { conversationId } }));
}

/** Sidebar badge: conversations with unread customer messages. Silent (0) for a
 *  logged-out tab or a role without inbox access — a badge must never trigger the
 *  401 → logout path by polling without a session. */
export function useInboxUnread(): number {
  const [count, setCount] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      if (!csrfToken()) return;
      try {
        const meta = await inboxApi.meta();
        if (!cancelled) setCount(Number(meta.unread_conversations) || 0);
      } catch {
        if (!cancelled) setCount(0);
      }
    };
    const onUpdate = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void load(), 800);
    };
    void load();
    const interval = setInterval(() => void load(), 60000);
    window.addEventListener(INBOX_UPDATE_EVENT, onUpdate);
    return () => {
      cancelled = true;
      clearInterval(interval);
      if (timer) clearTimeout(timer);
      window.removeEventListener(INBOX_UPDATE_EVENT, onUpdate);
    };
  }, []);
  return count;
}

export const PLATFORM_LABELS: Record<string, string> = {
  phone: 'Телефон', telegram: 'Telegram', matrix: 'Matrix', discord: 'Discord', slack: 'Slack', email: 'Email',
  vk: 'VK', instagram: 'Instagram', facebook: 'Messenger', whatsapp: 'WhatsApp',
};

export const HANDLING_LABELS: Record<string, string> = { ai: 'ИИ отвечает', human: 'Оператор', blocked: 'Заблокирован' };

export function formatWhen(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' +
      date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
