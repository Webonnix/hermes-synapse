import { useState } from 'react';
import type { ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';

/**
 * Markdown rendering for the Open WebUI-style chat view.
 *
 * utils.tsx's renderMarkdown paints its own colours inline (white bold, cyan
 * links, orange code) because the rest of the dashboard is dark-only. This view
 * has a light theme too, so everything here is class-based and takes its
 * colours from the stylesheet instead. Same block coverage plus fenced code
 * blocks with a language label and a copy button, which is what a chat answer
 * full of shell commands actually needs.
 */

// ─── Inline ─────────────────────────────────────────────────────────────────
// Order matters: code spans are matched first so `**not bold**` inside
// backticks stays literal.
const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(~~[^~]+~~)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]]*\]\([^)\s]+\))|(https?:\/\/[^\s<>()]+)/g;

export function renderInline(text: string, keyPrefix = ''): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(INLINE_RE);

  while ((match = re.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const key = `${keyPrefix}-${match.index}`;
    const [raw] = match;

    if (match[1]) out.push(<code key={key} className="owui-code-inline">{raw.slice(1, -1)}</code>);
    else if (match[2]) out.push(<strong key={key}>{renderInline(raw.slice(2, -2), key)}</strong>);
    else if (match[3]) out.push(<strong key={key}>{renderInline(raw.slice(2, -2), key)}</strong>);
    else if (match[4]) out.push(<del key={key}>{renderInline(raw.slice(2, -2), key)}</del>);
    else if (match[5]) out.push(<em key={key}>{renderInline(raw.slice(1, -1), key)}</em>);
    else if (match[6]) out.push(<em key={key}>{renderInline(raw.slice(1, -1), key)}</em>);
    else if (match[7]) {
      const label = raw.slice(1, raw.indexOf(']'));
      const href = raw.slice(raw.indexOf('](') + 2, -1);
      out.push(<a key={key} href={href} target="_blank" rel="noopener noreferrer">{renderInline(label, key)}</a>);
    } else if (match[8]) {
      out.push(<a key={key} href={raw} target="_blank" rel="noopener noreferrer">{raw}</a>);
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length ? out : [text];
}

// ─── Code block ─────────────────────────────────────────────────────────────

function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard blocked (insecure origin) — the text is still selectable */ }
  };

  return (
    <div className="owui-codeblock">
      <div className="owui-codeblock-head">
        <span>{language || 'text'}</span>
        <button type="button" onClick={copy} aria-label="Copy code">
          {copied ? <Check size={12} /> : <Copy size={12} />}
          <span>{copied ? 'OK' : 'Copy'}</span>
        </button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

// ─── Blocks ─────────────────────────────────────────────────────────────────

function renderTable(rows: string[], key: string): ReactNode {
  const cells = (line: string) => line.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
  const header = cells(rows[0]);
  // rows[1] is the |---|---| separator, which carries no content.
  const body = rows.slice(2).map(cells);
  return (
    <div className="owui-table-wrap" key={key}>
      <table>
        <thead>
          <tr>{header.map((cell, i) => <th key={i}>{renderInline(cell, `${key}h${i}`)}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((row, r) => (
            <tr key={r}>{row.map((cell, c) => <td key={c}>{renderInline(cell, `${key}b${r}${c}`)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface ListItem { text: string; depth: number; ordered: boolean }

function renderList(items: ListItem[], key: string): ReactNode {
  // Rebuild nesting from each item's indent depth; a jump of any size counts
  // as one level, because models indent sub-lists by 2, 3 or 4 spaces alike.
  const build = (start: number, depth: number): [ReactNode, number] => {
    const nodes: ReactNode[] = [];
    let i = start;
    const ordered = items[start].ordered;
    while (i < items.length && items[i].depth >= depth) {
      if (items[i].depth > depth) {
        const [child, next] = build(i, items[i].depth);
        const lastIndex = nodes.length - 1;
        nodes[lastIndex] = <li key={`${key}-${i}`}>{(nodes[lastIndex] as any)?.props?.children}{child}</li>;
        i = next;
        continue;
      }
      nodes.push(<li key={`${key}-${i}`}>{renderInline(items[i].text, `${key}i${i}`)}</li>);
      i += 1;
    }
    const Tag = ordered ? 'ol' : 'ul';
    return [<Tag key={`${key}-l${depth}`}>{nodes}</Tag>, i];
  };
  return build(0, items[0].depth)[0];
}

export function Markdown({ text }: { text: string }) {
  if (!text) return null;

  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let listItems: ListItem[] = [];
  let quote: string[] = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(<p key={`p${blocks.length}`}>{renderInline(paragraph.join('\n'), `p${blocks.length}`)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push(renderList(listItems, `l${blocks.length}`));
    listItems = [];
  };
  const flushQuote = () => {
    if (!quote.length) return;
    blocks.push(
      <blockquote key={`q${blocks.length}`}>{renderInline(quote.join('\n'), `q${blocks.length}`)}</blockquote>,
    );
    quote = [];
  };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // Fenced code — consume until the closing fence, or to the end of the text
    // when there isn't one yet (which is every streaming reply mid-block).
    const fence = line.match(/^\s*```(\w*)/);
    if (fence) {
      flushAll();
      const language = fence[1] || '';
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { body.push(lines[i]); i += 1; }
      blocks.push(<CodeBlock key={`c${blocks.length}`} code={body.join('\n')} language={language} />);
      continue;
    }

    if (/^\s*$/.test(line)) { flushAll(); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      const Tag = `h${Math.min(level, 4)}` as 'h1' | 'h2' | 'h3' | 'h4';
      blocks.push(<Tag key={`h${blocks.length}`}>{renderInline(heading[2], `h${blocks.length}`)}</Tag>);
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { flushAll(); blocks.push(<hr key={`hr${blocks.length}`} />); continue; }

    const quoteMatch = line.match(/^\s*>\s?(.*)$/);
    if (quoteMatch) { flushParagraph(); flushList(); quote.push(quoteMatch[1]); continue; }

    const bullet = line.match(/^(\s*)[-*+]\s+(.*)$/);
    const numbered = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushParagraph(); flushQuote();
      const match = (bullet || numbered) as RegExpMatchArray;
      listItems.push({ text: match[2], depth: match[1].length, ordered: !bullet });
      continue;
    }

    // A table is header + separator + rows; without the separator the pipes are
    // just text (a shell one-liner, most often).
    if (line.includes('|') && /^\s*\|?[\s:-]*-[\s:|-]*\|/.test(lines[i + 1] || '')) {
      flushAll();
      const rows = [line];
      i += 1;
      while (i < lines.length && lines[i].includes('|')) { rows.push(lines[i]); i += 1; }
      i -= 1;
      blocks.push(renderTable(rows, `t${blocks.length}`));
      continue;
    }

    flushList(); flushQuote();
    paragraph.push(line);
  }
  flushAll();

  return <>{blocks}</>;
}
