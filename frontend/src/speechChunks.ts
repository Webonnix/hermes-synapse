/**
 * Splits a reply into pieces that can be synthesised and played one after another.
 *
 * XTTS synthesis time is essentially linear in the length of the text (measured on the
 * deploy host: 12 chars → 0.9s, 69 → 2.4s, 257 → 9.1s), so synthesising a whole answer
 * before playing any of it makes the user wait for the *longest* possible job. Splitting
 * lets the first words play after a short job while the rest is still being generated.
 *
 * That overlap is safe because synthesis outruns speech: those same 257 characters took
 * 9.1s to generate but produce ~18s of audio, so once playback starts the queue stays fed.
 *
 * The first chunk is deliberately much smaller than the rest — it alone decides how long
 * the silence before Vexa starts talking lasts, while later chunks are hidden behind
 * playback and can be longer, which costs fewer round-trips and gives the model more
 * context for natural prosody.
 */

/** Keeps the opening silence short; the smaller this is, the sooner the first word plays. */
export const SPEECH_FIRST_CHUNK_MAX = 90;
/** Later chunks are masked by playback of earlier ones, so favour prosody over latency. */
export const SPEECH_CHUNK_MAX = 220;

/**
 * Index to cut `text` at, at or before `max`, preferring a natural pause. Clause
 * punctuation wins over a bare word gap; the 0.35 floor stops a separator near the very
 * start from producing a two-word fragment.
 */
export function findCut(text: string, max: number): number {
  const limit = Math.max(1, Math.min(max, text.length));
  const window = text.slice(0, limit);
  for (const separator of [', ', '; ', ': ', ' — ', ' – ']) {
    const at = window.lastIndexOf(separator);
    // +1 keeps the punctuation itself with the left-hand piece.
    if (at > limit * 0.35) return at + 1;
  }
  const space = window.lastIndexOf(' ');
  if (space > limit * 0.35) return space;
  return limit;
}

export function splitForSpeech(
  text: string,
  firstMax: number = SPEECH_FIRST_CHUNK_MAX,
  restMax: number = SPEECH_CHUNK_MAX,
): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [];

  // Sentence terminators are the preferred seam — only when followed by a space, so
  // "3.14" or "example.com" stay whole; the trailing `|$` keeps a final unterminated
  // fragment instead of dropping it.
  const sentences = (trimmed.match(/[\s\S]+?(?:[.!?…]+(?=\s|$)|$)/g) ?? [])
    .map(sentence => sentence.trim())
    .filter(Boolean);

  const chunks: string[] = [];
  let current = '';
  const limit = () => (chunks.length === 0 ? firstMax : restMax);
  const flush = () => {
    if (current.trim()) chunks.push(current.trim());
    current = '';
  };

  for (const sentence of sentences) {
    let rest = sentence;
    while (rest) {
      const max = limit();
      const room = current ? max - current.length - 1 : max;
      if (rest.length <= room) {
        current = current ? `${current} ${rest}` : rest;
        rest = '';
      } else if (current && room < max * 0.5) {
        // Too little room left to be worth wedging a fragment into — close this chunk
        // and let the sentence start the next one whole.
        flush();
      } else {
        const cut = findCut(rest, room > 0 ? room : max);
        const head = rest.slice(0, cut).trim();
        current = current ? `${current} ${head}` : head;
        flush();
        rest = rest.slice(cut).trim();
      }
    }
  }
  flush();
  return chunks;
}

/**
 * Turns a markdown reply into plain text a voice can read. Tables, rules and code are
 * dropped (a code block becomes the words "code block"), as are reasoning blocks a local
 * model may leave inline in its content.
 */
