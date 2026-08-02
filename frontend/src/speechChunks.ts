/**
 * Splits a reply into pieces that can be synthesised and played one after another.
 *
 * XTTS synthesis time is essentially linear in the length of the text (measured on the
 * deploy host: 12 chars → 0.9s, 69 → 2.4s, 257 → 9.1s), so synthesising a whole answer
 * before playing any of it makes the user wait for the *longest* possible job. Splitting
 * lets the first words play after a short job while the rest is still being generated.
 *
 * That overlap is safe because synthesis outruns speech: those same 257 characters took
 * 9.1s to generate but produce ~18s of audio, so once playback starts the queue stays fed.
 *
 * The first chunk is deliberately much smaller than the rest — it alone decides how long
 * the silence before Vexa starts talking lasts, while later chunks are hidden behind
 * playback and can be longer, which costs fewer round-trips and gives the model more
 * context for natural prosody.
 */

/** Keeps the opening silence short; the smaller this is, the sooner the first word plays. */
export const SPEECH_FIRST_CHUNK_MAX = 90;
/** Later chunks are masked by playback of earlier ones, so favour prosody over latency. */
export const SPEECH_CHUNK_MAX = 220;

/**
 * Index to cut `text` at, at or before `max`, preferring a natural pause. Clause
 * punctuation wins over a bare word gap; the 0.35 floor stops a separator near the very
 * start from producing a two-word fragment.
 */
function findCut(text: string, max: number): number {
  const limit = Math.max(1, Math.min(max, text.length));
  const window = text.slice(0, limit);
  for (const separator of [', ', '; ', ': ', ' — ', ' – ']) {
    const at = window.lastIndexOf(separator);
    // +1 keeps the punctuation itself with the left-hand piece.
    if (at > limit * 0.35) return at + 1;
  }
  const space = window.lastIndexOf(' ');
  if (space > limit * 0.35) return space;
  return limit;
}

export function splitForSpeech(
  text: string,
  firstMax: number = SPEECH_FIRST_CHUNK_MAX,
  restMax: number = SPEECH_CHUNK_MAX,
): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [];

  // Sentence terminators are the preferred seam; the trailing `|$` keeps a final
  // unterminated fragment instead of dropping it.
  const sentences = (trimmed.match(/[^.!?…]+(?:[.!?…]+|$)/g) ?? [])
    .map(sentence => sentence.trim())
    .filter(Boolean);

  const chunks: string[] = [];
  let current = '';
  const limit = () => (chunks.length === 0 ? firstMax : restMax);
  const flush = () => {
    if (current.trim()) chunks.push(current.trim());
    current = '';
  };

  for (const sentence of sentences) {
    let rest = sentence;
    while (rest) {
      const max = limit();
      const room = current ? max - current.length - 1 : max;
      if (rest.length <= room) {
        current = current ? `${current} ${rest}` : rest;
        rest = '';
      } else if (current && room < max * 0.5) {
        // Too little room left to be worth wedging a fragment into — close this chunk
        // and let the sentence start the next one whole.
        flush();
      } else {
        const cut = findCut(rest, room > 0 ? room : max);
        const head = rest.slice(0, cut).trim();
        current = current ? `${current} ${head}` : head;
        flush();
        rest = rest.slice(cut).trim();
      }
    }
  }
  flush();
  return chunks;
}
