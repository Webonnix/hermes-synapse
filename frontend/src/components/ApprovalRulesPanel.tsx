import { useCallback, useEffect, useState } from 'react';
import { Sparkles, Trash2 } from 'lucide-react';

type Rule = { id: number; tool_name: string; feature_key: string; feature_value: string; created_at: string };
type Suggestion = { tool_name: string; feature_key: string; feature_value: string; approved: number };
type Props = { language: 'ru' | 'en' };

const COPY = {
  ru: {
    title: 'Правила подтверждений',
    hint: 'Действия R2, которые вы одобряли много раз подряд и ни разу не отклоняли. Правило действует только в режиме auto и только для этого точного значения; R3, R4 и деньги всегда ждут вас.',
    suggested: 'Предложено', active: 'Действуют', none: 'Пока нечего предложить.', noRules: 'Правил нет.',
    approvals: 'одобрений', accept: 'Не спрашивать', remove: 'Удалить правило', failed: 'Не удалось сохранить правило',
  },
  en: {
    title: 'Approval rules',
    hint: 'R2 actions you approved many times in a row and never rejected. A rule applies only in auto mode and only to this exact value; R3, R4 and money always wait for you.',
    suggested: 'Suggested', active: 'Active', none: 'Nothing to suggest yet.', noRules: 'No rules.',
    approvals: 'approvals', accept: 'Stop asking', remove: 'Delete rule', failed: 'Could not save the rule',
  },
} as const;

export function ApprovalRulesPanel({ language }: Props) {
  const copy = COPY[language];
  const [rules, setRules] = useState<Rule[]>([]);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/control-plane/approval-rules');
      if (!response.ok) return;
      const data = await response.json() as { rules: Rule[]; suggestions: Suggestion[] };
      setRules(data.rules);
      setSuggestions(data.suggestions);
    } catch {
      /* the approval queue above stays usable without this panel */
    }
  }, []);

  useEffect(() => {
    void load();
    const interval = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(interval);
  }, [load]);

  const accept = async (suggestion: Suggestion) => {
    setError('');
    const response = await fetch('/api/control-plane/approval-rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool_name: suggestion.tool_name, feature_key: suggestion.feature_key, feature_value: suggestion.feature_value }),
    });
    if (!response.ok) setError(copy.failed);
    await load();
  };

  const remove = async (rule: Rule) => {
    await fetch(`/api/control-plane/approval-rules/${rule.id}`, { method: 'DELETE' });
    await load();
  };

  return <section className="contract-block approval-rules" aria-labelledby="approval-rules-title">
    <h4 id="approval-rules-title"><Sparkles size={14} /> {copy.title}</h4>
    <p>{copy.hint}</p>
    {error && <p role="alert">{error}</p>}
    <h5>{copy.suggested}</h5>
    {!suggestions.length && <p className="control-empty">{copy.none}</p>}
    <ul>{suggestions.map(item => <li key={`${item.tool_name}:${item.feature_key}:${item.feature_value}`}>
      <code>{item.tool_name}</code> · {item.feature_key} = <strong>{item.feature_value}</strong> · {item.approved} {copy.approvals}
      <button type="button" onClick={() => void accept(item)}>{copy.accept}</button>
    </li>)}</ul>
    <h5>{copy.active}</h5>
    {!rules.length && <p className="control-empty">{copy.noRules}</p>}
    <ul>{rules.map(rule => <li key={rule.id}>
      <code>{rule.tool_name}</code> · {rule.feature_key} = <strong>{rule.feature_value}</strong>
      <button type="button" aria-label={copy.remove} title={copy.remove} onClick={() => void remove(rule)}><Trash2 size={13} /></button>
    </li>)}</ul>
  </section>;
}
