import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { parseLineChartWidget } from './vexaDashboardTypes';
import { resourceBand, RESOURCE_THRESHOLDS } from './useVexaTelemetry';
import { VEXA_COPY } from './vexaCopy';
import { SystemStatusCard, NeuralDensityCard } from './VexaSystemSidebar';
import { ActiveProtocolsCard, DataStreamCard, SystemResourcesCard } from './VexaMetricsSidebar';
import { AgentCircuitCard, SystemInsightsCard } from './VexaRightSidebar';
import { VexaBottomNav } from './VexaBottomNav';
import type { InsightMetric, ProtocolInfo, SystemOverview } from './vexaDashboardTypes';

const copy = VEXA_COPY.ru;

const overview: SystemOverview = {
  circuit: { status: 'online' },
  agentsTotal: 10,
  agentsActive: 8,
  voice: { mode: 'local', engine: 'xtts', voice: 'v-exa', status: 'ready' },
  resources: { cpu: 68, memory: 72, gpu: 63, network: 100 },
  uptimeSeconds: 24 * 86400 + 7 * 3600,
  updatedAt: '2026-07-29T12:00:00Z',
  stale: false,
};

describe('parseLineChartWidget', () => {
  it('accepts a well-formed widget', () => {
    const parsed = parseLineChartWidget({
      type: 'line-chart',
      title: 'Trends',
      xAxis: ['00:00', '12:00'],
      series: [{ name: 'load', data: [1, 2, 3], colorToken: 'violet' }],
    });
    expect(parsed?.series[0].colorToken).toBe('violet');
    expect(parsed?.xAxis).toHaveLength(2);
  });

  it('rejects payloads that are not a line chart or carry non-numeric data', () => {
    expect(parseLineChartWidget(null)).toBeNull();
    expect(parseLineChartWidget({ type: 'pie', xAxis: [], series: [] })).toBeNull();
    expect(parseLineChartWidget({ type: 'line-chart', xAxis: ['a'], series: [] })).toBeNull();
    expect(parseLineChartWidget({
      type: 'line-chart', xAxis: ['a'], series: [{ name: 'x', data: ['nope'] }],
    })).toBeNull();
  });

  it('drops an unknown colour token instead of passing it through', () => {
    const parsed = parseLineChartWidget({
      type: 'line-chart', xAxis: ['a'], series: [{ name: 'x', data: [1], colorToken: 'neon' }],
    });
    expect(parsed?.series[0].colorToken).toBeUndefined();
  });
});

describe('resourceBand', () => {
  it('maps values onto the configured colour bands', () => {
    expect(resourceBand(null)).toBe('nominal');
    expect(resourceBand(RESOURCE_THRESHOLDS.nominal - 1)).toBe('nominal');
    expect(resourceBand(RESOURCE_THRESHOLDS.nominal)).toBe('elevated');
    expect(resourceBand(RESOURCE_THRESHOLDS.elevated)).toBe('high');
    expect(resourceBand(RESOURCE_THRESHOLDS.critical)).toBe('critical');
  });
});

describe('SystemStatusCard', () => {
  it('renders live telemetry with a formatted uptime', () => {
    render(<SystemStatusCard copy={copy} overview={overview} />);
    expect(screen.getAllByText('ONLINE').length).toBeGreaterThan(0);
    expect(screen.getByText('68%')).toBeInTheDocument();
    expect(screen.getByText('24d 7h')).toBeInTheDocument();
  });

  it('shows "нет данных" instead of a zero when a metric is unavailable', () => {
    render(
      <SystemStatusCard
        copy={copy}
        overview={{ ...overview, resources: { cpu: null, memory: null, gpu: null, network: null }, uptimeSeconds: null }}
      />,
    );
    expect(screen.getAllByText('Нет данных').length).toBe(4);
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });
});

