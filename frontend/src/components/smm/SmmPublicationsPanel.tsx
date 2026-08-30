/**
 * ПУБЛИКАЦИИ — the outbound register of SMM Factory.
 *
 * Everything else in this section answers people who wrote first. This panel
 * is what the accounts say on their own: what went out, what is queued for
 * tonight, what is still a draft, and how far any of it reached.
 *
 * Three decisions worth knowing before changing it:
 *
 * - **The KPI cards and the table read the same filter.** Both are fetched
 *   with the same platform/period query (the cards break down status, so they
 *   ignore only that one). A "Всего 128" that disagrees with the rows under it
 *   is worse than no number at all.
 * - **Nothing here is optimistic.** Publishing, deleting and reach all come
 *   back from the backend before the UI changes; a provider refusal leaves the
 *   row "Ошибка" with the provider's own wording, never "Опубликовано".
 * - **Unknown reach is "—", never 0.** A scheduled post has no reach yet;
 *   showing a zero would read as "nobody saw it".
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle, CheckCircle2, ChevronDown, Clock, Copy, Eye, FileEdit, Filter, Image as ImageIcon,
  LayoutGrid, List, MoreHorizontal, Pencil, RotateCcw, Search, Send, Trash2, TrendingUp,
} from 'lucide-react';
import { authHeaders } from './platforms';
import { SmmPublicationDialog } from './SmmPublicationDialog';
import {
  PER_PAGE_OPTIONS, POST_PLATFORMS, POST_STATUS_CHIP, POST_STATUS_LABELS, effectiveDate, excerpt,
  formatDate, formatDelta, formatReach, formatTime, postPlatform,
  type PostStatus, type Publication, type PublicationPage, type PublicationStats,
} from './publications';

type ViewMode = 'list' | 'grid';
type SortKey = 'date' | 'reach' | 'title';

interface Props {
  /** Hands a post to the Дизайнер tab. `null` starts a blank one. */
  onEdit: (post: Publication | null) => void;
}

/** Why a row has no reach yet. "Ожидается" on a draft would promise a number
 *  that nothing is going to produce, and on a failed post it would hide that
 *  the post never went out at all. */
const REACH_HINT: Record<PostStatus, string> = {
  published: 'Нет данных',
  scheduled: 'Ожидается',
  draft: 'Ещё не публиковалось',
  failed: 'Не опубликовано',
};

const EMPTY_STATS: PublicationStats = {
  total: 0, published: 0, scheduled: 0, drafts: 0, failed: 0,
  published_share: 0, scheduled_share: 0, drafts_share: 0, failed_share: 0,
  added_last_week: 0, best_reach: null, best_reach_delta: null, reach_series: [],
};

/** Reach over the last published posts, oldest first. Pure decoration for the
 *  KPI card — it carries no labels, so it is hidden from assistive tech and
 *  the number beside it is the accessible value. */