export function cleanForSpeech(rawText: string): string {
  const withoutReasoning = rawText.replace(/<think\b[^>]*>[\s\S]*?<\/think>/gi, ' ');
  // Tables and dividers go line by line, before the line breaks are flattened.
  const filtered = withoutReasoning
    .split('\n')
    .filter(line => {
      const trimmed = line.trim();
      if ((trimmed.match(/\|/g) || []).length >= 2) return false;
      if (/^[-=_*]{3,}$/.test(trimmed)) return false;
      return true;
    })
    .join('\n');

  return filtered
    .replace(/```[\s\S]*?```/g, 'code block.')
    .replace(/`[^`]+`/g, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/__(.+?)__/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/_(.+?)_/g, '$1')
    .replace(/#{1,6}\s/g, '')
    .replace(/!?\[.*?\]\(.*?\)/g, '')
    .replace(/[\r\n]+/g, '. ')
    .replace(/[^a-zA-Zа-яА-ЯёЁ0-9\+\-\=\.,\?!:;\s]/g, ' ')
    .replace(/\.{2,}/g, '.')
    // A line break after "!" or "?" (or a blank line) became a stray extra full stop.
    .replace(/([!?,;:.])(\s*\.)+/g, '$1')
    .replace(/\s+/g, ' ')
    // A piece of a streamed reply can begin right after a line break.
    .replace(/^[\s.,;:!?]+/, '')
    .trim();
}

/**
 * Takes the next piece of speech off `queue`, merging the queued pieces that follow it
 * while the result still fits in `max`. A streamed reply arrives one sentence at a time;
 * when the speaker falls behind, several short sentences synthesise better as one job.
 */
export function takeSpeechBatch(queue: string[], max: number): string {
  let batch = queue.shift() ?? '';
  while (queue.length && batch.length + 1 + queue[0].length <= max) {
    batch = `${batch} ${queue.shift()}`;
  }
  return batch;
}

const LETTER = /[a-zA-Zа-яА-ЯёЁ]/;
// A sentence ends at a line break, or at terminal punctuation (plus any closing quote or
// bracket) once the next character shows it is not "3.14" or "example.com".
const SENTENCE_END = /\n|[.!?…]+["'»”)\]]*(?=\s)/g;

/**
 * Whether `text` can be cut at `at` without splitting a markdown construct that
 * cleanForSpeech only understands whole: a code fence, inline code, a reasoning block,
 * a table row or a link.
 */
function isSafeCut(text: string, at: number): boolean {
  const before = text.slice(0, at);
  if ((before.match(/```/g)?.length ?? 0) % 2) return false;
  const thinkOpen = before.match(/<think\b[^>]*>/gi)?.length ?? 0;
  const thinkClose = before.match(/<\/think>/gi)?.length ?? 0;
  if (thinkOpen > thinkClose) return false;
  if ((before.replace(/```/g, '').match(/`/g)?.length ?? 0) % 2) return false;
  // Mid-line in what may be a table row: wait for the whole line.
  if (before.slice(before.lastIndexOf('\n') + 1).includes('|')) return false;
  if (before.lastIndexOf('[') > before.lastIndexOf(']')) return false;
  const linkTarget = before.lastIndexOf('](');
  if (linkTarget >= 0 && before.indexOf(')', linkTarget) < 0) return false;
  return true;
}

/**
 * Feeds a reply that is still streaming to the speech queue a sentence at a time, so
 * Vexa starts talking while the model is still writing.
 *
 * `push` takes each text delta and returns the pieces that became speakable; `finish`
 * returns whatever is left once the reply is complete. Every character is handed out at
 * most once. Sentences are only released whole (or, when one runs past the chunk
 * budget, at a clause break), so a piece never ends mid-word and never splits markdown
 * that the cleaner would otherwise read out as symbols.
 */
export class StreamingSpeechSplitter {
  private raw = '';
  /** Offset into `raw` of the first character not yet handed out. */
  private consumed = 0;
  private emitted = 0;
  private done = false;
  private firstMax: number;
  private restMax: number;

  constructor(firstMax: number = SPEECH_FIRST_CHUNK_MAX, restMax: number = SPEECH_CHUNK_MAX) {
    this.firstMax = firstMax;
    this.restMax = restMax;
  }

