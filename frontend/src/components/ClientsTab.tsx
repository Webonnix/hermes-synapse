import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bot, Calendar, Download, FileText, ListChecks, Plus, Target, Users, Wallet,
} from 'lucide-react';
import { styles } from '../styles';
import { api } from './currency/currencyApi';
import { useCurrencySettings } from './currency/useCurrencySettings';
import { ClientKpiCards, ClientKpiSkeleton } from './clients/ClientKpiCards';
import { ClientsToolbar, EMPTY_FILTERS, type ClientFilters } from './clients/ClientsToolbar';
import { ClientsPagination, ClientsTable, ClientsTableSkeleton, type SortKey } from './clients/ClientsTable';
import { ClientDrawer, type DraftState } from './clients/ClientDrawer';
import { AgentConnectionsPane } from './clients/AgentConnectionsPane';
import { ClientInvoicesPane } from './clients/ClientInvoicesPane';
import { ClientPaymentsPane } from './clients/ClientPaymentsPane';
import { ClientDetailsPane } from './clients/ClientDetailsPane';
import { DealsPipeline } from './clients/DealsPipeline';
import { DealDrawer } from './clients/DealDrawer';
import { ClientTasksPane } from './clients/ClientTasksPane';
import { ClientsCalendarPane } from './clients/ClientsCalendarPane';
import type {
  AgentConnection, CatalogueService, Client, ClientActivityEntry, ClientBillingSummary,
  ClientInvoice, ClientNote, ClientPayment, ClientsDashboard, ClientsPage, ClientTask, Deal,
  DealStage, PickerAgent, PipelineSummary, TaskPriority, TaskStats,
} from './clients/clientTypes';

/**
 * «Клиенты» — the CRM section (§3, §CRM).
 *
 * Seven panes, navigated with an in-page tab strip — the same pattern as
 * Content Factory and SMM Factory, not a sidebar submenu. This component owns
 * its own "which pane" state (persisted to localStorage, same idiom as
 * `smm_factory_view`), so App.tsx only ever mounts `<ClientsTab />` with no
 * navigation props, exactly like those two sections.
 *
 * State that every pane depends on (currency settings, the agent and service
 * pickers) is loaded once here and passed down, so switching panes does not
 * re-fetch the world. Deals and tasks are loaded unconditionally on mount —
 * both the sidebar's "Задачи" badge context and the Календарь pane need them
 * regardless of which pane is actually open.
 *
 * The pipeline and the client book meet at exactly one place: converting a
 * deal. That flow reuses this same `ClientDrawer` — pre-filled from the deal
 * via its `prefill` prop — instead of a second creation form; see
 * `submitClientOrConvert` and `startConvert` below.
 */

export type Pane = 'deals' | 'clients' | 'connections' | 'invoices' | 'payments' | 'tasks' | 'calendar';

const VIEWS: Array<[Pane, string, typeof Target]> = [
  ['deals', 'Сделки', Target],
  ['clients', 'Список клиентов', Users],
  ['connections', 'Подключения', Bot],
  ['invoices', 'Счета', FileText],
  ['payments', 'Платежи', Wallet],
  ['tasks', 'Задачи', ListChecks],
  ['calendar', 'Календарь', Calendar],
];
const VIEW_KEYS = VIEWS.map(([key]) => key);

/** Turns the filter/sort/page state into the query the backend understands.
 *  Filtering happens server-side across the whole book — see §38. */
function buildQuery(
  filters: ClientFilters, page: number, limit: number, sort: SortKey, order: string,
): string {
  const params = new URLSearchParams();
  if (filters.search.trim()) params.set('search', filters.search.trim());
  if (filters.status) params.set('status', filters.status);
  if (filters.serviceType) params.set('serviceType', filters.serviceType);
  if (filters.serviceId) params.set('serviceId', filters.serviceId);
  if (filters.agentStatus) params.set('agentStatus', filters.agentStatus);
  if (filters.paymentStatus) params.set('paymentStatus', filters.paymentStatus);
  if (filters.status === 'ARCHIVED') params.set('includeArchived', 'true');
  params.set('page', String(page));
  params.set('limit', String(limit));
  params.set('sort', sort);
  params.set('order', order);
  return params.toString();
}

/** CSV of what is on screen, built from the rows already fetched — an export
 *  that silently re-queried without the filters would not be an export of what
 *  the operator is looking at. */
