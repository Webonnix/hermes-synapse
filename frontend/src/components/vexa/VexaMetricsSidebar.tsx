import { useEffect, useRef, useState } from 'react';
import type { VexaCopy } from './vexaCopy';
import type { DataStreamRow, ProtocolInfo, ProtocolStatus } from './vexaDashboardTypes';
import { ActivityChart, ResourceGauge } from './VexaVisuals';
import { resourceBand } from './useVexaTelemetry';

/** Narrow metrics column: resources, activity trace, data stream, protocols. */

interface ResourceRow {
  id: string;
  label: string;
  value: number | null;
  /** Optional secondary reading shown next to the value, e.g. GPU temperature. */
  detail?: string | null;
}

export function SystemResourcesCard({ copy, rows }: { copy: VexaCopy; rows: ResourceRow[] }) {
  return (
    <section className="vx-panel is-analytics">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title is-violet">{copy.resources}</h2>
      </div>
      <div className="vx-panel-body">
        {rows.map(row => {
          const band = resourceBand(row.value);
          const tone = row.value === null ? 'is-muted' : band === 'critical' ? 'is-red' : band === 'high' ? 'is-amber' : '';
          return (
            <div className="vx-resource-row" key={row.id}>
              <ResourceGauge value={row.value} label={row.label} />
              <div>
                <span>{row.label}</span>
                <strong className={tone}>
                  {row.value === null ? copy.noData : `${Math.round(row.value)}%`}
                  {row.detail && <em className="vx-resource-detail">{row.detail}</em>}
                </strong>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

export function NeuralActivityCard({ copy, series, live }: { copy: VexaCopy; series: number[]; live: boolean }) {
  const latest = series.length ? series[series.length - 1] : 0;
  return (
    <section className="vx-panel">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.activity}</h2>
        <span className={`vx-panel-aside${live ? ' is-green' : ''}`}>{copy.liveFeed}</span>
      </div>
      <div className="vx-panel-body">
        <div className="vx-chart-box" title={`${copy.activity}: ${Math.round(latest)}%`}>
          <ActivityChart series={series} live={live} />
        </div>
      </div>
    </section>
  );
}

/** Highlights a row for ~400ms after its value changes, then settles back. */
function useFreshness(rows: DataStreamRow[]) {
  const [fresh, setFresh] = useState<Record<string, boolean>>({});
  const seenRef = useRef<Record<string, number>>({});

  useEffect(() => {
    const changed = rows.filter(row => row.updatedAt > 0 && seenRef.current[row.id] !== row.updatedAt);
    if (changed.length === 0) return;
    changed.forEach(row => { seenRef.current[row.id] = row.updatedAt; });
    setFresh(previous => {
      const next = { ...previous };
      changed.forEach(row => { next[row.id] = true; });
      return next;
    });
    const timer = window.setTimeout(() => {
      setFresh(previous => {
        const next = { ...previous };
        changed.forEach(row => { delete next[row.id]; });
        return next;
      });
    }, 400);
    return () => window.clearTimeout(timer);
  }, [rows]);

  return fresh;
}

interface DataStreamProps {
  copy: VexaCopy;
  rows: DataStreamRow[];
  onSeeAll: () => void;
}

export function DataStreamCard({ copy, rows, onSeeAll }: DataStreamProps) {
  const fresh = useFreshness(rows);
  return (
    <section className="vx-panel">
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.dataStream}</h2>
      </div>
      <div className="vx-panel-body">
        {rows.length === 0 && <p className="vx-empty">{copy.noData}</p>}
        {rows.map(row => (
          <div
            className={`vx-stream-row${fresh[row.id] ? ' is-fresh' : ''}`}
            key={row.id}
            title={`${row.label} · ${row.channel}${row.unit ? ` (${row.unit})` : ''}`}
          >
            <span>{row.label}</span>
            {row.value === null ? (
              <strong className="is-muted">—</strong>
            ) : (
              <strong className={row.value < 0 ? 'is-negative' : ''}>
                {row.value >= 0 ? '+' : ''}{row.value.toFixed(1)}
              </strong>
            )}
          </div>
        ))}
        <button type="button" className="vx-link" onClick={onSeeAll}>{copy.seeAll}</button>
      </div>
    </section>
  );
}

const STATUS_CLASS: Record<ProtocolStatus, string> = {
  running: '',
  standby: 'is-standby',
  paused: 'is-paused',
  error: 'is-error',
  offline: 'is-offline',
};

export function ActiveProtocolsCard({ copy, protocols }: { copy: VexaCopy; protocols: ProtocolInfo[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const open = protocols.find(item => item.id === openId) ?? null;

  const statusText: Record<ProtocolStatus, string> = {
    running: copy.statusRunning,
    standby: copy.statusStandby,
    paused: copy.statusPaused,
    error: copy.statusError,
    offline: copy.statusOffline,
  };

  useEffect(() => {
    if (!openId) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpenId(null); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [openId]);

  return (
    <section className="vx-panel" style={{ position: 'relative' }}>
      <div className="vx-panel-head">
        <h2 className="vx-panel-title">{copy.protocols}</h2>
      </div>
      <div className="vx-panel-body">
        {protocols.map(protocol => (
          <button
            type="button"
            className="vx-protocol-row"
            key={protocol.id}
            onClick={() => setOpenId(current => current === protocol.id ? null : protocol.id)}
            aria-expanded={openId === protocol.id}
            title={protocol.detail}
          >
            <span>{protocol.label}</span>
            <strong className={STATUS_CLASS[protocol.status]}>{statusText[protocol.status]}</strong>
          </button>
        ))}
      </div>

      {open && (
        <div className="vx-popover" style={{ left: '100%', bottom: 8, marginLeft: 8 }} role="dialog" aria-label={open.label}>
          <h4>{open.label}</h4>
          <dl>
            <dt>{copy.protocolState}</dt>
            <dd>{statusText[open.status]}</dd>
            {open.version && (<><dt>{copy.protocolVersion}</dt><dd>{open.version}</dd></>)}
            {typeof open.latencyMs === 'number' && (<><dt>{copy.protocolLatency}</dt><dd>{Math.round(open.latencyMs)}ms</dd></>)}
            {open.lastError && (<><dt>{copy.protocolLastError}</dt><dd>{open.lastError}</dd></>)}
          </dl>
          {open.detail && <p className="vx-hint">{open.detail}</p>}
        </div>
      )}
    </section>
  );
}
