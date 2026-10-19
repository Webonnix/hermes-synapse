import type { HostTelemetry, SystemStats } from '../../types';

/**
 * View-model for the «Метрики» tab: the shapes /api/system/metrics/history
 * returns, unit formatting, and the derived readings (period summary,
 * observations). Pure functions only, so the arithmetic is testable without
 * rendering anything.
 *
 * Rule carried over from useVexaTelemetry: nothing here invents a value. A
 * reading the host agent did not publish is `null` and renders as "нет данных".
 */

export type RangeKey = '1h' | '6h' | '24h' | '7d';
export type Lang = 'ru' | 'en';

export interface GpuPoint {
  i: number;
  util: number | null;
  mem: number | null;
  mem_total: number | null;
  temp: number | null;
  power: number | null;
  limit: number | null;
  util_peak: number | null;
  temp_peak: number | null;
}

export interface HistoryPoint {
  t: number;
  cpu: number | null;
  ram: number | null;
  ram_used: number | null;
  disk_used: number | null;
  disk_total: number | null;
  rx: number | null;
  tx: number | null;
  io_r: number | null;
  io_w: number | null;
  cpu_temp: number | null;
  nvme_temp: number | null;
  gpus: GpuPoint[];
}

export type AlertKind = 'gpu_temp' | 'gpu_peak' | 'cpu_temp' | 'ram' | 'disk' | 'telemetry_gap';

export interface MetricAlert {
  kind: AlertKind;
  severity: 'info' | 'warning' | 'critical';
  start: number;
  end: number;
  ongoing: boolean;
  peak: number | null;
  threshold: number | null;
  gpu?: number;
}

export interface MetricsHistory {
  range: RangeKey;
  from: number;
  to: number;
  bucket_seconds: number;
  sample_interval_seconds: number;
  samples: number;
  first_sample_at: number | null;
  points: HistoryPoint[];
  alerts: MetricAlert[];
  thresholds: {
    gpu_temp_warn: number;
    gpu_temp_crit: number;
    cpu_temp_warn: number;
    ram_warn: number;
    disk_warn: number;
  };
}

export type HostGpu = NonNullable<HostTelemetry['gpus']>[number];

export const RANGE_KEYS: RangeKey[] = ['1h', '6h', '24h', '7d'];
export const REFRESH_OPTIONS = [5, 10, 30, 60, 0] as const;

/** Series colours: GPU 0 green, GPU 1 violet as on the design board, then the rest of the palette. */
export const SERIES_COLORS = ['#35D49A', '#9277FF', '#36CFFB', '#F6B84A', '#F05D75', '#FF6AD5'];

export function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

const GIB = 1024 ** 3;
const TIB = 1024 ** 4;

export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  const value = finite(bytes);
  if (value === null) return '—';
  if (value >= TIB) return `${(value / TIB).toFixed(digits)} TB`;
  if (value >= GIB) return `${(value / GIB).toFixed(digits)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(0)} MB`;
  return `${Math.round(value / 1024)} KB`;
}

/** Network rates are quoted in bits, as every NIC and ISP quotes them. */
export function formatBitrate(bytesPerSecond: number | null | undefined): string {
  const value = finite(bytesPerSecond);
  if (value === null) return '—';
  const bits = value * 8;
  if (bits >= 1e9) return `${(bits / 1e9).toFixed(2)} Gbps`;
  if (bits >= 1e6) return `${(bits / 1e6).toFixed(1)} Mbps`;
  if (bits >= 1e3) return `${(bits / 1e3).toFixed(0)} Kbps`;
  return `${Math.round(bits)} bps`;
}

export function formatByteRate(bytesPerSecond: number | null | undefined): string {
  const value = finite(bytesPerSecond);
  if (value === null) return '—';
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB/s`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB/s`;
  return `${Math.round(value / 1024)} KB/s`;
}

