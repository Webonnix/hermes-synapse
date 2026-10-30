/**
 * Pure model of the «Десктопы агентов» tab: API shapes, copy, and how the
 * backend's event log (backend/agent_runtimes/sessions.py) folds into a
 * readable transcript.
 */

export type Lang = 'ru' | 'en';

export interface RuntimeInfo { id: string; title: string; login: string }
export type DesktopKindId = 'agent' | 'openbot';
export interface DesktopInfo { key: string; name: string; status: string; busy: string[]; kind?: DesktopKindId }
export interface KindInfo { id: DesktopKindId; enabled: boolean; screens: number }
export interface DesktopListing {
  enabled: boolean;
  runtimes: RuntimeInfo[];
  desktops: DesktopInfo[];
  kinds?: KindInfo[];
  external_models?: boolean;
}
/** /api/agent-desktops/<key>/model-access: what OpenBot's custom provider needs. */
export interface ModelAccess { base_url: string; model: string; api_key: string; models: string[] }

export interface DesktopEvent {
  seq: number;
  at: number;
  type: 'turn_start' | 'turn_end' | 'message' | 'thought' | 'tool' | 'plan' | 'permission' | 'permission_pending';
  runtime?: string;
  run_id?: string;
  text?: string;
  id?: string;
  title?: string;
  kind?: string;
  status?: string;
  entries?: { content: string; status: string }[];
  tool?: string;
  task_id?: string;
  granted?: boolean;
  reason?: string;
  risk_class?: string;
  stop_reason?: string;
  error?: string;
  error_code?: string;
}

export type TranscriptItem =
  | { kind: 'task'; key: string; runtime: string; text: string }
  | { kind: 'message'; key: string; text: string }
  | { kind: 'tool'; key: string; title: string; status: string; toolKind: string }
  | { kind: 'plan'; key: string; entries: { content: string; status: string }[] }
  | { kind: 'permission'; key: string; title: string; taskId: string; state: 'pending' | 'granted' | 'refused'; reason: string; risk: string }
  | { kind: 'end'; key: string; stopReason: string; error: string; needsLogin: boolean };

/** Folds events into transcript rows: streamed message chunks join into one
 * row, a tool call's updates replace its row, a permission's decision replaces
 * its pending row. Events must be in seq order. */
export function buildTranscript(events: DesktopEvent[]): TranscriptItem[] {
  const items: TranscriptItem[] = [];
  const tools = new Map<string, number>();
  const permissions = new Map<string, number>();
  for (const event of events) {
    const last = items[items.length - 1];
    switch (event.type) {
      case 'turn_start':
        items.push({ kind: 'task', key: `t${event.seq}`, runtime: event.runtime ?? '', text: event.text ?? '' });
        break;
      case 'message':
        if (last?.kind === 'message') last.text += event.text ?? '';
        else items.push({ kind: 'message', key: `m${event.seq}`, text: event.text ?? '' });
        break;
      case 'tool': {
        const id = `${event.run_id}:${event.id}`;
        const at = tools.get(id);
        if (at !== undefined) {
          const row = items[at] as Extract<TranscriptItem, { kind: 'tool' }>;
          row.title = event.title ?? row.title;
          row.status = event.status ?? row.status;
          row.toolKind = event.kind ?? row.toolKind;
        } else {
          tools.set(id, items.length);
          items.push({ kind: 'tool', key: `x${event.seq}`, title: event.title ?? event.id ?? '', status: event.status ?? '', toolKind: event.kind ?? '' });
        }
        break;
      }
      case 'plan':
        if (last?.kind === 'plan') last.entries = event.entries ?? [];
        else items.push({ kind: 'plan', key: `p${event.seq}`, entries: event.entries ?? [] });
        break;
      case 'permission_pending':
        permissions.set(event.task_id ?? '', items.length);
        items.push({ kind: 'permission', key: `a${event.seq}`, title: event.title ?? '', taskId: event.task_id ?? '', state: 'pending', reason: '', risk: event.risk_class ?? '' });
        break;
      case 'permission': {
        const at = event.task_id ? permissions.get(event.task_id) : undefined;
        const state = event.granted ? 'granted' : 'refused';
        if (at !== undefined) {
          const row = items[at] as Extract<TranscriptItem, { kind: 'permission' }>;
          row.state = state;
          row.reason = event.reason ?? '';
        } else if (!event.granted) {
          // Granted without waiting is routine and would drown the transcript;
          // only refusals are worth a row of their own.
          items.push({ kind: 'permission', key: `a${event.seq}`, title: event.title ?? '', taskId: event.task_id ?? '', state, reason: event.reason ?? '', risk: '' });
        }
        break;
      }
      case 'turn_end':
        items.push({
          kind: 'end', key: `e${event.seq}`, stopReason: event.stop_reason ?? '', error: event.error ?? '',
          needsLogin: event.error_code === 'auth_required',
        });
        break;
      default:
        break;
    }
  }
  return items;
}

