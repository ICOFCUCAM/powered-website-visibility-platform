/**
 * The arithmetic behind things that move.
 *
 * Kept out of the components so it can be tested without a browser, and so
 * the one rule that matters is enforced in a single place: an animated
 * number must ARRIVE at its target exactly, and must never show a value the
 * data does not support on the way there. A counter that eases past 72 to 74
 * and settles back has, for one frame, told the visitor something untrue.
 */

/** Decelerating ease. Fast at the start, settled at the end, no overshoot. */
export function easeOutCubic(t: number): number {
  const clamped = t < 0 ? 0 : t > 1 ? 1 : t;
  return 1 - Math.pow(1 - clamped, 3);
}

/**
 * The value to show `elapsed` ms into a count-up towards `target`.
 *
 * Rounded, because a score is a whole number and a counter flickering
 * through 71.6 is showing a precision the score does not have.
 */
export function valueAt(elapsed: number, duration: number, target: number): number {
  if (duration <= 0 || elapsed >= duration) return target;
  if (elapsed <= 0) return 0;
  return Math.round(target * easeOutCubic(elapsed / duration));
}

/**
 * A smooth path through evenly spaced values, as an SVG `d`.
 *
 * Catmull-Rom style midpoint curves: every point is ON the line, so the
 * drawing cannot imply a peak between two samples that the samples do not
 * contain.
 */
export function sparkPath(
  values: number[], width: number, height: number, pad = 3,
): string {
  if (values.length < 2) return "";
  const max = Math.max(...values, 1);
  const xs = values.map((_, i) => (i / (values.length - 1)) * width);
  const ys = values.map(
    (v) => height - pad - (v / max) * (height - pad * 2),
  );

  let d = `M ${xs[0].toFixed(2)},${ys[0].toFixed(2)}`;
  for (let i = 1; i < values.length; i++) {
    const mid = ((xs[i - 1] + xs[i]) / 2).toFixed(2);
    d += ` C ${mid},${ys[i - 1].toFixed(2)} ${mid},${ys[i].toFixed(2)}`;
    d += ` ${xs[i].toFixed(2)},${ys[i].toFixed(2)}`;
  }
  return d;
}
