import { useEffect, useRef } from 'react';
import type { VexaAudioAnalyser } from '../vexaAudioAnalyser';
import type { VoicePhase } from '../VexaCommandCenter';

/**
 * Horizontal voice waveform drawn either side of the microphone button.
 *
 * Deliberately state-free: the phase is mirrored into a ref and the canvas is repainted
 * from a throttled rAF loop, so a 30 FPS waveform never triggers a React render.
 */

const BAR_COUNT = 56;
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

    const bands = new Float32Array(BAR_COUNT);
    const smoothed = new Float32Array(BAR_COUNT);
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
        for (let index = 0; index < BAR_COUNT; index += 1) {
          const wave = Math.sin(seconds * 2.2 + index * 0.42) * 0.5 + Math.sin(seconds * 3.7 - index * 0.21) * 0.3;
          bands[index] = Math.max(0.02, amplitude * (0.55 + wave * 0.45));
        }
      }

      ctx.clearRect(0, 0, width, height);
      const centerY = height / 2;
      const barWidth = width / BAR_COUNT;
      const gap = Math.max(1, barWidth * 0.35);

      for (let index = 0; index < BAR_COUNT; index += 1) {
        smoothed[index] += (bands[index] - smoothed[index]) * 0.3;
        // Envelope: tall next to the microphone, tapering towards the outer edge.
        const distance = side === 'left' ? (index + 1) / BAR_COUNT : 1 - index / BAR_COUNT;
        const envelope = 0.25 + distance * 0.75;
        const magnitude = Math.max(0.02, smoothed[index] * envelope);
        const barHeight = Math.max(dpr * 1.5, magnitude * height * 0.82);
        const x = index * barWidth;
        const ratio = index / BAR_COUNT;
        const hue = side === 'left'
          ? `rgba(${Math.round(38 + 20 * ratio)}, ${Math.round(150 + 70 * ratio)}, 255, ${0.35 + magnitude * 0.6})`
          : `rgba(${Math.round(60 + 100 * ratio)}, ${Math.round(190 - 90 * ratio)}, 255, ${0.35 + magnitude * 0.6})`;
        ctx.fillStyle = hue;
        ctx.fillRect(x, centerY - barHeight / 2, Math.max(1, barWidth - gap), barHeight);
      }
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
