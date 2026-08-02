import { useCallback, useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from 'react';

/**
 * Layout state for the dashboard's two side flanks (system+metrics on one corner, the
 * agent/conversation column on the other): which corner each occupies, and how wide the
 * flank column touching the centre stage is. Both persist across sessions.
 *
 * Resizing uses plain `mousedown` + window-level `mousemove`/`mouseup` rather than the
 * Pointer Capture API — captured pointers aren't reliably reproduced by every input path
 * (including some browser-automation drivers), while window listeners fire for any mouse
 * that started the drag on the handle regardless of what the cursor is over mid-drag.
 */

const KEY_SWAPPED = 'hermes_vexa_layout_swapped';
const KEY_METRICS_WIDTH = 'hermes_vexa_col_metrics_w';
const KEY_RIGHT_WIDTH = 'hermes_vexa_col_right_w';

const METRICS_DEFAULT = 188;
const METRICS_MIN = 160;
const METRICS_MAX = 460;

const RIGHT_DEFAULT = 400;
const RIGHT_MIN = 300;
const RIGHT_MAX = 560;

type ResizeEdge = 'metrics' | 'right';

interface HandleProps {
  onMouseDown: (event: ReactMouseEvent<HTMLElement>) => void;
  onDoubleClick: () => void;
}

export interface VexaPanelLayout {
  swapped: boolean;
  toggleSwapped: () => void;
  /** Which handle is currently being dragged, for an ":active"-style highlight. */
  activeEdge: ResizeEdge | null;
  metricsHandleProps: HandleProps;
  rightHandleProps: HandleProps;
  /** Spread onto `.vx-dashboard`'s inline style; falls back to the CSS defaults if unset. */
  cssVars: CSSProperties;
}

function readStoredWidth(key: string, fallback: number, min: number, max: number): number {
  const stored = Number(localStorage.getItem(key));
  if (!Number.isFinite(stored) || stored <= 0) return fallback;
  return Math.max(min, Math.min(max, stored));
}

export function useVexaPanelLayout(): VexaPanelLayout {
  const [swapped, setSwapped] = useState(() => localStorage.getItem(KEY_SWAPPED) === '1');
  const [metricsWidth, setMetricsWidth] = useState(() => readStoredWidth(KEY_METRICS_WIDTH, METRICS_DEFAULT, METRICS_MIN, METRICS_MAX));
  const [rightWidth, setRightWidth] = useState(() => readStoredWidth(KEY_RIGHT_WIDTH, RIGHT_DEFAULT, RIGHT_MIN, RIGHT_MAX));
  const [activeEdge, setActiveEdge] = useState<ResizeEdge | null>(null);

  useEffect(() => { localStorage.setItem(KEY_SWAPPED, swapped ? '1' : '0'); }, [swapped]);
  useEffect(() => { localStorage.setItem(KEY_METRICS_WIDTH, String(metricsWidth)); }, [metricsWidth]);
  useEffect(() => { localStorage.setItem(KEY_RIGHT_WIDTH, String(rightWidth)); }, [rightWidth]);

  const toggleSwapped = useCallback(() => setSwapped(value => !value), []);

  // Mutable mirrors of the state the drag needs to read from a window-level listener that
  // is set up once per drag (closing over stale state would freeze `swapped`/widths as of
  // drag-start, which is wrong if either changed a moment before the drag began).
  const swappedRef = useRef(swapped);
  const widthsRef = useRef({ metrics: metricsWidth, right: rightWidth });
  useEffect(() => { swappedRef.current = swapped; }, [swapped]);
  useEffect(() => { widthsRef.current = { metrics: metricsWidth, right: rightWidth }; }, [metricsWidth, rightWidth]);

  const dragRef = useRef<{ edge: ResizeEdge; startX: number; startWidth: number } | null>(null);

  const makeHandleProps = useCallback((edge: ResizeEdge): HandleProps => ({
    onMouseDown: (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startWidth = edge === 'metrics' ? widthsRef.current.metrics : widthsRef.current.right;
      dragRef.current = { edge, startX: event.clientX, startWidth };
      setActiveEdge(edge);
      document.body.classList.add('vx-resizing');

      const onMove = (moveEvent: MouseEvent) => {
        const drag = dragRef.current;
        if (!drag) return;
        const delta = moveEvent.clientX - drag.startX;
        if (drag.edge === 'metrics') {
          // The flank sits left-of-core normally (dragging right grows it) and right-of-core
          // once swapped (dragging left grows it) — the handle's own CSS mirrors the same way.
          const sign = swappedRef.current ? -1 : 1;
          setMetricsWidth(Math.max(METRICS_MIN, Math.min(METRICS_MAX, drag.startWidth + sign * delta)));
        } else {
          const sign = swappedRef.current ? 1 : -1;
          setRightWidth(Math.max(RIGHT_MIN, Math.min(RIGHT_MAX, drag.startWidth + sign * delta)));
        }
      };
      const onUp = () => {
        dragRef.current = null;
        setActiveEdge(null);
        document.body.classList.remove('vx-resizing');
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    },
    onDoubleClick: () => {
      if (edge === 'metrics') setMetricsWidth(METRICS_DEFAULT);
      else setRightWidth(RIGHT_DEFAULT);
    },
  }), []);

  const cssVars = {
    '--vx-col-metrics': `${metricsWidth}px`,
    '--vx-col-right': `${rightWidth}px`,
  } as CSSProperties;

  return {
    swapped,
    toggleSwapped,
    activeEdge,
    metricsHandleProps: makeHandleProps('metrics'),
    rightHandleProps: makeHandleProps('right'),
    cssVars,
  };
}
