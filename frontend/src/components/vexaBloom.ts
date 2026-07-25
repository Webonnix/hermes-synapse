import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';

export interface VexaBloomRig {
  render: () => void;
  setSize: (width: number, height: number) => void;
  dispose: () => void;
}

// Experimental, off by default (see BLOOM_ENABLED in VexaEnergyCore.tsx). Isolated in its
// own file so the empirical alpha-safety test can be reverted in one clean commit
// (delete this file + the ~6-line integration branch) if bloom breaks the transparent
// renderer's alpha channel again, as it did previously (see the comment block at the top
// of VexaEnergyCore.tsx). Uses three's own bundled postprocessing examples — no new npm
// dependency.
export function createVexaBloomRig(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.PerspectiveCamera,
  width: number,
  height: number,
): VexaBloomRig | null {
  try {
    const composer = new EffectComposer(renderer);
    composer.setSize(width, height);
    // clearAlpha=0 on the render pass so the first internal pass doesn't stomp
    // transparency before bloom even runs.
    const renderPass = new RenderPass(scene, camera, undefined, undefined, 0);
    composer.addPass(renderPass);
    const bloomPass = new UnrealBloomPass(new THREE.Vector2(width, height), 0.55, 0.4, 0.15);
    composer.addPass(bloomPass);
    return {
      render: () => composer.render(),
      setSize: (w: number, h: number) => composer.setSize(w, h),
      dispose: () => composer.dispose(),
    };
  } catch {
    return null;
  }
}