/** Same slug rule as backend/agent_runtimes/desktop.py _KEY_RE. */
export const DESKTOP_KEY_RE = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;

export function normalizeKey(raw: string): string {
  return raw.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
}

export const COPY = {
  ru: {
    eyebrow: 'Изолированные Linux-десктопы',
    title: 'Десктопы агентов',
    subtitle: 'У каждого агента свой Linux с браузером и терминалом в песочнице gVisor. Смотрите, что он делает, и перехватывайте управление.',
    disabled: 'Десктопы агентов выключены на сервере (AGENT_DESKTOPS_ENABLED).',
    newPlaceholder: 'имя-агента',
    create: 'Запустить',
    empty: 'Десктопов пока нет. Введите имя агента и запустите первый.',
    running: 'работает',
    stopped: 'остановлен',
    start: 'Запустить',
    stop: 'Остановить',
    open: 'Открыть',
    viewOnly: 'Только просмотр',
    takeOver: 'Управлять',
    reconnect: 'Переподключить',
    connecting: 'Подключение к экрану…',
    disconnected: 'Экран отключён',
    notRunning: 'Десктоп остановлен. Запустите его, чтобы увидеть экран.',
    runtime: 'Агент',
    taskPlaceholder: 'Что сделать агенту? Например: «Сделай лендинг на Vite и открой его в браузере»',
    send: 'Отправить',
    cancel: 'Остановить ход',
    busy: 'работает…',
    transcript: 'Ход работы',
    noEvents: 'Агент ещё ничего не делал.',
    awaiting: 'ждёт подтверждения в Control Plane',
    granted: 'разрешено',
    refused: 'отказано',
    done: 'Готово',
    cancelled: 'Остановлено',
    loginHint: 'Агент не вошёл в аккаунт: нажмите «Управлять» и выполните в терминале',
    invalidKey: 'Имя: латиница в нижнем регистре, цифры и дефис, до 32 символов.',
    kindAgent: 'Агент (Grok / OpenCode)',
    kindOpenbot: 'OpenBot',
    screenOpenbot: 'OpenBot',
    screenAgents: 'Экран агентов',
    connectTitle: 'Подключение OpenBot',
    connectLogin: 'Нажмите «Управлять» и в окне «OpenBot terminal» выполните',
    connectApp: 'Откройте openbot.run/app или приложение OpenBot с тем же аккаунтом — этот сервер появится в списке.',
    connectProvider: 'В OpenBot добавьте провайдера «Custom OpenAI-compatible»:',
    connectPhone: 'Телефон: в том же терминале',
    connectNote: 'Агенты OpenBot действуют без подтверждений Control Plane: их граница — этот изолированный десктоп. Kill switch отключает им модели.',
    baseUrl: 'Base URL',
    apiKey: 'API key',
    model: 'Модель',
    models: 'Доступные модели',
    copy: 'Копировать',
    copied: 'Скопировано',
    show: 'Показать',
    hide: 'Скрыть',
    toDesktop: 'В десктоп',
    toDesktopTitle: 'Передать текст из вашего буфера обмена в буфер десктопа',
    fromDesktop: 'Из десктопа',
    fromDesktopTitle: 'Скопировать к себе текст, который десктоп положил в буфер',
    clipboardPlaceholder: 'Вставьте сюда текст (Ctrl+V) и нажмите «Передать»',
    clipboardSend: 'Передать',
    clipboardSent: 'Текст в буфере десктопа: нажмите «Управлять», кликните в поле и Ctrl+V.',
    clipboardNotConnected: 'Экран не подключён — передать нельзя.',
    clipboardCopiedOut: 'Скопировано из десктопа.',
    copiedToDesktop: 'Скопировано и передано в десктоп',
  },
  en: {
    eyebrow: 'Isolated Linux desktops',
    title: 'Agent desktops',
    subtitle: 'Every agent gets its own Linux with a browser and a terminal, sandboxed by gVisor. Watch what it does and take over.',
    disabled: 'Agent desktops are disabled on the server (AGENT_DESKTOPS_ENABLED).',
    newPlaceholder: 'agent-name',
    create: 'Start',
    empty: 'No desktops yet. Enter an agent name and start the first one.',
    running: 'running',
    stopped: 'stopped',
    start: 'Start',
    stop: 'Stop',
    open: 'Open',
    viewOnly: 'View only',
    takeOver: 'Take over',
    reconnect: 'Reconnect',
    connecting: 'Connecting to the screen…',
    disconnected: 'Screen disconnected',
    notRunning: 'The desktop is stopped. Start it to see the screen.',
    runtime: 'Agent',
    taskPlaceholder: 'What should the agent do? E.g. "Build a Vite landing page and open it in the browser"',
    send: 'Send',
    cancel: 'Stop turn',
    busy: 'working…',
    transcript: 'Progress',
    noEvents: 'The agent has not done anything yet.',
    awaiting: 'awaiting approval in the Control Plane',
    granted: 'granted',
    refused: 'refused',
    done: 'Done',
    cancelled: 'Stopped',
    loginHint: 'The agent is not signed in: press "Take over" and run in the terminal',
    invalidKey: 'Name: lowercase latin letters, digits and dashes, up to 32 characters.',
    kindAgent: 'Agent (Grok / OpenCode)',
    kindOpenbot: 'OpenBot',
    screenOpenbot: 'OpenBot',
    screenAgents: 'Agents screen',
    connectTitle: 'Connect OpenBot',
    connectLogin: 'Press "Take over" and in the "OpenBot terminal" window run',
    connectApp: 'Open openbot.run/app or the OpenBot app with the same account — this server shows up in its list.',
    connectProvider: 'In OpenBot, add a "Custom OpenAI-compatible" provider:',
    connectPhone: 'Phone: in the same terminal run',
    connectNote: 'OpenBot agents act without Control Plane approvals: this isolated desktop is their boundary. The kill switch cuts their models off.',
    baseUrl: 'Base URL',
    apiKey: 'API key',
    model: 'Model',
    models: 'Available models',
    copy: 'Copy',
    copied: 'Copied',
    show: 'Show',
    hide: 'Hide',
    toDesktop: 'To desktop',
    toDesktopTitle: 'Send text from your clipboard to the desktop clipboard',
    fromDesktop: 'From desktop',
    fromDesktopTitle: 'Copy the text the desktop put on its clipboard',
    clipboardPlaceholder: 'Paste text here (Ctrl+V) and press Send',
    clipboardSend: 'Send',
    clipboardSent: 'Text is on the desktop clipboard: press "Take over", click the field and Ctrl+V.',
    clipboardNotConnected: 'The screen is not connected — nothing sent.',
    clipboardCopiedOut: 'Copied from the desktop.',
    copiedToDesktop: 'Copied and sent to the desktop',
  },
} as const;