function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const width = 76;
  const height = 26;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min || 1;
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - min) / span) * (height - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg className="smm-sparkline" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" focusable="false">
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/** Row overflow menu. A popover rather than three more icon buttons: publish,
 *  duplicate and delete are rarer than view/edit and one of them is
 *  destructive, so they should not sit under the cursor by accident.
 *
 *  Rendered into a portal because its natural parent is `.admin-table-wrap`,
 *  which scrolls horizontally — and a scroll container clips both axes, so an
 *  in-flow popover would be cut off on every row.
 */
function RowMenu({ post, onPublish, onDuplicate, onDelete, busy }: {
  post: Publication;
  onPublish: (post: Publication) => void;
  onDuplicate: (post: Publication) => void;
  onDelete: (post: Publication) => void;
  busy: boolean;
}) {
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const open = anchor !== null;

  const place = useCallback(() => {
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) setAnchor({ top: rect.bottom + 6, right: window.innerWidth - rect.right });
  }, []);

  useEffect(() => {
    if (!open) return;
    const close = () => setAnchor(null);
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!buttonRef.current?.contains(target) && !menuRef.current?.contains(target)) close();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { close(); buttonRef.current?.focus(); }
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    // A fixed popover would otherwise stay put while the page moves under it.
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [open]);

  const platform = postPlatform(post.platform);
  const canPublish = post.status !== 'published'
    && (post.delivery_mode === 'manual' || (platform.deliverable && Boolean(post.binding_id && post.target_chat_id)));

  const act = (fn: (post: Publication) => void) => () => { setAnchor(null); fn(post); };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="icon-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Ещё действия: ${post.title}`}
        disabled={busy}
        onClick={() => (open ? setAnchor(null) : place())}
      >
        <MoreHorizontal size={15} />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          role="menu"
          className="glass-panel"
          style={{
            position: 'fixed', top: anchor.top, right: anchor.right, zIndex: 70, minWidth: 208,
            padding: 6, display: 'flex', flexDirection: 'column', gap: 2,
          }}
        >
          <button type="button" role="menuitem" className="btn-ghost" style={{ justifyContent: 'flex-start' }}
            disabled={!canPublish} onClick={act(onPublish)}>
            <Send size={13} />
            <span>{post.delivery_mode === 'auto' ? 'Опубликовать сейчас' : 'Отметить опубликованной'}</span>
          </button>
          <button type="button" role="menuitem" className="btn-ghost" style={{ justifyContent: 'flex-start' }}
            onClick={act(onDuplicate)}>
            <Copy size={13} /><span>Дублировать</span>
          </button>
          <button type="button" role="menuitem" className="btn-ghost danger" style={{ justifyContent: 'flex-start' }}
            onClick={act(onDelete)}>
            <Trash2 size={13} /><span>Удалить</span>
          </button>
        </div>,
        document.body,
      )}
    </>
  );
}

