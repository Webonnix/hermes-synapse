/**
 * The chat model picker's data layer: one catalogue combining the local llama.cpp
 * model this box serves with the external models reachable through OmniRoute,
 * plus the per-chat memory of which one the user picked.
 *
 * Backed by GET /api/chat/models (backend/chat_models.py). A picked model is
 * sent back as {id, source} on every chat frame; the backend resolves the pair
 * against the same live catalogue, so an id the server no longer knows simply
 * falls back to the configured default instead of failing the turn.
 */

export type ModelSource = 'local' | 'router';

export interface ChatModel {
  id: string;
  name: string;
  source: ModelSource;
  owned_by?: string;
  /** Local only — from the llama.cpp server's /v1/models. */
  size?: number | null;
  size_label?: string;
  parameter_size?: string;
  quantization?: string;
  family?: string;
  modified_at?: string;
  /** Local only — always true: llama-server keeps its model resident. */
  loaded?: boolean;
  /** OmniRoute only. */
  capabilities?: Record<string, unknown>;
  context_length?: number | null;
}

export interface ChatModelCatalog {
  local: ChatModel[];
  router: ChatModel[];
  local_error: string | null;
  router_error: string | null;
  /** Local tags the backend refuses to offer (backup copies, embedding-only
   * models), so "where did my model go?" has an answer. */
  local_hidden: { id: string; reason: string }[];
  default: { id: string; source: ModelSource; name: string };
}

export interface ModelSelection {
  id: string;
  source: ModelSource;
}

const SELECTION_KEY = 'vexa_chat_model_selection';
const DEFAULT_KEY = 'vexa_chat_model_default';

export const emptyCatalog: ChatModelCatalog = {
  local: [],
  router: [],
  local_error: null,
  router_error: null,
  local_hidden: [],
  default: { id: '', source: 'local', name: '' },
};

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('vexa_auth_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function fetchChatModels(refresh = false): Promise<ChatModelCatalog> {
  const res = await fetch(`/api/chat/models${refresh ? '?refresh=true' : ''}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`GET /api/chat/models → HTTP ${res.status}`);
  const data = await res.json();
  return { ...emptyCatalog, ...data };
}

/** Every model in one list, local first — what the picker's search runs over. */
export function allModels(catalog: ChatModelCatalog): ChatModel[] {
  return [...catalog.local, ...catalog.router];
}

export function findModel(catalog: ChatModelCatalog, selection: ModelSelection | null): ChatModel | null {
  if (!selection) return null;
  return (catalog[selection.source] || []).find(m => m.id === selection.id) || null;
}

/**
 * Whether a remembered selection still names something the server can route
 * to. A selection lives in localStorage indefinitely, so a model that has been
 * no longer served locally or lost its OmniRoute credentials otherwise keeps sitting
 * in the navbar looking active while every turn silently falls back to the
 * configured default. Only meaningful once a catalogue has actually loaded —
 * an empty one means "not fetched yet", never "your model is gone".
 */
export function isSelectionStale(catalog: ChatModelCatalog, selection: ModelSelection | null): boolean {
  if (!selection) return false;
  if (catalog.local.length === 0 && catalog.router.length === 0) return false;
  return !findModel(catalog, selection);
}

// ─── Persistence ────────────────────────────────────────────────────────────
// Two layers, both per-browser: the chat-scoped choice (so switching back to a
// conversation resumes the model it was being held in, the way Open WebUI
// restores a chat's model) and a user default for chats that have none yet.
// Neither is server state — the backend keeps owning the *configured* default.

function readMap(): Record<string, ModelSelection> {
  try {
    const raw = localStorage.getItem(SELECTION_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}

export function loadSelection(chatId: string): ModelSelection | null {
  const map = readMap();
  if (map[chatId]) return map[chatId];
  try {
    const raw = localStorage.getItem(DEFAULT_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export function saveSelection(chatId: string, selection: ModelSelection | null): void {
  try {
    const map = readMap();
    if (selection) map[chatId] = selection; else delete map[chatId];
    localStorage.setItem(SELECTION_KEY, JSON.stringify(map));
  } catch { /* storage unavailable — the choice just lasts this session */ }
}

export function loadDefaultSelection(): ModelSelection | null {
  try {
    const raw = localStorage.getItem(DEFAULT_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export function saveDefaultSelection(selection: ModelSelection | null): void {
  try {
    if (selection) localStorage.setItem(DEFAULT_KEY, JSON.stringify(selection));
    else localStorage.removeItem(DEFAULT_KEY);
  } catch { /* non-fatal */ }
}

/** Short human label for a model row — "8.0B · Q4_K_M" or "kimi · 200K ctx". */
export function modelMeta(model: ChatModel): string {
  if (model.source === 'local') {
    return [model.parameter_size, model.quantization, model.size_label]
      .filter(Boolean).join(' · ');
  }
  const ctx = model.context_length ? `${Math.round(model.context_length / 1000)}K ctx` : '';
  return [model.owned_by, ctx].filter(Boolean).join(' · ');
}
