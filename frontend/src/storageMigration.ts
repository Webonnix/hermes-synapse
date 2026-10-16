// One-time localStorage key rename: the dashboard's persisted keys were prefixed
// `jarvis_` before the assistant was renamed to Vexa. Copy any legacy value to
// its `vexa_` key and drop the old one, so an existing user keeps their session
// token, open chat, active tab and canvas layout instead of being logged out.
//
// Imported for its side effect at the very top of main.tsx, before React and any
// component reads storage. Safe to run on every load — it no-ops once migrated.

const RENAMES: Record<string, string> = {
  jarvis_auth_token: 'vexa_auth_token',
  jarvis_active_tab: 'vexa_active_tab',
  jarvis_settings_section: 'vexa_settings_section',
  jarvis_current_chat_id: 'vexa_current_chat_id',
  jarvis_skill_positions: 'vexa_skill_positions',
};

try {
  for (const [oldKey, newKey] of Object.entries(RENAMES)) {
    const legacy = localStorage.getItem(oldKey);
    if (legacy !== null) {
      if (localStorage.getItem(newKey) === null) {
        localStorage.setItem(newKey, legacy);
      }
      localStorage.removeItem(oldKey);
    }
  }
} catch {
  // private mode / storage disabled — nothing to migrate
}

export {};
