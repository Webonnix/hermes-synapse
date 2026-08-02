import { describe, expect, it } from 'vitest';
import { SPEECH_CHUNK_MAX, SPEECH_FIRST_CHUNK_MAX, splitForSpeech } from './speechChunks';

describe('splitForSpeech', () => {
  it('returns nothing for empty or whitespace-only text', () => {
    expect(splitForSpeech('')).toEqual([]);
    expect(splitForSpeech('   \n  ')).toEqual([]);
  });

  it('keeps a short reply as a single chunk', () => {
    expect(splitForSpeech('Да, конечно.')).toEqual(['Да, конечно.']);
  });

  it('front-loads a small first chunk so the first word plays sooner', () => {
    // The real failure mode: this whole answer used to be one 9-second synthesis job.
    const answer = 'Я понимаю ваше желание услышать меня, но у меня нет голосовых возможностей. '
      + 'Я общаюсь только текстом. Если хотите, могу переписать ответ другим тоном и стилем, '
      + 'чтобы вам было приятнее читать. Скажите, что именно нужно поменять.';
    const chunks = splitForSpeech(answer);

    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks[0].length).toBeLessThanOrEqual(SPEECH_FIRST_CHUNK_MAX);
    // The opening chunk must be a real utterance, not a two-word fragment.
    expect(chunks[0].length).toBeGreaterThan(20);
  });

  it('never exceeds the per-chunk budget', () => {
    const answer = 'Первое предложение про погоду. '.repeat(30);
    for (const [index, chunk] of splitForSpeech(answer).entries()) {
      expect(chunk.length).toBeLessThanOrEqual(index === 0 ? SPEECH_FIRST_CHUNK_MAX : SPEECH_CHUNK_MAX);
    }
  });

  it('splits a single over-long sentence at clause punctuation', () => {
    const runOn = 'Сначала я проверю логи, потом посмотрю конфигурацию сервиса, '
      + 'затем сверю переменные окружения, и только после этого перезапущу контейнер';
    const chunks = splitForSpeech(runOn);

    expect(chunks.length).toBeGreaterThan(1);
    // A clause boundary was used, so the opening piece ends on punctuation rather than
    // being chopped mid-phrase.
    expect(chunks[0]).toMatch(/[,;:]$/);
  });

  it('loses no words', () => {
    const answer = 'Первое предложение. Второе, чуть длиннее и с запятой! Третье? '
      + 'А это четвёртое предложение, которое заметно длиннее остальных и потому '
      + 'обязательно окажется разрезанным на несколько частей подряд.';
    const rejoined = splitForSpeech(answer).join(' ');
    expect(rejoined.replace(/\s+/g, ' ')).toBe(answer.replace(/\s+/g, ' ').trim());
  });

  it('does not strand a tiny fragment when a sentence barely overflows', () => {
    const chunks = splitForSpeech('Раз два три четыре. Пять шесть семь восемь девять десять.', 40, 40);
    for (const chunk of chunks) expect(chunk.length).toBeGreaterThan(5);
  });
});
