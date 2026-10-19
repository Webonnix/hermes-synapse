import { useEffect, useRef, useState } from 'react';

/**
 * Reveals a streamed reply at an even pace instead of in the bursts the network delivers.
 *
 * Providers send deltas in uneven lumps (a word, then nothing for 400ms, then a whole
 * sentence), which reads as stutter. Each frame this shows a slice of the backlog sized so
 * a small backlog types out character by character while a large one still catches up in
 * well under a second — the reply never lags far behind what has actually arrived.
 *
 * Text that was already complete when the component mounted (history) is shown at once.
 */

/** Frames the backlog is spread over; ~20 frames ≈ a third of a second at 60fps. */
const CATCH_UP_FRAMES = 20;

export function useSmoothText(target: string, streaming: boolean): string {
  // Only replies that were seen streaming are animated; history renders instantly.
  const [animated, setAnimated] = useState(streaming);
  if (streaming && !animated) setAnimated(true);

  const [shown, setShown] = useState(0);
  const shownRef = useRef(0);

  useEffect(() => {
    if (!animated) return undefined;
    let frame = 0;
    const tick = () => {
      const goal = target.length;
      // A replacement shorter than what is on screen — continue from its end.
      const from = Math.min(shownRef.current, goal);
      const backlog = goal - from;
      shownRef.current = backlog <= 0 ? goal : from + Math.max(1, Math.ceil(backlog / CATCH_UP_FRAMES));
      setShown(shownRef.current);
      if (shownRef.current < goal) frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [animated, target]);

  return animated ? target.slice(0, shown) : target;
}
