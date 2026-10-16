import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

/**
 * Last line of defence around the whole dashboard.
 *
 * React 19 unmounts the entire tree when a render throws and nothing catches
 * it, so a single bad value — a 401 body stored where a list was expected, say —
 * empties #root and leaves the user staring at the body's CSS gradient with no
 * hint that anything failed. That happened in production on 2026-09-09 and cost
 * a diagnosis cycle, because "blank page" looks identical to a failed deploy, a
 * dead server, and an auth redirect.
 *
 * This does not make the app work; it makes the failure legible and offers the
 * two things that actually recover it — a reload, and dropping a stale session.
 */

interface Props { children: ReactNode; compact?: boolean }
interface State { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Dashboard crashed during render:', error, info.componentStack);
  }

  private reload = () => window.location.reload();

  private signOutAndReload = () => {
    try {
      localStorage.removeItem('vexa_auth_token');
    } catch { /* storage unavailable — the reload is still worth doing */ }
    window.location.reload();
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        minHeight: this.props.compact ? '40vh' : '100vh', padding: '2rem',
        fontFamily: "'Inter', system-ui, sans-serif", color: '#e3e6ec',
      }}>
        <div style={{
          maxWidth: '38rem', width: '100%', padding: '1.75rem',
          border: '1px solid rgba(255,255,255,.12)', borderRadius: '14px',
          background: 'rgba(11,13,18,.72)', backdropFilter: 'blur(8px)',
        }}>
          <h1 style={{ margin: '0 0 .5rem', fontSize: '1.15rem', fontWeight: 600 }}>
            Интерфейс не смог отрисоваться
          </h1>
          <p style={{ margin: '0 0 1rem', color: '#99a1b0', fontSize: '.88rem', lineHeight: 1.6 }}>
            Не удалось открыть этот экран. Попробуйте перезагрузить страницу. Если проблема повторяется, выйдите из сессии и войдите снова.
          </p>
          <details><summary>Технические подробности</summary>
          <pre style={{
            margin: '0 0 1.25rem', padding: '.75rem', maxHeight: '11rem', overflow: 'auto',
            border: '1px solid rgba(255,255,255,.08)', borderRadius: '8px',
            background: 'rgba(0,0,0,.45)', color: '#f05d75',
            font: "12px ui-monospace, SFMono-Regular, Menlo, monospace", whiteSpace: 'pre-wrap',
          }}>{error.message}</pre></details>
          <div style={{ display: 'flex', gap: '.5rem', flexWrap: 'wrap' }}>
            <button onClick={this.signOutAndReload} style={{
              padding: '.5rem 1rem', border: 0, borderRadius: '999px',
              background: '#e3e6ec', color: '#0e1117', font: '600 .82rem inherit', cursor: 'pointer',
            }}>Выйти и перезагрузить</button>
            <button onClick={this.reload} style={{
              padding: '.5rem 1rem', borderRadius: '999px',
              border: '1px solid rgba(255,255,255,.22)', background: 'transparent',
              color: '#e3e6ec', font: '600 .82rem inherit', cursor: 'pointer',
            }}>Просто перезагрузить</button>
          </div>
        </div>
      </div>
    );
  }
}
