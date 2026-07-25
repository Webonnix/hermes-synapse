export interface VexaQualityPreset {
  particleCount: number;
  orbitCount: number;
  filamentCount: number;
  dprCap: number;
  bloomEnabled: boolean;
  reduceMotion: boolean;
}

// Desktop/mobile detection uses a max-width `window.matchMedia` check.
export function getVexaQualityPreset(): VexaQualityPreset {
  const reduceMotion = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduceMotion) {
    return { particleCount: 90, orbitCount: 3, filamentCount: 0, dprCap: 1.5, bloomEnabled: false, reduceMotion: true };
  }

  const isNarrow = typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 768px)').matches;
  const lowCores = (navigator.hardwareConcurrency || 4) <= 4;
  if (isNarrow || lowCores) {
    return { particleCount: 160, orbitCount: 4, filamentCount: 2, dprCap: 1.5, bloomEnabled: false, reduceMotion: false };
  }

  return { particleCount: 320, orbitCount: 6, filamentCount: 4, dprCap: 2, bloomEnabled: true, reduceMotion: false };
}
