import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, CircuitBoard, Cpu, Database, Fan, HardDrive, Lightbulb, Maximize2,
  MemoryStick, Minimize2, Monitor, Network, RefreshCw, Server, Thermometer, Zap,
} from 'lucide-react';
import type { SystemStats } from '../../types';
import { MetricsLineChart, Sparkline, type ChartSeries } from './MetricsCharts';
import {
  COPY, RANGE_KEYS, REFRESH_OPTIONS, SERIES_COLORS, alertText, deltaFromMean, diskTotals, finite,
  formatBitrate, formatByteRate, formatBytes, formatClock, formatUptime, gpuField, gpuIndexes,
  hottestGpu, observations, percent, periodSummary, seriesOf, vramTotals,
  type HistoryPoint, type Lang, type MetricsHistory, type RangeKey,
} from './serverMetricsModel';
import './serverMetrics.css';

/**
 * «Метрики»: live state and history of the physical GPU server.
 *
 * Live cards read /api/system/stats (the host agent's latest snapshot, ≤5 s
 * old); charts, alerts and the period summary read
 * /api/system/metrics/history, which the backend accumulates from those
 * snapshots every 30 s (backend/host_metrics.py).
 */

const RANGE_STORAGE_KEY = 'hermes_metrics_range';
const REFRESH_STORAGE_KEY = 'hermes_metrics_refresh';

