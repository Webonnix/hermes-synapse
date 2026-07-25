import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { FloatingWindow } from './FloatingWindow';

const labels = {
  minimize: 'Minimize',
  restore: 'Restore window',
  fullscreen: 'Fullscreen',
  exitFullscreen: 'Exit fullscreen',
  close: 'Close chat history',
};

describe('FloatingWindow', () => {
  it('minimizes to a dock pill and restores back to the panel', () => {
    render(
      <FloatingWindow title="VEXA" subtitle="Main Terminal" storageKey="test_window" onClose={vi.fn()} labels={labels}>
        <p>Panel content</p>
      </FloatingWindow>,
    );

    expect(screen.getByText('Panel content')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Minimize' }));
    expect(screen.queryByText('Panel content')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restore window' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Restore window' }));
    expect(screen.getByText('Panel content')).toBeInTheDocument();
  });

  it('toggles fullscreen mode', () => {
    render(
      <FloatingWindow title="VEXA" storageKey="test_window_fs" onClose={vi.fn()} labels={labels}>
        <p>Panel content</p>
      </FloatingWindow>,
    );

    const toggle = screen.getByRole('button', { name: 'Fullscreen' });
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Exit fullscreen' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Exit fullscreen' }));
    expect(screen.getByRole('button', { name: 'Fullscreen' })).toBeInTheDocument();
  });

  it('calls onClose when the close button is clicked', () => {
    const onClose = vi.fn();
    render(
      <FloatingWindow title="VEXA" storageKey="test_window_close" onClose={onClose} labels={labels}>
        <p>Panel content</p>
      </FloatingWindow>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Close chat history' }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
