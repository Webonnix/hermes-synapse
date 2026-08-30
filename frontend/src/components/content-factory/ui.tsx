/**
 * Shared presentation pieces for the Content Factory control centre.
 *
 * The reference design is a dark mission-control layout: panels with a quiet
 * header strip, cyan for "this is happening now", violet for "a human is
 * needed", green for done, amber for retrying, red for failed. Those meanings
 * come from §70 and are the same everywhere, so they live here once instead of
 * being re-picked per screen.
 *
 * Style objects rather than CSS classes: the app has no CSS module or utility
 * layer, and inline objects keep a panel's look next to its markup, which is
 * how the rest of this codebase is written.
 */

import type { CSSProperties, ReactNode } from 'react';

export const COLORS = {
  cyan: '#22d3ee',
  cyanSoft: '#67e8f9',
  violet: '#a78bfa',
  green: '#22c55e',
  greenSoft: '#4ade80',
  amber: '#f59e0b',
  amberSoft: '#fbbf24',
  red: '#ef4444',
  redSoft: '#f87171',
  text: '#e2e8f0',
  muted: '#94a3b8',
  faint: '#64748b',
  line: 'rgba(148,163,184,0.16)',
  lineStrong: 'rgba(148,163,184,0.28)',
  panel: 'rgba(13,20,38,0.72)',
  panelSoft: 'rgba(15,23,42,0.5)',
  inset: 'rgba(8,13,26,0.6)',
} as const;

export const panelStyle: CSSProperties = {
  background: COLORS.panel,
  border: `1px solid ${COLORS.line}`,
  borderRadius: 14,
  overflow: 'hidden',
};

export function Panel({ title, action, children, style, bodyStyle }: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  style?: CSSProperties;
  bodyStyle?: CSSProperties;
}) {
  return (
    <section style={{ ...panelStyle, ...style }}>
      {title !== undefined && (
        <header style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px',
          borderBottom: `1px solid ${COLORS.line}`, background: 'rgba(8,13,26,0.45)',
        }}>
          <h3 style={{
            margin: 0, fontSize: 11, fontWeight: 700, letterSpacing: 1.1,
            textTransform: 'uppercase', color: COLORS.muted,
          }}>{title}</h3>
          <span style={{ flex: 1 }} />
          {action}
        </header>
      )}
      <div style={{ padding: 14, ...bodyStyle }}>{children}</div>
    </section>
  );
}

export function Chip({ tone = 'muted', children, title }: {
  tone?: 'cyan' | 'violet' | 'green' | 'amber' | 'red' | 'muted';
  children: ReactNode;
  title?: string;
}) {
  const color = {
    cyan: COLORS.cyanSoft, violet: COLORS.violet, green: COLORS.greenSoft,
    amber: COLORS.amberSoft, red: COLORS.redSoft, muted: COLORS.muted,
  }[tone];
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 5, padding: '2px 8px',
        borderRadius: 5, fontSize: 10, fontWeight: 700, letterSpacing: 0.6,
        textTransform: 'uppercase', color,
        border: `1px solid ${color}55`, background: `${color}14`,
      }}
    >{children}</span>
  );
}

export function StatTile({ label, value, delta, tone, icon }: {
  label: string;
  value: ReactNode;
  delta?: number | null;
  tone?: string;
  icon?: ReactNode;
}) {
  return (
    <div style={{
      flex: '1 1 128px', minWidth: 118, padding: '12px 14px', borderRadius: 12,
      background: COLORS.panelSoft, border: `1px solid ${COLORS.line}`,
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, color: COLORS.faint, fontSize: 10,
        letterSpacing: 0.7, textTransform: 'uppercase',
      }}>
        {icon}<span>{label}</span>
      </div>
      <div style={{ marginTop: 7, fontSize: 22, fontWeight: 700, color: tone ?? COLORS.text }}>
        {value}
      </div>
      {delta != null && (
        <div style={{ marginTop: 2, fontSize: 10, color: delta >= 0 ? COLORS.greenSoft : COLORS.redSoft }}>
          {delta >= 0 ? '+' : ''}{delta}%
        </div>
      )}
    </div>
  );
}

export function ProgressBar({ percent, tone = COLORS.cyan, height = 5 }: {
  percent: number; tone?: string; height?: number;
}) {
  const clamped = Math.max(0, Math.min(100, percent));
  return (
    <div style={{
      height, borderRadius: height / 2, background: 'rgba(148,163,184,0.16)', overflow: 'hidden',
    }}>
      <div style={{
        width: `${clamped}%`, height: '100%', borderRadius: height / 2, background: tone,
        transition: 'width 0.4s ease',
      }} />
    </div>
  );
}

export function EmptyState({ title, hint, action }: {
  title: string; hint?: string; action?: ReactNode;
}) {
  return (
    <div style={{ padding: '22px 10px', textAlign: 'center' }}>
      <div style={{ color: COLORS.muted, fontSize: 13 }}>{title}</div>
      {hint && (
        <p style={{ margin: '6px auto 0', maxWidth: 420, color: COLORS.faint, fontSize: 11.5, lineHeight: 1.55 }}>
          {hint}
        </p>
      )}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  );
}

export function Field({ label, children, hint }: {
  label: string; children: ReactNode; hint?: string;
}) {
  return (
    <label style={{ display: 'block', marginBottom: 12 }}>
      <span style={{
        display: 'block', marginBottom: 5, fontSize: 11, color: COLORS.muted, letterSpacing: 0.3,
      }}>{label}</span>
      {children}
      {hint && (
        <span style={{ display: 'block', marginTop: 4, fontSize: 10.5, color: COLORS.faint, lineHeight: 1.45 }}>
          {hint}
        </span>
      )}
    </label>
  );
}

export const inputStyle: CSSProperties = {
  width: '100%', padding: '8px 10px', borderRadius: 8, fontSize: 12,
  color: COLORS.text, background: COLORS.inset, border: `1px solid ${COLORS.lineStrong}`,
  outline: 'none',
};

export function money(value: number | null | undefined, digits = 2) {
  return `$${Number(value ?? 0).toFixed(digits)}`;
}

export function bytes(value: number | null | undefined) {
  const size = Number(value ?? 0);
  if (size < 1024) return `${size} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let index = -1;
  let current = size;
  do {
    current /= 1024;
    index += 1;
  } while (current >= 1024 && index < units.length - 1);
  return `${current.toFixed(current >= 10 ? 0 : 1)} ${units[index]}`;
}

export function shortTime(value?: string | null) {
  if (!value) return '—';
  const ts = Date.parse(value);
  if (Number.isNaN(ts)) return value;
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(ts);
}

export function dateTime(value?: string | null) {
  if (!value) return '—';
  const ts = Date.parse(value);
  if (Number.isNaN(ts)) return value;
  return new Intl.DateTimeFormat(undefined, {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  }).format(ts);
}

export function duration(ms?: number | null) {
  const value = Number(ms ?? 0);
  if (!value) return '—';
  if (value < 1000) return `${value} ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
}
