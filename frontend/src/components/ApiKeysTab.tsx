import { useEffect, useState } from 'react';
import { KeyRound, Check, Trash2 } from 'lucide-react';
import { styles } from '../styles';

interface ApiKeyEntry {
  key_name: string;
  label: string;
  description: string;
  category: string;
  configured: boolean;
}

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('jarvis_auth_token');
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

function KeyRow({ entry, onSaved }: { entry: ApiKeyEntry; onSaved: () => void }) {
  const [value, setValue] = useState('');
  const [status, setStatus] = useState<'idle' | 'saving' | 'error'>('idle');
  const [error, setError] = useState('');

  const handleSave = async () => {
    if (!value.trim()) return;
    setStatus('saving');
    setError('');
    try {
      const res = await fetch('/api/settings/api-keys', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ key_name: entry.key_name, value: value.trim() }),
      });
      if (res.ok) {
        setValue('');
        setStatus('idle');
        onSaved();
      } else {
        const data = await res.json().catch(() => ({}));
        setStatus('error');
        setError(data.detail || 'Не удалось сохранить ключ.');
      }
    } catch {
      setStatus('error');
      setError('Ошибка соединения с бэкендом.');
    }
  };

  const handleRemove = async () => {
    setStatus('saving');
    setError('');
    try {
      const res = await fetch(`/api/settings/api-keys/${encodeURIComponent(entry.key_name)}`, {
        method: 'DELETE',
        headers: authHeaders(),
      });
      if (res.ok) {
        setStatus('idle');
        onSaved();
      } else {
        setStatus('error');
        setError('Не удалось удалить ключ.');
      }
    } catch {
      setStatus('error');
      setError('Ошибка соединения с бэкендом.');
    }
  };

  return (
    <div className="glass-panel" style={{ padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px' }}>
        <div>
          <strong style={{ fontSize: '0.92rem' }}>{entry.label}</strong>
          <span style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-dim, #8a94a6)', marginTop: '2px' }}>
            {entry.description}
          </span>
        </div>
        <span className={`admin-status-chip ${entry.configured ? 'is-active' : 'is-revoked'}`}>
          <i className="dot" />{entry.configured ? 'Настроен' : 'Не настроен'}
        </span>
      </div>
      <div style={{ display: 'flex', gap: '8px' }}>
        <input
          type="password"
          className="form-input"
          style={{ flex: 1 }}
          placeholder={entry.configured ? 'Заменить значение...' : 'Вставьте ключ...'}
          value={value}
          onChange={e => setValue(e.target.value)}
          disabled={status === 'saving'}
        />
        <button type="button" className="btn-primary" onClick={handleSave} disabled={status === 'saving' || !value.trim()}>
          <Check size={14} />
        </button>
        {entry.configured && (
          <button type="button" className="icon-btn danger" onClick={handleRemove} disabled={status === 'saving'} title="Удалить ключ">
            <Trash2 size={14} />
          </button>
        )}
      </div>
      {error && <span style={{ color: '#ef4444', fontSize: '0.8rem' }}>⚠️ {error}</span>}
    </div>
  );
}

export function ApiKeysTab() {
  const [keys, setKeys] = useState<ApiKeyEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchKeys = () => {
    fetch('/api/settings/api-keys', { headers: authHeaders() })
      .then(res => res.json())
      .then(data => setKeys(data.keys || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchKeys(); }, []);

  const categories = Array.from(new Set(keys.map(k => k.category)));

  return (
    <div style={styles.tabWrapper}>
      <div style={styles.tabHeader}>
        <div>
          <h2 className="glow-text-cyan" style={styles.tabTitle}>КЛЮЧИ API</h2>
          <p style={styles.tabSubtitle}>Секреты для интеграций и инструментов агентов — хранятся в базе данных, без правки .env на сервере вручную</p>
        </div>
      </div>

      {loading ? (
        <p style={{ color: 'var(--text-dim, #8a94a6)' }}>Загрузка...</p>
      ) : (
        categories.map(category => (
          <div key={category} style={{ marginBottom: '20px' }}>
            <h3 style={{ fontSize: '0.85rem', letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--text-dim, #8a94a6)', margin: '0 0 10px' }}>
              {category}
            </h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {keys.filter(k => k.category === category).map(entry => (
                <KeyRow key={entry.key_name} entry={entry} onSaved={fetchKeys} />
              ))}
            </div>
          </div>
        ))
      )}

      <span style={styles.formHelp}>
        <KeyRound size={13} style={{ verticalAlign: '-2px', marginRight: '4px' }} />
        Значения не показываются повторно после сохранения — при необходимости замените ключ целиком. Переменная окружения на сервере (.env), если задана, всегда имеет приоритет над значением из этой панели.
      </span>
    </div>
  );
}
