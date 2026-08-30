/**
 * ДИЗАЙНЕР — where a publication is written, scheduled and previewed.
 *
 * One editor for both halves of the register, because they are the same
 * object at different stages: a draft, a scheduled post and a published one
 * differ by two fields, not by form.
 *
 * The preview is not a pixel-accurate mock of each network. It answers the one
 * question the form cannot — where this platform cuts the text off, and what
 * the first line reads like — which is why the overflow is highlighted rather
 * than merely counted.
 *
 * Delivery is the part worth being careful about:
 *
 * - `manual` posts carry no credentials. The register tracks them; the backend
 *   never claims to have sent one.
 * - `auto` posts go out through the same per-platform sender the draft queue
 *   uses (backend/channel_replies.send_to_channel), so they are only offered
 *   for platforms that sender actually supports and only once a live channel
 *   and a destination are chosen. A platform we cannot post to says so here
 *   instead of failing later.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, ArrowLeft, Calendar, Info, Save, Send, Trash2,
} from 'lucide-react';
import { styles } from '../../styles';
import type { MessengerBinding } from '../../types';
import { authHeaders } from './platforms';
import {
  POST_PLATFORMS, POST_STATUS_LABELS, isoToLocalInput, localInputToIso, postPlatform,
  type DeliveryMode, type PostStatus, type Publication,
} from './publications';

interface Props {
  /** The post being edited. `null`, or one with an empty id, starts a new one. */
  post: Publication | null;
  onSaved: (post: Publication) => void;
  onCancel: () => void;
}

interface Draft {
  title: string;
  body: string;
  platform: string;
  account: string;
  binding_id: string;
  target_chat_id: string;
  delivery_mode: DeliveryMode;
  status: PostStatus;
  tags: string;
  media_url: string;
  scheduled_local: string;
  reach: string;
}

const blankDraft = (): Draft => ({
  title: '', body: '', platform: 'telegram', account: '', binding_id: '', target_chat_id: '',
  delivery_mode: 'manual', status: 'draft', tags: '', media_url: '', scheduled_local: '', reach: '',
});

const toDraft = (post: Publication | null): Draft => (post ? {
  title: post.title,
  body: post.body,
  platform: post.platform,
  account: post.account,
  binding_id: post.binding_id,
  target_chat_id: post.target_chat_id,
  delivery_mode: post.delivery_mode,
  status: post.status,
  tags: post.tags.join(', '),
  media_url: post.media_url,
  scheduled_local: isoToLocalInput(post.scheduled_at),
  reach: post.reach === null ? '' : String(post.reach),
} : blankDraft());

