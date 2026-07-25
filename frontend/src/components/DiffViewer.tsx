import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, FileDiff } from 'lucide-react';

type DiffFile = {
  header: string;
  lines: string[];
};

export function looksLikeUnifiedDiff(text: string): boolean {
  if (!text) return false;
  return /^diff --git |^--- a\/|^\+\+\+ b\/|^@@ -\d/m.test(text);
}

function splitFiles(diff: string): DiffFile[] {
  const lines = diff.split('\n');
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const line of lines) {
    if (line.startsWith('diff --git ') || (!current && (line.startsWith('--- ') || line.startsWith('@@')))) {
      const name = line.startsWith('diff --git ')
        ? (line.match(/ b\/(.+)$/)?.[1] || line.slice(11))
        : 'patch';
      current = { header: name, lines: [] };
      files.push(current);
      if (!line.startsWith('diff --git ')) current.lines.push(line);
      continue;
    }
    if (!current) {
      current = { header: 'patch', lines: [] };
      files.push(current);
    }
    current.lines.push(line);
  }
  return files;
}

function lineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-line is-meta';
  if (line.startsWith('@@')) return 'diff-line is-hunk';
  if (line.startsWith('+')) return 'diff-line is-add';
  if (line.startsWith('-')) return 'diff-line is-del';
  return 'diff-line';
}

/** Lightweight unified-diff renderer: monospace block, green/red line
 * backgrounds, per-file collapse. No external libraries. */
export function DiffViewer({ diff }: { diff: string }) {
  const files = useMemo(() => splitFiles(diff || ''), [diff]);
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({});

  if (!diff?.trim()) return null;
  return (
    <div className="diff-viewer" data-testid="diff-viewer">
      {files.map((file, index) => {
        const isCollapsed = Boolean(collapsed[index]);
        const additions = file.lines.filter(l => l.startsWith('+') && !l.startsWith('+++')).length;
        const deletions = file.lines.filter(l => l.startsWith('-') && !l.startsWith('---')).length;
        return (
          <div className="diff-file" key={`${file.header}-${index}`}>
            <button
              type="button"
              className="diff-file-head"
              onClick={() => setCollapsed(prev => ({ ...prev, [index]: !prev[index] }))}
              aria-expanded={!isCollapsed}
            >
              {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              <FileDiff size={14} />
              <code>{file.header}</code>
              <span className="diff-file-stats">
                <em className="is-add">+{additions}</em>
                <em className="is-del">-{deletions}</em>
              </span>
            </button>
            {!isCollapsed && (
              <pre className="diff-body">
                {file.lines.map((line, lineIndex) => (
                  <span key={lineIndex} className={lineClass(line)}>{line || ' '}</span>
                ))}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}
