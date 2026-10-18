import { describe, expect, it } from 'vitest';
import {
  DEFAULT_CHAT_SETTINGS, composeWithAttachment, exportFileName, hasCustomSettings, mentionCandidates,
  mentionHandle, parseMention, transcriptToJson, transcriptToMarkdown,
} from './chatApi';
import type { AgentSummary } from './chatApi';

const agents: AgentSummary[] = [
  { id: 'agent_coder', name: 'Code Reviewer', role: 'review' },
  { id: 'agent_smm', name: 'SMM', role: 'posts' },
  { id: 'agent_off', name: 'Retired', is_enabled: false },
];

describe('mentions', () => {
  it('uses the name without spaces as the handle', () => {
    expect(mentionHandle(agents[0])).toBe('CodeReviewer');
    expect(mentionHandle({ id: 'x', name: '' })).toBe('x');
  });

  it('routes a leading mention and strips it from the text', () => {
    expect(parseMention('@codereviewer посмотри PR', agents)).toEqual({
      agentId: 'agent_coder', agentName: 'Code Reviewer', text: 'посмотри PR',
    });
    expect(parseMention('@agent_smm пост на завтра', agents)?.agentId).toBe('agent_smm');
    expect(parseMention('@vexa привет', agents)?.agentId).toBe('vexa');
  });

  it('leaves ordinary text, unknown and disabled agents alone', () => {
    expect(parseMention('напиши на me@example.com', agents)).toBeNull();
    expect(parseMention('@ghost привет', agents)).toBeNull();
    expect(parseMention('@Retired привет', agents)).toBeNull();
    // A bare mention with nothing to send is not a turn yet.
    expect(parseMention('@SMM', agents)).toBeNull();
  });

  it('completes only while the handle is still being typed', () => {
    expect(mentionCandidates('@co', agents)?.map(a => a.id)).toEqual(['agent_coder']);
    expect(mentionCandidates('@', agents)?.map(a => a.id)).toEqual(['vexa', 'agent_coder', 'agent_smm']);
    expect(mentionCandidates('@CodeReviewer ', agents)).toBeNull();
    expect(mentionCandidates('привет', agents)).toBeNull();
  });
});

describe('settings', () => {
  it('counts only real overrides as custom', () => {
    expect(hasCustomSettings(DEFAULT_CHAT_SETTINGS)).toBe(false);
    expect(hasCustomSettings({ ...DEFAULT_CHAT_SETTINGS, ephemeral: true })).toBe(false);
    expect(hasCustomSettings({ ...DEFAULT_CHAT_SETTINGS, temperature: 0 })).toBe(true);
    expect(hasCustomSettings({ ...DEFAULT_CHAT_SETTINGS, think: 'off' })).toBe(true);
  });
});

describe('attachments', () => {
  it('sends the file under a labelled block after the typed text', () => {
    expect(composeWithAttachment('Что тут?', { name: 'a.txt', content: 'hello' }))
      .toBe('Что тут?\n\n[Содержимое файла «a.txt»]:\nhello');
  });

  it('asks about the file when nothing was typed, and marks truncation', () => {
    const text = composeWithAttachment('  ', { name: 'big.pdf', content: 'x', truncated: true });
    expect(text.startsWith('Пользователь прислал файл «big.pdf».')).toBe(true);
    expect(text.endsWith('[Файл обрезан — показана только начальная часть.]')).toBe(true);
  });
});

describe('export', () => {
  const messages = [
    { role: 'system' as const, content: 'hidden' },
    { role: 'user' as const, content: 'Привет' },
    { role: 'assistant' as const, content: 'Здравствуйте', meta: { model: 'qwen', agent_name: 'SMM' } },
  ];

  it('writes a readable Markdown transcript without system messages', () => {
    const md = transcriptToMarkdown('Мой чат', messages);
    expect(md).toContain('# Мой чат');
    expect(md).toContain('### Вы\n\nПривет');
    expect(md).toContain('### SMM · qwen\n\nЗдравствуйте');
    expect(md).not.toContain('hidden');
  });

  it('writes JSON with the answering agent and model', () => {
    const data = JSON.parse(transcriptToJson('t', messages));
    expect(data.messages).toEqual([
      { role: 'user', content: 'Привет' },
      { role: 'assistant', content: 'Здравствуйте', model: 'qwen', agent: 'SMM' },
    ]);
  });

  it('makes a safe file name', () => {
    expect(exportFileName('a/b: c?', 'markdown')).toBe('a b c.md');
    expect(exportFileName('', 'json')).toBe('chat.json');
  });
});
