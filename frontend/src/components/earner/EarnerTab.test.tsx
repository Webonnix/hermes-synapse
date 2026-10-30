import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { EarnerTab } from './EarnerTab';

const overview = {
  settings: { enabled: true, directions: [], constraints: '', sources: [{ id: 'demo', kind: 'demo', enabled: true }],
    allowed_hosts: [], limits: { tasks_per_day: 5 }, paid_search_enabled: false, revision: 2, updated_at: '',
    external_enabled: true, external_model: 'dva/deepseek-v4-pro-high', external_month_usd: 30, wallet: { address: '', networks: [] } },
  budget: {
    day: '2026-10-20', timezone: 'UTC',
    tasks: { used: 1, limit: 5, remaining: 4 }, llm_calls: { used: 2, limit: 20, remaining: 18 },
    tokens: { used: 900, limit: 100000, remaining: 99100, actual: 900 }, fetches: { used: 0, limit: 60, remaining: 60 },
    sandbox_runs: { used: 0, limit: 10, remaining: 10 }, sandbox_concurrency: { used: 0, limit: 1 },
    est_cost_usd: 0.001, est_cost_note: 'Оценка, не оплата.', submissions: { used: 0, limit: 3, remaining: 3 },
    external: { month: '2026-10', enabled: true, model: 'dva/deepseek-v4-pro-high', limit_usd: 30, used_usd: 0.12, reserved_usd: 0.12, remaining_usd: 29.88 },
  },
  llm: { provider: 'llama.cpp', host: 'hermes-llama', model: 'qwen3.8-27b', local: true, reason: '' },
  sandbox: { available: false, reason: 'в Docker нет runtime «runsc»', runtime: 'runsc', image: 'x' },
  schedule: null, counts: {}, pending_approvals: 1,
  report: { potential: 1, prepared: 0, delivered_manual: 0, paid_manual: 0, paid_onchain: 0, demo: 1, note: 'Это не заработанные деньги.' },
  runs: [],
  income: { wallet: { address: '', networks: [] }, total_usd: 0, linked_usd: 0, note: '', cursors: [] },
};

const approval = {
  id: 'apr-1', action: 'research', target_id: 'settings', content_hash: 'a'.repeat(64),
  summary: { title: 'Исследование: 1 источн.', will_do: ['Scout запросит демо'], data: ['Демо-источник'], limits: ['Задачи 1 из 5'], not_included: ['Финансовые операции'] },
  status: 'pending', status_reason: '', created_at: '2026-10-20T10:00:00+00:00',
  expires_at: new Date(Date.now() + 3600_000).toISOString(), requested_by: 'operator:owner', decided_at: null, decided_by: '',
};

function mockFetch(approvals: unknown[] = [approval]) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const body = url.endsWith('/overview') ? overview
      : url.endsWith('/opportunities') ? [{ id: 'opp-1', title: '<img src=x onerror=alert(1)> task', status: 'proposed', source_id: 'demo', is_demo: 1, claimed_reward: '', fetched_at: '', url: 'https://example.invalid/x' }]
      : url.endsWith('/approvals') ? approvals
      : url.includes('/confirm') ? { approval_id: 'apr-1', run_id: 'run-1' }
      : {};
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  }));
  return calls;
}

describe('EarnerTab', () => {
  beforeEach(() => vi.unstubAllGlobals());

  it('shows the budget, the honest report and the disabled sandbox reason', async () => {
    mockFetch();
    render(<EarnerTab />);
    expect(await screen.findByText('Autonomous Earner')).toBeInTheDocument();
    expect(screen.getByText(/нет runtime «runsc»/)).toBeInTheDocument();
    expect(screen.getByText(/Это не заработанные деньги/)).toBeInTheDocument();
    expect(screen.getByText('1 / 5')).toBeInTheDocument();
    expect(screen.getByText(/dva\/deepseek-v4-pro-high, осталось \$29.88 из \$30.00/)).toBeInTheDocument();
    expect(screen.getByText(/Кошелёк не указан/)).toBeInTheDocument();
  });

  it('confirms only after the operator ticks the check, with the shown hash and intent header', async () => {
    const calls = mockFetch();
    render(<EarnerTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Подтверждения/ }));
    const confirm = await screen.findByRole('button', { name: /Подтвердить/ });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Я проверил/));
    fireEvent.click(confirm);
    await waitFor(() => expect(calls.some(c => c.url.includes('/approvals/apr-1/confirm'))).toBe(true));
    const call = calls.find(c => c.url.includes('/confirm'))!;
    expect(new Headers(call.init?.headers).get('X-Hermes-Intent')).toBe('earner-approve');
    expect(JSON.parse(String(call.init?.body)).content_hash).toBe('a'.repeat(64));
  });

  it('renders untrusted titles as text, never as markup', async () => {
    mockFetch();
    const { container } = render(<EarnerTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Возможности/ }));
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)> task/)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows the exact diff of a patch approval as plain text', async () => {
    const diff = '--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return "<script>alert(1)</script>"';
    mockFetch([{ ...approval, id: 'apr-2', action: 'execute', summary: { ...approval.summary, title: 'Тесты репозитория o/r', diff } }]);
    const { container } = render(<EarnerTab />);
    fireEvent.click(await screen.findByRole('button', { name: /Подтверждения/ }));
    expect(await screen.findByText(/Точный diff/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
    const added = Array.from(container.querySelectorAll('.ea-diff .is-add')).map(node => node.textContent);
    expect(added).toEqual(['+    return "<script>alert(1)</script>"\n']);
    expect(container.querySelectorAll('.ea-diff .is-del')).toHaveLength(1);
  });
});
