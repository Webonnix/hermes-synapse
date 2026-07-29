import { useEffect, useRef } from 'react';
import * as THREE from 'three';
import type { VoicePhase } from './VexaCommandCenter';
import type { VexaAudioAnalyser } from './vexaAudioAnalyser';
import { getVexaQualityPreset } from './vexaQualityPresets';
import { createVexaBloomRig, type VexaBloomRig } from './vexaBloom';

// Re-tested with `three` r185's own EffectComposer + RenderPass(clearAlpha=0) +
// UnrealBloomPass (vexaBloom.ts) and the historical failure reproduces exactly: with
// BLOOM_ENABLED=true, every sampled canvas pixel — including fully-empty corners that
// read alpha=0 with bloom off — reads alpha=255. Root cause: UnrealBloomPass.render(),
// when it's the composer's final (renderToScreen) pass, first blits the base scene to
// the screen via an opaque `MeshBasicMaterial` full-screen quad before compositing the
// glow, which stomps the canvas's transparency regardless of clearAlpha/renderTarget
// settings upstream. Keeping this off; vexaBloom.ts is left in place, unused, in case a
// future attempt wants to try inserting a transparency-safe final pass instead of letting
// UnrealBloomPass itself be the renderToScreen pass.
const BLOOM_ENABLED = false;
import {
  PASSTHROUGH_VERTEX_SHADER,
  VORTEX_FRAGMENT_SHADER,
  RING_FRAGMENT_SHADER,
  ORBIT_FRAGMENT_SHADER,
  FILAMENT_FRAGMENT_SHADER,
  PARTICLE_VERTEX_SHADER,
  PARTICLE_FRAGMENT_SHADER,
  NEURAL_POINT_VERTEX_SHADER,
  NEURAL_POINT_FRAGMENT_SHADER,
  NEURAL_LINK_VERTEX_SHADER,
  NEURAL_LINK_FRAGMENT_SHADER,
} from './vexaShaders';
import { buildNeuralMesh } from './vexaNeuralMesh';

// Deliberately no EffectComposer/UnrealBloomPass: three.js's bloom composite does not
// reliably preserve per-pixel alpha against a transparent renderer (verified: it either
// drops the whole frame to alpha 0, or forces alpha 1 across the entire canvas — both
// break compositing over the app's backdrop). The soft glow is instead baked directly
// into each shader below, which is both correct and easier to keep subtle and controlled.

// Visual-state vocabulary note: "idle"/"stopped" (terms used in design discussions of
// this component) do not have dedicated VoicePhase values. "idle" maps to the existing
// `ready` phase (already the connected/resting state); "stopped" maps to the existing
// `offline` phase (already dimmed via `.vexa-command-center.is-offline` in index.css).
// "error" DOES have a real phase now (App.tsx's submitVoiceBlob sets micState='error' on
// a failed/empty voice transcription, mapped through VexaCommandCenter's phaseFor()) —
// see PHASE_COLOR below. The `errorPulseKey` prop remains for an independent one-off
// pulse burst, separate from this persistent phase-driven coloring.

interface VexaEnergyCoreProps {
  phase: VoicePhase;
  audioAnalyser: VexaAudioAnalyser;
  pulseKey: string;
  /** Optional: bump this string to trigger a one-off amber/red warning burst, independent
   * of `phase`. Undefined/unused by default — see the state-vocabulary note above. */
  errorPulseKey?: string;
  /** Fired once the scene has painted its first real frame, so the caller can cross-fade in. */
  onReady: () => void;
}

const ERROR_PULSE_COLOR = new THREE.Color(1.0, 0.42, 0.32);

const PHASE_COLOR: Record<VoicePhase, [number, number, number]> = {
  offline: [0.22, 0.26, 0.32],
  ready: [0.18, 0.58, 0.95],
  listening: [0.32, 0.86, 1.0],
  transcribing: [0.42, 0.78, 0.99],
  thinking: [0.52, 0.56, 0.97],
  speaking: [0.66, 0.86, 1.0],
  // Same RGB as ERROR_PULSE_COLOR above — the "real trigger" this file's state-vocabulary
  // note anticipated: a transient mic-transcription failure now sets micState/phase to
  // 'error' for a few seconds (App.tsx submitVoiceBlob), so this is a real persistent
  // phase rather than a one-off errorPulseKey burst.
  error: [1.0, 0.42, 0.32],
};

