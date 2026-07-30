import { useCallback, useEffect, useRef, useState } from 'react';
import { Gauge, OctagonX, Play, X as XIcon } from 'lucide-react';
import type { VexaCopy } from './vexaCopy';
import type { ConfirmationRequest } from './vexaDashboardTypes';

/**
 * Approval queue drawer, opened from the header bell and from the risk-control card.
 *
 * Approve/reject proxy straight to the existing control-plane endpoints; the drawer never
 * decides anything itself, it only surfaces what the backend is waiting on.
 */

/** Milliseconds the emergency-stop button must be held before it fires. */
const EMERGENCY_HOLD_MS = 700;

interface Props {
  copy: VexaCopy;
  open: boolean;
  requests: ConfirmationRequest[];
  onClose: () => void;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
  onOpenProcesses: () => void;
  emergencyStopped: boolean;
  onEmergencyStop: () => void;
  onResume: () => void;
  /** Reduced-graphics profile (fewer particles, no WebGL layer, slower waveform). */
  simpleGraphics: boolean;
  onToggleGraphics: () => void;
}

/**
 * Hold-to-fire emergency stop. Deliberately not a plain click: stopping every agent
 * mid-flight is disruptive enough to deserve a deliberate gesture, and the ring shows
 * how far along the hold is.
 */
function EmergencyStopButton({ copy, onFire }: { copy: VexaCopy; onFire: () => void }) {
  const [progress, setProgress] = useState(0);
  const startRef = useRef(0);
  const frameRef = useRef(0);

  const cancel = useCallback(() => {
    cancelAnimationFrame(frameRef.current);
    startRef.current = 0;
    setProgress(0);
  }, []);

  const begin = useCallback(() => {
    if (startRef.current) return;
    startRef.current = performance.now();
    const tick = () => {
      if (!startRef.current) return;
      const ratio = Math.min(1, (performance.now() - startRef.current) / EMERGENCY_HOLD_MS);
      setProgress(ratio);
      if (ratio >= 1) {
        startRef.current = 0;
        setProgress(0);
        onFire();
        return;
      }
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
  }, [onFire]);

  useEffect(() => () => cancelAnimationFrame(frameRef.current), []);

  return (
    <button
      type="button"
      className="vx-btn is-reject vx-estop"
      onPointerDown={begin}
      onPointerUp={cancel}
      onPointerLeave={cancel}
      onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') begin(); }}
      onKeyUp={cancel}
      title={copy.emergencyHold}
      aria-label={`${copy.emergencyStop}. ${copy.emergencyHold}`}
    >
      <OctagonX size={15} />
      {copy.emergencyStop}
      {progress > 0 && <i className="vx-estop-progress" style={{ width: `${progress * 100}%` }} />}
    </button>
  );
}

export function VexaConfirmationDrawer({
  copy, open, requests, onClose, onApprove, onReject, onOpenProcesses,
  emergencyStopped, onEmergencyStop, onResume, simpleGraphics, onToggleGraphics,
}: Props) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = panel.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      <button type="button" className="vx-scrim" onClick={onClose} aria-label={copy.close} />
      <div className="vx-drawer" role="dialog" aria-modal="true" aria-label={copy.confirmations} ref={panelRef}>
        <div className="vx-drawer-head">
          <h2>{copy.confirmations}</h2>
          <button type="button" className="vx-icon-btn" onClick={onClose} title={copy.close} aria-label={copy.close} ref={closeRef}>
            <XIcon size={14} />
          </button>
        </div>
        <div className="vx-drawer-body">
          {requests.length === 0 && <p className="vx-empty">{copy.noConfirmations}</p>}
          {requests.map(request => (
            <article className="vx-confirm-card" key={request.id}>
              <h3>{request.title}</h3>
              {request.description && <p>{request.description}</p>}
              <div className="vx-confirm-meta">
                <span className={`vx-tag is-${request.riskLevel}`}>{copy.riskLevel}: {request.riskLevel}</span>
                {request.agentId && <span className="vx-tag">{request.agentId}</span>}
                {request.requestedAt && (
                  <span className="vx-tag">{copy.requested}: {new Date(request.requestedAt).toLocaleTimeString()}</span>
                )}
              </div>
              <div className="vx-confirm-actions">
                <button type="button" className="vx-btn is-approve" onClick={() => onApprove(request.id)}>{copy.approve}</button>
                <button type="button" className="vx-btn is-reject" onClick={() => onReject(request.id)}>{copy.reject}</button>
              </div>
            </article>
          ))}
          <button type="button" className="vx-link" onClick={onOpenProcesses}>{copy.quickSecurity} ›</button>

          <section className="vx-drawer-section">
            <h3>{copy.riskControl}</h3>
            <div className="vx-confirm-actions">
              {emergencyStopped
                ? (
                  <button type="button" className="vx-btn is-approve" onClick={onResume}>
                    <Play size={15} />{copy.resume}
                  </button>
                )
                : <EmergencyStopButton copy={copy} onFire={onEmergencyStop} />}
              <button
                type="button"
                className={`vx-btn${simpleGraphics ? ' is-approve' : ''}`}
                onClick={onToggleGraphics}
                aria-pressed={simpleGraphics}
              >
                <Gauge size={15} />{copy.lightGraphics}
              </button>
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

export function VexaEmergencyOverlay({ copy, onResume }: { copy: VexaCopy; onResume: () => void }) {
  return (
    <div className="vx-emergency-overlay" role="alert">
      <div className="vx-emergency-card">
        <OctagonX size={17} />
        <span>{copy.emergencyActive}</span>
        <button type="button" className="vx-btn" onClick={onResume}>{copy.resume}</button>
      </div>
    </div>
  );
}