  /** True once any piece of this reply has been handed out for speaking. */
  get started(): boolean {
    return this.emitted > 0;
  }

  push(delta: string): string[] {
    if (this.done || !delta) return [];
    this.raw += delta;
    const pieces: string[] = [];

    const sentenceEnd = this.lastSentenceEnd();
    if (sentenceEnd > this.consumed) pieces.push(...this.take(sentenceEnd));

    // A sentence longer than the budget is released at a clause break instead of
    // waiting for its full stop — above all for the very first piece, which alone
    // decides how long the silence before Vexa speaks lasts.
    for (;;) {
      const tail = this.raw.slice(this.consumed);
      const limit = this.limit();
      if (tail.length <= limit) break;
      const cut = findCut(tail, limit);
      if (!/\s/.test(tail[cut] ?? '') || !LETTER.test(tail.slice(0, cut))) break;
      if (!isSafeCut(this.raw, this.consumed + cut)) break;
      pieces.push(...this.take(this.consumed + cut));
    }
    return pieces;
  }

  /**
   * The unspoken rest of the reply. `finalText` is the complete answer as the server
   * sent it at the end; it can differ from the streamed deltas (text written before a
   * tool call is not part of the final answer, and a reconnect can drop deltas), so the
   * already-spoken part is located in it rather than assumed to be its prefix.
   */
  finish(finalText?: string): string[] {
    if (this.done) return [];
    this.done = true;
    const rest = finalText === undefined ? this.raw.slice(this.consumed) : this.unspokenPart(finalText);
    this.consumed = this.raw.length;
    return this.emit(rest);
  }

  private unspokenPart(finalText: string): string {
    if (this.consumed === 0) return finalText;
    const spoken = this.raw.slice(0, this.consumed);
    if (finalText.startsWith(spoken)) return finalText.slice(spoken.length);

    const body = finalText.trimStart();
    const probe = body.slice(0, 16);
    // The final answer is the last model call's text, so look for it from the end. A
    // match counts only if the stream and the answer agree wherever both have text.
    for (
      let at = probe ? this.raw.lastIndexOf(probe) : -1;
      at >= 0;
      at = at > 0 ? this.raw.lastIndexOf(probe, at - 1) : -1
    ) {
      const overlap = Math.min(this.raw.length - at, body.length);
      if (this.raw.slice(at, at + overlap) !== body.slice(0, overlap)) continue;
      // Everything spoken came before the answer itself (e.g. "let me check" before a
      // tool call), so none of the answer has been heard yet.
      if (at >= this.consumed) return body;
      return body.slice(Math.min(this.consumed - at, body.length));
    }
    // Cannot line the two up: finish what the stream itself was saying rather than
    // risk reading the answer out a second time.
    return this.raw.slice(this.consumed);
  }

  private lastSentenceEnd(): number {
    const ends: number[] = [];
    SENTENCE_END.lastIndex = this.consumed;
    for (let match = SENTENCE_END.exec(this.raw); match; match = SENTENCE_END.exec(this.raw)) {
      ends.push(match.index + match[0].length);
    }
    for (let index = ends.length - 1; index >= 0; index -= 1) {
      const sentence = this.raw.slice(index > 0 ? ends[index - 1] : this.consumed, ends[index]).trim();
      // "1." of a numbered list is not a sentence: keep it with the item it numbers.
      if (sentence && !LETTER.test(sentence)) continue;
      if (isSafeCut(this.raw, ends[index])) return ends[index];
    }
    return this.consumed;
  }

  private limit(): number {
    return this.emitted === 0 ? this.firstMax : this.restMax;
  }

  private take(end: number): string[] {
    const segment = this.raw.slice(this.consumed, end);
    this.consumed = end;
    return this.emit(segment);
  }

  private emit(segment: string): string[] {
    const pieces = splitForSpeech(cleanForSpeech(segment), this.limit(), this.restMax);
    this.emitted += pieces.length;
    return pieces;
  }
}
