import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ServerMetricsTab } from './ServerMetricsTab';
import {
  alertText, deltaFromMean, formatBitrate, observations, periodSummary,
  type MetricsHistory,
} from './serverMetricsModel';
import type { SystemStats } from '../../types';

const GIB = 1024 ** 3;

const stats: SystemStats = {
  status: 'nominal',
  scope: 'physical_host',
  stale: false,
  age_seconds: 2,
  host: {
    hostname: 'webonnix-ai',
    os: 'Ubuntu 26.04.1 LTS',
    kernel: '7.0.0-38-generic',
    uptime_seconds: 191251,
    cpu: { model: 'Intel(R) Xeon(R) CPU E5-2699 v4 @ 2.20GHz', logical_cores: 44, usage_percent: 12.5, load_1m: 0.6 },
    memory: { total_bytes: 64 * GIB, used_bytes: 21.4 * GIB, usage_percent: 33.4 },
    disks: [{ mountpoint: '/', total_bytes: 1000 * GIB, used_bytes: 200 * GIB }],
    disk_io: { read_bytes_per_second: 0, write_bytes_per_second: 2 * 1024 ** 2, devices: [{ name: 'nvme0n1', model: 'Samsung SSD 980 PRO 1TB' }] },
    temperatures: { cpu_celsius: 41, nvme: [{ device: 'nvme0', celsius: 38 }] },
    network: { primary_ip: '192.0.2.10', rx_bytes_per_second: 15_000_000, tx_bytes_per_second: 10_625_000, interfaces: [{ name: 'enp6s0' }] },
    gpus: [
      { index: 0, name: 'NVIDIA GeForce RTX 3090', utilization_percent: 67, temperature_celsius: 68, memory_used_bytes: 14.8 * GIB, memory_total_bytes: 24 * GIB, power_draw_watts: 250, power_limit_watts: 300, fan_percent: 55, driver_version: '595.91' },
      { index: 1, name: 'NVIDIA GeForce RTX 3090', utilization_percent: 42, temperature_celsius: 50, memory_used_bytes: 9.1 * GIB, memory_total_bytes: 24 * GIB, power_draw_watts: 180, power_limit_watts: 300, fan_percent: 40 },
    ],
    containers: [{ name: 'hermes-web-backend', state: 'running', health: 'healthy', cpu_percent: 3 }],
  },
};

const history: MetricsHistory = {
  range: '24h', from: 1000, to: 1000 + 86400, bucket_seconds: 360, sample_interval_seconds: 30,
  samples: 1440, first_sample_at: 1000,
  points: [
    { t: 1000, cpu: 10, ram: 30, ram_used: 20 * GIB, disk_used: 200 * GIB, disk_total: 1000 * GIB, rx: 1e6, tx: 1e6, io_r: 0, io_w: 0, cpu_temp: 40, nvme_temp: 37,
      gpus: [{ i: 0, util: 40, mem: 14 * GIB, mem_total: 24 * GIB, temp: 60, power: 200, limit: 300, util_peak: 60, temp_peak: 66 },
             { i: 1, util: 20, mem: 9 * GIB, mem_total: 24 * GIB, temp: 48, power: 100, limit: 300, util_peak: 30, temp_peak: 50 }] },
    { t: 1360, cpu: 14, ram: 32, ram_used: 21 * GIB, disk_used: 200 * GIB, disk_total: 1000 * GIB, rx: 2e6, tx: 1e6, io_r: 0, io_w: 0, cpu_temp: 41, nvme_temp: 38,
      gpus: [{ i: 0, util: 80, mem: 14 * GIB, mem_total: 24 * GIB, temp: 70, power: 280, limit: 300, util_peak: 99, temp_peak: 81 },
             { i: 1, util: 40, mem: 9 * GIB, mem_total: 24 * GIB, temp: 52, power: 120, limit: 300, util_peak: 50, temp_peak: 53 }] },
  ],
  alerts: [{ kind: 'gpu_temp', severity: 'warning', start: 1300, end: 1360, ongoing: false, peak: 81, threshold: 80, gpu: 0 }],
  thresholds: { gpu_temp_warn: 80, gpu_temp_crit: 87, cpu_temp_warn: 85, ram_warn: 90, disk_warn: 85 },
};

describe('serverMetricsModel', () => {
  it('quotes network rates in bits', () => {
    expect(formatBitrate(15_000_000)).toBe('120.0 Mbps');
    expect(formatBitrate(null)).toBe('—');
  });

  it('measures the live reading against the period average', () => {
    expect(deltaFromMean(67, [40, 80])).toBe(7);
    expect(deltaFromMean(null, [40, 80])).toBeNull();
    expect(deltaFromMean(50, [null, null])).toBeNull();
  });

  it('summarises GPU power into energy over the measured time only', () => {
    const summary = periodSummary(history);
    // (300 W + 400 W) / 2 = 350 W over 1440 × 30 s = 12 h → 4.2 kWh
    expect(summary.gpuPowerAvg).toBe(350);
    expect(summary.gpuEnergyKwh).toBeCloseTo(4.2, 5);
    expect(summary.gpuTempPeak).toBe(81);
    expect(summary.coverage).toBe(50);
  });

  it('remarks on a hot card next to a cool one, and stays quiet otherwise', () => {
    const notes = observations(stats, history, 'ru');
    expect(notes.map(n => n.id)).toContain('gpu-temp-spread');
    const calm = structuredClone(stats);
    calm.host!.gpus![1].temperature_celsius = 64;
    calm.host!.gpus![1].utilization_percent = 60;
    expect(observations(calm, history, 'ru')).toEqual([]);
  });

  it('words alerts from structured fields', () => {
    expect(alertText(history.alerts[0], 'ru')).toEqual({
      title: 'Высокая температура GPU 0',
      body: 'Температура 81°C (порог 80°C)',
    });
  });
});

describe('ServerMetricsTab', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders live host readings and history alerts', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      const body = path.startsWith('/api/system/metrics/history') ? history : stats;
      return { ok: true, json: async () => body };
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ServerMetricsTab language="ru" />);

    expect((await screen.findAllByText('webonnix-ai')).length).toBeGreaterThan(0);
    expect(screen.getByText('Сервер онлайн', { selector: '.sm-chip' })).toBeInTheDocument();
    expect(screen.getByText('67%', { selector: '.sm-kpi-value' })).toBeInTheDocument();
    expect(screen.getByText('250 / 300 W', { selector: '.sm-kpi-sub' })).toBeInTheDocument();
    expect(screen.getByText('Высокая температура GPU 0')).toBeInTheDocument();
    expect(screen.getByText('Проверить охлаждение')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/system/metrics/history?range=24h');
  });

  it('says the host agent is missing instead of showing container numbers as the server', async () => {
    const runtimeOnly: SystemStats = { status: 'nominal', scope: 'backend_runtime', cpu_load_percent: 5 };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => runtimeOnly })));

    render(<ServerMetricsTab language="ru" />);

    expect(await screen.findByText('Только контейнер бэкенда', { selector: '.sm-chip' })).toBeInTheDocument();
  });
});
