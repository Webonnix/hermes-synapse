import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Mic, MicOff, Radio, Send, ShieldCheck, Square } from 'lucide-react';
import type { VexaAudioAnalyser } from '../vexaAudioAnalyser';
import type { VoicePhase } from '../VexaCommandCenter';
import type { VexaCopy } from './vexaCopy';
import type { CoreHudData } from './vexaDashboardTypes';
import { VexaCoreFallback2D } from './VexaCoreFallback2D';
import { VoiceWaveform } from './VoiceWaveform';

const VexaEnergyCore = lazy(() => import('../VexaEnergyCore'));

/** Longest command the composer accepts before it stops growing. */
const MAX_COMMAND_LENGTH = 8000;

export type MicrophoneState = 'unavailable' | 'permission_required' | 'ready' | 'listening' | 'muted' | 'processing' | 'error';

function hasWebGL(): boolean {
  if (typeof document === 'undefined') return false;
  try {
    const canvas = document.createElement('canvas');
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}

interface Props {
  copy: VexaCopy;
  phase: VoicePhase;
  audioAnalyser: VexaAudioAnalyser;
  pulseKey: string;
  simpleMode: boolean;
  hud: CoreHudData;
  micState: MicrophoneState;
  micErrorMessage?: string;
  conversationMode: boolean;
  onToggleConversation: () => void;
  onVoiceToggle: () => void;
  riskEnabled: boolean;
  pendingConfirmations: number;
  emergencyStopped: boolean;
  onOpenRiskControl: () => void;
  isConnected: boolean;
  isGenerating: boolean;
  draft: string;
  onDraftChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  sendError: string | null;
  onWebGLFallback: (available: boolean) => void;
}

export function VexaCoreStage({
  copy, phase, audioAnalyser, pulseKey, simpleMode, hud,
  micState, micErrorMessage, conversationMode, onToggleConversation, onVoiceToggle,
  riskEnabled, pendingConfirmations, emergencyStopped, onOpenRiskControl,
  isConnected, isGenerating, draft, onDraftChange, onSubmit, onStop, sendError,
  onWebGLFallback,
}: Props) {
  const [webglReady, setWebglReady] = useState(false);
  const [webglAvailable] = useState(() => hasWebGL());
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => { onWebGLFallback(webglAvailable); }, [webglAvailable, onWebGLFallback]);

  // Autosize the composer up to ~5 rows without a layout thrash on every keystroke.
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(116, textarea.scrollHeight)}px`;
  }, [draft]);

  // Alt+Space toggles the microphone, but never while the user is typing into a field.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.altKey || event.code !== 'Space') return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || target?.isContentEditable) return;
      event.preventDefault();
      onVoiceToggle();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onVoiceToggle]);

  const micClass = micState === 'listening' ? ' is-listening'
    : micState === 'processing' ? ' is-processing'
      : micState === 'muted' || micState === 'unavailable' ? ' is-muted'
        : micState === 'error' ? ' is-error' : '';

  // The label stays "start a voice command" even when the microphone is unavailable: the
  // click is still allowed so the caller can surface *why* (no secure context, permission
  // denied, no device) instead of the button silently doing nothing.
  const micLabel = micState === 'listening' ? copy.micStop : copy.mic;
  const micTitle = micState === 'unavailable' ? (micErrorMessage || copy.micUnavailable)
    : micState === 'error' && micErrorMessage ? micErrorMessage
      : micLabel;

  const thoughtFlowLabel = hud.thoughtFlow.toUpperCase();
  const composerDisabled = !isConnected || emergencyStopped;

  return (
    <section className={`vx-core-stage${webglReady ? ' is-webgl-ready' : ''}`}>
      <div
        className="vx-canvas-layer"
        role="img"
        aria-label={`${copy.coreVisualization}: ${hud.coreStatus}`}
      >
        <VexaCoreFallback2D phase={phase} audioAnalyser={audioAnalyser} pulseKey={pulseKey} simpleMode={simpleMode} />
        {webglAvailable && !simpleMode && (
          <Suspense fallback={null}>
            <div className="vx-webgl-layer">
              <VexaEnergyCore
                phase={phase}
                audioAnalyser={audioAnalyser}
                pulseKey={pulseKey}
                onReady={() => setWebglReady(true)}
              />
            </div>
          </Suspense>
        )}
      </div>

      <div className="vx-hud-layer" aria-hidden="true">
        <svg className="vx-hud-connectors">
          <line x1="18%" y1="16%" x2="40%" y2="34%" stroke="rgba(45,145,212,.3)" strokeWidth="1" />
          <line x1="82%" y1="16%" x2="60%" y2="34%" stroke="rgba(45,145,212,.3)" strokeWidth="1" />
          <line x1="14%" y1="72%" x2="40%" y2="58%" stroke="rgba(45,145,212,.24)" strokeWidth="1" />
          <line x1="86%" y1="72%" x2="60%" y2="58%" stroke="rgba(139,91,255,.26)" strokeWidth="1" />
        </svg>
        <div className="vx-hud-label at-tl">
          <b>{copy.hudAnalyzing}</b>
          <span>{hud.processLabel}</span>
        </div>
        <div className="vx-hud-label at-tr">
          <b>{copy.hudNeuralCore}</b>
          <span className="is-green">{hud.coreStatus}</span>
        </div>
        <div className="vx-hud-label at-bl">
          <b>{copy.hudLinkStrength}</b>
          <span>{hud.linkStrength === null ? '—' : `${Math.round(hud.linkStrength)}%`}</span>
        </div>
        <div className="vx-hud-label at-br is-violet">
          <b>{copy.hudThoughtFlow}</b>
          <span className="is-violet">{thoughtFlowLabel}</span>
        </div>
      </div>

      <div className="vx-voice-layer">
        <div className="vx-waveform-side">
          <VoiceWaveform phase={phase} audioAnalyser={audioAnalyser} side="left" simpleMode={simpleMode} />
        </div>

        <button
          type="button"
          className={`vx-mic-button${micClass}`}
          onClick={onVoiceToggle}
          disabled={micState === 'processing' || emergencyStopped}
          aria-label={micLabel}
          aria-pressed={micState === 'listening'}
          title={micTitle}
        >
          {micState === 'listening' ? <MicOff size={26} /> : <Mic size={26} />}
          <i aria-hidden="true" />
        </button>

        <div className="vx-waveform-side">
          <VoiceWaveform phase={phase} audioAnalyser={audioAnalyser} side="right" simpleMode={simpleMode} />
        </div>
      </div>

      <div className="vx-command-layer">
        <div className="vx-command-cards">
          <button
            type="button"
            className={`vx-mode-card${conversationMode ? ' is-active' : ''}`}
            onClick={onToggleConversation}
            aria-pressed={conversationMode}
          >
            <Radio size={17} />
            <span>
              <b>{copy.conversation}</b>
              <small>{copy.conversationHint}</small>
            </span>
          </button>

          <button
            type="button"
            className={`vx-mode-card${emergencyStopped ? ' is-stopped' : pendingConfirmations > 0 ? ' is-warn' : riskEnabled ? ' is-safe' : ''}`}
            onClick={onOpenRiskControl}
          >
            <ShieldCheck size={17} />
            <span>
              <b>{copy.riskControl}</b>
              <small>
                {emergencyStopped
                  ? copy.riskStopped
                  : pendingConfirmations > 0
                    ? `${pendingConfirmations} ${copy.riskPending}`
                    : copy.riskEnabled}
              </small>
            </span>
          </button>
        </div>

        <form
          className="vx-composer"
          onSubmit={event => { event.preventDefault(); onSubmit(); }}
        >
          <label htmlFor="vexa-command">{copy.prompt}</label>
          <textarea
            id="vexa-command"
            ref={textareaRef}
            value={draft}
            rows={1}
            maxLength={MAX_COMMAND_LENGTH}
            placeholder={`${copy.prompt}...`}
            disabled={composerDisabled}
            onChange={event => onDraftChange(event.target.value)}
            onKeyDown={event => {
              if (event.key !== 'Enter') return;
              if (event.shiftKey) return;
              event.preventDefault();
              onSubmit();
            }}
          />
          {draft.length > MAX_COMMAND_LENGTH * 0.9 && (
            <span className="vx-composer-count">{draft.length}/{MAX_COMMAND_LENGTH}</span>
          )}
          {isGenerating ? (
            <button type="button" className="is-stop" onClick={onStop} title={copy.stop} aria-label={copy.stop}>
              <Square size={16} fill="currentColor" />
            </button>
          ) : (
            <button type="submit" disabled={!draft.trim() || composerDisabled} title={copy.send} aria-label={copy.send}>
              <Send size={17} />
            </button>
          )}
        </form>
        {sendError && <p className="vx-composer-error" role="alert">{sendError}</p>}
      </div>
    </section>
  );
}
