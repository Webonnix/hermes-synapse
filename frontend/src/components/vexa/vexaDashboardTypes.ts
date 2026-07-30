/**
 * View-model types for the VEXA dashboard.
 *
 * These describe what the UI renders, not what the backend happens to return today —
 * `useVexaTelemetry` adapts Hermes' existing `/api/system/stats`, `/api/metrics` and
 * `/api/control-plane/summary` payloads into them. Every numeric field is nullable on
 * purpose: an unavailable metric must render as "нет данных", never as 0.
 */

export type GlobalSystemState =
  | 'online'
  | 'degraded'
  | 'maintenance'
  | 'offline'
  | 'emergency_stopped';

export interface SystemOverview {
  circuit: { status: 'online' | 'degraded' | 'offline' };
  agentsTotal: number;
  agentsActive: number;
  voice: {
    mode: 'local' | 'remote';
    engine: string;
    voice: string;
    status: 'ready' | 'busy' | 'error' | 'offline';
  };
  resources: {
    cpu: number | null;
    memory: number | null;
    gpu: number | null;
    network: number | null;
  };
  uptimeSeconds: number | null;
  updatedAt: string | null;
  stale: boolean;
}

export type ProtocolStatus = 'running' | 'standby' | 'paused' | 'error' | 'offline';

export interface ProtocolInfo {
  id: string;
  label: string;
  status: ProtocolStatus;
  version?: string;
  detail?: string;
  latencyMs?: number | null;
  lastError?: string | null;
}

export interface DataStreamRow {
  id: string;
  /** Display label, e.g. "DATA-01". */
  label: string;
  /** What the channel actually measures, surfaced in the row's tooltip. */
  channel: string;
  value: number | null;
  unit?: string;
  updatedAt: number;
}

export interface InsightMetric {
  id: string;
  label: string;
  value: number | null;
  formattedValue: string | null;
  min: number;
  max: number;
  status: 'normal' | 'warning' | 'critical' | 'unknown';
  updatedAt: string | null;
}

export interface CoreHudData {
  processLabel: string;
  coreStatus: string;
  linkStrength: number | null;
  thoughtFlow: 'idle' | 'receiving' | 'processing' | 'streaming';
}

/** Structured chart payload an assistant message may carry (validated at runtime). */
export interface LineChartWidget {
  type: 'line-chart';
  title?: string;
  xAxis: string[];
  series: Array<{
    name: string;
    data: number[];
    colorToken?: 'cyan' | 'blue' | 'violet' | 'green' | 'red';
  }>;
}

export type RiskLevel = 'low' | 'medium' | 'high' | 'critical';

export interface ConfirmationRequest {
  id: string;
  title: string;
  description: string;
  agentId: string;
  riskLevel: RiskLevel;
  requestedAt: string;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
}

export type DashboardRoute = 'terminal' | 'analytics' | 'agents' | 'protocols' | 'settings';

/**
 * Narrow runtime guard for a chart widget arriving over the WebSocket or embedded in a
 * message payload. Deliberately hand-written rather than a Zod schema: this is the only
 * externally-shaped structure the dashboard consumes, so a dependency isn't warranted.
 */
export function parseLineChartWidget(value: unknown): LineChartWidget | null {
  if (typeof value !== 'object' || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (raw.type !== 'line-chart') return null;
  if (!Array.isArray(raw.xAxis) || !raw.xAxis.every(item => typeof item === 'string')) return null;
  if (!Array.isArray(raw.series) || raw.series.length === 0 || raw.series.length > 8) return null;

  const series: LineChartWidget['series'] = [];
  for (const item of raw.series) {
    if (typeof item !== 'object' || item === null) return null;
    const entry = item as Record<string, unknown>;
    if (typeof entry.name !== 'string') return null;
    if (!Array.isArray(entry.data) || !entry.data.every(point => typeof point === 'number' && Number.isFinite(point))) return null;
    if (entry.data.length > 512) return null;
    const colorToken = typeof entry.colorToken === 'string'
      && ['cyan', 'blue', 'violet', 'green', 'red'].includes(entry.colorToken)
      ? entry.colorToken as NonNullable<LineChartWidget['series'][number]['colorToken']>
      : undefined;
    series.push({ name: entry.name, data: entry.data as number[], colorToken });
  }

  return {
    type: 'line-chart',
    title: typeof raw.title === 'string' ? raw.title : undefined,
    xAxis: raw.xAxis as string[],
    series,
  };
}