export function formatUptime(seconds: number | null | undefined, lang: Lang): string {
  const value = finite(seconds);
  if (value === null) return '—';
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const [d, h, m] = lang === 'ru' ? ['д', 'ч', 'м'] : ['d', 'h', 'm'];
  if (days > 0) return `${days}${d} ${hours}${h} ${minutes}${m}`;
  if (hours > 0) return `${hours}${h} ${minutes}${m}`;
  return `${minutes}${m}`;
}

export function formatClock(epochSeconds: number, withDate = false): string {
  const date = new Date(epochSeconds * 1000);
  const time = date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  if (!withDate) return time;
  const day = date.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
  return `${day} ${time}`;
}

export function percent(used: number | null | undefined, total: number | null | undefined): number | null {
  const u = finite(used);
  const t = finite(total);
  if (u === null || t === null || t <= 0) return null;
  return Math.max(0, Math.min(100, (u / t) * 100));
}

/** Summed VRAM across every card; null only when no card reports memory. */
export function vramTotals(gpus: HostGpu[]): { used: number | null; total: number | null } {
  const reporting = gpus.filter(g => finite(g.memory_total_bytes) !== null);
  if (!reporting.length) return { used: null, total: null };
  return {
    used: reporting.reduce((sum, g) => sum + (finite(g.memory_used_bytes) ?? 0), 0),
    total: reporting.reduce((sum, g) => sum + (finite(g.memory_total_bytes) ?? 0), 0),
  };
}

export function hottestGpu(gpus: HostGpu[]): number | null {
  const temps = gpus.map(g => finite(g.temperature_celsius)).filter((t): t is number => t !== null);
  return temps.length ? Math.max(...temps) : null;
}

/** All disks the agent watches, summed — the root LV and the fast NVMe store alike. */
export function diskTotals(host: HostTelemetry | undefined): { used: number | null; total: number | null } {
  const disks = host?.disks ?? [];
  if (!disks.length) return { used: null, total: null };
  return {
    used: disks.reduce((sum, d) => sum + (finite(d.used_bytes) ?? 0), 0),
    total: disks.reduce((sum, d) => sum + (finite(d.total_bytes) ?? 0), 0),
  };
}

/** Pulls one numeric series out of the history; nulls are kept so the chart can break the line. */
export function seriesOf(points: HistoryPoint[], read: (p: HistoryPoint) => number | null | undefined): Array<number | null> {
  return points.map(p => finite(read(p)));
}

export function gpuIndexes(points: HistoryPoint[], live: HostGpu[]): number[] {
  const indexes = new Set<number>();
  live.forEach(g => indexes.add(g.index ?? 0));
  points.forEach(p => p.gpus.forEach(g => indexes.add(g.i)));
  return [...indexes].sort((a, b) => a - b);
}

export function gpuField(point: HistoryPoint, index: number, field: keyof GpuPoint): number | null {
  const gpu = point.gpus.find(g => g.i === index);
  return gpu ? finite(gpu[field]) : null;
}

export function mean(values: Array<number | null>): number | null {
  const present = values.filter((v): v is number => v !== null);
  return present.length ? present.reduce((a, b) => a + b, 0) / present.length : null;
}

export function peak(values: Array<number | null>): number | null {
  const present = values.filter((v): v is number => v !== null);
  return present.length ? Math.max(...present) : null;
}

/**
 * Change of the live reading against the period average, in the reading's own
 * units (percentage points, °C). Null when either side is missing.
 */
export function deltaFromMean(current: number | null, series: Array<number | null>): number | null {
  const average = mean(series);
  if (current === null || average === null) return null;
  return current - average;
}

export interface PeriodSummary {
  gpuUtilAvg: number | null;
  gpuTempPeak: number | null;
  /** Average combined board power of all GPUs over the period, watts. */
  gpuPowerAvg: number | null;
  /** Energy the GPUs drew over the measured part of the period, kWh. */
  gpuEnergyKwh: number | null;
  /** Share of the period that actually has samples (agent + backend both up). */
  coverage: number | null;
}

