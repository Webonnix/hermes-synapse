import { Maximize2, Minimize2, Minus, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

interface Geometry {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface FloatingWindowLabels {
  minimize: string;
  restore: string;
  fullscreen: string;
  exitFullscreen: string;
  close: string;
}

interface FloatingWindowProps {
  title: string;
  subtitle?: string;
  storageKey: string;
  onClose: () => void;
  children: React.ReactNode;
  labels: FloatingWindowLabels;
}

type WindowMode = 'normal' | 'minimized' | 'fullscreen';

const MIN_WIDTH = 340;
const MIN_HEIGHT = 380;
const MARGIN = 16;

function defaultGeometry(): Geometry {
  const width = Math.min(440, window.innerWidth - MARGIN * 2);
  const height = Math.min(640, window.innerHeight - MARGIN * 2);
  return { width, height, x: window.innerWidth - width - 26, y: window.innerHeight - height - 26 };
}

function clampGeometry(geometry: Geometry): Geometry {
  const width = Math.max(MIN_WIDTH, Math.min(geometry.width, window.innerWidth - MARGIN * 2));
  const height = Math.max(MIN_HEIGHT, Math.min(geometry.height, window.innerHeight - MARGIN * 2));
  const x = Math.max(MARGIN, Math.min(geometry.x, window.innerWidth - width - MARGIN));
  const y = Math.max(MARGIN, Math.min(geometry.y, window.innerHeight - height - MARGIN));
  return { x, y, width, height };
}

function loadGeometry(storageKey: string): Geometry {
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw) return clampGeometry(JSON.parse(raw));
  } catch {
    // Ignore malformed saved geometry and fall back to the default placement.
  }
  return defaultGeometry();
}

/** A window-chrome wrapper (drag, resize, minimize, fullscreen) around arbitrary panel content. */
export function FloatingWindow({ title, subtitle, storageKey, onClose, children, labels }: FloatingWindowProps) {
  const [geometry, setGeometry] = useState<Geometry>(() => loadGeometry(storageKey));
  const [mode, setMode] = useState<WindowMode>('normal');
  const dragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);
  const resizeRef = useRef<{ startX: number; startY: number; originW: number; originH: number } | null>(null);

  useEffect(() => {
    if (mode !== 'normal') return;
    localStorage.setItem(storageKey, JSON.stringify(geometry));
  }, [geometry, mode, storageKey]);

  useEffect(() => {
    const onViewportResize = () => setGeometry(current => clampGeometry(current));
    window.addEventListener('resize', onViewportResize);
    return () => window.removeEventListener('resize', onViewportResize);
  }, []);

  const startDrag = useCallback((event: React.PointerEvent<HTMLElement>) => {
    if (mode !== 'normal' || (event.target as HTMLElement).closest('button')) return;
    dragRef.current = { startX: event.clientX, startY: event.clientY, originX: geometry.x, originY: geometry.y };
    document.body.style.userSelect = 'none';
    event.currentTarget.setPointerCapture(event.pointerId);
  }, [geometry.x, geometry.y, mode]);

  const onDragMove = useCallback((event: React.PointerEvent<HTMLElement>) => {
    if (!dragRef.current) return;
    const dx = event.clientX - dragRef.current.startX;
    const dy = event.clientY - dragRef.current.startY;
    const origin = dragRef.current;
    setGeometry(current => clampGeometry({ ...current, x: origin.originX + dx, y: origin.originY + dy }));
  }, []);

  const endDrag = useCallback((event: React.PointerEvent<HTMLElement>) => {
    dragRef.current = null;
    document.body.style.userSelect = '';
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }, []);

  const startResize = useCallback((event: React.PointerEvent<HTMLElement>) => {
    event.stopPropagation();
    resizeRef.current = { startX: event.clientX, startY: event.clientY, originW: geometry.width, originH: geometry.height };
    document.body.style.userSelect = 'none';
    event.currentTarget.setPointerCapture(event.pointerId);
  }, [geometry.width, geometry.height]);

  const onResizeMove = useCallback((event: React.PointerEvent<HTMLElement>) => {
    if (!resizeRef.current) return;
    const dx = event.clientX - resizeRef.current.startX;
    const dy = event.clientY - resizeRef.current.startY;
    const origin = resizeRef.current;
    setGeometry(current => clampGeometry({ ...current, width: origin.originW + dx, height: origin.originH + dy }));
  }, []);

  const endResize = useCallback((event: React.PointerEvent<HTMLElement>) => {
    resizeRef.current = null;
    document.body.style.userSelect = '';
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }, []);

  if (mode === 'minimized') {
    return (
      <div className="floating-window-dock">
        <button type="button" className="floating-window-restore" onClick={() => setMode('normal')} title={labels.restore} aria-label={labels.restore}>
          <span>{title}</span>
          <Maximize2 size={13} />
        </button>
      </div>
    );
  }

  const style: React.CSSProperties = mode === 'fullscreen'
    ? { left: MARGIN, top: MARGIN, right: MARGIN, bottom: MARGIN, width: 'auto', height: 'auto' }
    : { left: geometry.x, top: geometry.y, width: geometry.width, height: geometry.height };

  return (
    <div className={`floating-window is-${mode}`} style={style} role="dialog" aria-label={title}>
      <header
        className="floating-window-titlebar"
        onPointerDown={startDrag}
        onPointerMove={onDragMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <div className="floating-window-title">
          <strong>{title}</strong>
          {subtitle && <span>{subtitle}</span>}
        </div>
        <div className="floating-window-actions">
          <button type="button" onClick={() => setMode('minimized')} title={labels.minimize} aria-label={labels.minimize}>
            <Minus size={14} />
          </button>
          <button
            type="button"
            onClick={() => setMode(current => current === 'fullscreen' ? 'normal' : 'fullscreen')}
            title={mode === 'fullscreen' ? labels.exitFullscreen : labels.fullscreen}
            aria-label={mode === 'fullscreen' ? labels.exitFullscreen : labels.fullscreen}
          >
            {mode === 'fullscreen' ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
          <button type="button" className="is-close" onClick={onClose} title={labels.close} aria-label={labels.close}>
            <X size={14} />
          </button>
        </div>
      </header>
      <div className="floating-window-body">{children}</div>
      {mode === 'normal' && (
        <div
          className="floating-window-resize"
          onPointerDown={startResize}
          onPointerMove={onResizeMove}
          onPointerUp={endResize}
          onPointerCancel={endResize}
          aria-hidden="true"
        />
      )}
    </div>
  );
}
