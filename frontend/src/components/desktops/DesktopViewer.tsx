import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import type RFB from '@novnc/novnc';

type Status = 'connecting' | 'connected' | 'disconnected';

interface Props {
  desktopKey: string;
  viewOnly: boolean;
  /** Bumped to force a fresh connection (the reconnect button). */
  attempt: number;
  /** Which of the desktop's screens (an OpenBot desktop has two). */
  screen?: number;
  copy: { connecting: string; disconnected: string };
  /** Text the desktop put on its clipboard. Kept by the caller, never written
   * to the owner's clipboard without a click: the agent controls that text. */
  onDesktopClipboard?: (text: string) => void;
}

export interface DesktopViewerHandle {
  /** Puts text on the desktop's clipboard; false when the screen is not connected. */
  sendClipboard(text: string): boolean;
}

/**
 * Live screen of one agent desktop: noVNC over the backend's authenticated
 * relay (/api/agent-desktops/<key>/vnc). The browser sends its HttpOnly session cookie; no session secret is
 * exposed to JavaScript or WebSocket subprotocols.
 *
 * noVNC is imported on first use: it is only needed while someone watches a
 * desktop, and it should not weigh on every other tab's bundle.
 */
export const DesktopViewer = forwardRef<DesktopViewerHandle, Props>(function DesktopViewer(
  { desktopKey, viewOnly, attempt, screen = 0, copy, onDesktopClipboard }, ref,
) {
  const screenRef = useRef<HTMLDivElement | null>(null);
  const rfbRef = useRef<RFB | null>(null);
  const [status, setStatus] = useState<Status>('connecting');
  const onDesktopClipboardRef = useRef(onDesktopClipboard);
  useEffect(() => { onDesktopClipboardRef.current = onDesktopClipboard; }, [onDesktopClipboard]);

  useImperativeHandle(ref, () => ({
    sendClipboard(text: string) {
      const rfb = rfbRef.current;
      if (!rfb || status !== 'connected') return false;
      // noVNC drops clipboard text while view-only. Setting the clipboard is
      // not input — nothing is clicked or typed — so lift it for this one call.
      const wasViewOnly = rfb.viewOnly;
      rfb.viewOnly = false;
      rfb.clipboardPasteFrom(text);
      rfb.viewOnly = wasViewOnly;
      return true;
    },
  }), [status]);

  useEffect(() => {
    let disposed = false;
    setStatus('connecting');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/api/agent-desktops/${encodeURIComponent(desktopKey)}/vnc?screen=${screen}`;

    void import('@novnc/novnc').then(({ default: RFBClient }) => {
      if (disposed || !screenRef.current) return;
      const rfb = new RFBClient(screenRef.current, url, { wsProtocols: ['hermes-vnc'], shared: true });
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.background = 'transparent';
      rfb.viewOnly = viewOnly;
      rfb.addEventListener('connect', () => { if (!disposed) setStatus('connected'); });
      rfb.addEventListener('disconnect', () => { if (!disposed) setStatus('disconnected'); });
      rfb.addEventListener('clipboard', (event) => {
        const text = (event as CustomEvent<{ text?: string }>).detail?.text;
        if (!disposed && text) onDesktopClipboardRef.current?.(text);
      });
      rfbRef.current = rfb;
    }).catch(() => { if (!disposed) setStatus('disconnected'); });

    return () => {
      disposed = true;
      try { rfbRef.current?.disconnect(); } catch { /* already closed */ }
      rfbRef.current = null;
    };
    // viewOnly is applied live below; reconnecting for it would drop the view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desktopKey, attempt, screen]);

  useEffect(() => {
    if (rfbRef.current) {
      rfbRef.current.viewOnly = viewOnly;
      if (!viewOnly) rfbRef.current.focus();
    }
  }, [viewOnly]);

  return (
    <div className="ad-screen-wrap">
      <div ref={screenRef} className={`ad-screen ${viewOnly ? 'is-view-only' : 'is-interactive'}`} data-testid="desktop-screen" />
      {status !== 'connected' && (
        <div className="ad-screen-overlay">{status === 'connecting' ? copy.connecting : copy.disconnected}</div>
      )}
    </div>
  );
});
