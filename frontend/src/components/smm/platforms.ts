/**
 * Shared vocabulary for the SMM Factory panels.
 *
 * Every panel here (connections, dialogs, drafts, activity log) shows the same
 * handful of enums the backend stores as bare strings — platform, status,
 * response mode, access mode, per-conversation handling mode. They used to live
 * inside MessengerChannelsTab, which meant a second panel could only get at
 * them by importing a 1400-line component. They are data, so they live here.
 *
 * The social credential descriptions are the *labels* only: which fields a
 * platform actually requires comes from the backend (GET /api/social-platforms),
 * so a field added there shows up in the form without a frontend change, and a
 * field removed there stops being asked for.
 */

export type Platform =
  | 'telegram' | 'matrix' | 'discord' | 'slack' | 'email'
  | 'vk' | 'instagram' | 'facebook' | 'whatsapp';

export type ResponseMode = 'draft' | 'auto_labeled';
export type AccessMode = 'owner_only' | 'token';

export const PLATFORM_LABELS: Record<Platform, string> = {
  telegram: 'Telegram',
  matrix: 'Element / Matrix',
  discord: 'Discord',
  slack: 'Slack',
  email: 'Email',
  vk: 'VKontakte',
  instagram: 'Instagram',
  facebook: 'Facebook',
  whatsapp: 'WhatsApp Business',
};

/** Which platforms belong to the social half of the section. Kept in sync with
 *  SOCIAL_PLATFORMS in backend/agent_messenger_governance.py — the backend is
 *  the authority, this is only used to pick a form and an icon. */
export const SOCIAL_PLATFORMS: Platform[] = ['vk', 'instagram', 'facebook', 'whatsapp'];

export const isSocialPlatform = (platform: string): boolean =>
  SOCIAL_PLATFORMS.includes(platform as Platform);

export const STATUS_LABELS: Record<string, string> = {
  awaiting_approval: 'Ожидает подтверждения',
  approved: 'Подтверждено',
  active: 'Активен',
  failed: 'Ошибка',
  revoked: 'Отключён',
};

export const RESPONSE_MODE_LABELS: Record<ResponseMode, string> = {
  draft: 'Черновик на подтверждение',
  auto_labeled: 'Автоответ с пометкой «ассистент»',
};

export const ACCESS_MODE_LABELS: Record<AccessMode, string> = {
  owner_only: 'Только для вас',
  token: 'По токенам доступа',
};

export const HANDLING_MODE_LABELS: Record<string, string> = {
  undecided: 'Ждёт первого сообщения',
  pending_choice: 'Ждёт выбора собеседника',
  ai: 'Отвечает ИИ',
  waiting: 'Ждёт Альберта (ИИ молчит)',
  notified: 'Срочно — вы уведомлены',
  owner_takeover: 'Ведёт Альберт лично',
};

/** Label + hint for one credential field of a social platform. */
export const CREDENTIAL_FIELDS: Record<string, { label: string; hint?: string; secret?: boolean; placeholder?: string }> = {
  access_token: {
    label: 'Ключ доступа',
    hint: 'VK — ключ доступа сообщества с правами на сообщения. Meta — Page access token или токен системного пользователя.',
    secret: true,
  },
  group_id: {
    label: 'ID сообщества VK',
    hint: 'Цифры из адреса vk.com/club<ID> или screen name сообщества.',
    placeholder: '123456789',
  },
  ig_user_id: {
    label: 'Instagram Business account ID',
    hint: 'Профессиональный аккаунт, связанный со страницей Facebook.',
    placeholder: '17841400000000000',
  },
  page_id: {
    label: 'Facebook Page ID',
    placeholder: '100000000000000',
  },
  phone_number_id: {
    label: 'Phone number ID (WhatsApp Cloud API)',
    hint: 'Не сам номер, а его ID из панели WhatsApp → API Setup.',
    placeholder: '100000000000000',
  },
  app_secret: {
    label: 'App Secret приложения Meta',
    hint: 'Им подписывается каждая доставка вебхука. Без него входящие сообщения не принимаются — подделать их смог бы кто угодно.',
    secret: true,
  },
  verify_token: {
    label: 'Verify token (необязательно)',
    hint: 'Оставьте пустым — сгенерируется автоматически и будет показан после подключения.',
    secret: true,
  },
};

export function authHeaders(): Record<string, string> {

  return { 'Content-Type': 'application/json',  };
}
