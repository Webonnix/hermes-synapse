import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DiffViewer, looksLikeUnifiedDiff } from './DiffViewer';

const SAMPLE_DIFF = `diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-print("old")
+print("new")
+print("extra")
`;

describe('DiffViewer', () => {
  it('detects unified diffs', () => {
    expect(looksLikeUnifiedDiff(SAMPLE_DIFF)).toBe(true);
    expect(looksLikeUnifiedDiff('just a plain result string')).toBe(false);
    expect(looksLikeUnifiedDiff('')).toBe(false);
  });

  it('renders per-file blocks with add/del line classes and stats', () => {
    const { container } = render(<DiffViewer diff={SAMPLE_DIFF} />);
    expect(screen.getByText('app.py')).toBeInTheDocument();
    expect(screen.getByText('+2')).toBeInTheDocument();
    expect(screen.getByText('-1')).toBeInTheDocument();
    expect(container.querySelectorAll('.diff-line.is-add')).toHaveLength(2);
    expect(container.querySelectorAll('.diff-line.is-del')).toHaveLength(1);
    expect(container.querySelectorAll('.diff-line.is-hunk')).toHaveLength(1);
  });

  it('collapses a file when its header is clicked', () => {
    const { container } = render(<DiffViewer diff={SAMPLE_DIFF} />);
    expect(container.querySelector('.diff-body')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { expanded: true }));
    expect(container.querySelector('.diff-body')).toBeNull();
  });
});
