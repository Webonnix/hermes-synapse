/**
 * Client side of the simple chat's per-chat features: chat settings, branches,
 * the model arena, agents and @mentions, and transcript export.
 *
 * Every call here is a thin wrapper over a backend route (backend/main.py,
 * backend/chat_settings.py, backend/chat_arena.py); the browser only ever names
 * catalogue model ids and agent ids, never endpoints.
 */
import type { ChatMessage } from '../../types';
import type { ModelSelection } from './chatModels';

function authHeaders(): Record<string, string> {

  return { 'Content-Type': 'application/json',  };
}

async function readJson<T>(res: Response): Promise<T> {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { detail?: string }).detail || `HTTP ${res.status}`);
  return data as T;
}

// ─── Chat settings ──────────────────────────────────────────────────────────

export type ThinkMode = 'on' | 'off' | null;

export interface ChatSettings {
  system_prompt: string;
  temperature: number | null;
  top_p: number | null;
  max_tokens: number | null;
  think: ThinkMode;
  ephemeral: boolean;
  /** Local model without refusals (refusal-direction projection); this chat only. */
  uncensored: boolean;
}

export const DEFAULT_CHAT_SETTINGS: ChatSettings = {
  system_prompt: '',
  temperature: null,
  top_p: null,
  max_tokens: null,
  think: null,
  ephemeral: false,
  uncensored: true, // web chats default to it (backend/chat_settings.py)
};

/** True when a chat runs with anything other than the global configuration. */
export function hasCustomSettings(settings: ChatSettings): boolean {
  return Boolean(settings.system_prompt)
    || settings.temperature !== null
    || settings.top_p !== null
    || settings.max_tokens !== null
    || settings.think !== null
    || !settings.uncensored;
}

export async function getChatSettings(sessionId: string): Promise<ChatSettings> {
  const res = await fetch(`/api/history/${encodeURIComponent(sessionId)}/settings`, { headers: authHeaders() });
  return { ...DEFAULT_CHAT_SETTINGS, ...(await readJson<Partial<ChatSettings>>(res)) };
}

export async function saveChatSettings(sessionId: string, settings: ChatSettings): Promise<ChatSettings> {
  const res = await fetch(`/api/history/${encodeURIComponent(sessionId)}/settings`, {
    method: 'PUT',
    headers: authHeaders(),
    body: JSON.stringify(settings),
  });
  return { ...DEFAULT_CHAT_SETTINGS, ...(await readJson<Partial<ChatSettings>>(res)) };
}

// ─── Branches and incognito ─────────────────────────────────────────────────

/** Copies the chat up to and including `upToMessageId` into a new session. */
export async function branchChat(sessionId: string, upToMessageId: number, title: string): Promise<string> {
  const res = await fetch(`/api/history/${encodeURIComponent(sessionId)}/fork`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ up_to_message_id: upToMessageId, title }),
  });
  const data = await readJson<{ status?: string; new_session_id?: string; message?: string }>(res);
  if (!data.new_session_id) throw new Error(data.message || 'Branch failed');
  return data.new_session_id;
}

export function newIncognitoChatId(): string {
  return `chat_incognito_${Date.now()}`;
}

export async function startIncognitoChat(sessionId: string): Promise<void> {
  await saveChatSettings(sessionId, { ...DEFAULT_CHAT_SETTINGS, ephemeral: true });
}

export async function discardChat(sessionId: string): Promise<void> {
  await fetch(`/api/history/${encodeURIComponent(sessionId)}`, { method: 'DELETE', headers: authHeaders() });
}

// ─── Agents ─────────────────────────────────────────────────────────────────

export const MAIN_AGENT_ID = 'vexa';

export interface AgentSummary {
  id: string;
  name: string;
  role?: string;
  agent_type?: string;
  status?: string;
  current_task?: string;
  progress?: number;
  is_enabled?: boolean;
}

export async function fetchAgents(): Promise<AgentSummary[]> {
  const res = await fetch('/api/subagents', { headers: authHeaders() });
  const rows = await readJson<AgentSummary[]>(res);
  return Array.isArray(rows) ? rows.filter(agent => agent && agent.id && agent.id !== MAIN_AGENT_ID) : [];
}

/** A handle usable after "@": the name without spaces, falling back to the id. */
export function mentionHandle(agent: Pick<AgentSummary, 'id' | 'name'>): string {
  const compact = (agent.name || '').replace(/\s+/g, '');
  return compact || agent.id;
}

export interface ParsedMention {
  agentId: string;
  agentName: string;
  text: string;
}

/**
 * "@Handle rest of the message" → the agent and the message without the
 * mention. Only a leading mention routes a turn; "@" further into the text is
 * ordinary prose (an e-mail address, a quoted handle). Case-insensitive.
 */
