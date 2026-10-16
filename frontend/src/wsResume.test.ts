import { describe, it, expect } from 'vitest';
import {
  RunCursorTracker,
  reconnectDelayMs,
  RECONNECT_MAX_MS,
  MAX_RESUME_RUNS,
  CURSOR_TTL_MS,
} from './wsResume';

const chunk = (runId: string, seq: number, chatId = 'dashboard') => ({
  type: 'chat_stream_chunk', run_id: runId, chat_id: chatId, seq, content: 'x',
});

describe('RunCursorTracker', () => {
  it('applies each frame once and drops a replayed duplicate', () => {
    const tracker = new RunCursorTracker();
    expect(tracker.accept(chunk('r1', 1))).toBe(true);
    expect(tracker.accept(chunk('r1', 2))).toBe(true);
    // The same frames arriving again via replay must not be applied a second
    // time: stream chunks are deltas, so a double-apply duplicates the text.
    expect(tracker.accept(chunk('r1', 1))).toBe(false);
    expect(tracker.accept(chunk('r1', 2))).toBe(false);
    expect(tracker.accept(chunk('r1', 3))).toBe(true);
    expect(tracker.lastSeq('r1')).toBe(3);
  });

  it('keeps cursors independent per run', () => {
    const tracker = new RunCursorTracker();
    tracker.accept(chunk('r1', 5));
    expect(tracker.accept(chunk('r2', 1))).toBe(true);
    expect(tracker.lastSeq('r1')).toBe(5);
    expect(tracker.lastSeq('r2')).toBe(1);
  });

  it('always applies frames that carry no run/seq', () => {
    const tracker = new RunCursorTracker();
    expect(tracker.accept({ type: 'logs_update' })).toBe(true);
    expect(tracker.accept({ type: 'chat_message', chat_id: 'dashboard' })).toBe(true);
    // A server that does not stamp seq (older backend) must not be filtered out.
    expect(tracker.accept({ type: 'chat_stream_chunk', run_id: 'r1' })).toBe(true);
    expect(tracker.accept({ type: 'chat_stream_chunk', run_id: 'r1' })).toBe(true);
  });

  it('asks to resume only unfinished runs, from the last seq seen', () => {
    const tracker = new RunCursorTracker();
    tracker.accept(chunk('r1', 4));
    tracker.accept(chunk('r2', 7));
    tracker.accept({ type: 'chat_stream_end', run_id: 'r2', chat_id: 'dashboard', seq: 8 });
    expect(tracker.resumeRequest()).toEqual([{ run_id: 'r1', after_seq: 4 }]);
    expect(tracker.isActive('r2')).toBe(false);
  });

  it('treats a run whose end was never seen as resumable', () => {
    const tracker = new RunCursorTracker();
    tracker.accept({ type: 'chat_stream_start', run_id: 'r1', chat_id: 'c1', seq: 1 });
    tracker.accept(chunk('r1', 2, 'c1'));
    // Socket dies here; the answer completes server-side. From the client's
    // point of view the run is still open, which is what makes the missing
    // terminal frame recoverable.
    expect(tracker.resumeRequest()).toEqual([{ run_id: 'r1', after_seq: 2 }]);
    expect(tracker.activeChatIds()).toEqual(['c1']);
  });

  it('resets a cursor so a gap replay is applied in full', () => {
    const tracker = new RunCursorTracker();
    tracker.accept(chunk('r1', 40));
    tracker.reset('r1');
    expect(tracker.lastSeq('r1')).toBe(0);
    expect(tracker.accept(chunk('r1', 12))).toBe(true);
  });

  it('caps the resume request and prefers the most recent runs', () => {
    let now = 1000;
    const tracker = new RunCursorTracker(() => now);
    for (let i = 0; i < MAX_RESUME_RUNS + 5; i++) {
      now += 10;
      tracker.accept(chunk(`r${i}`, 1));
    }
    const request = tracker.resumeRequest();
    expect(request).toHaveLength(MAX_RESUME_RUNS);
    expect(request[0].run_id).toBe(`r${MAX_RESUME_RUNS + 4}`);
  });

  it('forgets cursors past the TTL', () => {
    let now = 1000;
    const tracker = new RunCursorTracker(() => now);
    tracker.accept(chunk('old', 1));
    now += CURSOR_TTL_MS + 1;
    expect(tracker.resumeRequest()).toEqual([]);
    // A frame for a forgotten run is applied again rather than dropped.
    expect(tracker.accept(chunk('old', 1))).toBe(true);
  });
});

describe('reconnectDelayMs', () => {
  it('grows with each attempt and stays under the cap', () => {
    const delays = [0, 1, 2, 3, 4, 10].map(attempt => reconnectDelayMs(attempt, () => 0.5));
    expect(delays[0]).toBeLessThan(delays[1]);
    expect(delays[1]).toBeLessThan(delays[2]);
    delays.forEach(delay => expect(delay).toBeLessThanOrEqual(RECONNECT_MAX_MS));
    expect(delays[5]).toBe(RECONNECT_MAX_MS);
  });

  it('never returns a zero or negative delay', () => {
    for (let attempt = 0; attempt < 12; attempt++) {
      expect(reconnectDelayMs(attempt, () => 0)).toBeGreaterThan(0);
      expect(reconnectDelayMs(attempt, () => 0.999)).toBeGreaterThan(0);
    }
    expect(reconnectDelayMs(-5, () => 0.5)).toBeGreaterThan(0);
  });
});