export function SmmPublicationsPanel({ onEdit }: Props) {
  const [page, setPage] = useState<PublicationPage>({ items: [], total: 0, page: 1, per_page: 10, pages: 1 });
  const [stats, setStats] = useState<PublicationStats>(EMPTY_STATS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState('');
  const [preview, setPreview] = useState<Publication | null>(null);

  const [view, setView] = useState<ViewMode>(() => (localStorage.getItem('smm_posts_view') as ViewMode) || 'list');
  const [showFilters, setShowFilters] = useState(false);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [platform, setPlatform] = useState('all');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [sort, setSort] = useState<SortKey>('date');
  const [direction, setDirection] = useState<'asc' | 'desc'>('desc');
  const [pageNumber, setPageNumber] = useState(1);
  const [perPage, setPerPage] = useState(10);
  const [selected, setSelected] = useState<string[]>([]);

  useEffect(() => { localStorage.setItem('smm_posts_view', view); }, [view]);

  // Typing filters as you type would fire a request per keystroke; 300ms is
  // long enough to coalesce a word and short enough not to feel laggy.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Any filter change invalidates the current page number — staying on page 4
  // of a result set that now has one page shows an empty table.
  useEffect(() => { setPageNumber(1); }, [query, status, platform, dateFrom, dateTo, perPage]);

  const listUrl = useMemo(() => {
    const params = new URLSearchParams({
      status, platform, page: String(pageNumber), per_page: String(perPage), sort, direction,
    });
    if (query) params.set('q', query);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    return `/api/smm/publications?${params}`;
  }, [status, platform, pageNumber, perPage, sort, direction, query, dateFrom, dateTo]);

  const statsUrl = useMemo(() => {
    const params = new URLSearchParams({ platform });
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    return `/api/smm/publications/stats?${params}`;
  }, [platform, dateFrom, dateTo]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [listResponse, statsResponse] = await Promise.all([
        fetch(listUrl, { headers: authHeaders() }),
        fetch(statsUrl, { headers: authHeaders() }),
      ]);
      if (!listResponse.ok) throw new Error('Не удалось загрузить публикации');
      const data = await listResponse.json() as PublicationPage;
      setPage(data);
      // The backend clamps the page to what exists; follow it rather than
      // keeping a number that would re-request the same empty page forever.
      setPageNumber(current => (current === data.page ? current : data.page));
      if (statsResponse.ok) setStats(await statsResponse.json() as PublicationStats);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось загрузить публикации');
    } finally {
      setLoading(false);
    }
  }, [listUrl, statsUrl]);

  useEffect(() => { void load(); }, [load]);

  // A selection that outlives its rows would delete things the owner can no
  // longer see, so it is pruned to what is actually on screen.
  useEffect(() => {
    setSelected(current => {
      const kept = current.filter(id => page.items.some(item => item.id === id));
      return kept.length === current.length ? current : kept;
    });
  }, [page.items]);

  const mutate = async (id: string, request: () => Promise<Response>, failure: string) => {
    setBusyId(id);
    try {
      const response = await request();
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || failure);
      }
      setError('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : failure);
    } finally {
      setBusyId('');
    }
  };

  const publish = (post: Publication) => void mutate(
    post.id,
    () => fetch(`/api/smm/publications/${post.id}/publish`, { method: 'POST', headers: authHeaders() }),
    'Не удалось опубликовать',
  );

  const remove = (post: Publication) => {
    if (!window.confirm(`Удалить публикацию «${post.title}»? Это действие необратимо.`)) return;
    void mutate(
      post.id,
      () => fetch(`/api/smm/publications/${post.id}`, { method: 'DELETE', headers: authHeaders() }),
      'Не удалось удалить публикацию',
    );
  };

  const duplicate = (post: Publication) => onEdit({
    ...post, id: '', title: `${post.title} (копия)`, status: 'draft',
    published_at: null, scheduled_at: null, reach: null, reach_delta: null, error: '',
  });

  const removeSelected = () => {
    if (!selected.length) return;
    if (!window.confirm(`Удалить выбранные публикации (${selected.length})? Это действие необратимо.`)) return;
    void mutate(
      'bulk',
      () => fetch('/api/smm/publications/bulk-delete', {
        method: 'POST', headers: authHeaders(), body: JSON.stringify({ ids: selected }),
      }),
      'Не удалось удалить публикации',
    );
  };

  const resetFilters = () => {
    setSearch(''); setStatus('all'); setPlatform('all'); setDateFrom(''); setDateTo('');
    setSort('date'); setDirection('desc');
  };

  const activeFilters = [
    query, status !== 'all' ? status : '', platform !== 'all' ? platform : '', dateFrom, dateTo,
  ].filter(Boolean).length;

  const toggleSort = (key: SortKey) => {
    if (sort === key) setDirection(current => (current === 'asc' ? 'desc' : 'asc'));
    else { setSort(key); setDirection(key === 'title' ? 'asc' : 'desc'); }
  };

  const sortState = (key: SortKey) => (sort === key ? (direction === 'asc' ? 'ascending' : 'descending') : 'none');

  const allOnPageSelected = page.items.length > 0 && page.items.every(item => selected.includes(item.id));
  const toggleAll = () => setSelected(allOnPageSelected ? [] : page.items.map(item => item.id));
  const toggleOne = (id: string) => setSelected(current =>
    current.includes(id) ? current.filter(value => value !== id) : [...current, id]);

  const from = page.total === 0 ? 0 : (page.page - 1) * page.per_page + 1;
  const to = Math.min(page.page * page.per_page, page.total);

  const renderStatus = (post: Publication) => (
    <span className={`admin-status-chip ${POST_STATUS_CHIP[post.status]}`}>
      <i className="dot" />{POST_STATUS_LABELS[post.status]}
    </span>
  );

  const renderReach = (post: Publication) => (
    <>
      <span className={`smm-reach${post.reach === null ? ' is-empty' : ''}`}>{formatReach(post.reach)}</span>
      {post.reach_delta !== null
        ? <span className={`smm-delta${post.reach_delta < 0 ? ' is-down' : ''}`}>{formatDelta(post.reach_delta)}</span>
        : <span className="smm-sub">{REACH_HINT[post.status]}</span>}
    </>
  );

  const renderActions = (post: Publication) => (
    <div className="smm-row-actions">
      <button type="button" className="icon-btn" aria-label={`Просмотреть: ${post.title}`} onClick={() => setPreview(post)}>
        <Eye size={15} />
      </button>
      <button type="button" className="icon-btn" aria-label={`Редактировать: ${post.title}`} onClick={() => onEdit(post)}>
        <Pencil size={15} />
      </button>
      <RowMenu post={post} onPublish={publish} onDuplicate={duplicate} onDelete={remove} busy={busyId === post.id} />
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="smm-kpi-row">
        <div className="glass-panel admin-stat-card">
          <div className="admin-stat-icon"><Send size={16} /></div>
          <div className="admin-stat-body">
            <span className="admin-stat-label">Всего публикаций</span>
            <strong className="admin-stat-value">{stats.total}</strong>
            <span className="admin-stat-hint">+{stats.added_last_week} за неделю</span>
          </div>
        </div>
        <div className="glass-panel admin-stat-card is-success">
          <div className="admin-stat-icon"><CheckCircle2 size={16} /></div>
          <div className="admin-stat-body">
            <span className="admin-stat-label">Опубликовано</span>
            <strong className="admin-stat-value">{stats.published}</strong>
            <span className="admin-stat-hint">{stats.published_share}% от всех</span>
          </div>
        </div>
        <div className="glass-panel admin-stat-card is-warning">
          <div className="admin-stat-icon"><Clock size={16} /></div>
          <div className="admin-stat-body">
            <span className="admin-stat-label">Запланировано</span>
            <strong className="admin-stat-value">{stats.scheduled}</strong>
            <span className="admin-stat-hint">{stats.scheduled_share}% от всех</span>
          </div>
        </div>
        <div className="glass-panel admin-stat-card is-info">
          <div className="admin-stat-icon"><FileEdit size={16} /></div>
          <div className="admin-stat-body">
            <span className="admin-stat-label">Черновиков</span>
            <strong className="admin-stat-value">{stats.drafts}</strong>
            <span className="admin-stat-hint">{stats.drafts_share}% от всех</span>
          </div>
        </div>
        <div className="glass-panel admin-stat-card smm-kpi-wide">
          <div className="admin-stat-body" style={{ flex: 1 }}>
            <span className="admin-stat-label">Лучший охват</span>
            <strong className="admin-stat-value">{formatReach(stats.best_reach)}</strong>
            <span className="admin-stat-hint">
              {stats.best_reach === null ? 'Охват ещё не измерялся' : formatDelta(stats.best_reach_delta) || 'без сравнения'}
            </span>
          </div>
          <Sparkline values={stats.reach_series} />
        </div>
        {stats.failed > 0 && (
          <div className="glass-panel admin-stat-card">
            <div className="admin-stat-icon" style={{ color: 'var(--danger)', background: 'rgba(255, 93, 143, 0.12)' }}>
              <AlertTriangle size={16} />
            </div>
            <div className="admin-stat-body">
              <span className="admin-stat-label">Ошибок</span>
              <strong className="admin-stat-value">{stats.failed}</strong>
              <span className="admin-stat-hint">{stats.failed_share}% от всех</span>
            </div>
          </div>
        )}
      </div>

      <div className="glass-panel" style={{ padding: 0 }}>
        <div className="smm-toolbar">
          <div className="smm-toolbar-field smm-toolbar-search">
            <label className="sr-only" htmlFor="smm-search">Поиск публикаций</label>
            <input
              id="smm-search" className="form-input" type="search" value={search}
              placeholder="Поиск публикаций..." onChange={event => setSearch(event.target.value)}
            />
            <Search size={15} />
          </div>

          <div className="smm-toolbar-field">
            <label htmlFor="smm-status">Статус</label>
            <select id="smm-status" className="form-input" value={status} onChange={event => setStatus(event.target.value)}>
              <option value="all">Все статусы</option>
              <option value="published">Опубликовано</option>
              <option value="scheduled">Запланировано</option>
              <option value="draft">Черновик</option>
              <option value="failed">Ошибка</option>
            </select>
          </div>

          <div className="smm-toolbar-field">
            <label htmlFor="smm-platform">Платформа</label>
            <select id="smm-platform" className="form-input" value={platform} onChange={event => setPlatform(event.target.value)}>
              <option value="all">Все платформы</option>
              {POST_PLATFORMS.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
          </div>

          <div className="smm-toolbar-field">
            <label htmlFor="smm-date-from">Период</label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <input id="smm-date-from" className="form-input" type="date" value={dateFrom}
                aria-label="Период с" onChange={event => setDateFrom(event.target.value)} />
              <span className="smm-muted">–</span>
              <input className="form-input" type="date" value={dateTo}
                aria-label="Период по" onChange={event => setDateTo(event.target.value)} />
            </div>
          </div>

          <div className="smm-toolbar-spacer" />

          <button
            type="button" className="btn-ghost" aria-expanded={showFilters}
            onClick={() => setShowFilters(value => !value)}
          >
            <Filter size={13} />
            <span>Фильтры{activeFilters > 0 ? ` · ${activeFilters}` : ''}</span>
          </button>

          <div className="smm-view-toggle" role="group" aria-label="Вид списка">
            <button type="button" aria-pressed={view === 'grid'} aria-label="Плитка" onClick={() => setView('grid')}>
              <LayoutGrid size={15} />
            </button>
            <button type="button" aria-pressed={view === 'list'} aria-label="Список" onClick={() => setView('list')}>
              <List size={15} />
            </button>
          </div>
        </div>

        {showFilters && (
          <div className="smm-toolbar" style={{ paddingTop: 0 }}>
            <div className="smm-toolbar-field">
              <label htmlFor="smm-sort">Сортировка</label>
              <select id="smm-sort" className="form-input" value={sort} onChange={event => setSort(event.target.value as SortKey)}>
                <option value="date">По дате</option>
                <option value="reach">По охвату</option>
                <option value="title">По названию</option>
              </select>
            </div>
            <div className="smm-toolbar-field">
              <label htmlFor="smm-direction">Порядок</label>
              <select id="smm-direction" className="form-input" value={direction}
                onChange={event => setDirection(event.target.value as 'asc' | 'desc')}>
                <option value="desc">По убыванию</option>
                <option value="asc">По возрастанию</option>
              </select>
            </div>
            <div className="smm-toolbar-field">
              <label htmlFor="smm-per-page">На странице</label>
              <select id="smm-per-page" className="form-input" value={perPage}
                onChange={event => setPerPage(Number(event.target.value))}>
                {PER_PAGE_OPTIONS.map(value => <option key={value} value={value}>{value}</option>)}
              </select>
            </div>
            <div className="smm-toolbar-spacer" />
            <button type="button" className="btn-ghost" onClick={resetFilters} disabled={activeFilters === 0}>
              <RotateCcw size={13} /><span>Сбросить</span>
            </button>
          </div>
        )}

        {selected.length > 0 && (
          <div className="smm-selection-bar" style={{ margin: '0 16px 12px' }}>
            <strong>Выбрано: {selected.length}</strong>
            <button type="button" className="btn-ghost danger" onClick={removeSelected} disabled={busyId === 'bulk'}>
              <Trash2 size={13} /><span>Удалить выбранные</span>
            </button>
            <button type="button" className="btn-ghost" onClick={() => setSelected([])}>Снять выделение</button>
          </div>
        )}

        {error && (
          <p className="smm-notice is-error" style={{ margin: '0 16px 12px' }}>
            <AlertTriangle size={14} /><span>{error}</span>
          </p>
        )}

        {loading && page.items.length === 0 ? (
          <p style={{ padding: '30px 16px', textAlign: 'center', color: 'var(--text-dim)', fontSize: '0.85rem' }}>
            Загрузка публикаций…
          </p>
        ) : page.items.length === 0 ? (
          <div className="admin-empty-cta" style={{ margin: '0 16px 16px' }}>
            <Send size={22} />
            <span>
              {activeFilters > 0
                ? 'Под эти фильтры ничего не подходит. Измените период или статус.'
                : 'Публикаций пока нет. Создайте первую — она появится здесь вместе с расписанием и охватом.'}
            </span>
            {activeFilters > 0
              ? <button type="button" className="btn-ghost" onClick={resetFilters}><RotateCcw size={13} /><span>Сбросить фильтры</span></button>
              : <button type="button" className="btn-primary" onClick={() => onEdit(null)}><Send size={13} /><span>Создать публикацию</span></button>}
          </div>
        ) : view === 'list' ? (
          <div className="admin-table-wrap" style={{ marginBottom: 0 }}>
            <table className="admin-table smm-table">
              <caption className="sr-only">Публикации в социальных сетях</caption>
              <thead>
                <tr>
                  <th className="smm-col-check">
                    <input
                      type="checkbox" checked={allOnPageSelected} onChange={toggleAll}
                      aria-label="Выбрать все публикации на странице"
                    />
                  </th>
                  <th aria-sort={sortState('title')}>
                    <button
                      type="button" className="smm-sort-btn" data-active={sort === 'title'} data-direction={direction}
                      onClick={() => toggleSort('title')}
                    >
                      Публикация{sort === 'title' && <ChevronDown size={12} />}
                    </button>
                  </th>
                  <th>Платформа</th>
                  <th>Статус</th>
                  <th aria-sort={sortState('date')}>
                    <button
                      type="button" className="smm-sort-btn" data-active={sort === 'date'} data-direction={direction}
                      onClick={() => toggleSort('date')}
                    >
                      Дата{sort === 'date' && <ChevronDown size={12} />}
                    </button>
                  </th>
                  <th aria-sort={sortState('reach')}>
                    <button
                      type="button" className="smm-sort-btn" data-active={sort === 'reach'} data-direction={direction}
                      onClick={() => toggleSort('reach')}
                    >
                      Охват{sort === 'reach' && <ChevronDown size={12} />}
                    </button>
                  </th>
                  <th style={{ textAlign: 'right' }}>Действия</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map(post => {
                  const meta = postPlatform(post.platform);
                  const Icon = meta.icon;
                  const when = effectiveDate(post);
                  return (
                    <tr key={post.id}>
                      <td className="smm-col-check">
                        <input
                          type="checkbox" checked={selected.includes(post.id)} onChange={() => toggleOne(post.id)}
                          aria-label={`Выбрать: ${post.title}`}
                        />
                      </td>
                      <td>
                        <div className="smm-post-cell">
                          {post.media_url
                            ? <img className="smm-post-thumb" src={post.media_url} alt="" loading="lazy" />
                            : <span className="smm-post-thumb"><ImageIcon size={16} /></span>}
                          <span className="smm-post-text">
                            <span className="smm-post-title">{post.title}</span>
                            {post.body && <span className="smm-post-excerpt">{excerpt(post.body)}</span>}
                            {post.tags.length > 0 && (
                              <span className="smm-tags">
                                {post.tags.slice(0, 2).map(tag => <span key={tag} className="smm-tag">#{tag}</span>)}
                              </span>
                            )}
                          </span>
                        </div>
                      </td>
                      <td>
                        <div className="smm-account">
                          <span className="smm-account-icon" style={{ color: meta.color }}><Icon size={15} /></span>
                          <span style={{ minWidth: 0 }}>
                            <span className="smm-account-name">{meta.label}</span>
                            <span className="smm-account-handle">{post.account || 'аккаунт не указан'}</span>
                          </span>
                        </div>
                      </td>
                      <td>
                        {renderStatus(post)}
                        {post.status === 'failed' && post.error && (
                          <span className="smm-sub" title={post.error}>{excerpt(post.error, 32)}</span>
                        )}
                      </td>
                      <td>
                        {formatDate(when)}
                        <span className="smm-sub">
                          {post.status === 'draft' && when ? `сохранено ${formatTime(when)}` : formatTime(when)}
                        </span>
                      </td>
                      <td>{renderReach(post)}</td>
                      <td>{renderActions(post)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="smm-grid" style={{ padding: '0 16px 16px' }}>
            {page.items.map(post => {
              const meta = postPlatform(post.platform);
              const Icon = meta.icon;
              const when = effectiveDate(post);
              return (
                <article key={post.id} className="glass-panel smm-card">
                  <div className="smm-card-media">
                    {post.media_url ? <img src={post.media_url} alt="" loading="lazy" /> : <ImageIcon size={22} />}
                    {renderStatus(post)}
                  </div>
                  <div>
                    <strong className="smm-post-title">{post.title}</strong>
                    {post.body && <span className="smm-post-excerpt">{excerpt(post.body, 58)}</span>}
                  </div>
                  <div className="smm-account">
                    <span className="smm-account-icon" style={{ color: meta.color }}><Icon size={15} /></span>
                    <span style={{ minWidth: 0 }}>
                      <span className="smm-account-name">{post.account || meta.label}</span>
                      <span className="smm-account-handle">{formatDate(when)}{formatTime(when) ? ` · ${formatTime(when)}` : ''}</span>
                    </span>
                  </div>
                  <div className="smm-card-foot">
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                      <TrendingUp size={13} className="smm-muted" />
                      <span className={`smm-reach${post.reach === null ? ' is-empty' : ''}`}>{formatReach(post.reach)}</span>
                    </span>
                    {renderActions(post)}
                  </div>
                </article>
              );
            })}
          </div>
        )}

        {page.total > 0 && (
          <div className="smm-pagination">
            <span aria-live="polite" aria-atomic="true">
              Показано {from}–{to} из {page.total} публикаций
            </span>
            <nav className="smm-pages" aria-label="Страницы публикаций">
              <button type="button" className="smm-page-btn" aria-label="Предыдущая страница"
                disabled={page.page <= 1} onClick={() => setPageNumber(page.page - 1)}>
                ‹
              </button>
              {pageNumbers(page.page, page.pages).map((value, index) => (
                value === null
                  ? <span key={`gap-${index}`} className="smm-page-ellipsis">…</span>
                  : (
                    <button
                      key={value} type="button" className="smm-page-btn"
                      aria-current={value === page.page ? 'page' : undefined}
                      aria-label={`Страница ${value}`}
                      onClick={() => setPageNumber(value)}
                    >
                      {value}
                    </button>
                  )
              ))}
              <button type="button" className="smm-page-btn" aria-label="Следующая страница"
                disabled={page.page >= page.pages} onClick={() => setPageNumber(page.page + 1)}>
                ›
              </button>
            </nav>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <label htmlFor="smm-per-page-foot">На странице</label>
              <select id="smm-per-page-foot" className="form-input" style={{ width: 78 }} value={perPage}
                onChange={event => setPerPage(Number(event.target.value))}>
                {PER_PAGE_OPTIONS.map(value => <option key={value} value={value}>{value}</option>)}
              </select>
            </div>
          </div>
        )}
      </div>

      {preview && (
        <SmmPublicationDialog
          post={preview}
          onClose={() => setPreview(null)}
          onEdit={post => { setPreview(null); onEdit(post); }}
        />
      )}
    </div>
  );
}

/** 1 … 4 5 6 … 13 — the window around the current page plus both ends, so the
 *  control stays one line however many pages there are. `null` is a gap. */
export function pageNumbers(current: number, total: number): (number | null)[] {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  const pages = new Set<number>([1, total, current, current - 1, current + 1]);
  if (current <= 3) [2, 3, 4].forEach(value => pages.add(value));
  if (current >= total - 2) [total - 1, total - 2, total - 3].forEach(value => pages.add(value));
  const sorted = [...pages].filter(value => value >= 1 && value <= total).sort((a, b) => a - b);
  const result: (number | null)[] = [];
  sorted.forEach((value, index) => {
    if (index > 0 && value - sorted[index - 1] > 1) result.push(null);
    result.push(value);
  });
  return result;
}

export default SmmPublicationsPanel;