export function periodSummary(history: MetricsHistory | null): PeriodSummary {
  const empty = { gpuUtilAvg: null, gpuTempPeak: null, gpuPowerAvg: null, gpuEnergyKwh: null, coverage: null };
  if (!history || !history.points.length) return empty;
  const utils: Array<number | null> = [];
  const temps: Array<number | null> = [];
  const powers: Array<number | null> = [];
  for (const point of history.points) {
    point.gpus.forEach(g => {
      utils.push(finite(g.util));
      temps.push(finite(g.temp_peak ?? g.temp));
    });
    const reported = point.gpus.map(g => finite(g.power)).filter((v): v is number => v !== null);
    powers.push(reported.length ? reported.reduce((a, b) => a + b, 0) : null);
  }
  const powerAvg = mean(powers);
  const measuredSeconds = history.samples * history.sample_interval_seconds;
  const span = history.to - history.from;
  return {
    gpuUtilAvg: mean(utils),
    gpuTempPeak: peak(temps),
    gpuPowerAvg: powerAvg,
    gpuEnergyKwh: powerAvg === null ? null : (powerAvg * measuredSeconds) / 3_600_000,
    coverage: span > 0 ? Math.min(100, (measuredSeconds / span) * 100) : null,
  };
}

export interface Observation {
  id: string;
  tone: 'amber' | 'green' | 'blue' | 'red';
  title: string;
  body: string;
}

/**
 * Rule-based remarks about the live readings. Each rule fires only on measured
 * data and says which number triggered it; with nothing to remark on the list
 * is empty and the panel says so, rather than padding itself with advice.
 */
export function observations(stats: SystemStats | null, history: MetricsHistory | null, lang: Lang): Observation[] {
  const copy = COPY[lang];
  const host = stats?.host;
  const gpus = host?.gpus ?? [];
  const result: Observation[] = [];
  const thresholds = history?.thresholds;

  const temps = gpus
    .map(g => ({ index: g.index ?? 0, temp: finite(g.temperature_celsius) }))
    .filter((g): g is { index: number; temp: number } => g.temp !== null);
  if (temps.length >= 2) {
    const hot = temps.reduce((a, b) => (b.temp > a.temp ? b : a));
    const cool = temps.reduce((a, b) => (b.temp < a.temp ? b : a));
    if (hot.temp - cool.temp >= 12) {
      result.push({ id: 'gpu-temp-spread', tone: 'amber', ...copy.obsTempSpread(hot.index, cool.index, Math.round(hot.temp - cool.temp)) });
    }
  }
  const warn = thresholds?.gpu_temp_warn ?? 80;
  temps.filter(g => g.temp >= warn).forEach(g => {
    result.push({ id: `gpu-hot-${g.index}`, tone: 'red', ...copy.obsGpuHot(g.index, Math.round(g.temp)) });
  });

  gpus.forEach(g => {
    const memory = percent(g.memory_used_bytes, g.memory_total_bytes);
    if (memory !== null && memory >= 92) {
      result.push({ id: `vram-${g.index}`, tone: 'blue', ...copy.obsVram(g.index ?? 0, Math.round(memory)) });
    }
    const draw = finite(g.power_draw_watts);
    const limit = finite(g.power_limit_watts);
    if (draw !== null && limit !== null && limit > 0 && draw >= limit * 0.97) {
      result.push({ id: `power-${g.index}`, tone: 'amber', ...copy.obsPowerCap(g.index ?? 0, Math.round(limit)) });
    }
  });

  const utils = gpus
    .map(g => ({ index: g.index ?? 0, util: finite(g.utilization_percent) }))
    .filter((g): g is { index: number; util: number } => g.util !== null);
  if (utils.length >= 2) {
    const busy = utils.reduce((a, b) => (b.util > a.util ? b : a));
    const idle = utils.reduce((a, b) => (b.util < a.util ? b : a));
    if (busy.util - idle.util >= 40) {
      result.push({ id: 'gpu-balance', tone: 'blue', ...copy.obsBalance(busy.index, idle.index, Math.round(busy.util), Math.round(idle.util)) });
    }
  }

  const disk = diskTotals(host);
  const diskPercent = percent(disk.used, disk.total);
  if (diskPercent !== null && diskPercent >= (thresholds?.disk_warn ?? 85)) {
    result.push({ id: 'disk', tone: 'red', ...copy.obsDisk(Math.round(diskPercent)) });
  }
  const ram = finite(host?.memory?.usage_percent);
  if (ram !== null && ram >= (thresholds?.ram_warn ?? 90)) {
    result.push({ id: 'ram', tone: 'red', ...copy.obsRam(Math.round(ram)) });
  }
  return result;
}

