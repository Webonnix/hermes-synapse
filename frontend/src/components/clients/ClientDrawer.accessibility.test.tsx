import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { ClientDrawer } from './ClientDrawer';

it('protects edited data on Escape and backdrop, and explicitly discards it', () => {
  const close = vi.fn();
  render(<ClientDrawer open mode="create" client={null} settings={{ baseCurrency: 'USD', currencies: [], rates: {}, displayCurrency: 'USD', updatedAt: null, updatedBy: null } as any} services={[]} agents={[]} onClose={close} onSubmit={vi.fn()} />);
  fireEvent.change(screen.getByPlaceholderText('Например, Midot Project'), { target: { value: 'Draft client' } });
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(close).not.toHaveBeenCalled();
  expect(screen.getByRole('alert')).toHaveTextContent('несохранённые');
  fireEvent.click(screen.getByRole('button', { name: 'Продолжить редактирование' }));
  expect(screen.getByPlaceholderText('Например, Midot Project')).toHaveValue('Draft client');
  fireEvent.click(document.querySelector('.cl-drawer-backdrop')!);
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Закрыть без сохранения' }));
  expect(close).toHaveBeenCalledOnce();
});