function exportCsv(clients: Client[], displayCurrency: string) {
  const header = [
    'Клиент', 'Проект', 'Контакт', 'Email', 'Услуга', 'Тип', 'Агент', 'Статус агента',
    `Сумма (${displayCurrency})`, 'Исходная сумма', 'Валюта', 'Следующий счёт', 'Оплата',
  ];
  const rows = clients.map(client => [
    client.name, client.projectName, client.primaryContact?.name ?? '',
    client.primaryContact?.email ?? '',
    client.primaryService?.title || client.primaryService?.serviceName || '',
    client.primaryService?.serviceType ?? '',
    client.primaryConnection?.agentName || client.primaryConnection?.agentId || '',
    client.primaryConnection?.status ?? 'NOT_CONNECTED',
    client.primaryAmountDisplay ?? '', client.primaryService?.amount ?? '',
    client.primaryService?.currency ?? '', client.nextBillingDate ?? '',
    client.paymentStatus ?? '',
  ]);
  const escape = (value: string) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const csv = [header, ...rows].map(row => row.map(escape).join(';')).join('\n');
  // BOM so Excel opens the Cyrillic correctly instead of showing mojibake.
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `clients-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

/** What DealDrawer's "Конвертировать в клиента" hands to ClientDrawer as a
 *  starting point — a person still reviews and completes it before saving. */
function draftFromDeal(deal: Deal, baseCurrency: string): Partial<DraftState> {
  return {
    type: deal.companyName ? 'COMPANY' : 'PERSON',
    name: deal.companyName || deal.contactName || deal.title,
    projectName: deal.title,
    contactName: deal.contactName,
    contactEmail: deal.contactEmail,
    contactPhone: deal.contactPhone,
    amount: deal.value ?? '',
    currency: deal.value ? deal.currency : baseCurrency,
  };
}

export function ClientsTab({ onOpenAgent }: { onOpenAgent?: (agentId: string) => void }) {
  const [pane, setPane] = useState<Pane>(() => {
    const stored = localStorage.getItem('clients_view') as Pane | null;
    // A view name from an older build (or a hand-edited key) must not leave
    // the section rendering nothing at all.
    return stored && VIEW_KEYS.includes(stored) ? stored : 'deals';
  });
  useEffect(() => { localStorage.setItem('clients_view', pane); }, [pane]);

  const { settings, loading: currencyLoading } = useCurrencySettings();

  const [filters, setFilters] = useState<ClientFilters>({ ...EMPTY_FILTERS });
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(50);
  const [sort, setSort] = useState<SortKey>('name');
  const [order, setOrder] = useState<'asc' | 'desc'>('asc');

  const [result, setResult] = useState<ClientsPage | null>(null);
  const [dashboard, setDashboard] = useState<ClientsDashboard | null>(null);
  const [connections, setConnections] = useState<AgentConnection[]>([]);
  const [invoices, setInvoices] = useState<ClientInvoice[]>([]);
  const [payments, setPayments] = useState<ClientPayment[]>([]);
  const [services, setServices] = useState<CatalogueService[]>([]);
  const [agents, setAgents] = useState<PickerAgent[]>([]);

  const [selected, setSelected] = useState<Client | null>(null);
  const [selectedBilling, setSelectedBilling] = useState<ClientBillingSummary | null>(null);
  const [selectedInvoices, setSelectedInvoices] = useState<ClientInvoice[]>([]);
  const [selectedPayments, setSelectedPayments] = useState<ClientPayment[]>([]);
  const [selectedActivity, setSelectedActivity] = useState<ClientActivityEntry[]>([]);
  const [selectedTasks, setSelectedTasks] = useState<ClientTask[]>([]);
  const [selectedTasksLoading, setSelectedTasksLoading] = useState(false);
  const [selectedNotes, setSelectedNotes] = useState<ClientNote[]>([]);
  const [selectedNotesLoading, setSelectedNotesLoading] = useState(false);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<'create' | 'edit'>('create');
  const [drawerClient, setDrawerClient] = useState<Client | null>(null);
  const [drawerPrefill, setDrawerPrefill] = useState<Partial<DraftState> | undefined>(undefined);
  const [convertingDeal, setConvertingDeal] = useState<Deal | null>(null);

  // ── deals (sales pipeline) — loaded unconditionally, see module docstring ──
  const [deals, setDeals] = useState<Deal[]>([]);
  const [pipelineSummary, setPipelineSummary] = useState<PipelineSummary | null>(null);
  const [dealsLoading, setDealsLoading] = useState(true);
  const [dealsError, setDealsError] = useState('');
  const [dealDrawerOpen, setDealDrawerOpen] = useState(false);
  const [dealDrawerDeal, setDealDrawerDeal] = useState<Deal | null>(null);

  // ── tasks — also loaded unconditionally, for the same reason ────────────
  const [tasks, setTasks] = useState<ClientTask[]>([]);
  const [taskStats, setTaskStats] = useState<TaskStats | null>(null);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [tasksError, setTasksError] = useState('');

  const [loading, setLoading] = useState(true);
  const [paneLoading, setPaneLoading] = useState(false);
  const [error, setError] = useState('');
  const [paneError, setPaneError] = useState('');
  const [toast, setToast] = useState('');

  /** A 403 on the dashboard means this role lacks clients.financials.view. The
   *  money columns then disappear — and the backend already omitted the
   *  numbers, so there is nothing to leak either way. Same idea for the
   *  pipeline, tasks and notes, each behind its own permission. */
  const [canSeeFinancials, setCanSeeFinancials] = useState(true);
  const [canViewDeals, setCanViewDeals] = useState(true);
  const [canViewTasks, setCanViewTasks] = useState(true);
  const [canViewNotes, setCanViewNotes] = useState(true);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(''), 2600);
    return () => clearTimeout(timer);
  }, [toast]);

  const loadClients = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const query = buildQuery(filters, page, limit, sort, order);
      const [page_, dashboardData] = await Promise.all([
        api<ClientsPage>(`/api/clients?${query}`),
        api<ClientsDashboard>('/api/clients/dashboard').catch(exc => {
          if (String((exc as Error).message).includes('financials')) {
            setCanSeeFinancials(false);
            return null;
          }
          throw exc;
        }),
      ]);
      setResult(page_);
      if (dashboardData) setDashboard(dashboardData);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setLoading(false);
    }
  }, [filters, page, limit, sort, order]);

  // Reference data the drawer and toolbar need. Loaded once — the service
  // catalogue and agent list barely change during a session.
  useEffect(() => {
    api<CatalogueService[]>('/api/clients/services').then(setServices).catch(() => setServices([]));
    api<PickerAgent[]>('/api/clients/agents').then(setAgents).catch(() => setAgents([]));
  }, []);

  // Debounced so typing in the search box does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => { void loadClients(); }, 250);
    return () => clearTimeout(timer);
  }, [loadClients]);

  // Changing the display currency (or a rate) invalidates every money figure on
  // screen — §60: switch currency, the dashboard follows, no page reload. The
  // rates object gets a fresh identity on every publish, so the effect keys off
  // a fingerprint that only changes when a number actually changed.
  const ratesKey = useMemo(
    () => Object.entries(settings.rates).map(([code, rate]) => `${code}:${rate}`).sort().join('|'),
    [settings.rates],
  );
  const [currencyReady, setCurrencyReady] = useState(false);
  useEffect(() => {
    if (currencyLoading) return;
    // Skip the very first run: the initial fetch above already loaded the list.
    if (!currencyReady) {
      setCurrencyReady(true);
      return;
    }
    void loadClients();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.displayCurrency, ratesKey, currencyLoading]);

  const loadDeals = useCallback(async () => {
    setDealsLoading(true);
    setDealsError('');
    try {
      const [list, summary] = await Promise.all([
        api<Deal[]>('/api/deals').catch(exc => {
          if (String((exc as Error).message).includes('403')) { setCanViewDeals(false); return []; }
          throw exc;
        }),
        api<PipelineSummary>('/api/deals/pipeline').catch(() => null),
      ]);
      setDeals(Array.isArray(list) ? list : []);
      setPipelineSummary(summary);
    } catch (exc) {
      setDealsError((exc as Error).message);
    } finally {
      setDealsLoading(false);
    }
  }, []);

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    setTasksError('');
    try {
      const [list, stats] = await Promise.all([
        api<ClientTask[]>('/api/client-tasks').catch(exc => {
          if (String((exc as Error).message).includes('403')) { setCanViewTasks(false); return []; }
          throw exc;
        }),
        api<TaskStats>('/api/client-tasks/stats').catch(() => null),
      ]);
      setTasks(Array.isArray(list) ? list : []);
      setTaskStats(stats);
    } catch (exc) {
      setTasksError((exc as Error).message);
    } finally {
      setTasksLoading(false);
    }
  }, []);

  // Deals and tasks load once on mount and then refresh on an interval,
  // independent of which pane is open — the "Задачи" tab badge and the
  // Календарь pane both need this data even when neither is the active view.
  useEffect(() => {
    void loadDeals();
    void loadTasks();
    const interval = setInterval(() => { void loadTasks(); }, 60000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadPane = useCallback(async (target: Pane) => {
    if (target !== 'connections' && target !== 'invoices' && target !== 'payments') return;
    setPaneLoading(true);
    setPaneError('');
    try {
      if (target === 'connections') {
        setConnections(await api<AgentConnection[]>('/api/client-agent-connections'));
      } else if (target === 'invoices') {
        setInvoices(await api<ClientInvoice[]>('/api/client-invoices'));
      } else {
        setPayments(await api<ClientPayment[]>('/api/client-payments'));
      }
    } catch (exc) {
      setPaneError((exc as Error).message);
    } finally {
      setPaneLoading(false);
    }
  }, []);

  useEffect(() => { void loadPane(pane); }, [pane, loadPane]);

  /** Fetches one client's notes and applies the same defensive shape guard
   *  every array-typed response in this file gets — a malformed response
   *  must not crash the notes pane, only leave it empty. */
  const fetchNotes = useCallback(async (clientId: string): Promise<ClientNote[]> => {
    try {
      const list = await api<ClientNote[]>(`/api/clients/${clientId}/notes`);
      return Array.isArray(list) ? list : [];
    } catch (exc) {
      if (String((exc as Error).message).includes('403')) setCanViewNotes(false);
      return [];
    }
  }, []);

  const loadClientExtras = useCallback(async (clientId: string) => {
    setSelectedTasksLoading(true);
    setSelectedNotesLoading(true);
    try {
      const list = await api<ClientTask[]>(`/api/client-tasks?clientId=${clientId}`);
      setSelectedTasks(Array.isArray(list) ? list : []);
    } catch {
      setSelectedTasks([]);
    } finally {
      setSelectedTasksLoading(false);
    }
    setSelectedNotes(await fetchNotes(clientId));
    setSelectedNotesLoading(false);
  }, [fetchNotes]);

  const openClient = useCallback(async (client: Client) => {
    try {
      const full = await api<Client>(`/api/clients/${client.id}`);
      setSelected(full);
      const [billing, clientInvoices, clientPayments, activity] = await Promise.all([
        canSeeFinancials
          ? api<ClientBillingSummary>(`/api/clients/${client.id}/billing`).catch(() => null)
          : Promise.resolve(null),
        canSeeFinancials
          ? api<ClientInvoice[]>(`/api/client-invoices?clientId=${client.id}`).catch(() => [])
          : Promise.resolve([]),
        canSeeFinancials
          ? api<ClientPayment[]>(`/api/client-payments?clientId=${client.id}`).catch(() => [])
          : Promise.resolve([]),
        api<ClientActivityEntry[]>(`/api/clients/${client.id}/activity`).catch(() => []),
      ]);
      setSelectedBilling(billing);
      setSelectedInvoices(clientInvoices);
      setSelectedPayments(clientPayments);
      setSelectedActivity(activity);
      void loadClientExtras(client.id);
    } catch (exc) {
      setToast((exc as Error).message);
    }
  }, [canSeeFinancials, loadClientExtras]);

  const submitClient = useCallback(async (
    payload: Record<string, unknown>, mode: 'create' | 'edit', clientId?: string,
  ) => {
    if (mode === 'edit' && clientId) {
      await api(`/api/clients/${clientId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      setToast('Клиент обновлён');
    } else {
      await api('/api/clients', { method: 'POST', body: JSON.stringify(payload) });
      setToast('Клиент добавлен');
    }
    await loadClients();
  }, [loadClients]);

  /** Fires when ClientDrawer's onSubmit runs while a deal conversion is in
   *  progress — the drawer itself does not know it is being reused for this,
   *  which is the point (see the module docstring). */
  const submitClientOrConvert = useCallback(async (
    payload: Record<string, unknown>, mode: 'create' | 'edit', clientId?: string,
  ) => {
    if (convertingDeal) {
      const result = await api<{ deal: Deal; client: Client }>(
        `/api/deals/${convertingDeal.id}/convert-to-client`, { method: 'POST', body: JSON.stringify(payload) },
      );
      setToast(`Сделка стала клиентом «${result.client.name}»`);
      setConvertingDeal(null);
      await loadDeals();
      return;
    }
    await submitClient(payload, mode, clientId);
  }, [convertingDeal, loadDeals, submitClient]);

  const connectionAction = useCallback(async (
    connection: AgentConnection, action: 'check' | 'reconnect' | 'disconnect',
  ) => {
    try {
      await api(`/api/client-agent-connections/${connection.id}/${action}`, { method: 'POST' });
      setToast(action === 'check' ? 'Соединение проверено'
        : action === 'reconnect' ? 'Переподключение выполнено' : 'Агент отключён');
      await loadPane('connections');
      if (selected) await openClient(selected);
      await loadClients();
    } catch (exc) {
      setToast((exc as Error).message);
    }
  }, [loadPane, loadClients, openClient, selected]);

  const invoiceAction = useCallback(async (
    invoice: ClientInvoice, action: 'issue' | 'mark-paid' | 'cancel',
  ) => {
    try {
      await api(`/api/client-invoices/${invoice.id}/${action}`, {
        method: 'POST', body: JSON.stringify({}),
      });
      setToast(action === 'issue' ? 'Счёт выставлен'
        : action === 'mark-paid' ? 'Счёт отмечен оплаченным' : 'Счёт отменён');
      await loadPane('invoices');
      if (selected) await openClient(selected);
      await loadClients();
    } catch (exc) {
      setToast((exc as Error).message);
    }
  }, [loadPane, loadClients, openClient, selected]);

  const createInvoiceForSelected = useCallback(async () => {
    if (!selected) return;
    const service = selected.primaryService;
    if (!service || service.amount === undefined) {
      setToast('У клиента нет услуги с ценой');
      return;
    }
    try {
      await api('/api/client-invoices', {
        method: 'POST',
        body: JSON.stringify({
          clientId: selected.id,
          clientServiceId: service.id,
          amount: service.amount,
          sourceCurrency: service.currency,
          status: 'ISSUED',
        }),
      });
      setToast('Счёт выставлен');
      await openClient(selected);
      await loadClients();
    } catch (exc) {
      setToast((exc as Error).message);
    }
  }, [selected, openClient, loadClients]);

  const toggleSort = useCallback((column: SortKey) => {
    setPage(1);
    setSort(current => {
      if (current === column) {
        setOrder(previous => (previous === 'asc' ? 'desc' : 'asc'));
        return current;
      }
      setOrder('asc');
      return column;
    });
  }, []);

  // ── deals actions ────────────────────────────────────────────────────────

  const submitDeal = useCallback(async (payload: Record<string, unknown>, dealId?: string) => {
    if (dealId) {
      await api(`/api/deals/${dealId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      setToast('Сделка обновлена');
    } else {
      await api('/api/deals', { method: 'POST', body: JSON.stringify(payload) });
      setToast('Сделка создана');
    }
    await loadDeals();
  }, [loadDeals]);

  const changeDealStage = useCallback(async (deal: Deal, stage: DealStage) => {
    try {
      await api(`/api/deals/${deal.id}`, {
        method: 'PATCH',
        body: JSON.stringify(stage === 'LOST'
          ? { stage, lostReason: window.prompt('Причина проигрыша сделки:', deal.lostReason || '') || '' }
          : { stage }),
      });
      await loadDeals();
    } catch (exc) {
      setToast((exc as Error).message);
      await loadDeals(); // snap the card back to its real stage
    }
  }, [loadDeals]);

  const deleteDeal = useCallback(async (deal: Deal) => {
    if (!window.confirm(`Удалить сделку «${deal.title}»?`)) return;
    try {
      await api(`/api/deals/${deal.id}`, { method: 'DELETE' });
      setToast('Сделка удалена');
      await loadDeals();
    } catch (exc) {
      setToast((exc as Error).message);
    }
  }, [loadDeals]);

  const startConvert = useCallback((deal: Deal) => {
    setDealDrawerOpen(false);
    setConvertingDeal(deal);
    setDrawerPrefill(draftFromDeal(deal, settings.baseCurrency));
    setDrawerMode('create');
    setDrawerClient(null);
    setDrawerOpen(true);
  }, [settings.baseCurrency]);

  // ── task actions (shared across the tasks pane, the calendar and a client's own card) ──

  const createTask = useCallback(async (payload: {
    title: string; dueAt: string; priority: TaskPriority; clientId?: string; dealId?: string;
  }) => {
    await api('/api/client-tasks', { method: 'POST', body: JSON.stringify(payload) });
    await loadTasks();
    if (payload.clientId && selected?.id === payload.clientId) await loadClientExtras(payload.clientId);
  }, [loadTasks, loadClientExtras, selected]);

  const refreshTasksEverywhere = useCallback(async () => {
    await loadTasks();
    if (selected) await loadClientExtras(selected.id);
  }, [loadTasks, loadClientExtras, selected]);

  const completeTask = useCallback((task: { id: string }) => {
    api(`/api/client-tasks/${task.id}/complete`, { method: 'POST' })
      .then(refreshTasksEverywhere)
      .catch(exc => setToast((exc as Error).message));
  }, [refreshTasksEverywhere]);

  const reopenTask = useCallback((task: { id: string }) => {
    api(`/api/client-tasks/${task.id}/reopen`, { method: 'POST' })
      .then(refreshTasksEverywhere)
      .catch(exc => setToast((exc as Error).message));
  }, [refreshTasksEverywhere]);

  const deleteTask = useCallback((task: { id: string; title: string }) => {
    if (!window.confirm(`Удалить задачу «${task.title}»?`)) return;
    api(`/api/client-tasks/${task.id}`, { method: 'DELETE' })
      .then(refreshTasksEverywhere)
      .catch(exc => setToast((exc as Error).message));
  }, [refreshTasksEverywhere]);

  // ── note actions ─────────────────────────────────────────────────────────

  const createNote = useCallback(async (body: string) => {
    if (!selected) return;
    await api(`/api/clients/${selected.id}/notes`, { method: 'POST', body: JSON.stringify({ body }) });
    setSelectedNotes(await fetchNotes(selected.id));
  }, [selected, fetchNotes]);

  const updateNote = useCallback(async (note: ClientNote, patch: { body?: string; pinned?: boolean }) => {
    await api(`/api/client-notes/${note.id}`, { method: 'PATCH', body: JSON.stringify(patch) });
    if (selected) setSelectedNotes(await fetchNotes(selected.id));
  }, [selected, fetchNotes]);

  const deleteNote = useCallback((note: ClientNote) => {
    if (!window.confirm('Удалить заметку?')) return;
    api(`/api/client-notes/${note.id}`, { method: 'DELETE' })
      .then(() => selected && fetchNotes(selected.id).then(setSelectedNotes))
      .catch(exc => setToast((exc as Error).message));
  }, [selected, fetchNotes]);

  const clients = useMemo(() => result?.items ?? [], [result]);
  const showSkeletons = loading && !result;

  if (selected) {
    return (
      <div style={styles.tabWrapper}>
        {toast && <div className="cl-toast" role="status">{toast}</div>}
        <ClientDetailsPane
          client={selected}
          billing={selectedBilling}
          invoices={selectedInvoices}
          payments={selectedPayments}
          activity={selectedActivity}
          tasks={selectedTasks}
          tasksLoading={selectedTasksLoading}
          notes={selectedNotes}
          notesLoading={selectedNotesLoading}
          settings={settings}
          canSeeFinancials={canSeeFinancials}
          canViewTasks={canViewTasks}
          canViewNotes={canViewNotes}
          onBack={() => setSelected(null)}
          onCheck={connection => connectionAction(connection, 'check')}
          onReconnect={connection => connectionAction(connection, 'reconnect')}
          onDisconnect={connection => connectionAction(connection, 'disconnect')}
          onOpenAgent={agentId => onOpenAgent?.(agentId)}
          onIssueInvoice={invoice => invoiceAction(invoice, 'issue')}
          onMarkPaid={invoice => invoiceAction(invoice, 'mark-paid')}
          onCancelInvoice={invoice => invoiceAction(invoice, 'cancel')}
          onCreateInvoice={createInvoiceForSelected}
          onReload={() => openClient(selected)}
          onCreateTask={createTask}
          onCompleteTask={completeTask}
          onReopenTask={reopenTask}
          onDeleteTask={deleteTask}
          onCreateNote={createNote}
          onUpdateNote={updateNote}
          onDeleteNote={deleteNote}
        />
      </div>
    );
  }

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>Клиенты</h2>
          <p style={styles.tabSubtitle}>Сделки, клиенты, услуги, агенты, платежи, задачи и календарь</p>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <nav className="admin-subnav is-wrap" aria-label="Клиенты">
          {VIEWS.map(([key, label, Icon]) => (
            <button key={key} type="button" className={pane === key ? 'is-active' : ''} onClick={() => setPane(key)}>
              <Icon size={15} />
              <span>{label}</span>
              {key === 'tasks' && taskStats && taskStats.overdue > 0 && (
                <span className="admin-subnav-count">{taskStats.overdue}</span>
              )}
            </button>
          ))}
        </nav>
        <div style={{ flex: 1 }} />
        {pane === 'clients' && (
          <div className="cl-header-actions">
            <button type="button" className="btn-ghost"
                    disabled={clients.length === 0}
                    onClick={() => exportCsv(clients, result?.displayCurrency ?? settings.displayCurrency)}>
              <Download size={15} /><span>Экспорт</span>
            </button>
            <button type="button" className="btn-primary"
                    onClick={() => { setDrawerMode('create'); setDrawerClient(null); setDrawerPrefill(undefined); setDrawerOpen(true); }}>
              <Plus size={16} /><span>Добавить клиента</span>
            </button>
          </div>
        )}
      </div>

      {toast && <div className="cl-toast" role="status">{toast}</div>}

      {pane === 'deals' && !canViewDeals && (
        <div className="admin-empty-cta"><span>Недостаточно прав для просмотра воронки сделок.</span></div>
      )}
      {pane === 'deals' && canViewDeals && (
        <DealsPipeline
          deals={deals}
          summary={pipelineSummary}
          loading={dealsLoading}
          error={dealsError}
          settings={settings}
          onCreate={() => { setDealDrawerDeal(null); setDealDrawerOpen(true); }}
          onOpen={deal => { setDealDrawerDeal(deal); setDealDrawerOpen(true); }}
          onStageChange={changeDealStage}
          onRetry={() => void loadDeals()}
        />
      )}

      {pane === 'clients' && (
        <>
          {canSeeFinancials && (
            showSkeletons || !dashboard
              ? <ClientKpiSkeleton />
              : <ClientKpiCards dashboard={dashboard} settings={settings} />
          )}

          <ClientsToolbar
            filters={filters}
            services={services}
            disabled={showSkeletons}
            onChange={next => { setFilters(next); setPage(1); }}
          />

          {error ? (
            <div className="cl-error-banner">
              <span>Не удалось загрузить клиентов</span>
              <button type="button" className="btn-ghost" onClick={() => void loadClients()}>Повторить</button>
            </div>
          ) : showSkeletons ? (
            <ClientsTableSkeleton />
          ) : clients.length === 0 ? (
            <div className="admin-empty-cta">
              <Users size={22} />
              <strong>Клиентов пока нет</strong>
              <span>Добавьте первого клиента, чтобы начать вести услуги, агентов и платежи.</span>
              <button type="button" className="btn-primary"
                      onClick={() => { setDrawerMode('create'); setDrawerClient(null); setDrawerPrefill(undefined); setDrawerOpen(true); }}>
                <Plus size={15} /><span>Добавить клиента</span>
              </button>
            </div>
          ) : (
            <>
              <ClientsTable
                clients={clients}
                settings={settings}
                sort={sort}
                order={order}
                canSeeFinancials={canSeeFinancials}
                onSort={toggleSort}
                onOpen={client => void openClient(client)}
                onEdit={client => { setDrawerMode('edit'); setDrawerClient(client); setDrawerPrefill(undefined); setDrawerOpen(true); }}
                onConnectAgent={client => void openClient(client)}
              />
              <ClientsPagination
                page={result?.page ?? 1}
                pages={result?.pages ?? 1}
                limit={limit}
                total={result?.total ?? 0}
                onPage={setPage}
                onLimit={next => { setLimit(next); setPage(1); }}
              />
            </>
          )}
        </>
      )}

      {pane === 'connections' && (
        <AgentConnectionsPane
          connections={connections}
          loading={paneLoading}
          error={paneError}
          onCheck={connection => connectionAction(connection, 'check')}
          onReconnect={connection => connectionAction(connection, 'reconnect')}
          onDisconnect={connection => connectionAction(connection, 'disconnect')}
          onOpenAgent={agentId => onOpenAgent?.(agentId)}
          onRetry={() => void loadPane('connections')}
        />
      )}

      {pane === 'invoices' && !canSeeFinancials && (
        <div className="admin-empty-cta">
          <span>Недостаточно прав для просмотра счетов.</span>
        </div>
      )}
      {pane === 'invoices' && canSeeFinancials && (
        <ClientInvoicesPane
          invoices={invoices}
          loading={paneLoading}
          error={paneError}
          settings={settings}
          onIssue={invoice => invoiceAction(invoice, 'issue')}
          onMarkPaid={invoice => invoiceAction(invoice, 'mark-paid')}
          onCancel={invoice => invoiceAction(invoice, 'cancel')}
          onRetry={() => void loadPane('invoices')}
        />
      )}

      {pane === 'payments' && !canSeeFinancials && (
        <div className="admin-empty-cta">
          <span>Недостаточно прав для просмотра платежей.</span>
        </div>
      )}
      {pane === 'payments' && canSeeFinancials && (
        <ClientPaymentsPane
          payments={payments}
          loading={paneLoading}
          error={paneError}
          settings={settings}
          onRetry={() => void loadPane('payments')}
        />
      )}

      {pane === 'tasks' && !canViewTasks && (
        <div className="admin-empty-cta"><span>Недостаточно прав для просмотра задач.</span></div>
      )}
      {pane === 'tasks' && canViewTasks && (
        <>
          {taskStats && (
            <div className="cl-kpi-row">
              <div className="glass-panel admin-stat-card">
                <div className="admin-stat-body">
                  <span className="admin-stat-label">Открыто</span>
                  <strong className="admin-stat-value">{taskStats.open}</strong>
                </div>
              </div>
              <div className="glass-panel admin-stat-card">
                <div className="admin-stat-body">
                  <span className="admin-stat-label">Сегодня</span>
                  <strong className="admin-stat-value">{taskStats.dueToday}</strong>
                </div>
              </div>
              <div className={`glass-panel admin-stat-card${taskStats.overdue > 0 ? ' is-warning' : ''}`}>
                <div className="admin-stat-body">
                  <span className="admin-stat-label">Просрочено</span>
                  <strong className="admin-stat-value">{taskStats.overdue}</strong>
                </div>
              </div>
            </div>
          )}
          <ClientTasksPane
            tasks={tasks}
            loading={tasksLoading}
            error={tasksError}
            onCreate={createTask}
            onComplete={completeTask}
            onReopen={reopenTask}
            onDelete={deleteTask}
            onRetry={() => void loadTasks()}
          />
        </>
      )}

      {pane === 'calendar' && !canViewTasks && (
        <div className="admin-empty-cta"><span>Недостаточно прав для просмотра календаря.</span></div>
      )}
      {pane === 'calendar' && canViewTasks && (
        <ClientsCalendarPane
          tasks={tasks}
          deals={canViewDeals ? deals : []}
          loading={tasksLoading || dealsLoading}
          onCompleteTask={completeTask}
          onOpenDeal={deal => { setDealDrawerDeal(deal); setDealDrawerOpen(true); }}
          onCreateTask={createTask}
        />
      )}

      <ClientDrawer
        open={drawerOpen}
        mode={drawerMode}
        client={drawerClient}
        settings={settings}
        services={services}
        agents={agents}
        prefill={drawerPrefill}
        title={convertingDeal ? `Клиент из сделки «${convertingDeal.title}»` : undefined}
        submitLabel={convertingDeal ? 'Создать клиента' : undefined}
        onClose={() => { setDrawerOpen(false); setConvertingDeal(null); }}
        onSubmit={submitClientOrConvert}
      />

      <DealDrawer
        open={dealDrawerOpen}
        deal={dealDrawerDeal}
        settings={settings}
        onClose={() => setDealDrawerOpen(false)}
        onSubmit={submitDeal}
        onDelete={deleteDeal}
        onConvert={startConvert}
      />
    </div>
  );
}

export default ClientsTab;