export function alertText(alert: MetricAlert, lang: Lang): { title: string; body: string } {
  const copy = COPY[lang];
  const gpu = `GPU ${alert.gpu ?? 0}`;
  const minutes = Math.max(1, Math.round((alert.end - alert.start) / 60));
  switch (alert.kind) {
    case 'gpu_temp':
      return copy.alertGpuTemp(gpu, alert.peak ?? 0, alert.threshold ?? 0);
    case 'gpu_peak':
      return copy.alertGpuPeak(gpu, alert.peak ?? 0, minutes);
    case 'cpu_temp':
      return copy.alertCpuTemp(alert.peak ?? 0, alert.threshold ?? 0);
    case 'ram':
      return copy.alertRam(alert.peak ?? 0, alert.threshold ?? 0);
    case 'disk':
      return copy.alertDisk(alert.peak ?? 0, alert.threshold ?? 0);
    case 'telemetry_gap':
      return copy.alertGap(minutes);
  }
}

const RU = {
  eyebrow: 'Локальный сервер',
  title: 'Метрики GPU сервера',
  live: 'Мониторинг в реальном времени',
  online: 'Сервер онлайн',
  stale: 'Данные устарели',
  offline: 'Нет данных с сервера',
  runtimeOnly: 'Только контейнер бэкенда',
  refresh: 'Обновление',
  refreshOff: 'выкл',
  sec: 'сек',
  ranges: { '1h': 'Последний час', '6h': 'Последние 6 часов', '24h': 'Последние 24 часа', '7d': 'Последние 7 дней' } as Record<RangeKey, string>,
  fullscreen: 'Во весь экран',
  exitFullscreen: 'Выйти из полноэкранного режима',
  noData: 'нет данных',
  vram: 'Видеопамять',
  cpu: 'CPU',
  ram: 'RAM',
  disk: 'Диски',
  gpuTemp: 'Температура GPU',
  network: 'Сеть (вх/исх)',
  vsAverage: 'относительно среднего за период',
  gpuLoad: 'Загрузка GPU',
  gpuPower: 'Мощность GPU',
  gpuMemory: 'Память GPU',
  cpuRam: 'CPU и RAM',
  temperatures: 'Температуры',
  diskSystem: 'Дисковая система',
  diskIo: 'I/O операции',
  diskFill: 'Заполненность',
  read: 'Чтение',
  write: 'Запись',
  used: 'Использовано',
  inbound: 'Входящий трафик',
  outbound: 'Исходящий трафик',
  load: 'Загрузка',
  power: 'Мощность',
  usage: 'Использование',
  temperature: 'Температура',
  networkActivity: 'Сетевая активность',
  cpuThreads: (n: number) => `CPU (${n} потоков)`,
  nvme: 'NVMe',
  chartEmpty: 'История пока не накоплена — первые точки появятся через минуту.',
  serverInfo: 'Информация о сервере',
  hardware: 'Аппаратная конфигурация',
  gpu: 'GPU',
  os: 'ОС',
  kernel: 'Ядро',
  driver: 'Драйвер NVIDIA',
  uptime: 'Аптайм',
  ip: 'IP',
  collector: 'Сбор телеметрии',
  collectorAge: (s: number) => `снимок ${Math.round(s)} с назад`,
  alerts: 'Текущие алерты',
  noAlerts: 'За выбранный период порогов не превышено.',
  showAll: (n: number) => `Показать все (${n})`,
  collapse: 'Свернуть',
  ongoing: 'сейчас',
  observations: 'Наблюдения VEXA',
  noObservations: 'Замечаний нет: температура, память и нагрузка в норме.',
  summary: 'Сводка за период',
  avgGpuLoad: 'Средняя загрузка GPU',
  peakGpuTemp: 'Пик температуры GPU',
  avgGpuPower: 'Средняя мощность GPU',
  gpuEnergy: 'Энергия GPU',
  coverage: 'Покрытие данными',
  services: 'Сервисы',
  servicesUp: (up: number, total: number) => `${up} из ${total} работают`,
  gpuDetails: 'Видеокарты',
  fan: 'Вентилятор',
  powerDraw: 'Потребление',
  loadError: 'Не удалось загрузить метрики',
  obsTempSpread: (hot: number, cool: number, diff: number) => ({
    title: 'Проверить охлаждение',
    body: `GPU ${hot} горячее GPU ${cool} на ${diff}°C. Обычно это значит, что карта стоит в потоке горячего воздуха соседней — помогут airflow или более низкий лимит мощности.`,
  }),
  obsGpuHot: (index: number, temp: number) => ({
    title: `GPU ${index}: ${temp}°C`,
    body: 'Температура выше порога предупреждения. Проверьте вентиляторы и пыль.',
  }),
  obsVram: (index: number, value: number) => ({
    title: 'Видеопамять почти заполнена',
    body: `GPU ${index} занят на ${value}%. Новая модель или больший контекст на эту карту не поместятся.`,
  }),
  obsPowerCap: (index: number, limit: number) => ({
    title: `GPU ${index} упирается в лимит мощности`,
    body: `Потребление на уровне лимита ${limit} W — карта ограничена питанием, а не вычислениями.`,
  }),
  obsBalance: (busy: number, idle: number, a: number, b: number) => ({
    title: 'Балансировка нагрузки',
    body: `GPU ${busy} загружен на ${a}%, GPU ${idle} — на ${b}%. Задачи распределены неравномерно.`,
  }),
  obsDisk: (value: number) => ({ title: 'Мало места на дисках', body: `Занято ${value}% объёма.` }),
  obsRam: (value: number) => ({ title: 'Оперативная память на исходе', body: `Занято ${value}% RAM.` }),
  alertGpuTemp: (gpu: string, peak: number, threshold: number) => ({
    title: `Высокая температура ${gpu}`,
    body: `Температура ${peak}°C (порог ${threshold}°C)`,
  }),
  alertGpuPeak: (gpu: string, peak: number, minutes: number) => ({
    title: 'Пиковая нагрузка GPU',
    body: `${gpu}: ${Math.round(peak)}% в течение ${minutes} мин`,
  }),
  alertCpuTemp: (peak: number, threshold: number) => ({
    title: 'Высокая температура CPU',
    body: `Температура ${peak}°C (порог ${threshold}°C)`,
  }),
  alertRam: (peak: number, threshold: number) => ({
    title: 'Высокое потребление RAM',
    body: `${peak}% (порог ${threshold}%)`,
  }),
  alertDisk: (peak: number, threshold: number) => ({
    title: 'Диски заполнены',
    body: `${peak}% (порог ${threshold}%)`,
  }),
  alertGap: (minutes: number) => ({
    title: 'Сбор телеметрии восстановлен',
    body: `Данных не было ${minutes} мин — сервер или сборщик были недоступны.`,
  }),
};

