import { describe, expect, it } from 'vitest';
import {
  SPEECH_CHUNK_MAX,
  SPEECH_FIRST_CHUNK_MAX,
  StreamingSpeechSplitter,
  cleanForSpeech,
  splitForSpeech,
  takeSpeechBatch,
} from './speechChunks';

/** Streams `text` into a splitter in fixed-size deltas, like token frames would. */
function streamIn(splitter: StreamingSpeechSplitter, text: string, size: number): string[][] {
  const batches: string[][] = [];
  for (let at = 0; at < text.length; at += size) batches.push(splitter.push(text.slice(at, at + size)));
  return batches;
}

const words = (text: string) => cleanForSpeech(text).split(/\s+/).filter(Boolean);

describe('StreamingSpeechSplitter', () => {
  it('releases a sentence only once its end has arrived, even across chunks', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Привет, Аль')).toEqual([]);
    expect(splitter.push('берт')).toEqual([]);
    // The full stop alone is not enough: "3." could still become "3.14".
    expect(splitter.push('.')).toEqual([]);
    expect(splitter.push(' Сейчас')).toEqual(['Привет, Альберт.']);
    expect(splitter.push(' проверю погоду!\n')).toEqual(['Сейчас проверю погоду!']);
    expect(splitter.finish()).toEqual([]);
  });

  it('does not cut inside numbers, domains or abbreviations with no space after the dot', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Курс 3.14 на сайте example.com')).toEqual([]);
    expect(splitter.finish()).toEqual(['Курс 3.14 на сайте example.com']);
  });

  it('never hands out a piece twice, and loses no words, whatever the chunk size', () => {
    const answer = 'Да, конечно. Я посмотрела прогноз: завтра в Москве будет солнечно, '
      + 'около двадцати градусов, ветер слабый. Вечером возможен небольшой дождь, '
      + 'так что зонт лучше взять с собой! Если хотите, я напомню об этом утром? '
      + 'Ещё могу прислать прогноз на всю неделю, с разбивкой по дням и часам, '
      + 'чтобы было удобно планировать поездки и встречи заранее';
    for (const size of [1, 3, 7, 16, 50, 1000]) {
      const splitter = new StreamingSpeechSplitter();
      const pieces = [...streamIn(splitter, answer, size).flat(), ...splitter.finish(answer)];
      expect(pieces.join(' ').split(/\s+/)).toEqual(words(answer));
      expect(new Set(pieces).size).toBe(pieces.length);
      expect(splitter.finish(answer)).toEqual([]);
    }
  });

  it('keeps the first piece short and the rest within budget', () => {
    const answer = 'Первое предложение про погоду и планы на выходные. '.repeat(12);
    const splitter = new StreamingSpeechSplitter();
    const pieces = [...streamIn(splitter, answer, 5).flat(), ...splitter.finish(answer)];
    expect(pieces[0].length).toBeLessThanOrEqual(SPEECH_FIRST_CHUNK_MAX);
    for (const piece of pieces) expect(piece.length).toBeLessThanOrEqual(SPEECH_CHUNK_MAX);
  });

  it('starts speaking a long opening sentence at a clause break, before its full stop', () => {
    const splitter = new StreamingSpeechSplitter();
    const opening = 'Сначала я проверю логи сервера, потом посмотрю конфигурацию сервиса, '
      + 'затем сверю переменные окружения';
    const early = streamIn(splitter, opening, 4).flat();
    expect(early.length).toBeGreaterThan(0);
    expect(early[0].length).toBeLessThanOrEqual(SPEECH_FIRST_CHUNK_MAX);
    expect(early[0].endsWith(',')).toBe(true);
    const rest = [...splitter.push(' и перезапущу контейнер.'), ...splitter.finish()];
    expect([...early, ...rest].join(' ').split(/\s+/)).toEqual(words(`${opening} и перезапущу контейнер.`));
  });

  it('flushes the unterminated tail on finish', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Готово. Файл сохранён в папке')).toEqual(['Готово.']);
    expect(splitter.finish()).toEqual(['Файл сохранён в папке']);
  });

  it('speaks only the unspoken tail of the final answer', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Готово. Файл сохра')).toEqual(['Готово.']);
    // Deltas lost on a reconnect: the final frame carries the whole answer.
    expect(splitter.finish('Готово. Файл сохранён.')).toEqual(['Файл сохранён.']);
  });

  it('speaks the whole final answer when only pre-tool chatter was spoken', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Сейчас проверю погоду. ')).toEqual(['Сейчас проверю погоду.']);
    // Tool runs; the final answer is the next model call's text only.
    splitter.push('Завтра солнечно, плюс двадцать.');
    expect(splitter.finish('Завтра солнечно, плюс двадцать.')).toEqual(['Завтра солнечно, плюс двадцать.']);
  });

  it('lines up the final answer when spoken text precedes it in the stream', () => {
    const splitter = new StreamingSpeechSplitter();
    splitter.push('Сейчас проверю. ');
    expect(splitter.push('Завтра солнечно. Ветер сла')).toEqual(['Завтра солнечно.']);
    expect(splitter.finish('Завтра солнечно. Ветер слабый.')).toEqual(['Ветер слабый.']);
  });

  it('lines up a final answer the server cleaned of inline reasoning', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('<think>План. Ответить.</think>Готово. Отчёт отпра')).toEqual(['Готово.']);
    expect(splitter.finish('Готово. Отчёт отправлен.')).toEqual(['Отчёт отправлен.']);
  });

  it('falls back to the stream tail when the final answer cannot be lined up', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Первая часть. Вторая')).toEqual(['Первая часть.']);
    expect(splitter.finish('Совсем другой текст.')).toEqual(['Вторая']);
  });

  it('holds a code block until it closes and reads it as one phrase', () => {
    const splitter = new StreamingSpeechSplitter();
    expect(splitter.push('Вот пример.\n```py\nprint("a. b")\n')).toEqual(['Вот пример.']);
    expect(splitter.push('x = 1. y = 2\n')).toEqual([]);
    expect(splitter.push('```\nГотово.\n')).toEqual(['code block. Готово.']);
  });

  it('skips tables and inline reasoning, and keeps list numbers with their items', () => {
    const splitter = new StreamingSpeechSplitter();
    const pieces = [
      ...streamIn(splitter, '<think>Надо. Подумать.</think>Итоги:\n| a. b | c |\n|---|---|\n1. Купить хлеб.\n2. Позвонить маме.\n', 3).flat(),
      ...splitter.finish(),
    ];
    expect(pieces.join(' ')).toBe('Итоги: 1. Купить хлеб. 2. Позвонить маме.');
    expect(pieces).not.toContain('1.');
    expect(pieces).not.toContain('2.');
  });

  it('does not split a markdown link or inline code', () => {
    const splitter = new StreamingSpeechSplitter();
    // The cut before the link is fine; the full stop inside it is not.
    expect(splitter.push('См. [док. ')).toEqual(['См.']);
    expect(splitter.push('тут](http://x.y) и `a. b` ок. Дальше')).toEqual(['и ок.']);
  });

  it('ignores deltas after finish', () => {
    const splitter = new StreamingSpeechSplitter();
    splitter.push('Раз. ');
    splitter.finish();
    expect(splitter.push('Два. Три. ')).toEqual([]);
    expect(splitter.started).toBe(true);
  });
});