interface RingConfig {
  radius: number;
  width: number;
  ticks: number;
  variant: number;
  dir: 1 | -1;
  speed: number;
  opacity: number;
}

const RING_CONFIGS: RingConfig[] = [
  { radius: 0.3, width: 0.012, ticks: 28, variant: 0, dir: 1, speed: 0.16, opacity: 0.85 },
  { radius: 0.47, width: 0.009, ticks: 46, variant: 1, dir: -1, speed: 0.1, opacity: 0.7 },
  { radius: 0.64, width: 0.008, ticks: 64, variant: 2, dir: 1, speed: 0.065, opacity: 0.6 },
  { radius: 0.84, width: 0.007, ticks: 8, variant: 3, dir: -1, speed: 0.045, opacity: 0.55 },
];

interface Orbit3DConfig {
  radius: number;
  tubeRadius: number;
  tiltX: number;
  tiltZ: number;
  dashCount: number;
  dashSpeed: number;
  speed: number;
  dir: 1 | -1;
  opacity: number;
}

// Six tilted 3D orbit rings around the core — real TorusGeometry, not the flat
// camera-facing quads above. Varying radius/tilt/speed/direction avoids the "perfectly
// concentric" look; slow rotation.y drift produces a precession-like tumble rather than a
// flat spin (a torus is rotationally symmetric around its own normal, so what reads as
// motion here is this precession plus the shader's traveling dash).
const ORBIT_CONFIGS: Orbit3DConfig[] = [
  { radius: 1.5, tubeRadius: 0.006, tiltX: 0.4, tiltZ: 0.15, dashCount: 3, dashSpeed: 0.35, speed: 0.05, dir: 1, opacity: 0.55 },
  { radius: 1.75, tubeRadius: 0.005, tiltX: -0.6, tiltZ: 0.45, dashCount: 4, dashSpeed: -0.28, speed: 0.04, dir: -1, opacity: 0.48 },
  { radius: 2.0, tubeRadius: 0.005, tiltX: 1.0, tiltZ: -0.3, dashCount: 2, dashSpeed: 0.42, speed: 0.035, dir: 1, opacity: 0.42 },
  { radius: 2.25, tubeRadius: 0.004, tiltX: -0.25, tiltZ: -0.85, dashCount: 5, dashSpeed: -0.22, speed: 0.03, dir: -1, opacity: 0.36 },
  { radius: 2.5, tubeRadius: 0.004, tiltX: 1.2, tiltZ: 0.55, dashCount: 3, dashSpeed: 0.3, speed: 0.025, dir: 1, opacity: 0.3 },
  { radius: 2.75, tubeRadius: 0.0035, tiltX: -1.05, tiltZ: 0.2, dashCount: 6, dashSpeed: -0.18, speed: 0.02, dir: -1, opacity: 0.24 },
];

interface FilamentConfig {
  seed: number;
  radius: number;
  tubeRadius: number;
  flowSpeed: number;
  driftSpeed: number;
  opacity: number;
}

// Internal energy filaments — organic closed loops threading through the core, inside
// the innermost orbit ring. Built once from a small jittered point ring via
// CatmullRomCurve3 + TubeGeometry; never rebuilt per frame, only rotated/re-tinted.
const FILAMENT_CONFIGS: FilamentConfig[] = [
  { seed: 0.7, radius: 0.85, tubeRadius: 0.008, flowSpeed: 0.6, driftSpeed: 0.02, opacity: 0.5 },
  { seed: 2.3, radius: 0.95, tubeRadius: 0.007, flowSpeed: -0.5, driftSpeed: -0.015, opacity: 0.45 },
  { seed: 4.1, radius: 0.75, tubeRadius: 0.006, flowSpeed: 0.75, driftSpeed: 0.025, opacity: 0.4 },
  { seed: 5.6, radius: 1.05, tubeRadius: 0.006, flowSpeed: -0.65, driftSpeed: -0.018, opacity: 0.4 },
];

