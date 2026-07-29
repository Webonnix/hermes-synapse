/**
 * Builds the neural point cloud and its link graph for the Vexa energy core.
 *
 * Runs exactly once per scene, off the animation loop. The neighbour search uses a
 * uniform spatial hash rather than a pairwise scan, so 2000 nodes cost O(n·k) instead of
 * O(n²), and everything lands in typed arrays that go straight into BufferGeometry.
 */

export interface NeuralMeshData {
  /** xyz per node. */
  positions: Float32Array;
  sizes: Float32Array;
  seeds: Float32Array;
  /** Two endpoints per link, flattened as xyzxyz. */
  linkPositions: Float32Array;
  /** Per-vertex normalised distance of the link's midpoint from the core (0 = centre). */
  linkDepths: Float32Array;
  linkSeeds: Float32Array;
  linkCount: number;
  /** Endpoint indices per link, used to animate pulses along an edge. */
  linkEndpoints: Uint32Array;
}

interface MeshOptions {
  pointCount: number;
  linksPerNode: number;
  minRadius: number;
  maxRadius: number;
  /** Fraction of nodes that seed links; the rest are pure "dust" in the shell. */
  linkedFraction: number;
  /** Links longer than this are dropped so the mesh doesn't turn into a spider web. */
  maxLinkLength: number;
}

const DEFAULTS: Omit<MeshOptions, 'pointCount' | 'linksPerNode'> = {
  minRadius: 0.35,
  maxRadius: 1.85,
  linkedFraction: 0.42,
  maxLinkLength: 0.42,
};

/** Deterministic PRNG so a reload produces the same core rather than reshuffling it. */
function createRandom(seed: number) {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

export function buildNeuralMesh(options: Pick<MeshOptions, 'pointCount' | 'linksPerNode'>): NeuralMeshData {
  const config: MeshOptions = { ...DEFAULTS, ...options };
  const { pointCount, linksPerNode, minRadius, maxRadius, maxLinkLength } = config;
  const random = createRandom(0x5eed);

  const positions = new Float32Array(pointCount * 3);
  const sizes = new Float32Array(pointCount);
  const seeds = new Float32Array(pointCount);

  for (let index = 0; index < pointCount; index += 1) {
    // Uniform direction on the sphere, then a radius biased outward so the shell reads
    // denser than the space immediately around the core.
    const u = random() * 2 - 1;
    const theta = random() * Math.PI * 2;
    const planar = Math.sqrt(Math.max(0, 1 - u * u));
    const radius = minRadius + Math.pow(random(), 0.65) * (maxRadius - minRadius);
    positions[index * 3] = planar * Math.cos(theta) * radius;
    positions[index * 3 + 1] = planar * Math.sin(theta) * radius;
    positions[index * 3 + 2] = u * radius;
    sizes[index] = 0.45 + random() * 1.15;
    seeds[index] = random();
  }

  // ── spatial hash ──────────────────────────────────────────────────────────
  const cell = maxLinkLength;
  const buckets = new Map<number, number[]>();
  const key = (x: number, y: number, z: number) =>
    ((Math.floor(x / cell) + 512) * 1024 + (Math.floor(y / cell) + 512)) * 1024 + (Math.floor(z / cell) + 512);

  for (let index = 0; index < pointCount; index += 1) {
    const hash = key(positions[index * 3], positions[index * 3 + 1], positions[index * 3 + 2]);
    const bucket = buckets.get(hash);
    if (bucket) bucket.push(index);
    else buckets.set(hash, [index]);
  }

  const seedCount = Math.max(1, Math.floor(pointCount * config.linkedFraction));
  const maxLinks = seedCount * linksPerNode;
  const linkPositions = new Float32Array(maxLinks * 6);
  const linkDepths = new Float32Array(maxLinks * 2);
  const linkSeeds = new Float32Array(maxLinks * 2);
  const linkEndpoints = new Uint32Array(maxLinks * 2);

  const candidates: Array<{ index: number; distance: number }> = [];
  const seen = new Set<number>();
  let linkCount = 0;

  for (let node = 0; node < seedCount; node += 1) {
    const ax = positions[node * 3];
    const ay = positions[node * 3 + 1];
    const az = positions[node * 3 + 2];
    candidates.length = 0;

    const cx = Math.floor(ax / cell);
    const cy = Math.floor(ay / cell);
    const cz = Math.floor(az / cell);
    for (let dx = -1; dx <= 1; dx += 1) {
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dz = -1; dz <= 1; dz += 1) {
          const bucket = buckets.get((((cx + dx) + 512) * 1024 + ((cy + dy) + 512)) * 1024 + ((cz + dz) + 512));
          if (!bucket) continue;
          for (const other of bucket) {
            if (other === node) continue;
            const bx = positions[other * 3] - ax;
            const by = positions[other * 3 + 1] - ay;
            const bz = positions[other * 3 + 2] - az;
            const distance = Math.sqrt(bx * bx + by * by + bz * bz);
            if (distance > maxLinkLength) continue;
            candidates.push({ index: other, distance });
          }
        }
      }
    }

    candidates.sort((left, right) => left.distance - right.distance);
    for (let picked = 0; picked < Math.min(linksPerNode, candidates.length); picked += 1) {
      const other = candidates[picked].index;
      // Undirected: skip a pair already linked from the other end.
      const pairKey = node < other ? node * pointCount + other : other * pointCount + node;
      if (seen.has(pairKey)) continue;
      seen.add(pairKey);

      const offset = linkCount * 6;
      linkPositions[offset] = ax;
      linkPositions[offset + 1] = ay;
      linkPositions[offset + 2] = az;
      linkPositions[offset + 3] = positions[other * 3];
      linkPositions[offset + 4] = positions[other * 3 + 1];
      linkPositions[offset + 5] = positions[other * 3 + 2];

      const midX = (ax + positions[other * 3]) / 2;
      const midY = (ay + positions[other * 3 + 1]) / 2;
      const midZ = (az + positions[other * 3 + 2]) / 2;
      const depth = Math.min(1, Math.sqrt(midX * midX + midY * midY + midZ * midZ) / maxRadius);
      linkDepths[linkCount * 2] = depth;
      linkDepths[linkCount * 2 + 1] = depth;
      const linkSeed = seeds[node];
      linkSeeds[linkCount * 2] = linkSeed;
      linkSeeds[linkCount * 2 + 1] = linkSeed;
      linkEndpoints[linkCount * 2] = node;
      linkEndpoints[linkCount * 2 + 1] = other;
      linkCount += 1;
    }
  }

  return {
    positions,
    sizes,
    seeds,
    linkPositions: linkPositions.subarray(0, linkCount * 6),
    linkDepths: linkDepths.subarray(0, linkCount * 2),
    linkSeeds: linkSeeds.subarray(0, linkCount * 2),
    linkCount,
    linkEndpoints: linkEndpoints.subarray(0, linkCount * 2),
  };
}