describe('takeSpeechBatch', () => {
  it('merges queued pieces up to the budget and always takes at least one', () => {
    const queue = ['Раз.', 'Два.', 'Три.'];
    expect(takeSpeechBatch(queue, 9)).toBe('Раз. Два.');
    expect(queue).toEqual(['Три.']);
    const long = ['x'.repeat(30), 'y'];
    expect(takeSpeechBatch(long, 10)).toBe('x'.repeat(30));
    expect(long).toEqual(['y']);
  });

  it('leaves pre-sized chunks of a whole reply as they are', () => {
    const answer = 'Я понимаю ваше желание услышать меня, но у меня нет голосовых возможностей. '
      + 'Я общаюсь только текстом. Если хотите, могу переписать ответ другим тоном и стилем, '
      + 'чтобы вам было приятнее читать. Скажите, что именно нужно поменять.';
    const chunks = splitForSpeech(answer);
    const queue = [...chunks];
    const taken = [takeSpeechBatch(queue, SPEECH_FIRST_CHUNK_MAX)];
    while (queue.length) taken.push(takeSpeechBatch(queue, SPEECH_CHUNK_MAX));
    expect(taken).toEqual(chunks);
  });
});

describe('cleanForSpeech', () => {
  it('strips markdown, bullets and stray punctuation from line breaks', () => {
    expect(cleanForSpeech('## Итог\n- **Да**!\n\n- `код` нет')).toBe('Итог. Да! нет');
    expect(cleanForSpeech('\nПродолжение')).toBe('Продолжение');
  });
});
