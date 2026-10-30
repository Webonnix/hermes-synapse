import { useEffect, useState } from 'react';
import { Copy, Eye, EyeOff, ShieldAlert } from 'lucide-react';
import type { COPY, Lang, ModelAccess } from './desktopModel';

type Copy = typeof COPY[Lang];

/**
 * How the owner connects an OpenBot desktop: sign in from its terminal, open
 * OpenBot's own apps, and give OpenBot the desktop's model door
 * (backend/agent_runtimes/llm_proxy.py) as a custom provider. The key shown is
 * this desktop's own /agent-llm token — it opens nothing else.
 */
export function OpenBotConnect({ desktopKey, copy }: { desktopKey: string; copy: Copy }) {
  const [access, setAccess] = useState<ModelAccess | null>(null);
  const [error, setError] = useState('');
  const [reveal, setReveal] = useState(false);
  const [copied, setCopied] = useState('');

  useEffect(() => {
    let cancelled = false;
    setAccess(null);
    setReveal(false);
    fetch(`/api/agent-desktops/${encodeURIComponent(desktopKey)}/model-access`)
      .then(async response => {
        if (!response.ok) throw new Error(`${response.status}`);
        return await response.json() as ModelAccess;
      })
      .then(data => { if (!cancelled) { setAccess(data); setError(''); } })
      .catch(exc => { if (!cancelled) setError((exc as Error).message); });
    return () => { cancelled = true; };
  }, [desktopKey]);

  const copyValue = (label: string, value: string) => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopied(label);
      window.setTimeout(() => setCopied(''), 1500);
    }).catch(() => { /* clipboard blocked: the value is on screen */ });
  };

  const field = (label: string, value: string, secret = false) => (
    <div className="ad-field">
      <span className="ad-label-text">{label}</span>
      <code className="ad-value">{secret && !reveal ? '•'.repeat(24) : value}</code>
      {secret && (
        <button className="ad-icon" title={reveal ? copy.hide : copy.show} aria-label={reveal ? copy.hide : copy.show}
          onClick={() => setReveal(r => !r)}>
          {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      )}
      <button className="ad-icon" title={copied === label ? copy.copied : copy.copy}
        aria-label={`${copy.copy} ${label}`} onClick={() => copyValue(label, value)}>
        <Copy size={14} />
      </button>
    </div>
  );

  return (
    <div className="ad-connect">
      <h2 className="ad-section-title">{copy.connectTitle}</h2>
      <ol className="ad-steps">
        <li>{copy.connectLogin} <code>openbot login</code></li>
        <li>{copy.connectApp}</li>
        <li>
          {copy.connectProvider}
          {error && <div className="ad-error" role="alert">{error}</div>}
          {access && (
            <div className="ad-fields">
              {field(copy.baseUrl, access.base_url)}
              {field(copy.apiKey, access.api_key, true)}
              {field(copy.model, access.model)}
              <details className="ad-models">
                <summary>{copy.models} ({access.models.length})</summary>
                <ul>{access.models.map(model => <li key={model}><code>{model}</code></li>)}</ul>
              </details>
            </div>
          )}
        </li>
        <li>{copy.connectPhone} <code>openbot mobile-connect</code></li>
      </ol>
      <p className="ad-note"><ShieldAlert size={14} /> {copy.connectNote}</p>
    </div>
  );
}
