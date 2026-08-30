import { useMemo, useState } from 'react';
import { Building2, Clock, Percent, Plus, Search, Target, TrendingUp } from 'lucide-react';
import { formatMoney, type CurrencySettings } from '../currency/currencyApi';
import { DEAL_STAGE_LABELS, DEAL_STAGE_ORDER, DEAL_STAGE_TONE, daysSince, formatDate } from './clientLabels';
import type { Deal, DealStage, PipelineSummary } from './clientTypes';

/**
 * «Сделки» — the kanban board (§CRM pipeline).
 *
 * A card moves between columns two ways, both wired to the same
 * `onStageChange`: dragging it (mouse-only, the expected kanban gesture) or
 * its own stage `<select>` (keyboard- and screen-reader-reachable — a board
 * that only responded to drag would be unusable without a mouse). Neither
 * path touches state optimistically; both wait for the backend the same way
 * the rest of the clients module does, so a rejected move (a converted
 * deal's frozen stage) snaps back instead of lying about what happened.
 */

const CLOSED = new Set<DealStage>(['WON', 'LOST']);

function StageChip({ stage }: { stage: DealStage }) {
  return (
    <span className={`admin-status-chip ${DEAL_STAGE_TONE[stage]}`}>
      <i className="dot" />{DEAL_STAGE_LABELS[stage]}
    </span>
  );
}

function DealCard({
  deal, settings, dragDisabled, onOpen, onStageChange, onDragStart, onDragEnd, isDragging,
}: {
  deal: Deal;
  settings: CurrencySettings;
  dragDisabled: boolean;
  onOpen: (deal: Deal) => void;
  onStageChange: (deal: Deal, stage: DealStage) => void;
  onDragStart: (deal: Deal) => void;
  onDragEnd: () => void;
  isDragging: boolean;
}) {
  const age = daysSince(deal.stageChangedAt);
  const stale = !CLOSED.has(deal.stage) && age >= 7;

  return (
    <article
      className={`glass-panel cl-deal-card${isDragging ? ' is-dragging' : ''}`}
      draggable={!dragDisabled}
      onDragStart={event => { event.dataTransfer.setData('text/plain', deal.id); onDragStart(deal); }}
      onDragEnd={onDragEnd}
    >
      <button type="button" className="cl-deal-card-title" onClick={() => onOpen(deal)}>
        {deal.title}
      </button>

      {(deal.companyName || deal.contactName) && (
        <span className="cl-deal-card-sub">
          <Building2 size={11} />{deal.companyName || deal.contactName}
        </span>
      )}

      <div className="cl-deal-card-foot">
        {deal.value !== null
          ? <span className="cl-deal-value">{formatMoney(deal.value, deal.currency, settings)}</span>
          : <span className="cl-muted">без оценки</span>}
        <span className={`cl-deal-days${stale ? ' is-stale' : ''}`} title={`В стадии с ${formatDate(deal.stageChangedAt)}`}>
          <Clock size={11} />{age === 0 ? 'сегодня' : `${age} дн.`}
        </span>
      </div>

      {deal.source && <span className="cl-origin-chip">{deal.source}</span>}

      <label className="cl-deal-stage-select">
        <span className="sr-only">Изменить стадию сделки «{deal.title}»</span>
        <select
          className="form-input" value={deal.stage} disabled={Boolean(deal.clientId)}
          onChange={event => onStageChange(deal, event.target.value as DealStage)}
        >
          {DEAL_STAGE_ORDER.map(stage => <option key={stage} value={stage}>{DEAL_STAGE_LABELS[stage]}</option>)}
        </select>
      </label>
    </article>
  );
}