function readStored<T>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw === null ? null : (JSON.parse(raw) as T);
    return parsed !== null && allowed.includes(parsed) ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function store(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: not remembered */ }
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status}`);
  return await response.json() as T;
}

type Copy = typeof COPY['ru'];

function Delta({ value, unit, invert = false, title }: { value: number | null; unit: string; invert?: boolean; title: string }) {
  if (value === null || Math.abs(value) < 0.5) return null;
  const up = value > 0;
  // For temperature and load "up" is the worse direction; the colour says which.
  const bad = invert ? !up : up;
  return (
    <span className={`sm-delta ${bad ? 'is-up' : 'is-down'}`} title={title}>
      {up ? '↑' : '↓'} {Math.abs(value).toFixed(0)}{unit}
    </span>
  );
}

function Bar({ value, color }: { value: number | null; color: string }) {
  return (
    <div className="sm-bar">
      <div style={{ width: `${value ?? 0}%`, background: color }} />
    </div>
  );
}

function KpiCard({ icon, label, children, accent, aside }: {
  icon: React.ReactNode; label: string; children: React.ReactNode; accent?: string; aside?: React.ReactNode;
}) {
  return (
    <section className="sm-card sm-kpi" style={accent ? { ['--sm-accent' as string]: accent } : undefined}>
      <div className="sm-kpi-head">
        <span className="sm-kpi-icon">{icon}</span>
        <span className="sm-kpi-label">{label}</span>
        {aside}
      </div>
      {children}
    </section>
  );
}

function Panel({ title, children, control }: { title: string; children: React.ReactNode; control?: React.ReactNode }) {
  return (
    <section className="sm-card sm-panel">
      <div className="sm-panel-head">
        <h3>{title}</h3>
        {control}
      </div>
      {children}
    </section>
  );
}

function Legend({ items }: { items: Array<{ label: string; color: string; value?: string; dashed?: boolean }> }) {
  return (
    <div className="sm-legend">
      {items.map(item => (
        <span key={item.label}>
          <i className={item.dashed ? 'is-dashed' : ''} style={{ background: item.dashed ? 'transparent' : item.color, borderColor: item.color }} />
          {item.label}
          {item.value && <b>{item.value}</b>}
        </span>
      ))}
    </div>
  );
}

function Select<T extends string>({ value, options, onChange, label }: {
  value: T; options: Array<{ value: T; label: string }>; onChange: (value: T) => void; label: string;
}) {
  return (
    <select className="sm-select" value={value} aria-label={label} onChange={e => onChange(e.target.value as T)}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

const pct = (v: number) => `${Math.round(v)}%`;
const celsius = (v: number) => `${Math.round(v)}°C`;
const gb = (v: number) => `${(v / 1024 ** 3).toFixed(v >= 10 * 1024 ** 3 || v === 0 ? 0 : 1)} GB`;
const watts = (v: number) => `${Math.round(v)} W`;
const mbps = (v: number) => `${Math.round((v * 8) / 1e6)} Mbps`;
const mbs = (v: number) => `${(v / 1024 ** 2).toFixed(v >= 10 * 1024 ** 2 || v === 0 ? 0 : 1)} MB/s`;
const tb = (v: number) => `${(v / 1024 ** 4).toFixed(1)} TB`;

export function ServerMetricsTab({ language }: { language: Lang }) {
  const copy: Copy = COPY[language] ?? COPY.en;
  const [range, setRange] = useState<RangeKey>(() => readStored(RANGE_STORAGE_KEY, RANGE_KEYS, '24h'));
  const [refreshSeconds, setRefreshSeconds] = useState<number>(() => readStored(REFRESH_STORAGE_KEY, REFRESH_OPTIONS as readonly number[], 10));
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [history, setHistory] = useState<MetricsHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [gpuMetric, setGpuMetric] = useState<'util' | 'power'>('util');
  const [diskMetric, setDiskMetric] = useState<'io' | 'fill'>('io');
  const [showAllAlerts, setShowAllAlerts] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const [statsResult, historyResult] = await Promise.allSettled([
      getJson<SystemStats>('/api/system/stats'),
      getJson<MetricsHistory>(`/api/system/metrics/history?range=${range}`),
    ]);
    if (statsResult.status === 'fulfilled') setStats(statsResult.value);
    // A backend older than the history endpoint (or a proxy error page) must not take the live cards down with it.
    if (historyResult.status === 'fulfilled' && Array.isArray(historyResult.value?.points)) {
      setHistory({ ...historyResult.value, alerts: Array.isArray(historyResult.value.alerts) ? historyResult.value.alerts : [] });
    }
    setError(statsResult.status === 'rejected' && historyResult.status === 'rejected' ? copy.loadError : null);
    setLoading(false);
  }, [range, copy.loadError]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!refreshSeconds) return;
    const timer = window.setInterval(() => {
      // A hidden tab does not need fresh numbers; it catches up on return.
      if (document.visibilityState !== 'hidden') void load();
    }, refreshSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [load, refreshSeconds]);

  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === rootRef.current);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen?.();
    else void rootRef.current?.requestFullscreen?.();
  };

  const host = stats?.scope === 'physical_host' ? stats.host : undefined;
  const gpus = host?.gpus ?? [];
  const points: HistoryPoint[] = history?.points ?? [];
  const times = points.map(p => p.t);
  const bucket = history?.bucket_seconds ?? 30;
  const from = history?.from ?? Math.floor(Date.now() / 1000) - 86400;
  const to = history?.to ?? Math.floor(Date.now() / 1000);
  const indexes = gpuIndexes(points, gpus);

  const status: 'online' | 'stale' | 'offline' = !stats ? 'offline'
    : stats.scope !== 'physical_host' ? 'offline'
      : stats.stale ? 'stale' : 'online';
  const statusLabel = status === 'online' ? copy.online : status === 'stale' ? copy.stale : (stats ? copy.runtimeOnly : copy.offline);

  const gpuName = (index: number) => {
    const name = gpus.find(g => (g.index ?? 0) === index)?.name;
    return name ? `GPU ${index} (${name.replace(/^NVIDIA (GeForce )?/, '')})` : `GPU ${index}`;
  };
  const gpuColor = (position: number) => SERIES_COLORS[position % SERIES_COLORS.length];

  const vram = vramTotals(gpus);
  const disks = diskTotals(host);
  const cpuNow = finite(host?.cpu?.usage_percent) ?? finite(stats?.cpu_load_percent);
  const ramNow = finite(host?.memory?.usage_percent) ?? finite(stats?.ram_used_percent);
  const gpuTempNow = hottestGpu(gpus);
  const rxNow = finite(host?.network?.rx_bytes_per_second);
  const txNow = finite(host?.network?.tx_bytes_per_second);

  const cpuSeries = seriesOf(points, p => p.cpu);
  const ramSeries = seriesOf(points, p => p.ram);
  const gpuTempSeries = points.map(p => {
    const temps = p.gpus.map(g => finite(g.temp)).filter((t): t is number => t !== null);
    return temps.length ? Math.max(...temps) : null;
  });
  const vramSeries = points.map(p => {
    const used = p.gpus.map(g => finite(g.mem)).filter((v): v is number => v !== null);
    return used.length ? used.reduce((a, b) => a + b, 0) : null;
  });
  const netSeries = points.map(p => (p.rx === null && p.tx === null ? null : (p.rx ?? 0) + (p.tx ?? 0)));

  const summary = periodSummary(history);
  const notes = useMemo(() => observations(stats, history, language), [stats, history, language]);
  const alerts = history?.alerts ?? [];
  const shownAlerts = showAllAlerts ? alerts : alerts.slice(0, 3);
  const containers = host?.containers ?? [];
  const containersUp = containers.filter(c => c.state === 'running' && c.health !== 'unhealthy').length;

  const chips = [
    gpus.length ? `${gpus.length}× ${(gpus[0].name ?? 'GPU').replace(/^NVIDIA (GeForce )?/, '')}` : null,
    host?.cpu?.model ? host.cpu.model.replace(/\(R\)|\(TM\)|CPU /g, '').replace(/\s+/g, ' ').trim() : null,
    host?.memory?.total_bytes ? `${Math.round(host.memory.total_bytes / 1024 ** 3)} GB RAM` : null,
    host?.os ?? null,
  ].filter((chip): chip is string => Boolean(chip));

  const gpuChartSeries: ChartSeries[] = indexes.map((index, position) => ({
    id: `gpu${index}-${gpuMetric}`,
    label: gpuName(index),
    color: gpuColor(position),
    values: points.map(p => gpuField(p, index, gpuMetric)),
    fill: true,
  }));
  if (gpuMetric === 'power') {
    // The cap the card is held to — after the 300 W change it is the line the draw rides under.
    indexes.slice(0, 1).forEach(index => gpuChartSeries.push({
      id: `gpu${index}-limit`, label: `${copy.power} limit`, color: '#6B7280', dashed: true,
      values: points.map(p => gpuField(p, index, 'limit')),
    }));
  }
  const gpuPowerMax = Math.max(0, ...gpus.map(g => finite(g.power_limit_watts) ?? 0));

  const memMax = Math.max(0, ...gpus.map(g => finite(g.memory_total_bytes) ?? 0));

  const tempSeries: ChartSeries[] = [
    ...indexes.map((index, position) => ({
      id: `t-gpu${index}`, label: `GPU ${index}`, color: gpuColor(position),
      values: points.map(p => gpuField(p, index, 'temp')),
    })),
    { id: 't-cpu', label: 'CPU', color: '#3B82F6', values: seriesOf(points, p => p.cpu_temp) },
    { id: 't-nvme', label: copy.nvme, color: '#F6B84A', values: seriesOf(points, p => p.nvme_temp) },
  ];

  const nvmeNow = (host?.temperatures?.nvme ?? []).map(n => finite(n.celsius)).filter((v): v is number => v !== null);
  const tempNow: Record<string, number | null> = {
    't-cpu': finite(host?.temperatures?.cpu_celsius),
    't-nvme': nvmeNow.length ? Math.max(...nvmeNow) : null,
  };
  indexes.forEach(index => {
    tempNow[`t-gpu${index}`] = finite(gpus.find(g => (g.index ?? 0) === index)?.temperature_celsius);
  });

  return (
    <div className={`sm-root${fullscreen ? ' is-fullscreen' : ''}`} ref={rootRef}>
      <div className="sm-main">
        <header className="sm-header">
          <div>
            <div className="sm-eyebrow"><Server size={13} /> {copy.eyebrow}</div>
            <h2 className="sm-title">{copy.title}</h2>
            <p className="sm-subtitle">
              {[host?.hostname, chips[0], copy.live].filter(Boolean).join(' • ')}
            </p>
          </div>
          <div className="sm-controls">
            <button type="button" className="sm-button" onClick={() => void load()} disabled={loading} title={copy.refresh}>
              <RefreshCw size={14} className={loading ? 'is-spinning' : ''} />
            </button>
            <Select
              label={copy.refresh}
              value={String(refreshSeconds)}
              options={REFRESH_OPTIONS.map(s => ({ value: String(s), label: `${copy.refresh}: ${s ? `${s} ${copy.sec}` : copy.refreshOff}` }))}
              onChange={value => { const next = Number(value); setRefreshSeconds(next); store(REFRESH_STORAGE_KEY, next); }}
            />
            <Select
              label={copy.ranges[range]}
              value={range}
              options={RANGE_KEYS.map(key => ({ value: key, label: copy.ranges[key] }))}
              onChange={value => { setRange(value); store(RANGE_STORAGE_KEY, value); }}
            />
            <button type="button" className="sm-button" onClick={toggleFullscreen} title={fullscreen ? copy.exitFullscreen : copy.fullscreen}>
              {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </button>
          </div>
        </header>

        <div className="sm-chips">
          <span className={`sm-chip is-status is-${status}`}><i />{statusLabel}</span>
          {chips.map(chip => <span key={chip} className="sm-chip">{chip}</span>)}
        </div>

        {error && <div className="sm-error"><AlertTriangle size={14} /> {error}</div>}

        <div className="sm-kpis">
          {indexes.slice(0, 2).map((index, position) => {
            const gpu = gpus.find(g => (g.index ?? 0) === index);
            const util = finite(gpu?.utilization_percent);
            const series = points.map(p => gpuField(p, index, 'util'));
            const draw = finite(gpu?.power_draw_watts);
            const limit = finite(gpu?.power_limit_watts);
            return (
              <KpiCard
                key={index}
                icon={<CircuitBoard size={18} />}
                label={`GPU ${index}`}
                accent={gpuColor(position)}
                aside={<Delta value={deltaFromMean(util, series)} unit="%" title={copy.vsAverage} />}
              >
                <div className="sm-kpi-value">{util === null ? copy.noData : pct(util)}</div>
                <div className="sm-kpi-sub">
                  {draw !== null && limit !== null ? `${Math.round(draw)} / ${Math.round(limit)} W` : ''}
                </div>
                <Sparkline times={times} values={series} color={gpuColor(position)} max={100} bucketSeconds={bucket} />
              </KpiCard>
            );
          })}
          <KpiCard
            icon={<Database size={18} />}
            label={copy.vram}
            accent="#9277FF"
            aside={<span className="sm-kpi-pct">{percent(vram.used, vram.total) === null ? '' : pct(percent(vram.used, vram.total) as number)}</span>}
          >
            <div className="sm-kpi-value">
              {vram.used === null ? copy.noData : <>{(vram.used / 1024 ** 3).toFixed(1)}<small> / {Math.round((vram.total ?? 0) / 1024 ** 3)} GB</small></>}
            </div>
            <Bar value={percent(vram.used, vram.total)} color="linear-gradient(90deg, #7C5CFC, #9277FF)" />
            <Sparkline times={times} values={vramSeries} color="#9277FF" max={vram.total ?? undefined} bucketSeconds={bucket} />
          </KpiCard>
          <KpiCard
            icon={<Cpu size={18} />}
            label={copy.cpu}
            accent="#3B82F6"
            aside={<Delta value={deltaFromMean(cpuNow, cpuSeries)} unit="%" title={copy.vsAverage} />}
          >
            <div className="sm-kpi-value">{cpuNow === null ? copy.noData : pct(cpuNow)}</div>
            <div className="sm-kpi-sub">
              {host?.cpu?.load_1m !== undefined && host?.cpu?.load_1m !== null ? `load ${host.cpu.load_1m} · ${host.cpu.logical_cores ?? '?'} threads` : ''}
            </div>
            <Sparkline times={times} values={cpuSeries} color="#3B82F6" max={100} bucketSeconds={bucket} />
          </KpiCard>
          <KpiCard
            icon={<MemoryStick size={18} />}
            label={copy.ram}
            accent="#36CFFB"
            aside={<span className="sm-kpi-pct">{ramNow === null ? '' : pct(ramNow)}</span>}
          >
            <div className="sm-kpi-value">
              {host?.memory?.used_bytes === undefined ? copy.noData
                : <>{(host.memory.used_bytes / 1024 ** 3).toFixed(1)}<small> / {Math.round((host.memory.total_bytes ?? 0) / 1024 ** 3)} GB</small></>}
            </div>
            <Bar value={ramNow} color="linear-gradient(90deg, #3B82F6, #36CFFB)" />
            <Sparkline times={times} values={ramSeries} color="#36CFFB" max={100} bucketSeconds={bucket} />
          </KpiCard>
          <KpiCard
            icon={<HardDrive size={18} />}
            label={copy.disk}
            accent="#F6B84A"
            aside={<span className="sm-kpi-pct">{percent(disks.used, disks.total) === null ? '' : pct(percent(disks.used, disks.total) as number)}</span>}
          >
            <div className="sm-kpi-value">
              {disks.used === null ? copy.noData : <>{formatBytes(disks.used, 0)}<small> / {formatBytes(disks.total)}</small></>}
            </div>
            <Bar value={percent(disks.used, disks.total)} color="linear-gradient(90deg, #F6B84A, #F59E0B)" />
            <div className="sm-kpi-sub">
              {host?.disk_io ? `${copy.read} ${formatByteRate(host.disk_io.read_bytes_per_second)} · ${copy.write} ${formatByteRate(host.disk_io.write_bytes_per_second)}` : ''}
            </div>
          </KpiCard>
          <KpiCard
            icon={<Thermometer size={18} />}
            label={copy.gpuTemp}
            accent="#F6B84A"
            aside={<Delta value={deltaFromMean(gpuTempNow, gpuTempSeries)} unit="°C" title={copy.vsAverage} />}
          >
            <div className={`sm-kpi-value${gpuTempNow !== null && gpuTempNow >= (history?.thresholds.gpu_temp_warn ?? 80) ? ' is-hot' : ' is-warm'}`}>
              {gpuTempNow === null ? copy.noData : celsius(gpuTempNow)}
            </div>
            <div className="sm-kpi-sub">
              {gpus.map(g => `GPU ${g.index ?? 0}: ${finite(g.temperature_celsius) === null ? '—' : celsius(g.temperature_celsius as number)}`).join(' · ')}
            </div>
            <Sparkline times={times} values={gpuTempSeries} color="#F6B84A" max={100} bucketSeconds={bucket} />
          </KpiCard>
          <KpiCard icon={<Network size={18} />} label={copy.network} accent="#36CFFB">
            <div className="sm-kpi-value sm-kpi-net">
              {rxNow === null ? copy.noData : <>{formatBitrate(rxNow)}<small> / {formatBitrate(txNow)}</small></>}
            </div>
            <div className="sm-kpi-sub">{host?.network?.interfaces?.map(i => i.name).join(', ')}</div>
            <Sparkline times={times} values={netSeries} color="#36CFFB" bucketSeconds={bucket} />
          </KpiCard>
        </div>

        <div className="sm-charts">
          <Panel
            title={gpuMetric === 'util' ? copy.gpuLoad : copy.gpuPower}
            control={<Select label={copy.gpuLoad} value={gpuMetric} onChange={setGpuMetric}
              options={[{ value: 'util', label: copy.load }, { value: 'power', label: copy.power }]} />}
          >
            <MetricsLineChart
              times={times} series={gpuChartSeries} from={from} to={to} bucketSeconds={bucket}
              yMax={gpuMetric === 'util' ? 100 : (gpuPowerMax ? Math.ceil((gpuPowerMax * 1.1) / 50) * 50 : undefined)}
              formatY={gpuMetric === 'util' ? pct : watts} emptyText={copy.chartEmpty}
            />
            <Legend items={indexes.map((index, position) => {
              const gpu = gpus.find(g => (g.index ?? 0) === index);
              const value = gpuMetric === 'util' ? finite(gpu?.utilization_percent) : finite(gpu?.power_draw_watts);
              return { label: gpuName(index), color: gpuColor(position), value: value === null ? undefined : (gpuMetric === 'util' ? pct(value) : watts(value)) };
            })} />
          </Panel>

          <Panel title={copy.gpuMemory}>
            <MetricsLineChart
              times={times} from={from} to={to} bucketSeconds={bucket} yMax={memMax || undefined}
              series={indexes.map((index, position) => ({
                id: `mem${index}`, label: `GPU ${index}`, color: gpuColor(position), fill: true,
                values: points.map(p => gpuField(p, index, 'mem')),
              }))}
              formatY={gb} emptyText={copy.chartEmpty}
            />
            <Legend items={indexes.map((index, position) => {
              const gpu = gpus.find(g => (g.index ?? 0) === index);
              const used = finite(gpu?.memory_used_bytes);
              const share = percent(gpu?.memory_used_bytes, gpu?.memory_total_bytes);
              return { label: `GPU ${index}`, color: gpuColor(position), value: used === null ? undefined : `${gb(used)}${share === null ? '' : ` (${pct(share)})`}` };
            })} />
          </Panel>

          <Panel title={copy.cpuRam}>
            <MetricsLineChart
              times={times} from={from} to={to} bucketSeconds={bucket} yMax={100}
              series={[
                { id: 'cpu', label: copy.cpu, color: '#3B82F6', values: cpuSeries, fill: true },
                { id: 'ram', label: copy.ram, color: '#9277FF', values: ramSeries },
              ]}
              formatY={pct} emptyText={copy.chartEmpty}
            />
            <Legend items={[
              { label: copy.cpuThreads(host?.cpu?.logical_cores ?? 0), color: '#3B82F6', value: cpuNow === null ? undefined : pct(cpuNow) },
              { label: copy.ram, color: '#9277FF', value: host?.memory?.used_bytes === undefined ? undefined : `${gb(host.memory.used_bytes)} (${pct(ramNow ?? 0)})` },
            ]} />
          </Panel>

          <Panel title={copy.temperatures}>
            <MetricsLineChart
              times={times} from={from} to={to} bucketSeconds={bucket} yMax={100}
              series={tempSeries} formatY={celsius} emptyText={copy.chartEmpty}
            />
            <Legend items={tempSeries.map(s => ({
              label: s.label, color: s.color, value: tempNow[s.id] === null || tempNow[s.id] === undefined ? undefined : celsius(tempNow[s.id] as number),
            }))} />
          </Panel>

          <Panel title={copy.networkActivity}>
            <MetricsLineChart
              times={times} from={from} to={to} bucketSeconds={bucket}
              series={[
                { id: 'rx', label: copy.inbound, color: '#35D49A', values: seriesOf(points, p => p.rx), fill: true },
                { id: 'tx', label: copy.outbound, color: '#9277FF', values: seriesOf(points, p => p.tx) },
              ]}
              formatY={mbps} formatValue={v => formatBitrate(v)} yUnit={125_000} emptyText={copy.chartEmpty}
            />
            <Legend items={[
              { label: copy.inbound, color: '#35D49A', value: rxNow === null ? undefined : formatBitrate(rxNow) },
              { label: copy.outbound, color: '#9277FF', value: txNow === null ? undefined : formatBitrate(txNow) },
            ]} />
          </Panel>

          <Panel
            title={copy.diskSystem}
            control={<Select label={copy.diskSystem} value={diskMetric} onChange={setDiskMetric}
              options={[{ value: 'io', label: copy.diskIo }, { value: 'fill', label: copy.diskFill }]} />}
          >
            {diskMetric === 'io' ? (
              <>
                <MetricsLineChart
                  times={times} from={from} to={to} bucketSeconds={bucket}
                  series={[
                    { id: 'io_r', label: copy.read, color: '#3B82F6', values: seriesOf(points, p => p.io_r) },
                    { id: 'io_w', label: copy.write, color: '#F6B84A', values: seriesOf(points, p => p.io_w), fill: true },
                  ]}
                  formatY={mbs} formatValue={v => formatByteRate(v)} yUnit={1024 ** 2} emptyText={copy.chartEmpty}
                />
                <Legend items={[
                  { label: copy.read, color: '#3B82F6', value: host?.disk_io ? formatByteRate(host.disk_io.read_bytes_per_second) : undefined },
                  { label: copy.write, color: '#F6B84A', value: host?.disk_io ? formatByteRate(host.disk_io.write_bytes_per_second) : undefined },
                ]} />
              </>
            ) : (
              <>
                <MetricsLineChart
                  times={times} from={from} to={to} bucketSeconds={bucket}
                  yMax={disks.total ? Math.ceil((disks.total * 1.1) / 1024 ** 4 * 2) / 2 * 1024 ** 4 : undefined}
                  series={[
                    { id: 'disk_used', label: copy.used, color: '#F6B84A', values: seriesOf(points, p => p.disk_used), fill: true },
                    { id: 'disk_total', label: 'Total', color: '#6B7280', values: seriesOf(points, p => p.disk_total), dashed: true },
                  ]}
                  formatY={tb} formatValue={v => formatBytes(v)} emptyText={copy.chartEmpty}
                />
                <Legend items={[
                  { label: copy.used, color: '#F6B84A', value: disks.used === null ? undefined : formatBytes(disks.used) },
                  { label: 'Total', color: '#6B7280', dashed: true, value: disks.total === null ? undefined : formatBytes(disks.total) },
                ]} />
              </>
            )}
          </Panel>
        </div>
      </div>

      <aside className="sm-side">
        <section className="sm-card sm-side-card">
          <h3>{copy.serverInfo}</h3>
          <div className="sm-server">
            <span className={`sm-dot is-${status}`} />
            <div>
              <strong>{host?.hostname ?? '—'}</strong>
              <em className={`is-${status}`}>{statusLabel}</em>
            </div>
            <span className="sm-uptime" title={copy.uptime}>{formatUptime(host?.uptime_seconds, language)}</span>
          </div>
          <h4>{copy.hardware}</h4>
          <dl className="sm-specs">
            <dt><CircuitBoard size={14} />{copy.gpu}</dt>
            <dd>{gpus.length ? `${gpus.length}× ${(gpus[0].name ?? '').replace(/^NVIDIA /, '')} (${formatBytes(gpus[0].memory_total_bytes, 0)})` : '—'}</dd>
            <dt><Cpu size={14} />CPU</dt>
            <dd>{host?.cpu?.model ? `${host.cpu.model.replace(/\(R\)|\(TM\)/g, '').replace(/\s+/g, ' ')} (${host.cpu.logical_cores})` : '—'}</dd>
            <dt><MemoryStick size={14} />RAM</dt>
            <dd>{formatBytes(host?.memory?.total_bytes, 0)}</dd>
            <dt><HardDrive size={14} />{copy.disk}</dt>
            <dd>{(host?.disk_io?.devices ?? []).map(d => d.model || d.name).filter(Boolean).join(', ') || formatBytes(disks.total)}</dd>
            <dt><Monitor size={14} />{copy.os}</dt>
            <dd>{host?.os ?? '—'}</dd>
            <dt><Activity size={14} />{copy.kernel}</dt>
            <dd>{host?.kernel ?? '—'}</dd>
            <dt><Zap size={14} />{copy.driver}</dt>
            <dd>{gpus[0]?.driver_version ?? '—'}</dd>
            <dt><Network size={14} />{copy.ip}</dt>
            <dd>{host?.network?.primary_ip ?? '—'}</dd>
            <dt><RefreshCw size={14} />{copy.collector}</dt>
            <dd>{finite(stats?.age_seconds) === null ? '—' : copy.collectorAge(stats?.age_seconds as number)}</dd>
          </dl>
        </section>

        <section className="sm-card sm-side-card">
          <div className="sm-side-head">
            <h3>{copy.alerts}</h3>
            {alerts.length > 3 && (
              <button type="button" className="sm-link" onClick={() => setShowAllAlerts(v => !v)}>
                {showAllAlerts ? copy.collapse : copy.showAll(alerts.length)}
              </button>
            )}
          </div>
          {alerts.length === 0 ? <p className="sm-empty">{copy.noAlerts}</p> : (
            <ul className="sm-alerts">
              {shownAlerts.map(alert => {
                const text = alertText(alert, language);
                return (
                  <li key={`${alert.kind}-${alert.gpu ?? ''}-${alert.start}`} className={`is-${alert.severity}`}>
                    <i />
                    <div>
                      <strong>{text.title}</strong>
                      <span>{text.body}</span>
                    </div>
                    <time>{alert.ongoing ? copy.ongoing : formatClock(alert.end, range === '7d')}</time>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="sm-card sm-side-card">
          <h3><Lightbulb size={14} /> {copy.observations}</h3>
          {notes.length === 0 ? <p className="sm-empty">{copy.noObservations}</p> : (
            <ul className="sm-notes">
              {notes.map(note => (
                <li key={note.id} className={`is-${note.tone}`}>
                  <i />
                  <div>
                    <strong>{note.title}</strong>
                    <span>{note.body}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="sm-card sm-side-card">
          <h3>{copy.gpuDetails}</h3>
          {gpus.length === 0 ? <p className="sm-empty">{copy.noData}</p> : gpus.map((gpu, position) => {
            const draw = finite(gpu.power_draw_watts);
            const limit = finite(gpu.power_limit_watts);
            return (
              <div key={gpu.index ?? position} className="sm-gpu">
                <div className="sm-gpu-head">
                  <i style={{ background: gpuColor(position) }} />
                  <strong>GPU {gpu.index ?? position}</strong>
                  <span>{(gpu.name ?? '').replace(/^NVIDIA /, '')}</span>
                </div>
                <div className="sm-gpu-row"><Zap size={12} />{copy.powerDraw}<b>{draw === null ? '—' : `${Math.round(draw)} / ${limit === null ? '?' : Math.round(limit)} W`}</b></div>
                <Bar value={percent(draw, limit)} color={gpuColor(position)} />
                <div className="sm-gpu-row"><Fan size={12} />{copy.fan}<b>{finite(gpu.fan_percent) === null ? '—' : pct(gpu.fan_percent as number)}</b></div>
              </div>
            );
          })}
        </section>

        <section className="sm-card sm-side-card">
          <h3>{copy.summary} · {copy.ranges[range].toLowerCase()}</h3>
          <dl className="sm-summary">
            <dt>{copy.avgGpuLoad}</dt>
            <dd>{summary.gpuUtilAvg === null ? '—' : `${summary.gpuUtilAvg.toFixed(1)}%`}</dd>
            <dt>{copy.peakGpuTemp}</dt>
            <dd>{summary.gpuTempPeak === null ? '—' : celsius(summary.gpuTempPeak)}</dd>
            <dt>{copy.avgGpuPower}</dt>
            <dd>{summary.gpuPowerAvg === null ? '—' : watts(summary.gpuPowerAvg)}</dd>
            <dt>{copy.gpuEnergy}</dt>
            <dd>{summary.gpuEnergyKwh === null ? '—' : `${summary.gpuEnergyKwh.toFixed(2)} kWh`}</dd>
            <dt>{copy.coverage}</dt>
            <dd>{summary.coverage === null ? '—' : `${summary.coverage.toFixed(1)}%`}</dd>
          </dl>
        </section>

        {containers.length > 0 && (
          <section className="sm-card sm-side-card">
            <div className="sm-side-head">
              <h3>{copy.services}</h3>
              <span className="sm-muted">{copy.servicesUp(containersUp, containers.length)}</span>
            </div>
            <ul className="sm-services">
              {containers.map(c => {
                const tone = c.state !== 'running' ? 'is-down' : c.health === 'unhealthy' ? 'is-bad' : c.health === 'starting' ? 'is-warn' : 'is-ok';
                return (
                  <li key={c.name} className={tone} title={c.status ?? ''}>
                    <i />
                    <span>{(c.name ?? '').replace(/^hermes-(web-)?/, '')}</span>
                    <b>{c.cpu_percent === null || c.cpu_percent === undefined ? c.state : `${c.cpu_percent.toFixed(0)}% CPU`}</b>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </aside>
    </div>
  );
}
