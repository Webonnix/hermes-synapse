import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, waitFor } from '@testing-library/react';
import App from './App';

// Streaming speech end to end: WebSocket frames in, /api/voice/synthesize requests out.

const sockets: MockWebSocket[] = [];

class MockWebSocket {
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  send = vi.fn();
  close = vi.fn();

  constructor(url: string) {
    this.url = url;
    sockets.push(this);
    setTimeout(() => this.onopen?.(), 10);
  }
}

const players: FakeAudio[] = [];

/** Plays instantly; a test decides when each clip ends. */
class FakeAudio {
  src = '';
  onplay: (() => void) | null = null;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  pause = vi.fn();

  constructor() {
    players.push(this);
  }

  play() {
    setTimeout(() => this.onplay?.(), 0);
    return Promise.resolve();
  }
}

let synthesized: string[] = [];

function stubBackend() {
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/api/voice/tts/status')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ available: true }) });
    }
    if (url.includes('/api/voice/synthesize')) {
      synthesized.push(JSON.parse(String(init?.body)).text);
      return Promise.resolve({ ok: true, blob: () => Promise.resolve(new Blob(['wav'])) });
    }
    if (url.includes('/api/config')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ system_prompt: 'p', model: 'm' }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve([]) });
  }));
}

async function connect() {
  render(<App />);
  await waitFor(() => expect(sockets.at(-1)?.onmessage).toBeTruthy());
  const socket = sockets.at(-1)!;
  return (frame: Record<string, unknown>) => act(() => {
    socket.onmessage!({ data: JSON.stringify({ chat_id: 'dashboard', run_id: 'run-1', ...frame }) });
  });
}

/** Ends the clip that is playing now. */
const endClip = () => act(() => {
  players.at(-1)?.onended?.();
});

describe('streamed replies are spoken while they stream', () => {
  beforeEach(() => {
    sockets.length = 0;
    players.length = 0;
    synthesized = [];
    localStorage.clear();
    document.cookie = 'hermes_csrf=mock-csrf; path=/';
    localStorage.setItem('hermes_language', 'en');
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubGlobal('Audio', FakeAudio);
    URL.createObjectURL = vi.fn(() => 'blob:clip');
    URL.revokeObjectURL = vi.fn();
    stubBackend();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('starts speaking at the first sentence and voices only the unspoken tail at the end', async () => {
    const send = await connect();
    await send({ type: 'chat_stream_start' });
    await send({ type: 'chat_stream_chunk', content: 'Привет, Альберт. Сейч' });

    // The answer is still streaming, yet its first sentence is already being voiced.
    await waitFor(() => expect(synthesized).toEqual(['Привет, Альберт.']));

    await send({ type: 'chat_stream_chunk', content: 'ас проверю погоду. Завтра' });
    await waitFor(() => expect(synthesized).toEqual(['Привет, Альберт.', 'Сейчас проверю погоду.']));

    await send({
      type: 'chat_message',
      role: 'assistant',
      content: 'Привет, Альберт. Сейчас проверю погоду. Завтра солнечно.',
    });
    await send({ type: 'chat_stream_end' });
    await endClip();
    await endClip();
    await waitFor(() => expect(synthesized).toEqual([
      'Привет, Альберт.',
      'Сейчас проверю погоду.',
      'Завтра солнечно.',
    ]));
  });

  it('speaks a reply with no finished sentence in full when it completes', async () => {
    const send = await connect();
    await send({ type: 'chat_stream_chunk', content: 'Да' });
    await send({ type: 'chat_message', role: 'assistant', content: 'Да' });
    await waitFor(() => expect(synthesized).toEqual(['Да']));
  });

  it('does not read the answer again when the stream end arrives before it', async () => {
    const send = await connect();
    await send({ type: 'chat_stream_chunk', content: 'Готово. Всё работает' });
    await waitFor(() => expect(synthesized).toEqual(['Готово.']));
    await send({ type: 'chat_stream_end' });
    await endClip();
    await waitFor(() => expect(synthesized).toEqual(['Готово.', 'Всё работает']));
    await send({ type: 'chat_message', role: 'assistant', content: 'Готово. Всё работает' });
    await endClip();
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(synthesized).toEqual(['Готово.', 'Всё работает']);
  });

  it('stops speaking a cancelled reply and does not resume on later deltas', async () => {
    const send = await connect();
    await send({ type: 'chat_stream_chunk', content: 'Первое предложение. ' });
    await waitFor(() => expect(synthesized).toEqual(['Первое предложение.']));
    await waitFor(() => expect(players.at(-1)?.src).toBe('blob:clip'));

    await send({ type: 'chat_cancelled' });
    expect(players.at(-1)?.pause).toHaveBeenCalled();
    await send({ type: 'chat_stream_chunk', content: 'Второе предложение. ' });
    await send({ type: 'chat_message', role: 'assistant', content: 'Первое предложение. Второе предложение.' });
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(synthesized).toEqual(['Первое предложение.']);
  });

  it('honours suppress_tts', async () => {
    const send = await connect();
    await send({ type: 'chat_message', role: 'assistant', content: 'Тихо.', suppress_tts: true });
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(synthesized).toEqual([]);
  });
});