export function DealsPipeline({
  deals, summary, loading, error, settings, onCreate, onOpen, onStageChange, onRetry,
}: {
  deals: Deal[];
  summary: PipelineSummary | null;
  loading: boolean;
  error: string;
  settings: CurrencySettings;
  onCreate: () => void;
  onOpen: (deal: Deal) => void;
  onStageChange: (deal: Deal, stage: DealStage) => void;
  onRetry: () => void;
}) {
  const [search, setSearch] = useState('');
  const [draggingId, setDraggingId] = useState('');
  const [dragOverStage, setDragOverStage] = useState<DealStage | ''>('');

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return deals;
    return deals.filter(deal =>
      deal.title.toLowerCase().includes(needle)
      || deal.companyName.toLowerCase().includes(needle)
      || deal.contactName.toLowerCase().includes(needle));
  }, [deals, search]);

  const byStage = useMemo(() => {
    const groups: Record<DealStage, Deal[]> = { NEW: [], CONTACTED: [], QUALIFIED: [], PROPOSAL: [], NEGOTIATION: [], WON: [], LOST: [] };
    filtered.forEach(deal => groups[deal.stage].push(deal));
    return groups;
  }, [filtered]);

  const drop = (stage: DealStage) => (event: React.DragEvent) => {
    event.preventDefault();
    setDragOverStage('');
    const id = event.dataTransfer.getData('text/plain');
    const deal = deals.find(item => item.id === id);
    if (deal && deal.stage !== stage) onStageChange(deal, stage);
  };

  if (error) {
    return (
      <div className="cl-error-banner">
        <span>Не удалось загрузить воронку сделок</span>
        <button type="button" className="btn-ghost" onClick={onRetry}>Повторить</button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {summary && (
        <div className="cl-kpi-row">
          <div className="glass-panel admin-stat-card is-info">
            <div className="admin-stat-icon"><Target size={16} /></div>
            <div className="admin-stat-body">
              <span className="admin-stat-label">Открытых сделок</span>
              <strong className="admin-stat-value">{summary.openCount}</strong>
              <span className="admin-stat-hint">{formatMoney(summary.openValue.display, summary.currency, settings)}</span>
            </div>
          </div>
          <div className="glass-panel admin-stat-card is-success">
            <div className="admin-stat-icon"><TrendingUp size={16} /></div>
            <div className="admin-stat-body">
              <span className="admin-stat-label">Выиграно в этом месяце</span>
              <strong className="admin-stat-value">{summary.wonThisMonthCount}</strong>
              <span className="admin-stat-hint">{formatMoney(summary.wonThisMonthValue.display, summary.currency, settings)}</span>
            </div>
          </div>
          <div className="glass-panel admin-stat-card">
            <div className="admin-stat-icon"><Percent size={16} /></div>
            <div className="admin-stat-body">
              <span className="admin-stat-label">Конверсия</span>
              <strong className="admin-stat-value">{summary.winRate === null ? '—' : `${summary.winRate}%`}</strong>
              <span className="admin-stat-hint">{summary.wonCount} выиграно · {summary.lostCount} проиграно</span>
            </div>
          </div>
        </div>
      )}

      <div className="cl-toolbar">
        <label className="cl-search">
          <Search size={15} aria-hidden />
          <input
            className="form-input" type="search" placeholder="Поиск сделок…"
            aria-label="Поиск сделок" value={search} onChange={event => setSearch(event.target.value)}
          />
        </label>
        <div style={{ flex: 1 }} />
        <button type="button" className="btn-primary" onClick={onCreate}>
          <Plus size={14} /><span>Новая сделка</span>
        </button>
      </div>

      {loading && deals.length === 0 ? (
        <div className="cl-kanban">
          {DEAL_STAGE_ORDER.map(stage => (
            <div key={stage} className="cl-kanban-column">
              <div className="cl-kanban-col-head"><span>{DEAL_STAGE_LABELS[stage]}</span></div>
              <div className="cl-skeleton cl-skeleton-card" />
              <div className="cl-skeleton cl-skeleton-card" />
            </div>
          ))}
        </div>
      ) : (
        <div className="cl-kanban">
          {DEAL_STAGE_ORDER.map(stage => {
            const items = byStage[stage];
            const aggregate = summary?.byStage[stage];
            return (
              <div
                key={stage}
                className={`cl-kanban-column${dragOverStage === stage ? ' is-drag-over' : ''}`}
                onDragOver={event => { event.preventDefault(); setDragOverStage(stage); }}
                onDragLeave={() => setDragOverStage(current => (current === stage ? '' : current))}
                onDrop={drop(stage)}
              >
                <div className="cl-kanban-col-head">
                  <StageChip stage={stage} />
                  <span className="cl-kanban-col-count">{items.length}</span>
                  {aggregate && Number(aggregate.valueUsd) > 0 && (
                    <span className="cl-kanban-col-value">{formatMoney(aggregate.valueUsd, 'USD', settings)}</span>
                  )}
                </div>
                <div className="cl-kanban-cards">
                  {items.length === 0 && <div className="cl-kanban-empty">Пусто</div>}
                  {items.map(deal => (
                    <DealCard
                      key={deal.id} deal={deal} settings={settings}
                      dragDisabled={Boolean(deal.clientId)}
                      isDragging={draggingId === deal.id}
                      onOpen={onOpen} onStageChange={onStageChange}
                      onDragStart={item => setDraggingId(item.id)}
                      onDragEnd={() => setDraggingId('')}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default DealsPipeline;
