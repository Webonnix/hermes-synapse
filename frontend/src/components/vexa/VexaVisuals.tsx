import { useEffect, useRef, useState } from 'react';
import type { LineChartWidget } from './vexaDashboardTypes';
import { resourceBand } from './useVexaTelemetry';

/**
 * Small presentational primitives for the dashboard: gauges, the rolling activity trace,
 * the neural-density thumbnail and the in-message line chart.
 *
 * All of them are SVG or a single 2D canvas redrawn on data change (≤1 Hz) — none of them
 * animate per frame, so they cost nothing between telemetry ticks.
 */

const BAND_COLOR: Record<ReturnType<typeof resourceBand>, string> = {
  nominal: '#2ce8aa',
  elevated: '#47dfff',
  high: '#f1b63d',
  critical: '#ff4e78',
};

export function ResourceGauge({ value, label }: { value: number | null; label: string }) {
  const radius = 11;
  const circumference = 2 * Math.PI * radius;
  const filled = value === null ? 0 : (value / 100) * circumference;
  const color = value === null ? '#344d61' : BAND_COLOR[resourceBand(value)];

  return (
    <svg className="vx-gauge" viewBox="0 0 30 30" role="img" aria-label={`${label}: ${value === null ? 'нет данных' : `${Math.round(value)}%`}`}>
      <circle cx="15" cy="15" r={radius} fill="none" stroke="rgba(45,145,212,.2)" strokeWidth="2.5" />
      <circle
        cx="15"
        cy="15"
        r={radius}
        fill="none"
        stroke={color}
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeDasharray={`${filled} ${circumference}`}
        transform="rotate(-90 15 15)"
        style={{ transition: 'stroke-dasharray 600ms cubic-bezier(.4,0,.2,1), stroke 300ms ease' }}
      />
      <circle cx="15" cy="15" r="4.5" fill="none" stroke={color} strokeWidth="1" opacity=".45" />
    </svg>
  );
}

/**
 * Rolling neural-activity trace. Redrawn only when the series changes (once per second);
 * the canvas is sized from its own box so it stays crisp on HiDPI without a layout pass.
 */
