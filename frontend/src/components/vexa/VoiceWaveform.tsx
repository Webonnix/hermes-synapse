import { useEffect, useRef } from 'react';
import type { VexaAudioAnalyser } from '../vexaAudioAnalyser';
import type { VoicePhase } from '../VexaCommandCenter';

/**
 * Horizontal voice waveform drawn either side of the microphone button.
 *
 * Deliberately state-free: the phase is mirrored into a ref and the canvas is repainted
 * from a throttled rAF loop, so a 30 FPS waveform never triggers a React render.
 */

const SAMPLE_COUNT = 96;
const TARGET_FPS = 30;
const SIMPLE_FPS = 15;

interface Props {
  phase: VoicePhase;
  audioAnalyser: VexaAudioAnalyser;
  /** 'left' fades cyan→blue outwards, 'right' fades blue→violet. */
  side: 'left' | 'right';
  simpleMode: boolean;
}

export function VoiceWaveform({ phase, audioAnalyser, side, simpleMode }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const phaseRef = useRef(phase);
  const simpleRef = useRef(simpleMode);

  // Mirrored into refs so the paint loop below can read the latest values without being
  // torn down and restarted on every phase change.
  useEffect(() => { phaseRef.current = phase; }, [phase]);
  useEffect(() => { simpleRef.current = simpleMode; }, [simpleMode]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    const bands = new Float32Array(SAMPLE_COUNT);
    const smoothed = new Float32Array(SAMPLE_COUNT);
    let frameId = 0;
    let lastPaint = 0;

    const paint = (time: number) => {
      frameId = requestAnimationFrame(paint);
      const minInterval = 1000 / (simpleRef.current ? SIMPLE_FPS : TARGET_FPS);
      if (time - lastPaint < minInterval) return;
      lastPaint = time;

      const bounds = canvas.getBoundingClientRect();
      if (bounds.width === 0 || bounds.height === 0) return;
      const dpr = Math.min(1.75, window.devicePixelRatio || 1);
      const width = Math.round(bounds.width * dpr);
      const height = Math.round(bounds.height * dpr);
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const currentPhase = phaseRef.current;
      const live = audioAnalyser.readBands(bands, currentPhase);
      if (!live) {
        // Synthetic trace: a barely-moving idle ripple, or a steadier "working" pattern
        // while Vexa is thinking, so the control never looks frozen.
        const seconds = reduceMotion ? 0 : time / 1000;
        const amplitude = currentPhase === 'offline' ? 0.03
          : currentPhase === 'thinking' || currentPhase === 'transcribing' ? 0.34
            : 0.1;
        for (let index = 0; index < SAMPLE_COUNT; index += 1) {
          const wave = Math.sin(seconds * 2.2 + index * 0.42) * 0.5 + Math.sin(seconds * 3.7 - index * 0.21) * 0.3;
          bands[index] = Math.max(0.02, amplitude * (0.55 + wave * 0.45));
        }
      }

      ctx.clearRect(0, 0, width, height);
      const centerY = height / 2;
      const step = width / (SAMPLE_COUNT - 1);

      // A single mirrored polyline, the way the reference draws it: the trace crosses the
      // centre line and its excursion grows towards the microphone.
      const gradient = ctx.createLinearGradient(0, 0, width, 0);
      if (side === 'left') {
        gradient.addColorStop(0, 'rgba(27, 220, 255, .35)');
        gradient.addColorStop(1, 'rgba(38, 132, 255, 1)');
      } else {
        gradient.addColorStop(0, 'rgba(38, 132, 255, 1)');
        gradient.addColorStop(1, 'rgba(161, 93, 255, .45)');
      }

      ctx.beginPath();
      for (let index = 0; index < SAMPLE_COUNT; index += 1) {
        smoothed[index] += (bands[index] - smoothed[index]) * 0.3;
        // Envelope: tall next to the microphone, tapering towards the outer edge.
        const distance = side === 'left' ? (index + 1) / SAMPLE_COUNT : 1 - index / SAMPLE_COUNT;
        const envelope = 0.2 + distance * 0.8;
        // Alternating sign turns the magnitude envelope into the jagged trace of the
        // reference rather than a smooth hill.
        const sign = index % 2 === 0 ? 1 : -1;
        const offset = smoothed[index] * envelope * height * 0.46 * sign;
        const x = index * step;
        const y = centerY + offset;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = gradient;
      ctx.lineWidth = Math.max(1, dpr * 1.4);
      ctx.lineJoin = 'round';
      ctx.stroke();

      // Soft glow pass under the trace.
      ctx.globalAlpha = 0.35;
      ctx.lineWidth = Math.max(2, dpr * 3.2);
      ctx.stroke();
      ctx.globalAlpha = 1;
    };

    frameId = requestAnimationFrame(paint);

    const onVisibility = () => {
      cancelAnimationFrame(frameId);
      if (!document.hidden) frameId = requestAnimationFrame(paint);
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelAnimationFrame(frameId);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [audioAnalyser, side]);

  return <canvas ref={canvasRef} className="vx-waveform" aria-hidden="true" />;
}
