/**
 * Client-side bookkeeping for resumable chat streams.
 *
 * The server stamps every run-scoped frame with a monotonic per-run `seq` and
 * keeps a bounded replay buffer. This module is the browser half of that
 * contract:
 *
 *  - it remembers the highest `seq` seen per run, so a replayed frame that the
 *    socket already delivered is dropped instead of being applied twice (the
 *    stream chunks are deltas — applying one twice duplicates text);
 *  - it knows which runs are still unfinished, so a reconnect can ask for
 *    exactly those;
 *  - it computes the reconnect backoff.
 *
 * Kept free of React so it can be unit-tested directly.
 */

export interface RunCursor {
  runId: string;
  chatId: string;
  lastSeq: number;
  /** False once a terminal frame (stream end / cancelled) has been observed. */
  active: boolean;
  updatedAt: number;
}

export interface ResumeRequestItem {
  run_id: string;
  after_seq: number;
}

/** Frames that end a run. After one of these there is nothing left to resume. */
export const TERMINAL_FRAME_TYPES = ['chat_stream_end', 'chat_cancelled'];

/** Never ask the server to replay more runs than it is willing to serve. */
export const MAX_RESUME_RUNS = 16;

/** Cursors older than this are dropped; a run that stale is refetched from history. */
export const CURSOR_TTL_MS = 15 * 60 * 1000;

export class RunCursorTracker {
  private cursors = new Map<string, RunCursor>();
  private now: () => number;

  constructor(now: () => number = () => Date.now()) {
    this.now = now;
  }

  /**
   * Record a frame and say whether it should be applied.
   *
   * Returns false only for a frame the caller has provably already applied.
   * Frames without a run/seq (global events, legacy servers) are always applied,
   * so an older backend degrades to the previous behaviour rather than going
   * silent.
   */
  accept(frame: any): boolean {
    if (!frame || typeof frame !== 'object') return true;
    const runId = typeof frame.run_id === 'string' ? frame.run_id : '';
    const seq = typeof frame.seq === 'number' ? frame.seq : null;
    if (!runId || seq === null || !Number.isFinite(seq)) return true;

    const existing = this.cursors.get(runId);
    if (existing && seq <= existing.lastSeq) return false;

    const terminal = TERMINAL_FRAME_TYPES.includes(frame.type);
    this.cursors.set(runId, {
      runId,
      chatId: String(frame.chat_id ?? existing?.chatId ?? ''),
      lastSeq: seq,
      active: existing ? existing.active && !terminal : !terminal,
      updatedAt: this.now(),
    });
    this.prune();
    return true;
  }

  /** Cursor position for a run, or 0 when nothing has been seen. */
  lastSeq(runId: string): number {
    return this.cursors.get(runId)?.lastSeq ?? 0;
  }

  isActive(runId: string): boolean {
    return this.cursors.get(runId)?.active ?? false;
  }

  /**
   * Reset a run's cursor to the start. Used when the server reports a `gap`:
   * the replay it is about to send begins further back than where we were, so
   * every frame in it must be applied.
   */
  reset(runId: string): void {
    const existing = this.cursors.get(runId);
    if (existing) this.cursors.set(runId, { ...existing, lastSeq: 0, updatedAt: this.now() });
  }

  forget(runId: string): void {
    this.cursors.delete(runId);
  }

  /**
   * What to ask for after a reconnect: every run the client still believes is
   * running, newest first. A run that finished while the socket was down looks
   * active from here — which is exactly right, because its terminal frame is
   * the thing we are missing.
   */
  resumeRequest(): ResumeRequestItem[] {
    this.prune();
    return Array.from(this.cursors.values())
      .filter(cursor => cursor.active)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_RESUME_RUNS)
      .map(cursor => ({ run_id: cursor.runId, after_seq: cursor.lastSeq }));
  }

  /** Chats that must be re-subscribed after a reconnect. */
  activeChatIds(): string[] {
    this.prune();
    const ids = new Set<string>();
    this.cursors.forEach(cursor => {
      if (cursor.active && cursor.chatId) ids.add(cursor.chatId);
    });
    return Array.from(ids);
  }

  private prune(): void {
    const cutoff = this.now() - CURSOR_TTL_MS;
    this.cursors.forEach((cursor, runId) => {
      if (cursor.updatedAt < cutoff) this.cursors.delete(runId);
    });
  }
}

export const RECONNECT_BASE_MS = 1000;
export const RECONNECT_MAX_MS = 15000;

/**
 * Exponential backoff with jitter. The previous fixed 3s retry hammered a
 * restarting backend at a constant rate from every open tab; the jitter keeps
 * several tabs from retrying in lockstep.
 */
export function reconnectDelayMs(attempt: number, random: () => number = Math.random): number {
  const safeAttempt = Math.max(0, Math.floor(attempt));
  const exponential = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * Math.pow(2, safeAttempt));
  const jitter = exponential * 0.25 * random();
  return Math.round(Math.min(RECONNECT_MAX_MS, exponential - exponential * 0.125 + jitter));
}
