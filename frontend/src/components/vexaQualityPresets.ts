export interface VexaQualityPreset {
  particleCount: number;
  orbitCount: number;
  filamentCount: number;
  /** Nodes in the neural point cloud around the core. */
  neuralPointCount: number;
  /** Neighbours each seed node links to when the mesh graph is built. */
  linksPerNode: number;
  /** Signal pulses travelling along the links at once. */
  pulseCount: number;
  dprCap: number;
  bloomEnabled: boolean;
  reduceMotion: boolean;
}

// Desktop/mobile detection uses a max-width `window.matchMedia` check.
export function getVexaQualityPreset(): VexaQualityPreset {
  const reduceMotion = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion) {
    return {
      particleCount: 90, orbitCount: 7, filamentCount: 0,
      neuralPointCount: 320, linksPerNode: 2, pulseCount: 8,
      dprCap: 1.5, bloomEnabled: false, reduceMotion: true,
    };
  }

  const isNarrow = typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 768px)').matches;
  const lowCores = (navigator.hardwareConcurrency || 4) <= 4;
  if (isNarrow || lowCores) {
    return {
      particleCount: 160, orbitCount: 10, filamentCount: 2,
      neuralPointCount: 900, linksPerNode: 3, pulseCount: 24,
      dprCap: 1.5, bloomEnabled: false, reduceMotion: false,
    };
  }

  return {
    particleCount: 320, orbitCount: 18, filamentCount: 4,
    neuralPointCount: 2400, linksPerNode: 4, pulseCount: 56,
    dprCap: 2, bloomEnabled: true, reduceMotion: false,
  };
}
