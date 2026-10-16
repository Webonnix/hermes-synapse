import { describe, it, expect } from 'vitest';
import { normalizeLanguage, translate, dictionaries } from './i18n';

describe('normalizeLanguage', () => {
  it('passes through the languages the app actually has', () => {
    expect(normalizeLanguage('ru')).toBe('ru');
    expect(normalizeLanguage('en')).toBe('en');
  });

  it('repairs a JSON-encoded value left in localStorage by an older build', () => {
    // This is the value that took the dashboard down on 2026-09-09: quotes and
    // all, written straight from an /api/settings response.
    expect(normalizeLanguage('"ru"')).toBe('ru');
    expect(normalizeLanguage("'en'")).toBe('en');
    expect(normalizeLanguage(' EN ')).toBe('en');
  });

  it('falls back rather than handing translate a language it has no dictionary for', () => {
    expect(normalizeLanguage('de')).toBe('ru');
    expect(normalizeLanguage('')).toBe('ru');
    expect(normalizeLanguage(null)).toBe('ru');
    expect(normalizeLanguage(undefined)).toBe('ru');
    expect(normalizeLanguage({ language: 'ru' })).toBe('ru');
    expect(normalizeLanguage(42)).toBe('ru');
  });

  it('never returns something missing from the dictionaries', () => {
    for (const value of ['ru', 'en', '"ru"', 'de', '', null, 7, {}]) {
      expect(dictionaries[normalizeLanguage(value)]).toBeDefined();
    }
  });
});

describe('translate', () => {
  it('translates a known key in both languages', () => {
    expect(translate('ru', 'appSubtitle')).toBe('Работает на базе Hermes');
    expect(translate('en', 'appSubtitle')).toBe('Powered by Hermes');
  });

  it('returns the key itself when nothing has it', () => {
    expect(translate('ru', 'no_such_key_anywhere')).toBe('no_such_key_anywhere');
  });

  it('does not throw on a language with no dictionary', () => {
    // The crash that blanked the app was exactly this: dictionaries[bad][key].
    // translate is called for every label, so it must degrade, never throw.
    const bad = 'de' as unknown as Parameters<typeof translate>[0];
    expect(() => translate(bad, 'appSubtitle')).not.toThrow();
    expect(translate(bad, 'appSubtitle')).toBe('Работает на базе Hermes');
  });
});
