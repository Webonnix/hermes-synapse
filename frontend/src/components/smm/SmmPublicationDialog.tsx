/**
 * Read-only detail view for one publication.
 *
 * The table row is deliberately one line: it exists to be scanned. Everything
 * that does not fit there — the full text, the media, why a failed post
 * failed — lives here, so the row never has to choose between being scannable
 * and being complete.
 */

import { useEffect, useRef } from 'react';
import { AlertTriangle, Calendar, Image as ImageIcon, TrendingUp, X } from 'lucide-react';
import {
  POST_STATUS_CHIP, POST_STATUS_LABELS, effectiveDate, formatDate, formatDelta, formatReach,
  formatTime, postPlatform, type Publication,
} from './publications';

interface Props {
  post: Publication;
  onClose: () => void;
  onEdit: (post: Publication) => void;
}

export function SmmPublicationDialog({ post, onClose, onEdit }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const platform = postPlatform(post.platform);
  const Icon = platform.icon;
  const when = effectiveDate(post);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      role="presentation"
      onClick={event => { if (event.target === event.currentTarget) onClose(); }}
      style={{
        position: 'fixed', inset: 0, zIndex: 60, display: 'flex', alignItems: 'center',
        justifyContent: 'center', padding: 20, background: 'rgba(6, 7, 14, 0.72)',
        backdropFilter: 'blur(3px)',
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Публикация: ${post.title}`}
        className="glass-panel"
        style={{ width: 'min(560px, 100%)', maxHeight: '86vh', overflowY: 'auto', padding: 18 }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 14 }}>
          <div className="smm-account-icon" style={{ color: platform.color }}><Icon size={16} /></div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <strong style={{ fontSize: '0.98rem', display: 'block' }}>{post.title}</strong>
            <span className="smm-account-handle">
              {platform.label}{post.account ? ` · ${post.account}` : ''}
            </span>
          </div>
          <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <X size={15} />
          </button>
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 14 }}>
          <span className={`admin-status-chip ${POST_STATUS_CHIP[post.status]}`}>
            <i className="dot" />{POST_STATUS_LABELS[post.status]}
          </span>
          <span className="admin-status-chip is-inactive">
            <Calendar size={12} />{formatDate(when)}{formatTime(when) ? ` в ${formatTime(when)}` : ''}
          </span>
          {post.reach !== null && (
            <span className="admin-status-chip is-inactive">
              <TrendingUp size={12} />{formatReach(post.reach)}
              {post.reach_delta !== null ? ` · ${formatDelta(post.reach_delta)}` : ''}
            </span>
          )}
        </div>

        {post.error && (
          <p className="smm-notice is-error" style={{ marginBottom: 14 }}>
            <AlertTriangle size={14} /><span>{post.error}</span>
          </p>
        )}

        {post.media_url && (
          <img
            src={post.media_url}
            alt=""
            style={{ width: '100%', borderRadius: 10, marginBottom: 14, border: '1px solid rgba(255,255,255,0.08)' }}
          />
        )}

        <p style={{ fontSize: '0.85rem', lineHeight: 1.6, whiteSpace: 'pre-wrap', color: 'var(--text-muted)' }}>
          {post.body || 'Текст не заполнен.'}
        </p>

        {post.tags.length > 0 && (
          <div className="smm-tags" style={{ marginTop: 12 }}>
            {post.tags.map(tag => <span key={tag} className="smm-tag">#{tag}</span>)}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, marginTop: 18, flexWrap: 'wrap' }}>
          <button type="button" className="btn-primary" onClick={() => onEdit(post)}>
            <ImageIcon size={13} /><span>Открыть в дизайнере</span>
          </button>
          <button type="button" className="btn-ghost" onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  );
}

export default SmmPublicationDialog;
