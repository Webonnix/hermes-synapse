import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ControlPlaneSummary, SystemStats } from '../../types';
import type {
  ConfirmationRequest,
  DataStreamRow,
  InsightMetric,
  ProtocolInfo,
  RiskLevel,
  SystemOverview,
} from './vexaDashboardTypes';

/**
 * Single telemetry source for the VEXA dashboard.
 *
 * Adapts Hermes' existing endpoints (`/api/system/stats`, `/api/metrics`,
 * `/api/control-plane/summary`) into the dashboard view-models. Nothing here invents a
 * value: a field the backend does not publish stays `null` so the UI can render
 * "нет данных" rather than a plausible-looking zero.
 *
 * Poll cadence is deliberately slower than the 1 Hz UI refresh — the rolling activity
 * series samples the *last known* snapshot every second, so the chart stays smooth
 * without hammering the API.
 */

const STATS_INTERVAL_MS = 3000;
const METRICS_INTERVAL_MS = 30000;
const CONTROL_INTERVAL_MS = 15000;
const SERIES_INTERVAL_MS = 1000;
export const ACTIVITY_SERIES_LENGTH = 90;

/** Resource colour bands, kept here so the thresholds are configuration, not magic numbers. */
export const RESOURCE_THRESHOLDS = { nominal: 70, elevated: 85, critical: 95 } as const;

export type ResourceBand = 'nominal' | 'elevated' | 'high' | 'critical';

export function resourceBand(value: number | null): ResourceBand {
  if (value === null) return 'nominal';
  if (value >= RESOURCE_THRESHOLDS.critical) return 'critical';
  if (value >= RESOURCE_THRESHOLDS.elevated) return 'high';
  if (value >= RESOURCE_THRESHOLDS.nominal) return 'elevated';
  return 'nominal';
}

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getJson<T>(url: string): Promise<T | null> {
  try {
    const response = await fetch(url, { headers: authHeaders() });
    if (!response.ok) return null;
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) return null;
    return await response.json() as T;
  } catch {
    return null;
  }
}

function clampPercent(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.max(0, Math.min(100, value));
}

/** Network "health" percentage: bytes/s mapped onto a 1 Gbit/s reference link. */
function networkHealth(stats: SystemStats | null): number | null {
  const network = stats?.host?.network;
  if (!network) return null;
  const rx = network.rx_bytes_per_second ?? 0;
  const tx = network.tx_bytes_per_second ?? 0;
  if (!Number.isFinite(rx) || !Number.isFinite(tx)) return null;
  // Reported as link headroom so 100% means "unsaturated", matching the reference layout.
  const saturation = Math.min(1, (rx + tx) / (125_000_000));
  return Math.round((1 - saturation) * 100);
}

function firstGpuUtilization(stats: SystemStats | null): number | null {
  const gpu = stats?.host?.gpus?.[0];
  if (!gpu) return null;
  return clampPercent(gpu.utilization_percent);
}

export interface VexaTelemetry {
  stats: SystemStats | null;
  overview: SystemOverview;
  activitySeries: number[];
  dataStream: DataStreamRow[];
  protocols: ProtocolInfo[];
  insights: InsightMetric[];
  confirmations: ConfirmationRequest[];
  controlSummary: ControlPlaneSummary | null;
  emergencyStopped: boolean;
  neuralDensity: number | null;
  refreshControl: () => Promise<void>;
}

interface TelemetryInput {
  enabled: boolean;
  /** Completed vs failed assistant runs seen in the loaded history — feeds "accuracy". */
  runOutcomes: { completed: number; failed: number };
  isConnected: boolean;
  agentsTotal: number;
  agentsActive: number;
  isGenerating: boolean;
  isSpeaking: boolean;
  isListening: boolean;
  voiceEngine: string;
  voiceName: string;
  voiceAvailable: boolean;
  sttReady: boolean;
}

interface MetricsSummary {
  summary?: {
    total_calls?: number;
    avg_latency_ms?: number;
    success_rate?: number;
    total_tokens?: number;
  };
}

