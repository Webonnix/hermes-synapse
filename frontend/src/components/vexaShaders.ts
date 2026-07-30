// GLSL source for VexaEnergyCore, extracted out of the component file so its
// scene/lifecycle logic isn't buried in string literals. Pure data — no behavior here.

// A flat, camera-facing unit quad shared by every HUD element (vortex, rings, pulses).
// All shader math below works in normalized UV space (-1..1); world-space size comes
// from each mesh's own `scale`, keeping one geometry reusable everywhere. Also reused
// as-is for the 3D orbit/filament geometry (torus/tube UVs), since it just forwards
// `uv` and MVP-transforms `position` — it doesn't assume a flat quad.
export const PASSTHROUGH_VERTEX_SHADER = `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;

export const VORTEX_FRAGMENT_SHADER = `
uniform float uTime;
uniform float uAmp;
uniform float uEnergy;
uniform vec3 uColor;
varying vec2 vUv;

// The core's own light: a compact white-hot kernel, a tight containment ring around it and
// a soft halo falling off into the node cloud. Deliberately radially symmetric — an
// earlier spiral-armed version read as a flat swirl rather than a lit sphere, which is not
// what the reference composition shows.
void main() {
  vec2 uv = vUv * 2.0 - 1.0;
  float r = length(uv);

  float breathe = 1.0 + sin(uTime * 1.1) * 0.03 + uAmp * 0.12;

  // White-hot centre. Two stacked falloffs: a tiny saturated core plus a wider bleed, so
  // it stays a point of light instead of a flat disc.
  float kernel = smoothstep(0.075 * breathe, 0.0, r);
  float bleed = smoothstep(0.17 * breathe, 0.02, r) * 0.42;

  // Thin containment ring just outside the kernel.
  float ringRadius = 0.145 * breathe;
  float ring = smoothstep(0.012, 0.0, abs(r - ringRadius)) * (0.45 + uEnergy * 0.5);

  // Soft halo bridging the kernel and the surrounding point cloud.
  float halo = smoothstep(0.62, 0.05, r) * (0.1 + uEnergy * 0.16);
  float ambient = smoothstep(1.0, 0.1, r) * 0.05 * (0.5 + uEnergy * 0.5);

  float alpha = clamp(kernel + bleed + ring + halo + ambient, 0.0, 1.0);
  if (alpha < 0.012) discard;

  // Toward white at the centre, toward the phase colour further out.
  vec3 hot = mix(uColor, vec3(1.0), clamp(kernel * 1.1 + bleed * 0.55, 0.0, 1.0));
  vec3 color = hot * (0.6 + uEnergy * 0.5 + kernel * 1.6 + ring * 0.8);
  gl_FragColor = vec4(color, alpha);
}`;

export const RING_FRAGMENT_SHADER = `
uniform float uRadius;
uniform float uWidth;
uniform float uTicks;
uniform float uVariant;
uniform float uEnergy;
uniform float uOpacity;
uniform vec3 uColor;
varying vec2 vUv;

void main() {
  vec2 uv = vUv * 2.0 - 1.0;
  float r = length(uv);
  float theta = atan(uv.y, uv.x);
  float angle01 = theta / 6.28318530718 + 0.5;

  float thinBand = smoothstep(uWidth, uWidth * 0.15, abs(r - uRadius));
  float mask;
  float pattern;

  if (uVariant < -0.5) {
    mask = thinBand;
    pattern = 1.0;
  } else if (uVariant < 0.5) {
    mask = thinBand;
    pattern = step(0.5, fract(angle01 * uTicks));
  } else if (uVariant < 1.5) {
    mask = thinBand;
    pattern = step(0.82, fract(angle01 * uTicks));
  } else if (uVariant < 2.5) {
    mask = thinBand;
    float d = fract(angle01 * uTicks);
    pattern = step(0.9, d) * step(d, 0.97);
  } else {
    float spokeBand = smoothstep(uWidth * 4.5, uWidth * 0.5, abs(r - uRadius));
    float spokeMask = step(0.985, fract(angle01 * uTicks));
    mask = max(thinBand, spokeBand * spokeMask);
    pattern = max(thinBand * 0.4, spokeMask);
  }

  if (mask < 0.02) discard;
  float alpha = mask * mix(0.08, 1.0, pattern) * uOpacity * (0.5 + uEnergy * 0.4);
  if (alpha < 0.012) discard;
  gl_FragColor = vec4(uColor, alpha);
}`;

// Traveling comet-tail dash around a tilted torus (real 3D orbit ring, unlike the flat
// camera-facing RING_FRAGMENT_SHADER above). Uses the torus's own UV: x wraps once around
// the main ring, y wraps once around the tube's cross-section.
export const ORBIT_FRAGMENT_SHADER = `
uniform float uTime;
uniform float uEnergy;
uniform float uAmp;
uniform float uOpacity;
uniform float uDashCount;
uniform float uDashSpeed;
uniform vec3 uColor;
varying vec2 vUv;

