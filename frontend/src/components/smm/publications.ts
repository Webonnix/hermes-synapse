/**
 * Shared vocabulary for the publication register (Публикации + Дизайнер).
 *
 * Same reason platforms.ts exists: the two panels show one set of enums the
 * backend stores as bare strings, and a second copy inside a component would
 * drift the moment one of them gained a status.
 *
 * The publishing platforms are a *superset* of the messenger ones. A channel
 * you can be written to (backend/agent_messenger_governance.py) and a channel
 * you can post to are different sets — nobody DMs a Twitter feed, and the
 * register has to be able to track a LinkedIn post the owner made by hand.
 * Only the platforms marked `deliverable` can be sent by the backend itself;
 * the rest are manual-only, and the Дизайнер says so rather than offering an
 * "Опубликовать" button that would always fail.
 */

import {
  AtSign, Facebook, Globe, Instagram, Linkedin, MessageCircle, Send, Twitter, Youtube,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export type PostStatus = 'draft' | 'scheduled' | 'published' | 'failed';
export type DeliveryMode = 'manual' | 'auto';

export interface Publication {
  id: string;
  title: string;
  body: string;
  platform: string;
  account: string;
  binding_id: string;
  target_chat_id: string;
  delivery_mode: DeliveryMode;
  status: PostStatus;
  tags: string[];
  media_url: string;
  reach: number | null;
  reach_delta: number | null;
  scheduled_at: string | null;
  published_at: string | null;
  error: string;
  created_at: string;
  updated_at: string;
}

export interface PublicationPage {
  items: Publication[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

export interface PublicationStats {
  total: number;
  published: number;
  scheduled: number;
  drafts: number;
  failed: number;
  published_share: number;
  scheduled_share: number;
  drafts_share: number;
  failed_share: number;
  added_last_week: number;
  best_reach: number | null;
  best_reach_delta: number | null;
  reach_series: number[];
}

export interface PostPlatform {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Brand tint, used only as an icon/accent colour so the row stays legible
   *  on the dark surface — never as a background for text. */
  color: string;
  /** Can the backend actually post here? See the module note. */
  deliverable: boolean;
  /** Hard limit the composer counts against. 0 = no meaningful limit. */
  limit: number;
  /** What `target_chat_id` means on this platform, shown as the field hint. */
  targetHint?: string;
}

export const POST_PLATFORMS: PostPlatform[] = [
  { id: 'telegram', label: 'Telegram', icon: Send, color: '#36cffb', deliverable: true, limit: 4096, targetHint: 'ID канала или @username, куда публиковать' },
  { id: 'vk', label: 'VK', icon: MessageCircle, color: '#3b82f6', deliverable: true, limit: 16000, targetHint: 'ID беседы или пользователя' },
  { id: 'discord', label: 'Discord', icon: MessageCircle, color: '#9277ff', deliverable: true, limit: 2000, targetHint: 'ID канала Discord' },
  { id: 'slack', label: 'Slack', icon: MessageCircle, color: '#35d49a', deliverable: true, limit: 4000, targetHint: 'ID или #название канала' },
  { id: 'instagram', label: 'Instagram', icon: Instagram, color: '#ff6ad5', deliverable: false, limit: 2200 },
  { id: 'facebook', label: 'Facebook', icon: Facebook, color: '#3b82f6', deliverable: false, limit: 63206 },
  { id: 'twitter', label: 'Twitter / X', icon: Twitter, color: '#c9cfd8', deliverable: false, limit: 280 },
  { id: 'linkedin', label: 'LinkedIn', icon: Linkedin, color: '#36cffb', deliverable: false, limit: 3000 },
  { id: 'youtube', label: 'YouTube', icon: Youtube, color: '#f05d75', deliverable: false, limit: 5000 },
  { id: 'whatsapp', label: 'WhatsApp', icon: MessageCircle, color: '#35d49a', deliverable: true, limit: 4096, targetHint: 'Номер получателя в формате E.164' },
  { id: 'email', label: 'Email-рассылка', icon: AtSign, color: '#f6b84a', deliverable: false, limit: 0 },
];

const FALLBACK_PLATFORM: PostPlatform = {
  id: 'other', label: 'Другое', icon: Globe, color: 'var(--text-dim)', deliverable: false, limit: 0,
};

/** Never throws on an unknown id: the register keeps history, and a platform
 *  dropped from the list above must not blank out the rows that used it. */
export const postPlatform = (id: string): PostPlatform =>
  POST_PLATFORMS.find(p => p.id === id) ?? { ...FALLBACK_PLATFORM, id, label: id || FALLBACK_PLATFORM.label };

export const POST_STATUS_LABELS: Record<PostStatus, string> = {
  draft: 'Черновик',
  scheduled: 'Запланировано',
  published: 'Опубликовано',
  failed: 'Ошибка',
};

/** Maps onto the .admin-status-chip modifiers that already exist in index.css,
 *  so statuses look the same here as everywhere else in the dashboard. */
export const POST_STATUS_CHIP: Record<PostStatus, string> = {
  draft: 'is-inactive',
  scheduled: 'is-pending',
  published: 'is-active',
  failed: 'is-revoked',
};

export const PER_PAGE_OPTIONS = [10, 25, 50];

/** 12 400 → "12.4K". Keeps the KPI card and the Охват column on one line at
 *  the widths the table actually gets. */
export function formatReach(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}K`;
  return String(value);
}

export function formatDelta(value: number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return `${value > 0 ? '+' : ''}${value.toFixed(1).replace(/\.0$/, '')}%`;
}

const PAD = (n: number) => String(n).padStart(2, '0');

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return `${PAD(date.getDate())}.${PAD(date.getMonth() + 1)}.${date.getFullYear()}`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return `${PAD(date.getHours())}:${PAD(date.getMinutes())}`;
}

/** The date a row is *about*, mirroring EFFECTIVE_AT in backend/smm_posts.py.
 *  Kept in sync deliberately: the backend sorts by it, so the column has to
 *  show the same value or the ordering looks random. */
export function effectiveDate(post: Publication): string | null {
  if (post.status === 'published') return post.published_at || post.updated_at;
  if (post.status === 'scheduled') return post.scheduled_at || post.created_at;
  return post.updated_at;
}

/** `<input type="datetime-local">` speaks local wall-clock with no zone; the
 *  backend speaks ISO-8601 UTC. These two are the only places that convert. */
export function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}-${PAD(date.getMonth() + 1)}-${PAD(date.getDate())}T${PAD(date.getHours())}:${PAD(date.getMinutes())}`;
}

export function localInputToIso(value: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toISOString();
}

export function excerpt(text: string, length = 70): string {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  return clean.length > length ? `${clean.slice(0, length - 1)}…` : clean;
}
