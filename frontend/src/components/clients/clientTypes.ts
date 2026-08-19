/** Wire shapes for the clients module — mirrors backend/clients.py and
 *  backend/client_billing.py. Money always arrives as a decimal *string*: a
 *  JSON number would have already been through a double by the time it got
 *  here, which is the rounding the backend goes out of its way to avoid. */

export type ClientType = 'COMPANY' | 'PERSON';
export type ClientStatus = 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'OVERDUE' | 'ARCHIVED';
export type ServiceType = 'ONE_TIME' | 'RECURRING';
export type ClientServiceStatus = 'ACTIVE' | 'PAUSED' | 'COMPLETED';
export type BillingFrequency =
  | 'ONE_TIME' | 'MONTHLY' | 'QUARTERLY' | 'SEMI_ANNUAL' | 'YEARLY' | 'CUSTOM';
export type ConnectionStatus =
  | 'CONNECTED' | 'NOT_CONNECTED' | 'DISCONNECTED' | 'OFFLINE' | 'ERROR' | 'PAUSED';
export type ConnectionType = 'BOT' | 'API' | 'TOKEN' | 'CHANNEL' | 'INTERNAL';
export type PaymentStatus =
  | 'NO_INVOICE' | 'PLANNED' | 'AWAITING_PAYMENT' | 'PARTIALLY_PAID' | 'PAID' | 'OVERDUE';
export type InvoiceStatus =
  | 'PLANNED' | 'ISSUED' | 'PARTIALLY_PAID' | 'PAID' | 'OVERDUE' | 'CANCELLED';

export interface ClientContact {
  id: string;
  clientId: string;
  name: string;
  email: string;
  phone: string;
  telegram: string;
  isPrimary: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface BillingConfiguration {
  id: string;
  clientServiceId: string;
  frequency: BillingFrequency;
  billingDay: number | null;
  nextBillingDate: string | null;
  customIntervalDays: number | null;
  autoAdvanceBillingDate: boolean;
}

export interface ClientService {
  id: string;
  clientId: string;
  serviceId: string;
  serviceName?: string;
  title: string;
  description: string;
  serviceType: ServiceType;
  /** Decimal string in `currency` — never pre-converted. */
  amount?: string;
  currency: string;
  status: ClientServiceStatus;
  startedAt: string | null;
  completedAt: string | null;
  billing: BillingConfiguration | null;
}

export interface AgentConnection {
  id: string;
  clientId: string;
  clientServiceId: string | null;
  agentId: string;
  agentName?: string | null;
  clientName?: string | null;
  projectName?: string | null;
  serviceTitle?: string | null;
  status: ConnectionStatus;
  desiredState: 'ACTIVE' | 'DISCONNECTED' | 'PAUSED';
  connectionType: ConnectionType;
  channel: string;
  connectedAt: string | null;
  disconnectedAt: string | null;
  lastSeenAt: string | null;
  lastHealthCheckAt: string | null;
  errorCode: string | null;
  errorMessage: string | null;
}

export interface Client {
  id: string;
  type: ClientType;
  name: string;
  projectName: string;
  description: string;
  status: ClientStatus;
  primaryContactId: string | null;
  responsibleUserId: string | null;
  createdAt: string;
  updatedAt: string;
  archivedAt: string | null;
  contacts: ClientContact[];
  primaryContact: ClientContact | null;
  services: ClientService[];
  agentConnections: AgentConnection[];
  primaryService: ClientService | null;
  primaryConnection: AgentConnection | null;
  /** Absent entirely for roles without `clients.financials.view`. */
  paymentStatus?: PaymentStatus;
  primaryAmountUsd?: string;
  primaryAmountDisplay?: string;
  nextBillingDate: string | null;
  billingFrequency: BillingFrequency | null;
}

export interface ClientsPage {
  items: Client[];
  total: number;
  page: number;
  limit: number;
  pages: number;
  displayCurrency: string;
}

export interface MoneyPair {
  usd: string;
  display: string;
  count?: number;
}

export interface ClientsDashboard {
  currency: string;
  baseCurrency: string;
  totalClients: number;
  activeClients: number;
  connectedAgents: number;
  agentsWithErrors: number;
  agentsOffline: number;
  mrr: MoneyPair;
  toInvoice: MoneyPair;
  overdue: MoneyPair;
  collectedUsd: string;
}

export interface ClientInvoice {
  id: string;
  clientId: string;
  clientName?: string;
  projectName?: string;
  serviceTitle?: string;
  clientServiceId: string | null;
  invoiceNumber: string;
  sourceAmount: string;
  sourceCurrency: string;
  normalizedUsdAmount: string;
  displayAmount: string | null;
  displayCurrency: string | null;
  /** Rates frozen when the invoice was created — never recomputed. */
  exchangeRateSnapshot: Record<string, string>;
  invoiceDate: string;
  dueDate: string | null;
  billingPeriod: string | null;
  status: InvoiceStatus;
  origin: string;
  comment: string;
  paidAmount: string;
  outstandingAmount: string;
}

export interface ClientPayment {
  id: string;
  clientId: string;
  clientName?: string;
  invoiceId: string | null;
  invoiceNumber?: string | null;
  amount: string;
  currency: string;
  normalizedUsdAmount: string;
  paymentDate: string;
  paymentMethod: string;
  reference: string;
  comment: string;
}

export interface ClientBillingSummary {
  currency: string;
  baseCurrency: string;
  mrr: MoneyPair;
  outstanding: MoneyPair;
  paid: MoneyPair;
  nextBillingDate: string | null;
}

export interface PickerAgent {
  id: string;
  name: string;
  role: string;
  status: string;
  isEnabled: boolean;
}

export interface CatalogueService {
  id: string;
  name: string;
  description: string;
  isActive: boolean;
}

export interface ClientActivityEntry {
  id: string;
  event_type: string;
  client_id: string | null;
  entity_type: string;
  entity_id: string | null;
  summary: string;
  payload: Record<string, unknown>;
  actor_id: string | null;
  created_at: string;
}