void main() {
  float travel = fract(vUv.x * uDashCount - uTime * uDashSpeed);
  float dash = smoothstep(0.0, 0.08, travel) * smoothstep(0.85, 0.35, travel);
  float rim = smoothstep(0.5, 0.0, abs(vUv.y - 0.5));
  float alpha = (0.05 + dash * 1.1 * rim) * uOpacity * (0.5 + uEnergy * 0.4 + uAmp * 0.1);
  if (alpha < 0.01) discard;
  vec3 color = uColor * (0.7 + dash * 1.3);
  gl_FragColor = vec4(color, clamp(alpha, 0.0, 1.0));
}`;

// Traveling pulse along a TubeGeometry filament. Uses the tube's own UV: x runs along
// the curve's length, y wraps once around the tube's cross-section.
export const FILAMENT_FRAGMENT_SHADER = `
uniform float uTime;
uniform float uEnergy;
uniform float uAmp;
uniform float uOpacity;
uniform float uFlowSpeed;
uniform vec3 uColor;
varying vec2 vUv;

void main() {
  float flow = fract(vUv.x * 2.5 - uTime * uFlowSpeed);
  float pulse = smoothstep(0.0, 0.18, flow) * smoothstep(0.55, 0.18, flow);
  float rim = smoothstep(0.5, 0.0, abs(vUv.y - 0.5));
  float alpha = (0.06 + pulse * 0.9 * rim) * uOpacity * (0.5 + uEnergy * 0.4) * (0.6 + uAmp * 0.5);
  if (alpha < 0.01) discard;
  vec3 color = uColor * (0.75 + pulse);
  gl_FragColor = vec4(color, clamp(alpha, 0.0, 1.0));
}`;

export const PARTICLE_VERTEX_SHADER = `
uniform float uTime;
uniform float uAmp;
uniform float uSpeedMul;
attribute vec3 aBasisA;
attribute vec3 aBasisB;
attribute float aSpeed;
attribute float aPhase0;
attribute float aSize;
varying float vAlpha;

void main() {
  float angle = aPhase0 + uTime * aSpeed * uSpeedMul;
  vec3 pos = aBasisA * cos(angle) + aBasisB * sin(angle);
  vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
  gl_PointSize = aSize * (9.0 / -mvPosition.z) * (1.0 + uAmp * 0.35);
  vAlpha = 0.14 + 0.16 * (sin(uTime * 1.4 + aPhase0) * 0.5 + 0.5);
  gl_Position = projectionMatrix * mvPosition;
}`;

export const PARTICLE_FRAGMENT_SHADER = `
uniform vec3 uColor;
varying float vAlpha;
void main() {
  vec2 uv = gl_PointCoord - 0.5;
  float d = length(uv);
  if (d > 0.5) discard;
  float core = smoothstep(0.5, 0.14, d);
  gl_FragColor = vec4(uColor, core * vAlpha);
}`;

// ── Neural mesh ──────────────────────────────────────────────────────────────
// A static point cloud filling a spherical shell around the core. Positions are baked
// into the buffer once; the shader only breathes them in and out and modulates
// brightness, so no per-frame CPU work touches the geometry.
export const NEURAL_POINT_VERTEX_SHADER = `
uniform float uTime;
uniform float uAmp;
uniform float uEnergy;
uniform float uBoot;
attribute float aSize;
attribute float aSeed;
attribute float aTint;
varying float vAlpha;
varying float vTint;

void main() {
  vTint = aTint;
  float breathe = 1.0 + sin(uTime * 0.5 + aSeed * 6.283) * 0.018 + uAmp * 0.05;
  vec3 pos = position * breathe * uBoot;
  vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
  gl_PointSize = aSize * (13.5 / -mvPosition.z) * (1.0 + uAmp * 0.4);
  float twinkle = 0.55 + 0.45 * sin(uTime * 1.7 + aSeed * 12.0);
  vAlpha = (0.3 + 0.7 * twinkle) * (0.55 + uEnergy * 0.6) * uBoot;
  gl_Position = projectionMatrix * mvPosition;
}`;

export const NEURAL_POINT_FRAGMENT_SHADER = `
uniform vec3 uColor;
uniform vec3 uColorAlt;
varying float vAlpha;
varying float vTint;
void main() {
  vec2 uv = gl_PointCoord - 0.5;
  float d = length(uv);
  if (d > 0.5) discard;
  float core = smoothstep(0.5, 0.08, d);
  vec3 tinted = mix(uColor, uColorAlt, vTint);
  gl_FragColor = vec4(tinted + vec3(core * 0.4), core * vAlpha);
}`;

// Link segments between neighbouring nodes. `aDepth` is the midpoint's normalised
// distance from the core, so links nearer the centre read brighter — the depth cue the
// reference image relies on.
export const NEURAL_LINK_VERTEX_SHADER = `
uniform float uTime;
uniform float uAmp;
uniform float uBoot;
attribute float aDepth;
attribute float aSeed;
varying float vDepth;
varying float vSeed;

void main() {
  vec3 pos = position * (1.0 + uAmp * 0.04) * uBoot;
  vDepth = aDepth;
  vSeed = aSeed;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
}`;

export const NEURAL_LINK_FRAGMENT_SHADER = `
uniform float uTime;
uniform float uEnergy;
uniform float uBoot;
uniform vec3 uColor;
uniform vec3 uColorFar;
varying float vDepth;
varying float vSeed;

void main() {
  float near = 1.0 - vDepth;
  // Slow per-link shimmer so the mesh never looks like a frozen wireframe.
  float flicker = 0.72 + 0.28 * sin(uTime * 0.9 + vSeed * 21.0);
  float alpha = (0.18 + near * 0.78) * (0.6 + uEnergy * 0.8) * flicker * uBoot;
  if (alpha < 0.006) discard;
  gl_FragColor = vec4(mix(uColorFar, uColor, near), alpha);
}`;