describe('SystemResourcesCard', () => {
  it('labels an unavailable gauge rather than drawing it at zero', () => {
    render(<SystemResourcesCard copy={copy} rows={[{ id: 'gpu', label: 'GPU', value: null }]} />);
    expect(screen.getByText('Нет данных')).toBeInTheDocument();
    expect(screen.getByLabelText('GPU: нет данных')).toBeInTheDocument();
  });
});

describe('DataStreamCard', () => {
  it('signs deltas and colours negatives differently', () => {
    render(
      <DataStreamCard
        copy={copy}
        onSeeAll={vi.fn()}
        rows={[
          { id: 'cpu', label: 'DATA-01', channel: 'CPU', value: 12.6, updatedAt: 1 },
          { id: 'ram', label: 'DATA-02', channel: 'RAM', value: -2.1, updatedAt: 1 },
        ]}
      />,
    );
    expect(screen.getByText('+12.6')).toBeInTheDocument();
    expect(screen.getByText('-2.1')).toHaveClass('is-negative');
    // The DATA-NN label matches the reference, but the row must still say what it measures.
    expect(screen.getByText('DATA-01').closest('.vx-stream-row')).toHaveAttribute('title', expect.stringContaining('CPU'));
  });
});

describe('ActiveProtocolsCard', () => {
  const protocols: ProtocolInfo[] = [
    { id: 'nlu', label: 'NLU Engine', status: 'running', detail: 'ядро', latencyMs: 48 },
    { id: 'tts', label: 'Voice Synthesis', status: 'standby' },
  ];

  it('opens a detail popover for the clicked protocol', () => {
    render(<ActiveProtocolsCard copy={copy} protocols={protocols} />);
    expect(screen.getByText('STANDBY')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /NLU Engine/ }));
    expect(screen.getByRole('dialog', { name: 'NLU Engine' })).toBeInTheDocument();
    expect(screen.getByText('48ms')).toBeInTheDocument();
  });
});

describe('AgentCircuitCard', () => {
  it('reports the active/total ratio and opens an agent channel', () => {
    const onOpenAgent = vi.fn();
    render(
      <AgentCircuitCard
        copy={copy}
        agents={[
          { id: 'jarvis', name: 'Vexa (Main)', system_prompt: '', model: 'q', status: 'idle' },
          { id: 'research', name: 'Data Analyst', system_prompt: '', model: 'q', status: 'working' },
        ]}
        activeCount={1}
        selectedAgentId="jarvis"
        onOpenAgent={onOpenAgent}
        onOpenAll={vi.fn()}
      />,
    );
    expect(screen.getByText('1/2')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Data Analyst/ }));
    expect(onOpenAgent).toHaveBeenCalledWith('research');
  });
});

describe('SystemInsightsCard', () => {
  const metrics: InsightMetric[] = [
    { id: 'efficiency', label: 'efficiency', value: 97.3, formattedValue: '97.3%', min: 0, max: 100, status: 'normal', updatedAt: null },
    { id: 'accuracy', label: 'accuracy', value: null, formattedValue: null, min: 0, max: 100, status: 'unknown', updatedAt: null },
  ];

  it('renders known values and marks missing ones as having no data', () => {
    render(<SystemInsightsCard copy={copy} metrics={metrics} />);
    expect(screen.getByText('97.3%')).toBeInTheDocument();
    expect(screen.getByText('Нет данных')).toBeInTheDocument();
  });
});

describe('NeuralDensityCard', () => {
  it('falls back to a dash when the aggregate cannot be computed', () => {
    render(<NeuralDensityCard copy={copy} density={null} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});

describe('VexaBottomNav', () => {
  it('marks the active route and routes clicks', () => {
    const onNavigate = vi.fn();
    render(<VexaBottomNav copy={copy} active="terminal" onNavigate={onNavigate} version="2.0.1" connected />);
    expect(screen.getByRole('button', { name: 'Терминал' })).toHaveAttribute('aria-current', 'page');
    fireEvent.click(screen.getByRole('button', { name: 'Аналитика' }));
    expect(onNavigate).toHaveBeenCalledWith('analytics');
  });
});
