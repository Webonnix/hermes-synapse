import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { formatClock } from './serverMetricsModel';

/**
 * SVG charts for the «Метрики» tab. Hand-rolled rather than a chart library:
 * the app ships none, and these need only lines, gaps and a hover readout.
 *
 * A hole in the data (null value, or two points further apart than two
 * buckets) breaks the line instead of bridging it — a straight segment across
 * an outage would claim measurements nobody took.
 */

export interface ChartSeries {
  id: string;
  label: string;
  color: string;
  values: Array<number | null>;
  /** Dashed reference line (e.g. a disk's total capacity) rather than a measurement. */
  dashed?: boolean;
  fill?: boolean;
}

interface LineChartProps {
  times: number[];
  series: ChartSeries[];
  from: number;
  to: number;
  bucketSeconds: number;
  /** Fixed upper bound (100 for percentages); omitted → fitted to the data. */
  yMax?: number;
  /** Size of one display unit (1 MiB for MB/s axes) so a fitted axis lands on round numbers of it. */
  yUnit?: number;
  formatY: (value: number) => string;
  formatValue?: (value: number) => string;
  height?: number;
  emptyText: string;
}

const PAD = { top: 10, right: 12, bottom: 24, left: 52 };

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function useWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(fallback);
  // Layout effect: measure before the first paint, so the fallback width is never drawn on a narrow screen.
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (element.clientWidth > 0) setWidth(element.clientWidth);
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(entries => {
      const next = Math.round(entries[0]?.contentRect.width ?? 0);
      if (next > 0) setWidth(next);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Path segments for one series, split wherever the data has a hole. */
function segments(times: number[], values: Array<number | null>, gap: number) {
  const result: Array<Array<[number, number]>> = [];
  let current: Array<[number, number]> = [];
  values.forEach((value, index) => {
    const broken = index > 0 && times[index] - times[index - 1] > gap;
    if (value === null || broken) {
      if (current.length) result.push(current);
      current = [];
    }
    if (value !== null) current.push([times[index], value]);
  });
  if (current.length) result.push(current);
  return result;
}

export function MetricsLineChart({
  times, series, from, to, bucketSeconds, yMax, yUnit = 1, formatY, formatValue, height = 190, emptyText,
}: LineChartProps) {
  const [wrapRef, width] = useWidth<HTMLDivElement>(560);
  const [hover, setHover] = useState<number | null>(null);

  const top = useMemo(() => {
    if (yMax !== undefined) return yMax;
    let max = 0;
    series.forEach(s => s.values.forEach(v => { if (v !== null && v > max) max = v; }));
    return niceMax((max * 1.1) / yUnit) * yUnit;
  }, [series, yMax, yUnit]);

  const plotW = Math.max(10, width - PAD.left - PAD.right);
  const plotH = Math.max(10, height - PAD.top - PAD.bottom);
  const span = Math.max(1, to - from);
  const x = (t: number) => PAD.left + ((t - from) / span) * plotW;
  const y = (v: number) => PAD.top + plotH - (Math.max(0, Math.min(top, v)) / top) * plotH;
  const gap = Math.max(2 * bucketSeconds, 90);
  const withDate = span > 36 * 3600;
  const hasData = series.some(s => s.values.some(v => v !== null));

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map(f => f * top);
  const xTicks = Array.from({ length: 7 }, (_, i) => from + (span * i) / 6);

  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!times.length) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const t = from + ((event.clientX - bounds.left - PAD.left) / plotW) * span;
    let best = 0;
    times.forEach((time, index) => {
      if (Math.abs(time - t) < Math.abs(times[best] - t)) best = index;
    });
    setHover(best);
  };

  const fmt = formatValue ?? formatY;

  return (
    <div className="sm-chart" ref={wrapRef} style={{ height }}>
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={series.map(s => s.label).join(', ')}
        onMouseMove={handleMove}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          {series.filter(s => s.fill).map(s => (
            <linearGradient key={s.id} id={`sm-fill-${s.id}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity="0.28" />
              <stop offset="100%" stopColor={s.color} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>
        {yTicks.map(tick => (
          <g key={tick}>
            <line className="sm-grid" x1={PAD.left} x2={PAD.left + plotW} y1={y(tick)} y2={y(tick)} />
            <text className="sm-axis" x={PAD.left - 8} y={y(tick) + 4} textAnchor="end">{formatY(tick)}</text>
          </g>
        ))}
        {xTicks.map(tick => (
          <text key={tick} className="sm-axis" x={x(tick)} y={height - 6} textAnchor="middle">
            {formatClock(tick, withDate)}
          </text>
        ))}
        {series.map(s => segments(times, s.values, gap).map((segment, index) => {
          const line = segment.map(([t, v], i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join('');
          const area = segment.length > 1
            ? `${line}L${x(segment[segment.length - 1][0]).toFixed(1)},${y(0)}L${x(segment[0][0]).toFixed(1)},${y(0)}Z`
            : '';
          return (
            <g key={`${s.id}-${index}`}>
              {s.fill && area && <path d={area} fill={`url(#sm-fill-${s.id})`} />}
              {segment.length === 1
                ? <circle cx={x(segment[0][0])} cy={y(segment[0][1])} r={1.8} fill={s.color} />
                : <path d={line} fill="none" stroke={s.color} strokeWidth={s.dashed ? 1 : 1.6}
                    strokeDasharray={s.dashed ? '4 4' : undefined} strokeLinejoin="round" />}
            </g>
          );
        }))}
        {hover !== null && times[hover] !== undefined && (
          <line className="sm-crosshair" x1={x(times[hover])} x2={x(times[hover])} y1={PAD.top} y2={PAD.top + plotH} />
        )}
      </svg>
      {!hasData && <div className="sm-chart-empty">{emptyText}</div>}
      {hover !== null && times[hover] !== undefined && (
        <div
          className="sm-tooltip"
          style={{
            left: Math.min(width - 170, Math.max(0, x(times[hover]) + 10)),
            top: PAD.top,
          }}
        >
          <div className="sm-tooltip-time">{formatClock(times[hover], withDate)}</div>
          {series.map(s => (
            <div key={s.id} className="sm-tooltip-row">
              <i style={{ background: s.color }} />
              <span>{s.label}</span>
              <strong>{s.values[hover] === null ? '—' : fmt(s.values[hover] as number)}</strong>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Tiny trend line for a KPI card; no axes, gaps still break the line. */
export function Sparkline({ times, values, color, max, bucketSeconds }: {
  times: number[];
  values: Array<number | null>;
  color: string;
  max?: number;
  bucketSeconds: number;
}) {
  const width = 160;
  const height = 38;
  const present = values.filter((v): v is number => v !== null);
  if (present.length < 2 || times.length < 2) return <svg className="sm-spark" viewBox={`0 0 ${width} ${height}`} aria-hidden="true" />;
  const top = max ?? (Math.max(...present) * 1.15 || 1);
  const from = times[0];
  const span = Math.max(1, times[times.length - 1] - from);
  const x = (t: number) => ((t - from) / span) * width;
  const y = (v: number) => height - 2 - (Math.max(0, Math.min(top, v)) / top) * (height - 4);
  const gradientId = `sm-spark-${color.replace('#', '')}`;
  return (
    <svg className="sm-spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.35" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {segments(times, values, Math.max(2 * bucketSeconds, 90)).map((segment, index) => {
        if (segment.length < 2) return null;
        const line = segment.map(([t, v], i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join('');
        const area = `${line}L${x(segment[segment.length - 1][0]).toFixed(1)},${height}L${x(segment[0][0]).toFixed(1)},${height}Z`;
        return (
          <g key={index}>
            <path d={area} fill={`url(#${gradientId})`} />
            <path d={line} fill="none" stroke={color} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
          </g>
        );
      })}
    </svg>
  );
}