const parseTags = (raw: string): string[] =>
  raw.split(/[,\s]+/).map(tag => tag.trim().replace(/^#/, '')).filter(Boolean).slice(0, 12);

export function SmmDesignerPanel({ post, onSaved, onCancel }: Props) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(post));
  const [bindings, setBindings] = useState<MessengerBinding[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const editingId = post?.id || '';
  // The manual "published" flag is blocked for auto delivery — but only as a
  // transition. A post the backend really did send stays editable, so its text
  // and measured reach can be corrected without the form fighting back.
  const alreadyPublished = post?.status === 'published' && Boolean(post?.id);

  useEffect(() => { setDraft(toDraft(post)); setError(''); }, [post]);

  useEffect(() => {
    fetch('/api/messenger-bindings', { headers: authHeaders() })
      .then(response => response.json())
      .then(data => setBindings(Array.isArray(data) ? data.filter((b: MessengerBinding) => b.status === 'active') : []))
      .catch(() => setBindings([]));
  }, []);

  const platform = postPlatform(draft.platform);
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft(current => ({ ...current, [key]: value }));

  // Only channels on the chosen platform can carry this post, and only if the
  // shared sender supports that platform at all.
  const channels = useMemo(
    () => (platform.deliverable ? bindings.filter(binding => binding.platform === draft.platform) : []),
    [bindings, draft.platform, platform.deliverable],
  );

  const composed = [draft.title.trim(), draft.body.trim()].filter(Boolean).join('\n\n');
  const tags = parseTags(draft.tags);
  const previewText = [composed, tags.map(tag => `#${tag}`).join(' ')].filter(Boolean).join('\n\n');
  const limit = platform.limit;
  const over = limit > 0 && previewText.length > limit;

  const autoAvailable = platform.deliverable && channels.length > 0;
  const canPublishNow = draft.delivery_mode === 'manual'
    || (autoAvailable && Boolean(draft.binding_id && draft.target_chat_id.trim()));

  // Switching to a platform the backend cannot post to must not leave the form
  // in an auto mode that can never succeed.
  useEffect(() => {
    if (!platform.deliverable && draft.delivery_mode === 'auto') {
      setDraft(current => ({ ...current, delivery_mode: 'manual', binding_id: '', target_chat_id: '' }));
    }
  }, [platform.deliverable, draft.delivery_mode]);

  const validate = (): string => {
    if (!draft.title.trim()) return 'Укажите название публикации';
    if (draft.status === 'scheduled' && !draft.scheduled_local) return 'Для запланированной публикации нужны дата и время';
    if (draft.delivery_mode === 'auto' && !draft.binding_id) return 'Выберите подключённый канал для автопубликации';
    if (draft.delivery_mode === 'auto' && !draft.target_chat_id.trim()) return 'Укажите, куда публиковать';
    if (draft.status === 'published' && draft.delivery_mode === 'auto' && !alreadyPublished) {
      return 'Автопубликацию нельзя отметить опубликованной вручную — сохраните и нажмите «Опубликовать сейчас»';
    }
    return '';
  };

  const payload = () => ({
    title: draft.title.trim(),
    platform: draft.platform,
    body: draft.body,
    account: draft.account.trim(),
    binding_id: draft.delivery_mode === 'auto' ? draft.binding_id : '',
    target_chat_id: draft.delivery_mode === 'auto' ? draft.target_chat_id.trim() : '',
    delivery_mode: draft.delivery_mode,
    status: draft.status,
    tags,
    media_url: draft.media_url.trim(),
    scheduled_at: draft.status === 'scheduled' ? localInputToIso(draft.scheduled_local) : '',
    reach: draft.reach.trim() === '' ? null : Number(draft.reach),
  });

  const save = async (thenPublish: boolean) => {
    const problem = validate();
    if (problem) { setError(problem); return; }
    setSaving(true);
    try {
      const body = payload();
      const response = await fetch(
        editingId ? `/api/smm/publications/${editingId}` : '/api/smm/publications',
        { method: editingId ? 'PUT' : 'POST', headers: authHeaders(), body: JSON.stringify(body) },
      );
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.detail || 'Не удалось сохранить публикацию');

      if (!thenPublish) { onSaved(saved as Publication); return; }

      const published = await fetch(`/api/smm/publications/${saved.id}/publish`, {
        method: 'POST', headers: authHeaders(),
      });
      const result = await published.json();
      // The post is saved either way — a failed send must not look like a lost
      // draft, so the error is reported against a row that already exists.
      if (!published.ok) throw new Error(result.detail || 'Публикация сохранена, но отправить её не удалось');
      onSaved(result as Publication);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось сохранить публикацию');
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!editingId) { onCancel(); return; }
    if (!window.confirm(`Удалить публикацию «${draft.title}»? Это действие необратимо.`)) return;
    setSaving(true);
    try {
      const response = await fetch(`/api/smm/publications/${editingId}`, { method: 'DELETE', headers: authHeaders() });
      if (!response.ok) throw new Error('Не удалось удалить публикацию');
      onCancel();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось удалить публикацию');
    } finally {
      setSaving(false);
    }
  };

  const PlatformIcon = platform.icon;

  return (
    <div className="smm-designer">
      <div className="glass-panel smm-designer-form">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button type="button" className="icon-btn" onClick={onCancel} aria-label="Вернуться к публикациям">
            <ArrowLeft size={15} />
          </button>
          <strong style={{ fontSize: '0.95rem' }}>
            {editingId ? 'Редактирование публикации' : 'Новая публикация'}
          </strong>
        </div>

        <fieldset className="smm-field" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: 5 }}>
            Платформа
          </legend>
          <div className="smm-platform-picker">
            {POST_PLATFORMS.map(item => {
              const Icon = item.icon;
              return (
                <button
                  key={item.id} type="button" className="smm-platform-chip"
                  aria-pressed={draft.platform === item.id}
                  onClick={() => set('platform', item.id)}
                >
                  <Icon size={14} style={{ color: item.color }} />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </div>
        </fieldset>

        <div className="smm-field">
          <label htmlFor="smm-title">Название</label>
          <input id="smm-title" className="form-input" value={draft.title}
            placeholder="Обновление платформы VEXA" onChange={event => set('title', event.target.value)} />
        </div>

        <div className="smm-field">
          <label htmlFor="smm-body">Текст публикации</label>
          <textarea
            id="smm-body" className="form-input" rows={7} value={draft.body}
            placeholder="Что вы хотите рассказать?" onChange={event => set('body', event.target.value)}
            style={{ resize: 'vertical', fontFamily: 'inherit' }}
          />
          {limit > 0 && (
            <span className={`smm-counter${over ? ' is-over' : ''}`}>
              {previewText.length} / {limit}
              {over ? ` · превышение на ${previewText.length - limit}` : ''}
            </span>
          )}
        </div>

        <div className="smm-field-row">
          <div className="smm-field">
            <label htmlFor="smm-tags">Хештеги</label>
            <input id="smm-tags" className="form-input" value={draft.tags}
              placeholder="обновление, безопасность" onChange={event => set('tags', event.target.value)} />
            <span style={styles.formHelp}>Через запятую, без решётки.</span>
          </div>
          <div className="smm-field">
            <label htmlFor="smm-media">Обложка (URL)</label>
            <input id="smm-media" className="form-input" type="url" value={draft.media_url}
              placeholder="https://…/cover.jpg" onChange={event => set('media_url', event.target.value)} />
          </div>
        </div>

        <div className="smm-field-row">
          <div className="smm-field">
            <label htmlFor="smm-delivery">Способ публикации</label>
            <select id="smm-delivery" className="form-input" value={draft.delivery_mode}
              onChange={event => set('delivery_mode', event.target.value as DeliveryMode)}>
              <option value="manual">Вручную — я публикую сам</option>
              <option value="auto" disabled={!autoAvailable}>Автоматически — через подключённый канал</option>
            </select>
          </div>
          <div className="smm-field">
            <label htmlFor="smm-status-select">Состояние</label>
            <select id="smm-status-select" className="form-input" value={draft.status}
              onChange={event => set('status', event.target.value as PostStatus)}>
              <option value="draft">{POST_STATUS_LABELS.draft}</option>
              <option value="scheduled">{POST_STATUS_LABELS.scheduled}</option>
              <option value="published" disabled={draft.delivery_mode === 'auto' && !alreadyPublished}>
                {POST_STATUS_LABELS.published}
              </option>
            </select>
          </div>
        </div>

        {!platform.deliverable && (
          <p className="smm-notice">
            <Info size={14} />
            <span>
              {platform.label} не публикуется бэкендом — здесь эта публикация только учитывается.
              Опубликуйте её в сети сами и отметьте состояние «Опубликовано».
            </span>
          </p>
        )}

        {draft.delivery_mode === 'auto' ? (
          <div className="smm-field-row">
            <div className="smm-field">
              <label htmlFor="smm-binding">Канал</label>
              <select id="smm-binding" className="form-input" value={draft.binding_id}
                onChange={event => {
                  const binding = channels.find(item => item.id === event.target.value);
                  setDraft(current => ({
                    ...current,
                    binding_id: event.target.value,
                    account: binding?.bot_username || current.account,
                  }));
                }}>
                <option value="">Выберите канал…</option>
                {channels.map(binding => (
                  <option key={binding.id} value={binding.id}>{binding.bot_username} · {binding.agent_name}</option>
                ))}
              </select>
            </div>
            <div className="smm-field">
              <label htmlFor="smm-target">Куда публиковать</label>
              <input id="smm-target" className="form-input" value={draft.target_chat_id}
                placeholder="@vexa_official" onChange={event => set('target_chat_id', event.target.value)} />
              {platform.targetHint && <span style={styles.formHelp}>{platform.targetHint}</span>}
            </div>
          </div>
        ) : (
          <div className="smm-field">
            <label htmlFor="smm-account">Аккаунт</label>
            <input id="smm-account" className="form-input" value={draft.account}
              placeholder="@vexa_official" onChange={event => set('account', event.target.value)} />
            <span style={styles.formHelp}>Как этот аккаунт называется в списке публикаций.</span>
          </div>
        )}

        <div className="smm-field-row">
          {draft.status === 'scheduled' && (
            <div className="smm-field">
              <label htmlFor="smm-schedule">Дата и время</label>
              <input id="smm-schedule" className="form-input" type="datetime-local" value={draft.scheduled_local}
                onChange={event => set('scheduled_local', event.target.value)} />
              <span style={styles.formHelp}>
                {draft.delivery_mode === 'auto'
                  ? 'Бэкенд отправит публикацию сам, когда наступит это время.'
                  : 'Напоминание в списке — отправить публикацию нужно вручную.'}
              </span>
            </div>
          )}
          {draft.status === 'published' && (
            <div className="smm-field">
              <label htmlFor="smm-reach">Охват</label>
              <input id="smm-reach" className="form-input" type="number" min={0} value={draft.reach}
                placeholder="Например, 12400" onChange={event => set('reach', event.target.value)} />
              <span style={styles.formHelp}>Оставьте пустым, пока цифры нет — в списке будет «—», а не ноль.</span>
            </div>
          )}
        </div>

        {error && (
          <p className="smm-notice is-error">
            <AlertTriangle size={14} /><span>{error}</span>
          </p>
        )}

        <div className="smm-designer-actions">
          <button type="button" className="btn-primary" disabled={saving} onClick={() => void save(false)}>
            <Save size={13} /><span>{saving ? 'Сохранение…' : 'Сохранить'}</span>
          </button>
          <button
            type="button" className="btn-ghost" disabled={saving || !canPublishNow}
            title={canPublishNow ? undefined : 'Выберите канал и адрес назначения'}
            onClick={() => void save(true)}
          >
            <Send size={13} />
            <span>{draft.delivery_mode === 'auto' ? 'Опубликовать сейчас' : 'Сохранить как опубликованную'}</span>
          </button>
          <div style={{ flex: 1 }} />
          {editingId && (
            <button type="button" className="btn-ghost danger" disabled={saving} onClick={() => void remove()}>
              <Trash2 size={13} /><span>Удалить</span>
            </button>
          )}
        </div>
      </div>

      <aside className="glass-panel smm-preview" aria-label="Предпросмотр публикации">
        <strong style={{ fontSize: '0.85rem' }}>Предпросмотр</strong>
        <div className="smm-preview-card">
          <div className="smm-preview-head">
            <span className="smm-account-icon" style={{ color: platform.color }}><PlatformIcon size={15} /></span>
            <span style={{ minWidth: 0 }}>
              <span className="smm-account-name">{draft.account || platform.label}</span>
              <span className="smm-account-handle">
                {draft.status === 'scheduled' && draft.scheduled_local
                  ? `Выйдет ${draft.scheduled_local.replace('T', ' в ')}`
                  : POST_STATUS_LABELS[draft.status]}
              </span>
            </span>
          </div>
          {draft.media_url && <img className="smm-preview-media" src={draft.media_url} alt="" />}
          <div className="smm-preview-body">
            {previewText
              ? (over
                ? <>{previewText.slice(0, limit)}<span className="smm-preview-overflow">{previewText.slice(limit)}</span></>
                : previewText)
              : <span className="smm-muted">Текст появится здесь по мере ввода.</span>}
          </div>
        </div>
        <p style={{ ...styles.formHelp, display: 'flex', gap: 7, alignItems: 'flex-start' }}>
          <Calendar size={13} style={{ marginTop: 2, flexShrink: 0 }} />
          <span>
            Приблизительный вид: показывает первую строку и место обрезки в {platform.label}, а не точную вёрстку сети.
          </span>
        </p>
      </aside>
    </div>
  );
}

export default SmmDesignerPanel;