type Copy = typeof RU;

const EN: Copy = {
  eyebrow: 'Local server',
  title: 'GPU server metrics',
  live: 'Real-time monitoring',
  online: 'Server online',
  stale: 'Data is stale',
  offline: 'No data from the server',
  runtimeOnly: 'Backend container only',
  refresh: 'Refresh',
  refreshOff: 'off',
  sec: 's',
  ranges: { '1h': 'Last hour', '6h': 'Last 6 hours', '24h': 'Last 24 hours', '7d': 'Last 7 days' },
  fullscreen: 'Full screen',
  exitFullscreen: 'Exit full screen',
  noData: 'no data',
  vram: 'VRAM',
  cpu: 'CPU',
  ram: 'RAM',
  disk: 'Disks',
  gpuTemp: 'GPU temperature',
  network: 'Network (in/out)',
  vsAverage: 'vs. the period average',
  gpuLoad: 'GPU load',
  gpuPower: 'GPU power',
  gpuMemory: 'GPU memory',
  cpuRam: 'CPU and RAM',
  temperatures: 'Temperatures',
  diskSystem: 'Storage',
  diskIo: 'I/O',
  diskFill: 'Usage',
  read: 'Read',
  write: 'Write',
  used: 'Used',
  inbound: 'Inbound',
  outbound: 'Outbound',
  load: 'Load',
  power: 'Power',
  usage: 'Usage',
  temperature: 'Temperature',
  networkActivity: 'Network activity',
  cpuThreads: (n: number) => `CPU (${n} threads)`,
  nvme: 'NVMe',
  chartEmpty: 'No history yet — the first points appear within a minute.',
  serverInfo: 'Server information',
  hardware: 'Hardware',
  gpu: 'GPU',
  os: 'OS',
  kernel: 'Kernel',
  driver: 'NVIDIA driver',
  uptime: 'Uptime',
  ip: 'IP',
  collector: 'Telemetry',
  collectorAge: (s: number) => `snapshot ${Math.round(s)} s ago`,
  alerts: 'Current alerts',
  noAlerts: 'No thresholds crossed in this period.',
  showAll: (n: number) => `Show all (${n})`,
  collapse: 'Collapse',
  ongoing: 'now',
  observations: 'VEXA observations',
  noObservations: 'Nothing to report: temperatures, memory and load are normal.',
  summary: 'Period summary',
  avgGpuLoad: 'Average GPU load',
  peakGpuTemp: 'Peak GPU temperature',
  avgGpuPower: 'Average GPU power',
  gpuEnergy: 'GPU energy',
  coverage: 'Data coverage',
  services: 'Services',
  servicesUp: (up: number, total: number) => `${up} of ${total} running`,
  gpuDetails: 'Graphics cards',
  fan: 'Fan',
  powerDraw: 'Draw',
  loadError: 'Could not load metrics',
  obsTempSpread: (hot, cool, diff) => ({
    title: 'Check cooling',
    body: `GPU ${hot} runs ${diff}°C hotter than GPU ${cool}, usually because it sits in the other card's exhaust. Better airflow or a lower power limit helps.`,
  }),
  obsGpuHot: (index, temp) => ({ title: `GPU ${index}: ${temp}°C`, body: 'Above the warning threshold. Check the fans and dust.' }),
  obsVram: (index, value) => ({ title: 'VRAM nearly full', body: `GPU ${index} is ${value}% used. A new model or a longer context will not fit there.` }),
  obsPowerCap: (index, limit) => ({ title: `GPU ${index} is at its power limit`, body: `Drawing the full ${limit} W — the card is power-bound, not compute-bound.` }),
  obsBalance: (busy, idle, a, b) => ({ title: 'Load balance', body: `GPU ${busy} is at ${a}%, GPU ${idle} at ${b}%. Work is unevenly spread.` }),
  obsDisk: value => ({ title: 'Disks nearly full', body: `${value}% of capacity used.` }),
  obsRam: value => ({ title: 'Memory running out', body: `${value}% of RAM used.` }),
  alertGpuTemp: (gpu, peak, threshold) => ({ title: `${gpu} temperature high`, body: `${peak}°C (threshold ${threshold}°C)` }),
  alertGpuPeak: (gpu, peak, minutes) => ({ title: 'GPU peak load', body: `${gpu}: ${Math.round(peak)}% for ${minutes} min` }),
  alertCpuTemp: (peak, threshold) => ({ title: 'CPU temperature high', body: `${peak}°C (threshold ${threshold}°C)` }),
  alertRam: (peak, threshold) => ({ title: 'High RAM usage', body: `${peak}% (threshold ${threshold}%)` }),
  alertDisk: (peak, threshold) => ({ title: 'Disks full', body: `${peak}% (threshold ${threshold}%)` }),
  alertGap: minutes => ({ title: 'Telemetry resumed', body: `No data for ${minutes} min — the server or the collector was down.` }),
};

export const COPY: Record<Lang, Copy> = { ru: RU, en: EN };
