/**
 * «Телефония»: the wiring from the settings screen and the call dialog to the phone API.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PhonePanel } from './PhonePanel';
import { CallDialog } from './CallDialog';

const providers = {
  zadarma: {
    label: 'Zadarma', recommended: true, transport: 'sip', needs_public_url: false, number_purchase: false,
    cabinet_url: 'https://my.zadarma.com/', note: 'SIP через Asterisk',
    fields: [
      { key: 'sip_login', label: 'SIP-логин', secret: false, required: true },
      { key: 'sip_password', label: 'Пароль SIP', secret: true, required: true },
    ],
  },
  twilio: {
    label: 'Twilio', recommended: false, transport: 'media_stream', needs_public_url: true, number_purchase: true,
    cabinet_url: 'https://console.twilio.com/', note: 'Номера с покупкой отсюда',
    fields: [
      { key: 'account_sid', label: 'Account SID', secret: false, required: true },
      { key: 'auth_token', label: 'Auth Token', secret: true, required: true },
    ],
  },
};

const twilioLine = {
  id: 'ln1', name: 'Продажи', provider: 'twilio', number: '+15550000001', subagent_id: 'agent-1', greeting: 'Здравствуйте',
  language: 'auto', owner_number: '+15550009999', enabled: true, inbound_enabled: true, outbound_enabled: true,
  auto_book: false, max_call_minutes: 20, config: { account_sid: 'AC1' }, status: 'ok', last_error: '',
  secrets_set: { auth_token: true }, provider_label: 'Twilio', created_at: '', updated_at: '',
};

const speech = {
  config: { stt_provider: 'deepgram', stt_model: 'nova-3', stt_language: 'multi', tts_provider: 'elevenlabs',
    tts_model: 'eleven_flash_v2_5', tts_voice: 'v1', tts_voice_en: '', yandex_folder_id: '', fallback_local: true },
  stt: [
    { id: 'deepgram', label: 'Deepgram Nova-3', recommended: true, needs_key: true, models: ['nova-3'], default_model: 'nova-3',
      languages: ['multi', 'ru', 'en'], default_language: 'multi', note: 'низкая задержка', has_secret: false },
    { id: 'local', label: 'Локально', recommended: false, needs_key: false, models: ['small'], default_model: 'small',
      languages: ['auto'], default_language: 'auto', note: 'резерв', has_secret: true },
  ],
  tts: [
    { id: 'elevenlabs', label: 'ElevenLabs Flash v2.5', recommended: true, needs_key: true, models: ['eleven_flash_v2_5'],
      default_model: 'eleven_flash_v2_5', default_voice: 'v1', note: 'быстро', has_secret: true },
  ],
};

function overview(extra: Record<string, unknown> = {}) {
  return {
    providers, lines: [twilioLine], speech, can_manage: true, can_call: true, can_control_calls: true,
    status: { asterisk: { enabled: true, connected: true, last_error: '', version: '22.5', active_calls: 0 },
      active_calls: [], settings: { public_base_url: 'https://hermes.example.com', max_concurrent_calls: 4 } },
    ...extra,
  };
}

function stubFetch() {
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method || 'GET';
    const ok = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data });
    if (path === '/api/phone/overview') return ok(overview());
    if (path.startsWith('/api/phone/calls?')) return ok([]);
    if (path === '/api/inbox/route-targets') return ok([{ type: 'subagent', id: 'agent-1', name: 'Секретарь', enabled: true, available: true }]);
    if (path === '/api/phone/lines' && method === 'POST') return ok({ ...twilioLine, id: 'ln2' });
    if (path.startsWith('/api/phone/lines/ln1/numbers/search')) {
      return ok([{ phone_number: '+15551230000', friendly_name: '', locality: 'Austin', country: 'US', kind: 'Local', monthly_cost: '1.15', currency: 'USD' }]);
    }
    if (path === '/api/phone/lines/ln1/numbers/owned') return ok([]);
    if (path === '/api/phone/lines/ln1/numbers/buy') return ok({ sid: 'PN1' });
    if (path.startsWith('/api/phone/speech/keys/')) return ok({ has_secret: true });
    if (path === '/api/phone/calls' && method === 'POST') {
      return ok({ id: 'call1', remote_number: '+15554443333', conversation_id: 'ibx-9', status: 'ringing' });
    }
    return ok({});
  });
  vi.stubGlobal('fetch', handler);
  return handler;
}

const calls = (handler: ReturnType<typeof stubFetch>, path: string, method = 'POST') =>
  handler.mock.calls.filter(([url, init]) => String(url) === path && ((init as RequestInit)?.method || 'GET') === method);

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('PhonePanel', () => {
  it('shows lines, the recommended provider and speech services', async () => {
    stubFetch();
    render(<PhonePanel />);
    expect(await screen.findByText('Продажи')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Deepgram Nova-3/ })).toHaveTextContent('рекомендуется');
    expect(screen.getByRole('radio', { name: /Локально/ })).not.toHaveTextContent('рекомендуется');
    expect(screen.getByText('на связи · 22.5')).toBeInTheDocument();
  });

  it('creates a line with the recommended provider preselected and without empty secrets', async () => {
    const handler = stubFetch();
    render(<PhonePanel />);
    fireEvent.click(await screen.findByRole('button', { name: /Добавить линию/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Новая линия' });
    expect(within(dialog).getByRole('radio', { name: /Zadarma/ })).toHaveAttribute('aria-checked', 'true');
    fireEvent.change(within(dialog).getByLabelText('Название'), { target: { value: 'Офис' } });
    fireEvent.change(within(dialog).getByLabelText('SIP-логин'), { target: { value: '123456' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(calls(handler, '/api/phone/lines')).toHaveLength(1));
    const body = JSON.parse(String((calls(handler, '/api/phone/lines')[0][1] as RequestInit).body));
    expect(body.provider).toBe('zadarma');
    expect(body.sip_login).toBe('123456');
    expect('sip_password' in body).toBe(false);
  });

  it('buys a number only after the person confirms the price', async () => {
    const handler = stubFetch();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<PhonePanel />);
    fireEvent.click(await screen.findByRole('button', { name: /Номера/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Номера' });
    fireEvent.click(within(dialog).getByRole('button', { name: /Найти/ }));
    const buy = await within(dialog).findByRole('button', { name: 'Купить' });
    fireEvent.click(buy);
    expect(calls(handler, '/api/phone/lines/ln1/numbers/buy')).toHaveLength(0);
    fireEvent.click(buy);
    await waitFor(() => expect(calls(handler, '/api/phone/lines/ln1/numbers/buy')).toHaveLength(1));
    expect(confirm.mock.calls[0][0]).toContain('1.15 USD');
    expect(JSON.parse(String((calls(handler, '/api/phone/lines/ln1/numbers/buy')[0][1] as RequestInit).body)).confirm).toBe(true);
  });

  it('saves a speech key and clears the field', async () => {
    const handler = stubFetch();
    render(<PhonePanel />);
    const input = await screen.findByLabelText('Ключ Deepgram Nova-3');
    fireEvent.change(input, { target: { value: 'dg-secret' } });
    fireEvent.click(input.parentElement!.querySelector('button')!);
    await waitFor(() => expect(calls(handler, '/api/phone/speech/keys/deepgram', 'PUT')).toHaveLength(1));
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(''));
  });
});

describe('CallDialog', () => {
  it('starts an outbound call and offers the conversation', async () => {
    const handler = stubFetch();
    const onClose = vi.fn();
    render(<CallDialog defaultNumber="+15554443333" clientId="cli-1" onClose={onClose} />);
    fireEvent.change(await screen.findByLabelText('Цель звонка'), { target: { value: 'Подтвердить заказ' } });
    await waitFor(() => expect((screen.getByLabelText('Линия') as HTMLSelectElement).value).toBe('ln1'));
    fireEvent.click(screen.getByRole('button', { name: /Позвонить/ }));
    await waitFor(() => expect(calls(handler, '/api/phone/calls')).toHaveLength(1));
    const body = JSON.parse(String((calls(handler, '/api/phone/calls')[0][1] as RequestInit).body));
    expect(body).toEqual({ line_id: 'ln1', to: '+15554443333', purpose: 'Подтвердить заказ', client_id: 'cli-1' });
    expect(await screen.findByRole('button', { name: 'Открыть разговор' })).toBeInTheDocument();
  });
});
