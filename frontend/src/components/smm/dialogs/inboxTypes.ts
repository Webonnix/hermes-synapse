/** Shapes served by backend/inbox_api.py. Kept beside the inbox components rather
 *  than in src/types.ts — nothing outside this section reads them. */

export type Handling = 'ai' | 'human' | 'blocked';
export type Speaker = 'customer' | 'agent' | 'human' | 'whisper' | 'system';
export type Delivery = 'sent' | 'draft' | 'discarded' | 'failed';

export interface InboxConversation {
  id: string;
  binding_id: string;
  platform: string;
  chat_id: string;
  session_id: string;
  subagent_id: string;
  project_id: string | null;
  client_id: string | null;
  contact_id: string | null;
  display_name: string;
  external_user_id: string;
  status: 'open' | 'closed';
  unread_count: number;
  last_message_at: string | null;
  last_message_preview: string;
  last_speaker: string;
  summary: string;
  cost_usd?: number;
  last_rule_action: string | null;
  last_rule_pattern: string | null;
  channel_meta: Record<string, unknown>;
  handling: Handling;
  state_mode: string | null;
  account: string | null;
  response_mode: string | null;
  agent_name: string | null;
  client_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface InboxAttachment {
  id: string;
  message_id: number;
  kind: string;
  mime: string;
  size_bytes: number;
  duration_ms: number | null;
  transcript: string | null;
  created_at: string;
}

export interface InboxMessage {
  id: number;
  conversation_id: string;
  speaker: Speaker;
  text: string;
  delivery: Delivery;
  pending_reply_id: string | null;
  external_event_id: string | null;
  consumed_at: string | null;
  meta: Record<string, unknown>;
  created_at: string;
  attachments: InboxAttachment[];
}

export interface InboxMeta {
  fts: boolean;
  unread_conversations: number;
  can_reply: boolean;
  can_edit_rules: boolean;
  can_manage_voice: boolean;
  can_create_clients: boolean;
  can_view_financials: boolean;
}

export interface InboxBinding {
  id: string;
  platform: string;
  bot_username: string;
  subagent_id: string;
  status: string;
  response_mode: string;
  agent_name: string | null;
}

export interface RouteTarget {
  type: 'subagent' | 'external_voice';
  id: string;
  name: string;
  enabled: boolean;
  available: boolean;
  project_id?: string | null;
  provider_name?: string;
  provider_kind?: string;
  capability_mode?: string;
  voice_id?: string;
  failure_policy?: string;
  health?: { ok?: boolean; latency_ms?: number; checked_at?: string; error?: string };
}

export interface RouteState {
  conversation_id: string;
  pinned: boolean;
  target_type: string | null;
  target_id: string | null;
  pinned_by: string | null;
  pinned_at: string | null;
  last_target_type: string | null;
  last_target_id: string | null;
  last_source: string | null;
  last_reason: string | null;
  last_latency_ms: number | null;
  last_decided_at: string | null;
  target: RouteTarget | null;
}

export interface InboxRule {
  id: string;
  binding_id: string | null;
  pattern: string;
  match: 'exact' | 'prefix';
  action: 'pass' | 'block' | 'ai';
  note: string;
  created_at: string;
}

export interface BindingSettings {
  binding_id: string;
  business_hours: string;
  timezone: string;
  updated_at: string | null;
  within_hours_now?: boolean;
}

export interface ProviderHealth {
  ok?: boolean;
  latency_ms?: number;
  status_code?: number | null;
  error?: string;
  checked_at?: string;
}

export interface VoiceProvider {
  id: string;
  name: string;
  kind: string;
  kind_label: string;
  base_url: string;
  enabled: boolean;
  capabilities: string[];
  config: Record<string, unknown>;
  timeout_seconds: number;
  max_retries: number;
  last_health: ProviderHealth;
  has_secret: boolean;
  has_webhook_secret: boolean;
  webhook_path: string;
  created_at: string;
  updated_at: string;
}

export interface VoiceProviderKind {
  label: string;
  capabilities: string[];
}

export interface VoiceTarget {
  id: string;
  provider_id: string;
  name: string;
  capability_mode: 'voice_agent' | 'tts';
  voice_id: string;
  agent_id: string;
  failure_policy: string;
  enabled: boolean;
  provider_name: string;
  provider_kind: string;
  provider_enabled: boolean;
  available: boolean;
  health: ProviderHealth;
}

export interface VoiceInfo {
  id: string;
  name: string;
  language: string;
}

// ── telephony (backend/phone_api.py) ────────────────────────────────────────
export interface PhoneProviderField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  default?: string;
}

export interface PhoneProvider {
  label: string;
  recommended: boolean;
  transport: 'sip' | 'media_stream';
  needs_public_url: boolean;
  number_purchase: boolean;
  cabinet_url: string;
  note: string;
  fields: PhoneProviderField[];
}

export interface PhoneLine {
  id: string;
  name: string;
  provider: string;
  number: string;
  subagent_id: string;
  greeting: string;
  language: 'auto' | 'ru' | 'en';
  owner_number: string;
  enabled: boolean;
  inbound_enabled: boolean;
  outbound_enabled: boolean;
  auto_book: boolean;
  max_call_minutes: number;
  config: Record<string, string>;
  status: string;
  last_error: string;
  secrets_set: Record<string, boolean>;
  provider_label: string;
  created_at: string;
  updated_at: string;
}

export interface SpeechProvider {
  id: string;
  label: string;
  recommended: boolean;
  needs_key: boolean;
  streaming?: boolean;
  models: string[];
  default_model: string;
  languages?: string[];
  default_language?: string;
  default_voice?: string;
  voices?: string[];
  needs_folder?: boolean;
  note: string;
  has_secret: boolean;
}

export interface SpeechConfig {
  stt_provider: string;
  stt_model: string;
  stt_language: string;
  tts_provider: string;
  tts_model: string;
  tts_voice: string;
  tts_voice_en: string;
  yandex_folder_id: string;
  fallback_local: boolean;
}

export interface ActivePhoneCall {
  call_id: string;
  remote_number: string;
  direction: string;
  line_id: string;
  conversation_id: string;
}

export interface PhoneOverview {
  providers: Record<string, PhoneProvider>;
  lines: PhoneLine[];
  status: {
    asterisk: { enabled: boolean; connected: boolean; last_error: string; version: string; active_calls: number };
    active_calls: ActivePhoneCall[];
    settings: { public_base_url: string; max_concurrent_calls: number };
  };
  speech: { config: SpeechConfig; stt: SpeechProvider[]; tts: SpeechProvider[] };
  can_manage: boolean;
  can_call: boolean;
  can_control_calls: boolean;
}

export interface PhoneCallRecord {
  id: string;
  line_id: string;
  direction: 'inbound' | 'outbound';
  provider: string;
  provider_call_id: string;
  remote_number: string;
  local_number: string;
  status: 'ringing' | 'active' | 'ended' | 'failed' | 'rejected';
  purpose: string;
  client_id: string | null;
  conversation_id: string | null;
  started_at: string;
  answered_at: string | null;
  ended_at: string | null;
  duration_s: number | null;
  end_reason: string;
  turns: number;
  interruptions: number;
  first_audio_ms_avg: number | null;
  first_audio_ms_p90: number | null;
  transferred_to: string;
  degraded: string[];
}

export interface AvailableNumber {
  phone_number: string;
  friendly_name: string;
  locality: string;
  country: string;
  kind: string;
  monthly_cost?: string | number | null;
  upfront_cost?: string | number | null;
  currency?: string | null;
}

export interface OwnedNumber {
  sid: string;
  phone_number: string;
}
