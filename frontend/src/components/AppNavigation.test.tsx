import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AppNavigation } from './AppNavigation';

describe('workspace navigation', () => {
  it('opens the canonical inbox from Clients and keeps unread indicators', () => {
    const onNavigate = vi.fn();
    render(<AppNavigation language="ru" current={{ tab: 'clients' }} collapsed={false} overdue={1} unread={3} onNavigate={onNavigate} onSignOut={vi.fn()} routerUrl={null} />);
    const workspaces = screen.getByLabelText('Рабочие зоны');
    expect(workspaces.querySelectorAll('.nav-zone-button')).toHaveLength(6);
    expect(within(workspaces).getByText('4')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Входящие' }));
    expect(onNavigate).toHaveBeenCalledWith({ tab: 'smm', view: 'dialogs' });
  });
  it('keeps settings and sign out accessible when collapsed', () => {
    const signOut = vi.fn();
    render(<AppNavigation language="en" current={{ tab: 'vexa' }} collapsed overdue={0} unread={0} onNavigate={vi.fn()} onSignOut={signOut} routerUrl={null} />);
    expect(screen.getByRole('button', { name: 'Settings' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(signOut).toHaveBeenCalledOnce();
  });
  it('shows pending approvals on Operations wherever the owner is', () => {
    render(<AppNavigation language="ru" current={{ tab: 'kanban' }} collapsed={false} overdue={0} unread={0} pendingApprovals={2} onNavigate={vi.fn()} onSignOut={vi.fn()} routerUrl={null} />);
    expect(screen.getByLabelText('2 ждут подтверждения')).toBeInTheDocument();
  });
});