function buildFilamentCurve(seed: number, radius: number): THREE.CatmullRomCurve3 {
  const points: THREE.Vector3[] = [];
  const pointCount = 7;
  for (let index = 0; index < pointCount; index += 1) {
    const angle = (index / pointCount) * Math.PI * 2 + seed;
    const r = radius * (0.3 + 0.7 * Math.abs(Math.sin(seed * 3 + index * 1.3)));
    const y = Math.sin(seed * 5 + index * 2.1) * radius * 0.55;
    points.push(new THREE.Vector3(Math.cos(angle) * r, y, Math.sin(angle) * r));
  }
  return new THREE.CatmullRomCurve3(points, true, 'catmullrom', 0.5);
}

const MAX_PULSES = 3;
const SCENE_SCALE = 5.2;

function randomOrthonormalPair(): [THREE.Vector3, THREE.Vector3] {
  const a = new THREE.Vector3(Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5).normalize();
  const seed = new THREE.Vector3(Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5);
  const b = seed.sub(a.clone().multiplyScalar(a.dot(seed))).normalize();
  return [a, b];
}

export default function VexaEnergyCore({ phase, audioAnalyser, pulseKey, errorPulseKey, onReady }: VexaEnergyCoreProps) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const phaseRef = useRef(phase);
  const pulsesRef = useRef<Array<{ start: number; claimed: boolean }>>([]);
  const lastPulseKeyRef = useRef(pulseKey);
  const errorPulsesRef = useRef<Array<{ start: number; claimed: boolean }>>([]);
  const lastErrorPulseKeyRef = useRef(errorPulseKey);
  const onReadyRef = useRef(onReady);

  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    onReadyRef.current = onReady;
  }, [onReady]);

  useEffect(() => {
    if (pulseKey !== lastPulseKeyRef.current) {
      lastPulseKeyRef.current = pulseKey;
      pulsesRef.current.push({ start: performance.now(), claimed: false });
    }
  }, [pulseKey]);

  useEffect(() => {
    if (errorPulseKey !== undefined && errorPulseKey !== lastErrorPulseKeyRef.current) {
      lastErrorPulseKeyRef.current = errorPulseKey;
      errorPulsesRef.current.push({ start: performance.now(), claimed: false });
    }
  }, [errorPulseKey]);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    const quality = getVexaQualityPreset();
    const reduceMotion = quality.reduceMotion;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    } catch {
      // No WebGL support (or a headless/test environment) — leave the 2D canvas fallback in place.
      return;
    }
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(quality.dprCap, window.devicePixelRatio || 1));
    mount.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
    camera.position.set(0, 0, 5.4);

    const quad = new THREE.PlaneGeometry(SCENE_SCALE, SCENE_SCALE);
    const disposables: Array<{ dispose: () => void }> = [quad];

    const makeRadialMaterial = (fragmentShader: string, uniforms: Record<string, THREE.IUniform>) => {
      const material = new THREE.ShaderMaterial({
        uniforms,
        vertexShader: PASSTHROUGH_VERTEX_SHADER,
        fragmentShader,
        transparent: true,
        depthWrite: false,
        depthTest: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
      });
      disposables.push(material);
      return material;
    };

    // Central vortex.
    const vortexUniforms = {
      uTime: { value: 0 },
      uAmp: { value: 0 },
      uEnergy: { value: 0.4 },
      uColor: { value: new THREE.Color(0.2, 0.62, 0.97) },
    };
    const vortex = new THREE.Mesh(quad, makeRadialMaterial(VORTEX_FRAGMENT_SHADER, vortexUniforms));
    scene.add(vortex);

    // Concentric HUD rings.
    const rings = RING_CONFIGS.map(config => {
      const uniforms = {
        uRadius: { value: config.radius },
        uWidth: { value: config.width },
        uTicks: { value: config.ticks },
        uVariant: { value: config.variant },
        uEnergy: { value: 0.4 },
        uOpacity: { value: config.opacity },
        uColor: { value: new THREE.Color(0.3, 0.75, 1) },
      };
      const mesh = new THREE.Mesh(quad, makeRadialMaterial(RING_FRAGMENT_SHADER, uniforms));
      scene.add(mesh);
      return { mesh, uniforms, config };
    });

    // Tilted 3D orbital rings — real geometry, so they read as orbits in space rather
    // than flat overlays.
    const orbits = ORBIT_CONFIGS.slice(0, quality.orbitCount).map(config => {
      const uniforms = {
        uTime: { value: 0 },
        uEnergy: { value: 0.4 },
        uAmp: { value: 0 },
        uOpacity: { value: config.opacity },
        uDashCount: { value: config.dashCount },
        uDashSpeed: { value: config.dashSpeed },
        uColor: { value: new THREE.Color(0.3, 0.75, 1) },
      };
      const geometry = new THREE.TorusGeometry(config.radius, config.tubeRadius, 8, 128);
      disposables.push(geometry);
      const material = new THREE.ShaderMaterial({
        uniforms,
        vertexShader: PASSTHROUGH_VERTEX_SHADER,
        fragmentShader: ORBIT_FRAGMENT_SHADER,
        transparent: true,
        depthWrite: false,
        depthTest: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
      });
      disposables.push(material);
      const mesh = new THREE.Mesh(geometry, material);
      mesh.rotation.set(config.tiltX, 0, config.tiltZ);
      scene.add(mesh);
      return { mesh, uniforms, config };
    });

    // Internal energy filaments.
    const filaments = FILAMENT_CONFIGS.slice(0, quality.filamentCount).map(config => {
      const curve = buildFilamentCurve(config.seed, config.radius);
      const geometry = new THREE.TubeGeometry(curve, 96, config.tubeRadius, 6, true);
      disposables.push(geometry);
      const uniforms = {
        uTime: { value: 0 },
        uEnergy: { value: 0.4 },
        uAmp: { value: 0 },
        uOpacity: { value: config.opacity },
        uFlowSpeed: { value: config.flowSpeed },
        uColor: { value: new THREE.Color(0.4, 0.8, 1) },
      };
      const material = new THREE.ShaderMaterial({
        uniforms,
        vertexShader: PASSTHROUGH_VERTEX_SHADER,
        fragmentShader: FILAMENT_FRAGMENT_SHADER,
        transparent: true,
        depthWrite: false,
        depthTest: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
      });
      disposables.push(material);
      const mesh = new THREE.Mesh(geometry, material);
      scene.add(mesh);
      return { mesh, uniforms, config };
    });

    // Pulse ring pool (message-arrival bursts).
    const pulsePool = Array.from({ length: MAX_PULSES }, () => {
      const uniforms = {
        uRadius: { value: 0.2 },
        uWidth: { value: 0.01 },
        uTicks: { value: 1 },
        uVariant: { value: -1 },
        uEnergy: { value: 0.4 },
        uOpacity: { value: 0 },
        uColor: { value: new THREE.Color(0.65, 0.95, 1) },
      };
      const mesh = new THREE.Mesh(quad, makeRadialMaterial(RING_FRAGMENT_SHADER, uniforms));
      mesh.visible = false;
      scene.add(mesh);
      return { mesh, uniforms, active: false, start: 0 };
    });

    // Error/warning burst pool — same mechanics as the message-arrival pulses above, but
    // a fixed amber/red color (never lerped toward the phase's targetColor) so it reads
    // distinctly. Triggered by the optional `errorPulseKey` prop.
    const errorPulsePool = Array.from({ length: 2 }, () => {
      const uniforms = {
        uRadius: { value: 0.2 },
        uWidth: { value: 0.014 },
        uTicks: { value: 1 },
        uVariant: { value: -1 },
        uEnergy: { value: 0.4 },
        uOpacity: { value: 0 },
        uColor: { value: ERROR_PULSE_COLOR.clone() },
      };
      const mesh = new THREE.Mesh(quad, makeRadialMaterial(RING_FRAGMENT_SHADER, uniforms));
      mesh.visible = false;
      scene.add(mesh);
      return { mesh, uniforms, active: false, start: 0 };
    });

    // Fine orbiting dust — small, sharp points on tilted elliptical trajectories.
    const particleCount = quality.particleCount;
    const basisA = new Float32Array(particleCount * 3);
    const basisB = new Float32Array(particleCount * 3);
    const speeds = new Float32Array(particleCount);
    const phases0 = new Float32Array(particleCount);
    const sizes = new Float32Array(particleCount);
    for (let index = 0; index < particleCount; index += 1) {
      const a = 1.7 + Math.random() * 1.9;
      const b = a * (0.32 + Math.random() * 0.38);
      // ~40% of particles align to a randomly-picked orbit ring's tilt instead of a fully
      // random plane, visually tying the dust field to the new 3D orbits above.
      const [u, v]: [THREE.Vector3, THREE.Vector3] = Math.random() < 0.4
        ? (() => {
            const orbitConfig = ORBIT_CONFIGS[Math.floor(Math.random() * ORBIT_CONFIGS.length)];
            const tilt = new THREE.Euler(orbitConfig.tiltX, 0, orbitConfig.tiltZ);
            return [new THREE.Vector3(1, 0, 0).applyEuler(tilt), new THREE.Vector3(0, 0, 1).applyEuler(tilt)];
          })()
        : randomOrthonormalPair();
      basisA[index * 3] = u.x * a;
      basisA[index * 3 + 1] = u.y * a;
      basisA[index * 3 + 2] = u.z * a;
      basisB[index * 3] = v.x * b;
      basisB[index * 3 + 1] = v.y * b;
      basisB[index * 3 + 2] = v.z * b;
      speeds[index] = (0.04 + Math.random() * 0.1) * (Math.random() < 0.5 ? 1 : -1);
      phases0[index] = Math.random() * Math.PI * 2;
      sizes[index] = 0.5 + Math.random() * 0.9;
    }
    const particleGeometry = new THREE.BufferGeometry();
    particleGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(particleCount * 3), 3));
    particleGeometry.setAttribute('aBasisA', new THREE.BufferAttribute(basisA, 3));
    particleGeometry.setAttribute('aBasisB', new THREE.BufferAttribute(basisB, 3));
    particleGeometry.setAttribute('aSpeed', new THREE.BufferAttribute(speeds, 1));
    particleGeometry.setAttribute('aPhase0', new THREE.BufferAttribute(phases0, 1));
    particleGeometry.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
    disposables.push(particleGeometry);
    const particleUniforms = {
      uTime: { value: 0 },
      uAmp: { value: 0 },
      uSpeedMul: { value: 1 },
      uColor: { value: new THREE.Color(0.4, 0.82, 1) },
    };
    const particleMaterial = new THREE.ShaderMaterial({
      uniforms: particleUniforms,
      vertexShader: PARTICLE_VERTEX_SHADER,
      fragmentShader: PARTICLE_FRAGMENT_SHADER,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    disposables.push(particleMaterial);
    const particles = new THREE.Points(particleGeometry, particleMaterial);
    scene.add(particles);

    // ── Neural mesh: point cloud + link graph + signal pulses ─────────────────
    // Built once (spatial-hash kNN in vexaNeuralMesh.ts), then only re-tinted and
    // rotated. The pulse positions are the single buffer updated per frame, and they
    // are written in place — no allocation inside the loop.
    const mesh = buildNeuralMesh({
      pointCount: quality.neuralPointCount,
      linksPerNode: quality.linksPerNode,
    });

    const neuralUniforms = {
      uTime: { value: 0 },
      uAmp: { value: 0 },
      uEnergy: { value: 0.4 },
      uBoot: { value: 0 },
      uColor: { value: new THREE.Color(0.45, 0.82, 1) },
    };
    const neuralGeometry = new THREE.BufferGeometry();
    neuralGeometry.setAttribute('position', new THREE.BufferAttribute(mesh.positions, 3));
    neuralGeometry.setAttribute('aSize', new THREE.BufferAttribute(mesh.sizes, 1));
    neuralGeometry.setAttribute('aSeed', new THREE.BufferAttribute(mesh.seeds, 1));
    disposables.push(neuralGeometry);
    const neuralMaterial = new THREE.ShaderMaterial({
      uniforms: neuralUniforms,
      vertexShader: NEURAL_POINT_VERTEX_SHADER,
      fragmentShader: NEURAL_POINT_FRAGMENT_SHADER,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    disposables.push(neuralMaterial);
    const neuralPoints = new THREE.Points(neuralGeometry, neuralMaterial);
    scene.add(neuralPoints);

    const linkUniforms = {
      uTime: { value: 0 },
      uAmp: { value: 0 },
      uEnergy: { value: 0.4 },
      uBoot: { value: 0 },
      uColor: { value: new THREE.Color(0.35, 0.78, 1) },
      uColorFar: { value: new THREE.Color(0.32, 0.28, 0.72) },
    };
    const linkGeometry = new THREE.BufferGeometry();
    linkGeometry.setAttribute('position', new THREE.BufferAttribute(mesh.linkPositions, 3));
    linkGeometry.setAttribute('aDepth', new THREE.BufferAttribute(mesh.linkDepths, 1));
    linkGeometry.setAttribute('aSeed', new THREE.BufferAttribute(mesh.linkSeeds, 1));
    disposables.push(linkGeometry);
    const linkMaterial = new THREE.ShaderMaterial({
      uniforms: linkUniforms,
      vertexShader: NEURAL_LINK_VERTEX_SHADER,
      fragmentShader: NEURAL_LINK_FRAGMENT_SHADER,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    disposables.push(linkMaterial);
    const links = new THREE.LineSegments(linkGeometry, linkMaterial);
    scene.add(links);

    // Signal pulses ride the existing edges. Edge assignment, progress and speed live in
    // typed arrays; only `pulsePositions` is uploaded each frame.
    const pulseCount = mesh.linkCount > 0 ? Math.min(quality.pulseCount, mesh.linkCount) : 0;
    const pulseEdges = new Uint32Array(pulseCount);
    const pulseProgress = new Float32Array(pulseCount);
    const pulseSpeeds = new Float32Array(pulseCount);
    const pulsePositions = new Float32Array(pulseCount * 3);
    const pulseSizes = new Float32Array(pulseCount);
    const pulseSeeds = new Float32Array(pulseCount);
    for (let index = 0; index < pulseCount; index += 1) {
      pulseEdges[index] = Math.floor(Math.random() * mesh.linkCount);
      pulseProgress[index] = Math.random();
      pulseSpeeds[index] = 0.35 + Math.random() * 0.75;
      pulseSizes[index] = 1.1 + Math.random() * 0.9;
      pulseSeeds[index] = Math.random();
    }
    const pulseGeometry = new THREE.BufferGeometry();
    pulseGeometry.setAttribute('position', new THREE.BufferAttribute(pulsePositions, 3));
    pulseGeometry.setAttribute('aSize', new THREE.BufferAttribute(pulseSizes, 1));
    pulseGeometry.setAttribute('aSeed', new THREE.BufferAttribute(pulseSeeds, 1));
    disposables.push(pulseGeometry);
    const signalUniforms = {
      uTime: { value: 0 },
      uAmp: { value: 0 },
      uEnergy: { value: 0.9 },
      uBoot: { value: 1 },
      uColor: { value: new THREE.Color(0.7, 0.95, 1) },
    };
    const signalMaterial = new THREE.ShaderMaterial({
      uniforms: signalUniforms,
      vertexShader: NEURAL_POINT_VERTEX_SHADER,
      fragmentShader: NEURAL_POINT_FRAGMENT_SHADER,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    disposables.push(signalMaterial);
    const signals = new THREE.Points(pulseGeometry, signalMaterial);
    if (pulseCount > 0) scene.add(signals);

    let bloomRig: VexaBloomRig | null = null;
    const resize = () => {
      const rect = mount.getBoundingClientRect();
      const width = Math.max(1, rect.width);
      const height = Math.max(1, rect.height);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      bloomRig?.setSize(width, height);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(mount);
    resize();

    if (BLOOM_ENABLED && quality.bloomEnabled) {
      const rect = mount.getBoundingClientRect();
      bloomRig = createVexaBloomRig(renderer, scene, camera, Math.max(1, rect.width), Math.max(1, rect.height));
    }

    const clock = new THREE.Clock();
    const targetColor = new THREE.Color();
    let frameId = 0;
    let frame = 0;
    let readySignalled = false;
    let smoothedAmp = 0;

    const render = () => {
      const currentPhase = phaseRef.current;
      audioAnalyser.sync(currentPhase);
      const amp = audioAnalyser.read(currentPhase);
      const t = clock.getElapsedTime();
      const now = performance.now();
      // Smoothed separately from the raw `amp` used for brightness/displacement below —
      // this one only drives accumulated rotation deltas, so a single-frame FFT spike
      // can't jolt the orbit/filament spin.
      smoothedAmp += (amp - smoothedAmp) * 0.15;

      // Brief decaying spike from an active error burst — only affects the vortex/
      // particle energy, not orbits/rings/filaments, so it reads as a jolt at the core
      // rather than a full-scene state change.
      let errorBoost = 0;
      errorPulsesRef.current.forEach(pulse => {
        const elapsed = now - pulse.start;
        if (elapsed < 600) errorBoost = Math.max(errorBoost, (1 - elapsed / 600) * 0.5);
      });

      const speedMul = currentPhase === 'listening' ? 1.4 : currentPhase === 'thinking' ? 0.9 : currentPhase === 'speaking' ? 1.15 : 0.55;
      const baseEnergy = currentPhase === 'offline' ? 0.15 : currentPhase === 'ready' ? 0.4 : 0.8;
      const energy = Math.min(1.2, baseEnergy + amp * 0.6);
      const [r, g, b] = PHASE_COLOR[currentPhase] ?? PHASE_COLOR.ready;
      targetColor.setRGB(r, g, b);

      vortexUniforms.uTime.value = t;
      vortexUniforms.uAmp.value = amp;
      vortexUniforms.uEnergy.value = energy + errorBoost;
      (vortexUniforms.uColor.value as THREE.Color).lerp(targetColor, 0.05);
      vortex.scale.setScalar(1 + amp * 0.05);

      rings.forEach(({ mesh, uniforms, config }) => {
        uniforms.uEnergy.value = energy;
        (uniforms.uColor.value as THREE.Color).lerp(targetColor, 0.05);
        if (!reduceMotion) mesh.rotation.z += config.dir * config.speed * speedMul * 0.016;
      });

      orbits.forEach(({ mesh, uniforms, config }) => {
        uniforms.uTime.value = t;
        uniforms.uEnergy.value = energy;
        uniforms.uAmp.value = amp;
        (uniforms.uColor.value as THREE.Color).lerp(targetColor, 0.05);
        if (!reduceMotion) mesh.rotation.y += config.dir * config.speed * speedMul * (1 + smoothedAmp * 0.4) * 0.016;
      });

      filaments.forEach(({ mesh, uniforms, config }) => {
        uniforms.uTime.value = t;
        uniforms.uEnergy.value = energy;
        uniforms.uAmp.value = amp;
        (uniforms.uColor.value as THREE.Color).lerp(targetColor, 0.05);
        if (!reduceMotion) mesh.rotation.y += config.driftSpeed * speedMul * (1 + smoothedAmp * 0.3) * 0.016;
      });

      particleUniforms.uTime.value = t;
      particleUniforms.uAmp.value = amp + errorBoost * 0.6;
      particleUniforms.uSpeedMul.value = speedMul * (1 + amp * 0.25);
      (particleUniforms.uColor.value as THREE.Color).lerp(targetColor, 0.04);

      // Boot-in: the mesh assembles from the centre over ~1.4s on first paint.
      const boot = Math.min(1, t / 1.4);
      const eased = boot * boot * (3 - 2 * boot);

      neuralUniforms.uTime.value = t;
      neuralUniforms.uAmp.value = amp;
      neuralUniforms.uEnergy.value = energy;
      neuralUniforms.uBoot.value = eased;
      (neuralUniforms.uColor.value as THREE.Color).lerp(targetColor, 0.05);

      linkUniforms.uTime.value = t;
      linkUniforms.uAmp.value = amp;
      linkUniforms.uEnergy.value = energy;
      linkUniforms.uBoot.value = eased;
      (linkUniforms.uColor.value as THREE.Color).lerp(targetColor, 0.05);

      if (!reduceMotion) {
        const spin = 0.028 * speedMul * (1 + smoothedAmp * 0.3) * 0.016;
        neuralPoints.rotation.y += spin;
        links.rotation.y = neuralPoints.rotation.y;
        signals.rotation.y = neuralPoints.rotation.y;
      }

      // Advance the signal pulses along their edges and write the new positions in place.
      if (pulseCount > 0) {
        const step = 0.016 * speedMul * (1 + amp * 0.6);
        for (let index = 0; index < pulseCount; index += 1) {
          let progress = pulseProgress[index] + pulseSpeeds[index] * step;
          if (progress >= 1) {
            progress -= 1;
            // Re-seed onto another edge so the traffic pattern keeps changing without
            // ever rebuilding the graph.
            pulseEdges[index] = Math.floor(Math.random() * mesh.linkCount);
          }
          pulseProgress[index] = progress;
          const edge = pulseEdges[index] * 6;
          const ax = mesh.linkPositions[edge];
          const ay = mesh.linkPositions[edge + 1];
          const az = mesh.linkPositions[edge + 2];
          pulsePositions[index * 3] = ax + (mesh.linkPositions[edge + 3] - ax) * progress;
          pulsePositions[index * 3 + 1] = ay + (mesh.linkPositions[edge + 4] - ay) * progress;
          pulsePositions[index * 3 + 2] = az + (mesh.linkPositions[edge + 5] - az) * progress;
        }
        pulseGeometry.attributes.position.needsUpdate = true;
        signalUniforms.uTime.value = t;
        signalUniforms.uAmp.value = amp;
        signalUniforms.uEnergy.value = Math.min(1.2, energy + 0.3);
        signalUniforms.uBoot.value = eased;
        (signalUniforms.uColor.value as THREE.Color).lerp(targetColor, 0.03);
      }

      pulsesRef.current = pulsesRef.current.filter(pulse => now - pulse.start < 1200);
      pulsesRef.current.forEach(pulse => {
        if (pulse.claimed) return;
        const freeSlot = pulsePool.find(slot => !slot.active);
        if (freeSlot) {
          freeSlot.active = true;
          freeSlot.start = pulse.start;
          freeSlot.mesh.visible = true;
        }
        pulse.claimed = true;
      });
      pulsePool.forEach(slot => {
        if (!slot.active) return;
        const elapsed = now - slot.start;
        const progress = Math.min(1, elapsed / 1200);
        if (progress >= 1) {
          slot.active = false;
          slot.mesh.visible = false;
          return;
        }
        slot.uniforms.uRadius.value = 0.15 + progress * 0.95;
        slot.uniforms.uWidth.value = 0.012 * (1 - progress * 0.4);
        slot.uniforms.uOpacity.value = (1 - progress) * 0.6;
        (slot.uniforms.uColor.value as THREE.Color).lerp(targetColor, 0.1);
      });

      errorPulsesRef.current = errorPulsesRef.current.filter(pulse => now - pulse.start < 900);
      errorPulsesRef.current.forEach(pulse => {
        if (pulse.claimed) return;
        const freeSlot = errorPulsePool.find(slot => !slot.active);
        if (freeSlot) {
          freeSlot.active = true;
          freeSlot.start = pulse.start;
          freeSlot.mesh.visible = true;
        }
        pulse.claimed = true;
      });
      errorPulsePool.forEach(slot => {
        if (!slot.active) return;
        const elapsed = now - slot.start;
        const progress = Math.min(1, elapsed / 900);
        if (progress >= 1) {
          slot.active = false;
          slot.mesh.visible = false;
          return;
        }
        // Fixed warning color — deliberately not lerped toward targetColor, so it always
        // reads as amber/red regardless of the current phase's hue.
        slot.uniforms.uRadius.value = 0.12 + progress * 0.75;
        slot.uniforms.uWidth.value = 0.016 * (1 - progress * 0.3);
        slot.uniforms.uOpacity.value = (1 - progress) * 0.75;
      });

      if (bloomRig) bloomRig.render(); else renderer.render(scene, camera);
      frame += 1;
      if (!readySignalled && frame >= 2) {
        readySignalled = true;
        onReadyRef.current();
      }
      if (!reduceMotion || frame < 3) frameId = requestAnimationFrame(render);
    };
    frameId = requestAnimationFrame(render);

    return () => {
      cancelAnimationFrame(frameId);
      observer.disconnect();
      bloomRig?.dispose();
      renderer.dispose();
      disposables.forEach(item => item.dispose());
      if (renderer.domElement.parentElement === mount) mount.removeChild(renderer.domElement);
    };
  }, [audioAnalyser]);

  return <div ref={mountRef} className="vexa-energy-core" aria-hidden="true" />;
}
