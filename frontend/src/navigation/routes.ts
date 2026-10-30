/** Domain destinations share the existing pages and preserve bookmarked hashes. */
export const appTabs = ['vexa', 'processes', 'devruns', 'kanban', 'clients', 'agents', 'schedule', 'contentfactory', 'smm', 'settings', 'network', 'metrics', 'earner', 'desktops'] as const;
export type AppTab = typeof appTabs[number];
export type SettingsSection = 'config' | 'currency' | 'subagents' | 'mcp' | 'obsidian' | 'memory' | 'logs' | 'activity' | 'keys' | 'browser';
export type SmmView = 'publications' | 'designer' | 'connections' | 'dialogs' | 'drafts' | 'activity' | 'settings';
export type Destination = { tab: AppTab; section?: SettingsSection; view?: SmmView; filter?: 'approval' | 'all' };
export type Zone = { id: string; ru: string; en: string; home: Destination; pages: { ru: string; en: string; to: Destination }[] };
export const ZONES: Zone[] = [
  { id: 'vexa', ru: 'Vexa', en: 'Vexa', home: { tab: 'vexa' }, pages: [] },
  { id: 'agents', ru: 'Агенты', en: 'Agents', home: { tab: 'agents' }, pages: [
    { ru: 'Карточки агентов', en: 'Agent profiles', to: { tab: 'agents' } },
    { ru: 'Десктопы', en: 'Desktops', to: { tab: 'desktops' } },
    { ru: 'Память', en: 'Memory', to: { tab: 'settings', section: 'memory' } },
  ] },
  { id: 'work', ru: 'Работа', en: 'Work', home: { tab: 'kanban' }, pages: [
    { ru: 'Доска задач', en: 'Task board', to: { tab: 'kanban' } },
    { ru: 'Запуски', en: 'Runs', to: { tab: 'processes', filter: 'all' } },
    { ru: 'Разработка', en: 'Development', to: { tab: 'devruns' } },
    { ru: 'Расписание', en: 'Schedule', to: { tab: 'schedule' } },
    { ru: 'Заработок', en: 'Earner', to: { tab: 'earner' } },
  ] },
  { id: 'clients', ru: 'Клиенты', en: 'Clients', home: { tab: 'clients' }, pages: [
    { ru: 'Клиенты и сделки', en: 'Clients and deals', to: { tab: 'clients' } },
    { ru: 'Входящие', en: 'Inbox', to: { tab: 'smm', view: 'dialogs' } },
  ] },
  { id: 'content', ru: 'Контент', en: 'Content', home: { tab: 'contentfactory' }, pages: [
    { ru: 'Создание', en: 'Create', to: { tab: 'contentfactory' } },
    { ru: 'Публикации', en: 'Publications', to: { tab: 'smm', view: 'publications' } },
    { ru: 'Дизайнер', en: 'Designer', to: { tab: 'smm', view: 'designer' } },
    { ru: 'Черновики', en: 'Drafts', to: { tab: 'smm', view: 'drafts' } },
  ] },
  { id: 'operations', ru: 'Операции', en: 'Operations', home: { tab: 'processes', filter: 'approval' }, pages: [
    { ru: 'Подтверждения', en: 'Approvals', to: { tab: 'processes', filter: 'approval' } },
    { ru: 'Решения', en: 'Decisions', to: { tab: 'settings', section: 'logs' } },
    { ru: 'Активность', en: 'Activity', to: { tab: 'settings', section: 'activity' } },
    { ru: 'Метрики', en: 'Metrics', to: { tab: 'metrics' } },
    { ru: 'Архитектура', en: 'Architecture', to: { tab: 'network' } },
  ] },
];
export const UTILITIES: Zone[] = [
  { id: 'integrations', ru: 'Интеграции', en: 'Integrations', home: { tab: 'settings', section: 'mcp' }, pages: [
    { ru: 'MCP', en: 'MCP', to: { tab: 'settings', section: 'mcp' } },
    { ru: 'Каналы', en: 'Channels', to: { tab: 'smm', view: 'connections' } },
    { ru: 'Логины браузера', en: 'Browser logins', to: { tab: 'settings', section: 'browser' } },
    { ru: 'Ключи API', en: 'API keys', to: { tab: 'settings', section: 'keys' } },
    { ru: 'Obsidian', en: 'Obsidian', to: { tab: 'settings', section: 'obsidian' } },
  ] },
  { id: 'settings', ru: 'Настройки', en: 'Settings', home: { tab: 'settings', section: 'config' }, pages: [
    { ru: 'Аккаунт и предпочтения', en: 'Account and preferences', to: { tab: 'settings', section: 'config' } },
    { ru: 'Валюта', en: 'Currency', to: { tab: 'settings', section: 'currency' } },
  ] },
];
export function normalizeTab(value: string | null): AppTab {
  if (value === 'inbox') return 'smm';
  if (value === 'subagents') return 'agents';
  return appTabs.includes(value as AppTab) ? value as AppTab : 'vexa';
}
export function matchesDestination(current: Destination, target: Destination): boolean {
  return current.tab === target.tab && (!target.section || current.section === target.section)
    && (!target.view || current.view === target.view)
    && (!target.filter || (current.filter || 'all') === target.filter);
}
export function activeZone(current: Destination): Zone {
  return [...ZONES, ...UTILITIES].find(zone => zone.pages.some(page => matchesDestination(current, page.to)))
    || ZONES.find(zone => matchesDestination(current, zone.home)) || UTILITIES[1];
}