export function parseMention(text: string, agents: AgentSummary[]): ParsedMention | null {
  const match = /^@(\S+)\s+([\s\S]+)$/.exec(text.trim());
  if (!match) return null;
  const handle = match[1].toLowerCase();
  if (handle === MAIN_AGENT_ID) return { agentId: MAIN_AGENT_ID, agentName: 'Vexa', text: match[2].trim() };
  const agent = agents.find(candidate =>
    mentionHandle(candidate).toLowerCase() === handle || candidate.id.toLowerCase() === handle);
  if (!agent || agent.is_enabled === false) return null;
  return { agentId: agent.id, agentName: agent.name, text: match[2].trim() };
}

/** The agents an "@prefix" still being typed could complete to. */
export function mentionCandidates(text: string, agents: AgentSummary[]): AgentSummary[] | null {
  const match = /^@(\S*)$/.exec(text);
  if (!match) return null;
  const prefix = match[1].toLowerCase();
  const main: AgentSummary = { id: MAIN_AGENT_ID, name: 'Vexa', role: '' };
  return [main, ...agents.filter(agent => agent.is_enabled !== false)]
    .filter(agent => mentionHandle(agent).toLowerCase().startsWith(prefix) || agent.name.toLowerCase().startsWith(prefix))
    .slice(0, 8);
}

// ─── Arena ──────────────────────────────────────────────────────────────────

export interface ArenaResult {
  model: string;
  model_source: string;
  status: string;
  content: string;
  error?: string | null;
  latency_ms?: number | null;
  decode_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
}

export async function runArena(
  prompt: string,
  contenders: [ModelSelection | null, ModelSelection | null],
  options: { systemPrompt?: string; temperature?: number; maxTokens?: number } = {},
): Promise<ArenaResult[]> {
  const res = await fetch('/api/chat/arena', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({
      prompt,
      contenders: contenders.map(selection => ({ model: selection?.id || '', model_source: selection?.source || '' })),
      system_prompt: options.systemPrompt || '',
      ...(options.temperature !== undefined ? { temperature: options.temperature } : {}),
      ...(options.maxTokens !== undefined ? { max_tokens: options.maxTokens } : {}),
    }),
  });
  return (await readJson<{ results: ArenaResult[] }>(res)).results;
}

// ─── Export ─────────────────────────────────────────────────────────────────

export type ExportFormat = 'markdown' | 'json';

export function transcriptToMarkdown(title: string, messages: ChatMessage[], assistantName = 'Vexa'): string {
  const lines = [`# ${title}`, ''];
  messages.filter(message => message.role !== 'system').forEach(message => {
    const who = message.role === 'user'
      ? 'Вы'
      : (message.meta?.agent_name || assistantName) + (message.meta?.model ? ` · ${message.meta.model}` : '');
    lines.push(`### ${who}`, '', message.content.trim(), '');
  });
  return lines.join('\n');
}

export function transcriptToJson(title: string, messages: ChatMessage[]): string {
  return JSON.stringify({
    title,
    exported_at: new Date().toISOString(),
    messages: messages.filter(message => message.role !== 'system').map(message => ({
      role: message.role,
      content: message.content,
      ...(message.meta?.model ? { model: message.meta.model } : {}),
      ...(message.meta?.agent_name ? { agent: message.meta.agent_name } : {}),
    })),
  }, null, 2);
}

export function exportFileName(title: string, format: ExportFormat): string {
  const base = title.replace(/[\\/:*?"<>|]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 60) || 'chat';
  return `${base}.${format === 'markdown' ? 'md' : 'json'}`;
}

export function downloadTranscript(title: string, messages: ChatMessage[], format: ExportFormat, assistantName?: string): void {
  const body = format === 'markdown' ? transcriptToMarkdown(title, messages, assistantName) : transcriptToJson(title, messages);
  const blob = new Blob([body], { type: format === 'markdown' ? 'text/markdown;charset=utf-8' : 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = exportFileName(title, format);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// ─── Attachments ────────────────────────────────────────────────────────────

/**
 * The message as sent when a file is attached: what the user typed (or a
 * default ask), then the file's content under a labelled block — the same shape
 * the messenger bots hand the agent (backend/document_extract.build_document_prompt).
 */
export function composeWithAttachment(
  typed: string,
  file: { name: string; content: string; truncated?: boolean },
): string {
  const intro = typed.trim() || `Пользователь прислал файл «${file.name}». Проанализируй его содержимое и ответь по существу.`;
  const note = file.truncated ? '\n\n[Файл обрезан — показана только начальная часть.]' : '';
  return `${intro}\n\n[Содержимое файла «${file.name}»]:\n${file.content}${note}`;
}
