import { useEffect, useRef } from 'react';
import type { VexaAudioAnalyser } from '../vexaAudioAnalyser';
import type { VoicePhase } from '../VexaCommandCenter';

/**
 * Canvas-2D neural core.
 *
 * Serves two purposes: it paints instantly while the Three.js chunk is still loading (the
 * WebGL layer cross-fades over it), and it is the permanent core when WebGL is
 * unavailable — so the dashboard never depends on WebGL to be usable.
 *
 * Everything is driven from refs inside a single rAF loop; no React state is touched per
 * frame and no object is allocated inside the loop beyond the gradient, which the 2D
 * context requires per paint.
 */

interface Props {
  phase: VoicePhase;
  audioAnalyser: VexaAudioAnalyser;
  pulseKey: string;
  /** Simple mode halves the particle budget and drops the inner signal ring. */
  simpleMode: boolean;
}

export function VexaCoreFallback2D({ phase, audioAnalyser, pulseKey, simpleMode }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const phaseRef = useRef(phase);
  const simpleRef = useRef(simpleMode);
  const smoothedAmpRef = useRef(0);
  const pulsesRef = useRef<Array<{ start: number }>>([]);
  const lastPulseKeyRef = useRef(pulseKey);

  // Mirrored into refs so the render loop reads current values without restarting.
  useEffect(() => { phaseRef.current = phase; }, [phase]);
  useEffect(() => { simpleRef.current = simpleMode; }, [simpleMode]);

  useEffect(() => {
    if (pulseKey !== lastPulseKeyRef.current) {
      lastPulseKeyRef.current = pulseKey;
      pulsesRef.current.push({ start: performance.now() });
    }
  }, [pulseKey]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    let frame = 0;
    let frameId = 0;

    const render = (time: number) => {
      const currentPhase = phaseRef.current;
      const simple = simpleRef.current;
      audioAnalyser.sync(currentPhase);

      const bounds = canvas.getBoundingClientRect();
      if (bounds.width === 0 || bounds.height === 0) {
        frameId = requestAnimationFrame(render);
        return;
      }
      const dpr = Math.min(1.75, window.devicePixelRatio || 1);
      const width = Math.max(1, Math.round(bounds.width * dpr));
      const height = Math.max(1, Math.round(bounds.height * dpr));
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;

      const liveAmp = audioAnalyser.read(currentPhase);
      smoothedAmpRef.current += (liveAmp - smoothedAmpRef.current) * 0.25;
      const voiceBoost = smoothedAmpRef.current;

      ctx.clearRect(0, 0, width, height);
      const cx = width / 2;
      const cy = height * 0.46;
      const radius = Math.min(width, height) * 0.34;
      const speed = currentPhase === 'listening' ? 2.4
        : currentPhase === 'thinking' ? 1.7
          : currentPhase === 'speaking' ? 2 : 0.7;
      const baseEnergy = currentPhase === 'offline' ? 0.16 : currentPhase === 'ready' ? 0.42 : 0.9;
      const energy = Math.min(1.4, baseEnergy + voiceBoost * 0.75);
      const tick = reduceMotion ? 0 : time / 1000;

      ctx.save();
      ctx.globalCompositeOperation = 'screen';

      const ringCount = simple ? 4 : 7;
      for (let ring = 0; ring < ringCount; ring += 1) {
        const ringRadius = radius * (0.48 + ring * 0.105) * (1 + voiceBoost * 0.04);
        const rotation = tick * speed * (ring % 2 ? -1 : 1) * (0.12 + ring * 0.018);
        ctx.lineWidth = Math.max(1, dpr * (ring % 3 === 0 ? 1.25 : 0.65));
        ctx.strokeStyle = `rgba(${ring % 2 ? '58, 187, 255' : '27, 128, 255'}, ${0.12 + energy * 0.2})`;
        for (let segment = 0; segment < 9; segment += 1) {
          const start = rotation + segment * Math.PI * 2 / 9;
          const length = 0.16 + ((ring * 7 + segment * 3) % 5) * 0.055;
          ctx.beginPath();
          ctx.arc(cx, cy, ringRadius, start, start + length);
          ctx.stroke();
        }
      }

      const particleCount = reduceMotion ? 28 : simple ? 34 : 74;
      for (let index = 0; index < particleCount; index += 1) {
        const seed = index * 12.9898;
        const orbit = radius * (0.34 + ((Math.sin(seed) + 1) / 2) * 0.8);
        const angle = seed + tick * speed * (0.08 + index % 5 * 0.018);
        const wobble = Math.sin(tick * 1.4 + seed) * radius * 0.025;
        const x = cx + Math.cos(angle) * (orbit + wobble);
        const y = cy + Math.sin(angle) * (orbit * 0.72 + wobble);
        const size = dpr * (0.5 + (index % 4) * 0.32) * (1 + voiceBoost * 0.5);
        ctx.beginPath();
        ctx.arc(x, y, size, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(99, 213, 255, ${0.12 + energy * ((index % 5) / 8 + 0.18)})`;
        ctx.fill();
      }

      if (!simple && (currentPhase === 'listening' || currentPhase === 'speaking' || currentPhase === 'transcribing')) {
        ctx.lineWidth = Math.max(1, dpr * 1.1);
        ctx.strokeStyle = `rgba(111, 224, 255, ${0.25 + energy * 0.35})`;
        ctx.beginPath();
        for (let index = 0; index <= 96; index += 1) {
          const ratio = index / 96;
          const angle = ratio * Math.PI * 2;
          const signal = Math.sin(angle * 7 + tick * speed * 4) * 0.5 + Math.sin(angle * 13 - tick * 3) * 0.24;
          const waveRadius = radius * (0.31 + signal * (0.035 + voiceBoost * 0.09) * energy);
          const x = cx + Math.cos(angle) * waveRadius;
          const y = cy + Math.sin(angle) * waveRadius;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.stroke();
      }

      const pulse = reduceMotion ? 0.65 : 0.6 + Math.sin(tick * (currentPhase === 'ready' ? 1.8 : 4.2)) * 0.14;
      const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 0.48);
      glow.addColorStop(0, `rgba(197, 244, 255, ${Math.min(1, pulse * energy + voiceBoost * 0.3)})`);
      glow.addColorStop(0.13, `rgba(38, 174, 255, ${0.35 * energy})`);
      glow.addColorStop(1, 'rgba(0, 76, 180, 0)');
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, radius * (0.5 + voiceBoost * 0.06), 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      pulsesRef.current = pulsesRef.current.filter(item => time - item.start < 1300);
      pulsesRef.current.forEach(item => {
        const progress = Math.min(1, (time - item.start) / 1300);
        ctx.beginPath();
        ctx.arc(cx, cy, radius * (0.42 + progress * 1.65), 0, Math.PI * 2);
        ctx.lineWidth = Math.max(1, dpr * 2.4 * (1 - progress));
        ctx.strokeStyle = `rgba(167, 243, 255, ${(1 - progress) * 0.5})`;
        ctx.stroke();
      });

      frame += 1;
      if (!reduceMotion || frame < 2) frameId = requestAnimationFrame(render);
    };

    frameId = requestAnimationFrame(render);

    const onVisibility = () => {
      cancelAnimationFrame(frameId);
      if (!document.hidden) frameId = requestAnimationFrame(render);
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelAnimationFrame(frameId);
      document.removeEventListener('visibilitychange', onVisibility);
      audioAnalyser.disconnectMic();
    };
  }, [audioAnalyser]);

  return <canvas ref={canvasRef} className="vx-canvas-2d" aria-hidden="true" />;
}
