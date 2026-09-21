/**
 * The orbital figure behind the product preview. Decorative only — hidden
 * from assistive technology, and it holds still for anyone who has asked for
 * reduced motion (the animation lives behind that media query in the CSS).
 */
export function Orbit() {
  const rings = [
    { rx: 306, ry: 306, rot: 0, o: 0.42, w: 1 },
    { rx: 306, ry: 112, rot: -24, o: 0.66, w: 1.1 },
    { rx: 306, ry: 188, rot: 14, o: 0.5, w: 1 },
    { rx: 306, ry: 252, rot: -8, o: 0.3, w: 1 },
    { rx: 228, ry: 228, rot: 0, o: 0.28, w: 1 },
    { rx: 150, ry: 150, rot: 0, o: 0.2, w: 1 },
  ];

  // Deterministic scatter: a seeded walk rather than Math.random(), so the
  // server and the client render the same stars and hydration stays quiet.
  const stars = Array.from({ length: 90 }, (_, i) => {
    const a = (i * 137.508 * Math.PI) / 180;
    const r = 86 + ((i * 53) % 215);
    return {
      cx: 320 + Math.cos(a) * r,
      cy: 320 + Math.sin(a) * r * 0.82,
      r: 0.8 + ((i * 7) % 5) * 0.4,
      o: 0.34 + ((i * 11) % 7) * 0.09,
    };
  });

  return (
    <div className="mk-orbit" aria-hidden="true">
      <svg viewBox="0 0 640 640" fill="none" preserveAspectRatio="xMidYMid meet">
        <defs>
          <radialGradient id="mk-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#f0c274" stopOpacity="0.44" />
            <stop offset="30%" stopColor="#d9a441" stopOpacity="0.22" />
            <stop offset="62%" stopColor="#43b6a2" stopOpacity="0.12" />
            <stop offset="100%" stopColor="#07090b" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="mk-bloom" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#d9a441" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#d9a441" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="mk-ring" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#d9a441" />
            <stop offset="50%" stopColor="#8fd8c9" />
            <stop offset="100%" stopColor="#43b6a2" />
          </linearGradient>
        </defs>

        <circle cx="320" cy="320" r="318" fill="url(#mk-core)" />
        {/* Two off-centre blooms, the way a light source behind a sphere
            actually reads — one warm at the lower left, one cool upper right. */}
        <circle cx="168" cy="404" r="132" fill="url(#mk-bloom)" />
        <circle cx="470" cy="214" r="104" fill="url(#mk-bloom)" opacity="0.5" />

        <g className="mk-spin">
          {rings.map((r, i) => (
            <ellipse
              key={i}
              cx="320"
              cy="320"
              rx={r.rx}
              ry={r.ry}
              stroke="url(#mk-ring)"
              strokeOpacity={r.o}
              strokeWidth={r.w}
              transform={`rotate(${r.rot} 320 320)`}
            />
          ))}
        </g>

        {stars.map((s, i) => (
          <circle key={i} cx={s.cx} cy={s.cy} r={s.r} fill="#cfe6df" fillOpacity={s.o} />
        ))}

        <circle cx="320" cy="320" r="3.5" fill="#f3d9a5" fillOpacity="0.95" />
      </svg>
    </div>
  );
}
