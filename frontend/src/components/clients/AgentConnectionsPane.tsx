import { useMemo, useState } from 'react';
import { Activity, ExternalLink, Link2Off, RefreshCw, Stethoscope } from 'lucide-react';
import { AgentConnectionBadge } from './ClientBadges';
import { formatRelative } from './clientLabels';
import type { AgentConnection, ConnectionStatus } from './clientTypes';

const FILTERS: Array<{ value: '' | ConnectionStatus; label: string }> = [
  { value: '', label: 'Все' },
  { value: 'CONNECTED', label: 'Подключены' },
  { value: 'NOT_CONNECTED', label: 'Не подключены' },
  { value: 'OFFLINE', label: 'Offline' },
  { value: 'ERROR', label: 'Ошибка' },
  { value: 'PAUSED', label: 'Пауза' },
];

/**
 * «Подключения агентов» (§45).
 *
 * Every status shown here was decided by the backend — this pane never infers
 * "online" from a timestamp of its own, it renders what the resolver said and
 * offers the three actions that can change it.
 */
export function AgentConnectionsPane({
  connections, loading, error, onCheck, onReconnect, onDisconnect, onOpenAgent, onRetry,
}: {
  connections: AgentConnection[];
  loading: boolean;
  error: string;
  onCheck: (connection: AgentConnection) => void;
  onReconnect: (connection: AgentConnection) => void;
  onDisconnect: (connection: AgentConnection) => void;
  onOpenAgent: (agentId: string) => void;
  onRetry: () => void;
}) {
  const [filter, setFilter] = useState<'' | ConnectionStatus>('');
  const [busy, setBusy] = useState<string>('');

  const visible = useMemo(
    () => (filter ? connections.filter(item => item.status === filter) : connections),
    [connections, filter],
  );

  const run = async (id: string, action: () => void) => {
    setBusy(id);
    try {
      await action();
    } finally {
      setBusy('');
    }
  };

  if (error) {
    return (
      <div className="cl-error-banner">
        <span>Не удалось загрузить подключения</span>
        <button type="button" className="btn-ghost" onClick={onRetry}>Повторить</button>
      </div>
    );
  }

  return (
    <>
      <nav className="admin-subnav cl-status-filter">
        {FILTERS.map(item => (
          <button
            key={item.value || 'all'}
            type="button"
            className={filter === item.value ? 'is-active' : ''}
            onClick={() => setFilter(item.value)}
          >
            <span>{item.label}</span>
            <em className="admin-subnav-count">
              {item.value ? connections.filter(c => c.status === item.value).length : connections.length}
            </em>
          </button>
        ))}
      </nav>

      {loading ? (
        <div className="admin-table-wrap">
          {[0, 1, 2, 3].map(index => <div key={index} className="cl-skeleton cl-skeleton-row" />)}
        </div>
      ) : visible.length === 0 ? (
        <div className="admin-empty-cta">
          <Activity size={22} />
          <strong>Подключений пока нет</strong>
          <span>Подключите агента к клиенту, чтобы отслеживать состояние связи.</span>
        </div>
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Клиент</th><th>Проект</th><th>Агент</th><th>Услуга</th><th>Канал</th>
                <th>Статус</th><th>Последняя активность</th><th>Health check</th><th aria-label="Действия" />
              </tr>
            </thead>
            <tbody>
              {visible.map(connection => (
                <tr key={connection.id}>
                  <td><strong>{connection.clientName || '—'}</strong></td>
                  <td>{connection.projectName || '—'}</td>
                  <td>{connection.agentName || connection.agentId}</td>
                  <td>{connection.serviceTitle || '—'}</td>
                  <td>
                    <span className="cl-service-chip">
                      {connection.channel || connection.connectionType}
                    </span>
                  </td>
                  <td>
                    <AgentConnectionBadge
                      agentName={connection.agentName}
                      agentId={connection.agentId}
                      status={connection.status}
                      errorMessage={connection.errorMessage}
                      full
                    />
                    {connection.status === 'ERROR' && connection.errorMessage && (
                      <div className="cl-error-note">{connection.errorMessage}</div>
                    )}
                  </td>
                  <td>{formatRelative(connection.lastSeenAt)}</td>
                  <td>{formatRelative(connection.lastHealthCheckAt)}</td>
                  <td>
                    <div className="cl-row-actions">
                      <button type="button" className="icon-btn" title="Проверить соединение"
                              aria-label={`Проверить соединение ${connection.agentName || connection.agentId}`}
                              disabled={busy === connection.id}
                              onClick={() => run(connection.id, () => onCheck(connection))}>
                        <Stethoscope size={14} />
                      </button>
                      <button type="button" className="icon-btn" title="Переподключить"
                              aria-label={`Переподключить ${connection.agentName || connection.agentId}`}
                              disabled={busy === connection.id}
                              onClick={() => run(connection.id, () => onReconnect(connection))}>
                        <RefreshCw size={14} />
                      </button>
                      <button type="button" className="icon-btn danger" title="Отключить"
                              aria-label={`Отключить ${connection.agentName || connection.agentId}`}
                              disabled={busy === connection.id || connection.status === 'DISCONNECTED'}
                              onClick={() => run(connection.id, () => onDisconnect(connection))}>
                        <Link2Off size={14} />
                      </button>
                      <button type="button" className="icon-btn" title="Открыть агента"
                              aria-label={`Открыть агента ${connection.agentName || connection.agentId}`}
                              onClick={() => onOpenAgent(connection.agentId)}>
                        <ExternalLink size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="cl-hint">
        Статус подключения рассчитывается на сервере: отключение оператором, пауза агента,
        зафиксированная ошибка и отсутствие активности проверяются именно в этом порядке.
      </p>
    </>
  );
}
