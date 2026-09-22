"use client";

import { sparkPath } from "@/lib/animate";

/**
 * The activity line on the example screen.
 *
 * Decorative and inside the "Example — not real data" frame, like every
 * other figure on that card. It exists because a dashboard with nothing
 * moving on it reads as a screenshot, and the thing being sold is a product
 * that is doing something while you are not looking.
 */
const SAMPLE = [6, 11, 8, 16, 12, 22, 17, 28, 21, 31, 26, 38];

// The viewBox matches the rendered box closely. A 220-wide box stretched
// into 430px squashes the curve to a flat line — the y axis compresses
// while the x axis doubles, and the chart stops being a chart.
const W = 420;
const H = 40;

export function Spark() {
  const d = sparkPath(SAMPLE, W, H);
  const last = SAMPLE[SAMPLE.length - 1];
  const headY = H - 3 - (last / Math.max(...SAMPLE)) * (H - 6);
  return (
    <svg className="mk-spark" viewBox={`0 0 ${W} ${H}`}
         preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id="mk-spark-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#43b6a2" stopOpacity="0.28" />
          <stop offset="100%" stopColor="#43b6a2" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d} L ${W},${H} L 0,${H} Z`} fill="url(#mk-spark-fill)" />
      <path className="mk-spark-line" d={d} fill="none" stroke="#8fd8c9"
            strokeWidth="1.5" strokeLinecap="round" />
      {/* A signal travelling the line, the way a live feed reads. */}
      <path className="mk-spark-pulse" d={d} fill="none" stroke="#eaf7f3"
            strokeWidth="1.5" strokeLinecap="round" strokeDasharray="2 18" />
      {/* "Now", at the leading edge. */}
      <circle className="mk-spark-head" cx={W - 1} cy={headY} r="2.4" />
    </svg>
  );
}