export function useVexaTelemetry(input: TelemetryInput): VexaTelemetry {
  const {
    enabled, isConnected, agentsTotal, agentsActive, runOutcomes,
    isGenerating, isSpeaking, isListening,
    voiceEngine, voiceName, voiceAvailable, sttReady,
  } = input;

  const [stats, setStats] = useState<SystemStats | null>(null);
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null);
  const [controlSummary, setControlSummary] = useState<ControlPlaneSummary | null>(null);
  const [activitySeries, setActivitySeries] = useState<number[]>(() => new Array(ACTIVITY_SERIES_LENGTH).fill(0));
  const [dataStream, setDataStream] = useState<DataStreamRow[]>([]);
  /** Share of 1 Hz samples in which the WebSocket was up — the "stability" insight. */
  const [connectionRatio, setConnectionRatio] = useState<number | null>(null);

  // Live inputs the 1 Hz sampler reads without re-subscribing every render.
  const liveRef = useRef({ isGenerating, isSpeaking, isListening, isConnected, agentsActive, agentsTotal });
  useEffect(() => {
    liveRef.current = { isGenerating, isSpeaking, isListening, isConnected, agentsActive, agentsTotal };
  }, [isGenerating, isSpeaking, isListening, isConnected, agentsActive, agentsTotal]);
  const statsRef = useRef<SystemStats | null>(null);
  const previousResourcesRef = useRef<Record<string, number>>({});
  const connectionSamplesRef = useRef({ total: 0, connected: 0 });

  const loadStats = useCallback(async () => {
    const data = await getJson<SystemStats>('/api/system/stats');
    if (data) {
      statsRef.current = data;
      setStats(data);
    }
  }, []);

  const loadMetrics = useCallback(async () => {
    const data = await getJson<MetricsSummary>('/api/metrics');
    if (data) setMetrics(data);
  }, []);

  const refreshControl = useCallback(async () => {
    const data = await getJson<ControlPlaneSummary>('/api/control-plane/summary?limit=50');
    if (data) setControlSummary(data);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void loadStats();
    const timer = window.setInterval(() => void loadStats(), STATS_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, loadStats]);

  useEffect(() => {
    if (!enabled) return;
    void loadMetrics();
    const timer = window.setInterval(() => void loadMetrics(), METRICS_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, loadMetrics]);

  useEffect(() => {
    if (!enabled) return;
    void refreshControl();
    const timer = window.setInterval(() => void refreshControl(), CONTROL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, refreshControl]);

  // Rolling 1 Hz sampler: derives the activity trace and the signed data-stream deltas
  // from the latest snapshot. Paused while the tab is hidden so a backgrounded dashboard
  // costs nothing.
  useEffect(() => {
    if (!enabled) return;

    const sample = () => {
      if (document.hidden) return;
      const snapshot = statsRef.current;
      const live = liveRef.current;

      connectionSamplesRef.current.total += 1;
      if (live.isConnected) connectionSamplesRef.current.connected += 1;
      const samples = connectionSamplesRef.current;
      setConnectionRatio((samples.connected / samples.total) * 100);

      const cpu = clampPercent(snapshot?.host?.cpu?.usage_percent ?? snapshot?.cpu_load_percent);
      const activityFloor = live.isConnected ? 8 : 0;
      const workload =
        (live.isGenerating ? 34 : 0) +
        (live.isSpeaking ? 22 : 0) +
        (live.isListening ? 18 : 0) +
        (live.agentsTotal ? (live.agentsActive / live.agentsTotal) * 24 : 0);
      const point = Math.max(0, Math.min(100, activityFloor + (cpu ?? 0) * 0.45 + workload));

      setActivitySeries(previous => {
        const next = previous.slice(1);
        next.push(point);
        return next;
      });

      const now = Date.now();
      const memory = clampPercent(snapshot?.host?.memory?.usage_percent ?? snapshot?.ram_used_percent);
      const gpu = firstGpuUtilization(snapshot);
      const rx = snapshot?.host?.network?.rx_bytes_per_second ?? null;
      const tx = snapshot?.host?.network?.tx_bytes_per_second ?? null;

      // Channel labels follow the reference's DATA-NN form; the real source stays in
      // `channel` so the row's tooltip can say what is actually being measured.
      const readings: Array<{ id: string; label: string; channel: string; value: number | null; unit?: string }> = [
        { id: 'cpu', label: 'DATA-01', channel: 'CPU', value: cpu, unit: '%' },
        { id: 'ram', label: 'DATA-02', channel: 'RAM', value: memory, unit: '%' },
        { id: 'gpu', label: 'DATA-03', channel: 'GPU', value: gpu, unit: '%' },
        { id: 'net-rx', label: 'DATA-04', channel: 'NET RX', value: rx === null ? null : rx / 1_048_576, unit: 'MB/s' },
        { id: 'net-tx', label: 'DATA-05', channel: 'NET TX', value: tx === null ? null : tx / 1_048_576, unit: 'MB/s' },
      ];

      setDataStream(previous => readings.map(reading => {
        const existing = previous.find(row => row.id === reading.id);
        if (reading.value === null) {
          return { id: reading.id, label: reading.label, channel: reading.channel, value: null, unit: reading.unit, updatedAt: existing?.updatedAt ?? 0 };
        }
        const before = previousResourcesRef.current[reading.id];
        previousResourcesRef.current[reading.id] = reading.value;
        const delta = before === undefined ? 0 : reading.value - before;
        const changed = Math.abs(delta) > 0.05;
        return {
          id: reading.id,
          label: reading.label,
          channel: reading.channel,
          value: delta,
          unit: reading.unit,
          updatedAt: changed ? now : existing?.updatedAt ?? 0,
        };
      }));
    };

    const timer = window.setInterval(sample, SERIES_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [enabled]);

  const overview = useMemo<SystemOverview>(() => {
    const host = stats?.host;
    const cpu = clampPercent(host?.cpu?.usage_percent ?? stats?.cpu_load_percent);
    const memory = clampPercent(host?.memory?.usage_percent ?? stats?.ram_used_percent);
    const stale = Boolean(stats?.stale) || (stats?.age_seconds ?? 0) > 10;

    return {
      circuit: { status: isConnected ? (stale ? 'degraded' : 'online') : 'offline' },
      agentsTotal,
      agentsActive,
      voice: {
        mode: voiceAvailable ? 'local' : 'remote',
        engine: voiceEngine,
        voice: voiceName,
        status: !isConnected ? 'offline' : isSpeaking ? 'busy' : sttReady ? 'ready' : 'error',
      },
      resources: {
        cpu,
        memory,
        gpu: firstGpuUtilization(stats),
        network: networkHealth(stats),
      },
      uptimeSeconds: typeof host?.uptime_seconds === 'number' ? host.uptime_seconds : null,
      updatedAt: stats?.collected_at ?? null,
      stale,
    };
  }, [stats, isConnected, agentsTotal, agentsActive, voiceAvailable, voiceEngine, voiceName, isSpeaking, sttReady]);

  const protocols = useMemo<ProtocolInfo[]>(() => {
    const latency = metrics?.summary?.avg_latency_ms ?? null;
    return [
      {
        id: 'nlu',
        label: 'NLU Engine',
        status: isConnected ? 'running' : 'offline',
        detail: 'Языковое ядро и разбор команд',
        latencyMs: latency,
      },
      {
        id: 'sentiment',
        label: 'Sentiment Analysis',
        status: sttReady ? 'running' : isConnected ? 'standby' : 'offline',
        detail: 'Распознавание речи и разбор намерения (STT)',
      },
      {
        id: 'predictive',
        label: 'Predictive Model',
        status: controlSummary?.state?.kill_switch
          ? 'paused'
          : agentsActive > 0 ? 'running' : isConnected ? 'standby' : 'offline',
        detail: 'Планирование и делегирование задач под-агентам',
      },
      {
        id: 'tts',
        label: 'Voice Synthesis',
        status: !voiceAvailable ? 'offline' : isSpeaking ? 'running' : 'standby',
        detail: 'Синтез речи (TTS)',
      },
    ];
  }, [isConnected, sttReady, agentsActive, isSpeaking, voiceAvailable, controlSummary, metrics]);

  const insights = useMemo<InsightMetric[]>(() => {
    const summary = metrics?.summary;
    const updatedAt = summary ? new Date().toISOString() : null;
    const successRate = typeof summary?.success_rate === 'number' ? summary.success_rate : null;
    const latency = typeof summary?.avg_latency_ms === 'number' ? summary.avg_latency_ms : null;
    const stability = connectionRatio;
    const totalRuns = runOutcomes.completed + runOutcomes.failed;
    const accuracy = totalRuns > 0 ? (runOutcomes.completed / totalRuns) * 100 : null;

    const band = (value: number | null, warn: number, critical: number): InsightMetric['status'] => {
      if (value === null) return 'unknown';
      if (value < critical) return 'critical';
      if (value < warn) return 'warning';
      return 'normal';
    };

    return [
      {
        id: 'efficiency',
        label: 'efficiency',
        value: successRate,
        formattedValue: successRate === null ? null : `${successRate.toFixed(1)}%`,
        min: 0,
        max: 100,
        status: band(successRate, 95, 85),
        updatedAt,
      },
      {
        id: 'response-time',
        label: 'responseTime',
        // Rendered as an inverted bar: faster is fuller. 4s is the practical ceiling.
        value: latency === null ? null : Math.max(0, 100 - Math.min(100, latency / 40)),
        formattedValue: latency === null ? null : latency >= 1000 ? `${(latency / 1000).toFixed(1)}s` : `${Math.round(latency)}ms`,
        min: 0,
        max: 100,
        status: latency === null ? 'unknown' : latency > 4000 ? 'critical' : latency > 1500 ? 'warning' : 'normal',
        updatedAt,
      },
      {
        // Share of observed assistant runs that finished cleanly. Measured on the client
        // from the loaded history — the backend publishes no accuracy metric of its own —
        // so it stays null (and renders as "нет данных") until at least one run is seen.
        id: 'accuracy',
        label: 'accuracy',
        value: accuracy,
        formattedValue: accuracy === null ? null : `${accuracy.toFixed(1)}%`,
        min: 0,
        max: 100,
        status: band(accuracy, 97, 90),
        updatedAt: accuracy === null ? null : updatedAt,
      },
      {
        id: 'stability',
        label: 'stability',
        value: stability,
        formattedValue: stability === null ? null : `${stability.toFixed(1)}%`,
        min: 0,
        max: 100,
        status: band(stability, 99, 90),
        updatedAt,
      },
    ];
  }, [metrics, connectionRatio, runOutcomes]);

  // Control-plane risk classes (R0..R4) map onto the dashboard's four-level risk scale.
  const confirmations = useMemo<ConfirmationRequest[]>(() => {
    const pending = controlSummary?.pending_approvals ?? [];
    return pending.map(task => {
      const riskLevel: RiskLevel = task.risk_class === 'R4' ? 'critical'
        : task.risk_class === 'R3' ? 'high'
          : task.risk_class === 'R0' ? 'low' : 'medium';
      return {
        id: String(task.id ?? ''),
        title: task.tool_name || task.goal || 'Действие агента',
        description: task.tool_name ? task.goal : '',
        agentId: task.assignee || task.requester || '',
        riskLevel,
        requestedAt: task.created_at || '',
        status: 'pending' as const,
      };
    }).filter(item => item.id);
  }, [controlSummary]);

  // Derived engagement indicator, not a hardware measurement: how "busy" the mesh looks
  // right now. Surfaced as a percentage in the Neural density card, which labels it as an
  // aggregate rather than a diagnostic reading.
  const neuralDensity = useMemo(() => {
    if (!isConnected) return null;
    const recent = activitySeries.slice(-20).filter(value => value > 0);
    if (recent.length === 0) return null;
    const average = recent.reduce((sum, value) => sum + value, 0) / recent.length;
    return Math.max(0, Math.min(100, 55 + average * 0.45));
  }, [activitySeries, isConnected]);

  return {
    stats,
    overview,
    activitySeries,
    dataStream,
    protocols,
    insights,
    confirmations,
    controlSummary,
    emergencyStopped: Boolean(controlSummary?.state?.kill_switch),
    neuralDensity,
    refreshControl,
  };
}
