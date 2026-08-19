import { useCallback, useEffect, useMemo, useState } from 'react';
import { Download, Plus, Users } from 'lucide-react';
import { styles } from '../styles';
import { api } from './currency/currencyApi';
import { useCurrencySettings } from './currency/useCurrencySettings';
import { ClientKpiCards, ClientKpiSkeleton } from './clients/ClientKpiCards';
import { ClientsToolbar, EMPTY_FILTERS, type ClientFilters } from './clients/ClientsToolbar';
import { ClientsPagination, ClientsTable, ClientsTableSkeleton, type SortKey } from './clients/ClientsTable';
import { ClientDrawer } from './clients/ClientDrawer';
import { AgentConnectionsPane } from './clients/AgentConnectionsPane';
import { ClientInvoicesPane } from './clients/ClientInvoicesPane';
import { ClientPaymentsPane } from './clients/ClientPaymentsPane';
import { ClientDetailsPane } from './clients/ClientDetailsPane';
import type {
  AgentConnection, CatalogueService, Client, ClientActivityEntry, ClientBillingSummary,
  ClientInvoice, ClientPayment, ClientsDashboard, ClientsPage, PickerAgent,
} from './clients/clientTypes';

/**
 * «Клиенты» — the commercial section (§3).
 *
 * Four panes: the client book, agent connections, invoices and payments.
 * Navigation between them lives in the left sidebar (App.tsx), not on this
 * page — the sidebar owns the "which pane" question and passes it down as
 * `pane`/`onPaneChange`, the same controlled-with-fallback shape as any
 * externally-driven tab state. The fallback (internal state, defaulting to
 * 'clients') keeps this component fully usable standalone — every existing
 * test renders it with zero nav props and still gets a working clients list.
 *
 * State that every pane depends on (currency settings, the agent and service
 * pickers) is loaded once here and passed down, so switching panes does not
 * re-fetch the world.
 */

export type Pane = 'clients' | 'connections' | 'invoices' | 'payments';

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
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `clients-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

export function ClientsTab({
  onOpenAgent, pane = 'clients',
}: {
  onOpenAgent?: (agentId: string) => void;
  /** Which of the four panes to show — set by the sidebar (App.tsx), which
   *  owns navigation for this section entirely; this component has no
   *  internal way to change it. Defaults to 'clients' so it stays usable
   *  standalone (every test renders it with no `pane` prop at all). */
  pane?: Pane;
}) {
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

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<'create' | 'edit'>('create');
  const [drawerClient, setDrawerClient] = useState<Client | null>(null);

  const [loading, setLoading] = useState(true);
  const [paneLoading, setPaneLoading] = useState(false);
  const [error, setError] = useState('');
  const [paneError, setPaneError] = useState('');
  const [toast, setToast] = useState('');

  /** A 403 on the dashboard means this role lacks clients.financials.view. The
   *  money columns then disappear — and the backend already omitted the
   *  numbers, so there is nothing to leak either way. */
  const [canSeeFinancials, setCanSeeFinancials] = useState(true);

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

  const loadPane = useCallback(async (target: Pane) => {
    if (target === 'clients') return;
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
    } catch (exc) {
      setToast((exc as Error).message);
    }
  }, [canSeeFinancials]);

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
          settings={settings}
          canSeeFinancials={canSeeFinancials}
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
        />
      </div>
    );
  }

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>Клиенты</h2>
          <p style={styles.tabSubtitle}>Клиенты, услуги, агенты и платежи</p>
        </div>
        <div className="cl-header-actions">
          <button type="button" className="btn-ghost"
                  disabled={clients.length === 0}
                  onClick={() => exportCsv(clients, result?.displayCurrency ?? settings.displayCurrency)}>
            <Download size={15} /><span>Экспорт</span>
          </button>
          <button type="button" className="btn-primary"
                  onClick={() => { setDrawerMode('create'); setDrawerClient(null); setDrawerOpen(true); }}>
            <Plus size={16} /><span>Добавить клиента</span>
          </button>
        </div>
      </div>

      {toast && <div className="cl-toast" role="status">{toast}</div>}

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
                      onClick={() => { setDrawerMode('create'); setDrawerClient(null); setDrawerOpen(true); }}>
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
                onEdit={client => { setDrawerMode('edit'); setDrawerClient(client); setDrawerOpen(true); }}
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

      <ClientDrawer
        open={drawerOpen}
        mode={drawerMode}
        client={drawerClient}
        settings={settings}
        services={services}
        agents={agents}
        onClose={() => setDrawerOpen(false)}
        onSubmit={submitClient}
      />
    </div>
  );
}