export function ActivityChart({ series, live }: { series: number[]; live: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const bounds = canvas.getBoundingClientRect();
    if (bounds.width === 0 || bounds.height === 0) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const width = Math.round(bounds.width * dpr);
    const height = Math.round(bounds.height * dpr);
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.clearRect(0, 0, width, height);

    // grid
    ctx.strokeStyle = 'rgba(45, 145, 212, .12)';
    ctx.lineWidth = Math.max(1, dpr * 0.5);
    for (let index = 1; index < 4; index += 1) {
      const y = (height / 4) * index;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }

    if (series.length < 2) return;
    const step = width / (series.length - 1);
    const pointY = (value: number) => height - (Math.max(0, Math.min(100, value)) / 100) * (height * 0.9) - height * 0.05;

    ctx.beginPath();
    ctx.moveTo(0, height);
    series.forEach((value, index) => ctx.lineTo(index * step, pointY(value)));
    ctx.lineTo(width, height);
    ctx.closePath();
    const fill = ctx.createLinearGradient(0, 0, 0, height);
    fill.addColorStop(0, live ? 'rgba(27, 220, 255, .28)' : 'rgba(88, 117, 139, .16)');
    fill.addColorStop(1, 'rgba(27, 220, 255, 0)');
    ctx.fillStyle = fill;
    ctx.fill();

    ctx.beginPath();
    series.forEach((value, index) => {
      const x = index * step;
      const y = pointY(value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = live ? '#1bdcff' : '#58758b';
    ctx.lineWidth = Math.max(1, dpr * 1.2);
    ctx.stroke();
  }, [series, live]);

  return <canvas ref={canvasRef} className="vx-chart" aria-hidden="true" />;
}

/**
 * Neural-density thumbnail: a static point cloud with a very slow CSS rotation. Positions
 * are generated once from a fixed seed so the graph does not reshuffle on every render.
 */
export function DensityGraph({ intensity }: { intensity: number }) {
  const [nodes] = useState(() => {
    let seed = 7;
    const random = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    return Array.from({ length: 26 }, () => {
      const angle = random() * Math.PI * 2;
      const radius = 6 + Math.sqrt(random()) * 22;
      return {
        x: 30 + Math.cos(angle) * radius,
        y: 30 + Math.sin(angle) * radius,
        r: 0.9 + random() * 1.5,
        violet: random() > 0.65,
      };
    });
  });
  const opacity = 0.35 + Math.max(0, Math.min(1, intensity)) * 0.6;

  return (
    <svg viewBox="0 0 60 60" width="60" height="60" aria-hidden="true">
      <g style={{ transformOrigin: '30px 30px', animation: 'vx-orbit 42s linear infinite' }}>
        {nodes.map((node, index) => {
          const next = nodes[(index + 5) % nodes.length];
          return (
            <line
              key={`l${index}`}
              x1={node.x} y1={node.y} x2={next.x} y2={next.y}
              stroke={node.violet ? 'rgba(161,93,255,.35)' : 'rgba(38,132,255,.32)'}
              strokeWidth=".4"
            />
          );
        })}
        {nodes.map((node, index) => (
          <circle
            key={`c${index}`}
            cx={node.x} cy={node.y} r={node.r}
            fill={node.violet ? '#a15dff' : '#47dfff'}
            opacity={opacity}
          />
        ))}
      </g>
      <circle cx="30" cy="30" r="4.5" fill="#c9f2ff" opacity={opacity} />
      <circle cx="30" cy="30" r="9" fill="none" stroke="rgba(27,220,255,.28)" strokeWidth=".6" />
    </svg>
  );
}

const SERIES_COLOR = {
  cyan: '#1bdcff',
  blue: '#2684ff',
  violet: '#a15dff',
  green: '#2ce8aa',
  red: '#ff4e78',
} as const;

/** Compact area/line chart rendered inside an assistant message. */
export function MessageLineChart({ widget }: { widget: LineChartWidget }) {
  const width = 300;
  const height = 84;
  const all = widget.series.flatMap(item => item.data);
  if (all.length === 0) return null;
  const max = Math.max(...all);
  const min = Math.min(0, Math.min(...all));
  const span = max - min || 1;

  const toPath = (data: number[], close: boolean) => {
    if (data.length === 0) return '';
    const step = data.length > 1 ? width / (data.length - 1) : width;
    const points = data.map((value, index) => `${index * step},${height - ((value - min) / span) * (height - 10) - 5}`);
    return close ? `M0,${height} L${points.join(' L')} L${width},${height} Z` : `M${points.join(' L')}`;
  };

  return (
    <figure className="vx-message-chart">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={widget.title || 'График'}>
        {[0.25, 0.5, 0.75].map(ratio => (
          <line key={ratio} x1="0" y1={height * ratio} x2={width} y2={height * ratio} stroke="rgba(45,145,212,.14)" strokeWidth=".5" />
        ))}
        {widget.series.map((item, index) => {
          const color = SERIES_COLOR[item.colorToken ?? (index === 0 ? 'violet' : 'blue')];
          return (
            <g key={item.name}>
              {index === 0 && <path d={toPath(item.data, true)} fill={color} opacity=".22" />}
              <path d={toPath(item.data, false)} fill="none" stroke={color} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
            </g>
          );
        })}
      </svg>
      <figcaption>
        {widget.xAxis.length > 0 && <span>{widget.xAxis[0]}</span>}
        {widget.xAxis.length > 2 && <span>{widget.xAxis[Math.floor(widget.xAxis.length / 2)]}</span>}
        {widget.xAxis.length > 1 && <span>{widget.xAxis[widget.xAxis.length - 1]}</span>}
      </figcaption>
    </figure>
  );
}
