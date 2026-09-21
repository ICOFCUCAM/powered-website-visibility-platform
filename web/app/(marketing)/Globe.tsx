"use client";

import { useEffect, useRef } from "react";

import { CITY_DOTS, LAND_DOTS } from "./globe-dots";

/* Precomputed once at module scope — never recomputed at runtime. */
const DEG = Math.PI / 180;
type Geo = { lat: number; lng: number };

const LAND: Geo[] = [];
for (let i = 0; i < LAND_DOTS.length; i += 2) {
  LAND.push({ lat: LAND_DOTS[i], lng: LAND_DOTS[i + 1] });
}
const CITIES: Geo[] = CITY_DOTS.map(([lat, lng]) => ({ lat, lng }));

/** Intercontinental routes (indices into CITIES) — purposeful, not random. */
const ROUTES: [number, number][] = [
  [0, 5], [5, 11], [11, 15], [15, 18], [18, 19], [0, 3],
  [5, 9], [16, 17], [17, 18], [19, 25], [1, 19], [14, 15],
];
const PULSE_HUBS = [0, 5, 19, 16];

const AXIS = -0.34; // axial tilt, for a natural lean
const sinT = Math.sin(AXIS);
const cosT = Math.cos(AXIS);

/**
 * Where the light comes from, in the globe's own space. Everything shaded by
 * this rather than by depth alone is what gives the sphere a terminator —
 * a lit limb and a dark side — instead of reading as a flat disc of dots.
 */
const LIGHT = { x: -0.52, y: 0.42, z: 0.74 };

type Projected = { x: number; y: number; z: number; dot: number };

/** Hermite smoothstep — a soft edge where a hard `if` would band. */
function smooth(edge0: number, edge1: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

function project(
  lat: number, lng: number, rot: number,
  cx: number, cy: number, R: number, lift = 0,
): Projected {
  const la = lat * DEG;
  const lo = (lng + rot) * DEG;
  const cphi = Math.cos(la);
  const x0 = cphi * Math.sin(lo);
  const y0 = Math.sin(la);
  const z0 = cphi * Math.cos(lo);
  const y = y0 * cosT - z0 * sinT;
  const z = y0 * sinT + z0 * cosT;
  const r = R * (1 + lift);
  // Raw Lambert term against the light. Kept unclamped so the caller can
  // smooth ACROSS zero, which is what makes a terminator a gradient rather
  // than a hard line.
  const dot = x0 * LIGHT.x + y * LIGHT.y + z * LIGHT.z;
  return { x: cx + x0 * r, y: cy - y * r, z, dot };
}

type Quality = "high" | "med";

/** A latitude/longitude cage, faint, for the wireframe read. */
function graticule(
  ctx: CanvasRenderingContext2D, rot: number,
  cx: number, cy: number, R: number,
) {
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(120, 190, 175, 0.16)";
  for (let lat = -60; lat <= 60; lat += 30) {
    ctx.beginPath();
    let on = false;
    for (let lng = -180; lng <= 180; lng += 6) {
      const p = project(lat, lng, rot, cx, cy, R);
      if (p.z <= 0) { on = false; continue; }
      if (!on) { ctx.moveTo(p.x, p.y); on = true; } else ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
  }
  for (let lng = 0; lng < 180; lng += 30) {
    ctx.beginPath();
    let on = false;
    for (let lat = -90; lat <= 90; lat += 5) {
      const p = project(lat, lng, rot, cx, cy, R);
      if (p.z <= 0) { on = false; continue; }
      if (!on) { ctx.moveTo(p.x, p.y); on = true; } else ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
  }
}

function paint(
  ctx: CanvasRenderingContext2D, w: number, h: number,
  rot: number, time: number, q: Quality,
) {
  const cx = w / 2;
  const cy = h / 2;
  const R = Math.min(w, h) * 0.46;
  ctx.clearRect(0, 0, w, h);

  // Where the light lands on the sphere, in screen space — the sub-solar
  // point. Everything below is positioned relative to it.
  const lx = cx + LIGHT.x * R;
  const ly = cy - LIGHT.y * R;

  // Ocean. Darker than the land dots so continents read against it, and lit
  // from the sub-solar point rather than from an arbitrary corner.
  const body = ctx.createRadialGradient(
    lx * 0.5 + cx * 0.5, ly * 0.5 + cy * 0.5, R * 0.05, cx, cy, R,
  );
  body.addColorStop(0, "#123040");
  body.addColorStop(0.42, "#0b1d28");
  body.addColorStop(0.78, "#061015");
  body.addColorStop(1, "#03070a");
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = body;
  ctx.fill();

  // Specular glint: the sun reflecting off water. Small, offset to the
  // sub-solar point, and additive.
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.clip();
  ctx.globalCompositeOperation = "lighter";
  const glint = ctx.createRadialGradient(lx, ly, 0, lx, ly, R * 0.5);
  glint.addColorStop(0, "rgba(150, 214, 224, 0.26)");
  glint.addColorStop(0.45, "rgba(90, 170, 190, 0.09)");
  glint.addColorStop(1, "rgba(0, 0, 0, 0)");
  ctx.fillStyle = glint;
  ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
  ctx.restore();

  // Limb darkening: a sphere is dimmer at its edge because you are looking
  // through more of it at a glancing angle. Without this it reads as a disc.
  const limb = ctx.createRadialGradient(cx, cy, R * 0.55, cx, cy, R);
  limb.addColorStop(0, "rgba(0, 0, 0, 0)");
  limb.addColorStop(1, "rgba(0, 0, 0, 0.55)");
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = limb;
  ctx.fill();

  // Atmosphere. A faint shell all round, then a brighter bloom on the LIT
  // limb only — a real atmosphere scatters where the light enters it, not
  // evenly around the planet.
  const atmo = ctx.createRadialGradient(cx, cy, R * 0.9, cx, cy, R * 1.2);
  atmo.addColorStop(0, "rgba(90, 190, 200, 0)");
  atmo.addColorStop(0.45, "rgba(90, 190, 200, 0.1)");
  atmo.addColorStop(1, "rgba(90, 190, 200, 0)");
  ctx.beginPath();
  ctx.arc(cx, cy, R * 1.2, 0, Math.PI * 2);
  ctx.fillStyle = atmo;
  ctx.fill();

  ctx.save();
  ctx.globalCompositeOperation = "lighter";
  const rim = ctx.createRadialGradient(
    cx + LIGHT.x * R * 0.72, cy - LIGHT.y * R * 0.72, R * 0.1,
    cx + LIGHT.x * R * 0.72, cy - LIGHT.y * R * 0.72, R * 0.78,
  );
  rim.addColorStop(0, "rgba(140, 226, 214, 0.20)");
  rim.addColorStop(1, "rgba(140, 226, 214, 0)");
  ctx.fillStyle = rim;
  ctx.beginPath();
  ctx.arc(cx, cy, R * 1.2, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  graticule(ctx, rot, cx, cy, R);

  // Land dots: LOD by stride, limb-fade by depth, day/night by the light term.
  const stride = q === "med" || w < 440 ? 2 : 1;
  const dotR = R * 0.0125;
  for (let i = 0; i < LAND.length; i += stride) {
    const d = LAND[i];
    const p = project(d.lat, d.lng, rot, cx, cy, R);
    if (p.z <= 0.02) continue;
    // Smoothed across zero, so the day/night edge is a band, not a line.
    const day = smooth(-0.18, 0.34, p.dot);
    ctx.globalAlpha = (0.16 + 0.64 * p.z) * (0.18 + 0.82 * day);
    ctx.fillStyle =
      day > 0.72 ? "#92e2cd" : day > 0.34 ? "#5aa899" : "#2f6a64";
    ctx.beginPath();
    ctx.arc(p.x, p.y, dotR * (0.55 + 0.45 * p.z), 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  // City lights — warm, and brightest where the globe is in shadow, the way
  // city lights actually are.
  for (const c of CITIES) {
    const p = project(c.lat, c.lng, rot, cx, cy, R);
    if (p.z <= 0.04) continue;
    const night = 1 - smooth(-0.18, 0.34, p.dot);
    const a = (0.3 + 0.7 * p.z) * (0.3 + 0.7 * night);
    ctx.shadowColor = "rgba(240, 194, 116, 0.9)";
    ctx.shadowBlur = 9 * p.z;
    ctx.fillStyle = `rgba(247, 222, 172, ${a})`;
    ctx.beginPath();
    ctx.arc(p.x, p.y, dotR * 1.7, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.shadowBlur = 0;

  // Arcs with a travelling signal.
  const segs = q === "med" ? 18 : 26;
  for (let ri = 0; ri < ROUTES.length; ri++) {
    const a = CITIES[ROUTES[ri][0]];
    const b = CITIES[ROUTES[ri][1]];
    ctx.beginPath();
    let on = false;
    for (let s = 0; s <= segs; s++) {
      const f = s / segs;
      const lift = 0.18 * Math.sin(Math.PI * f);
      const p = project(
        a.lat + (b.lat - a.lat) * f, a.lng + (b.lng - a.lng) * f,
        rot, cx, cy, R, lift,
      );
      if (p.z <= 0) { on = false; continue; }
      if (!on) { ctx.moveTo(p.x, p.y); on = true; } else ctx.lineTo(p.x, p.y);
    }
    ctx.strokeStyle = "rgba(143, 216, 201, 0.24)";
    ctx.lineWidth = 1;
    ctx.stroke();

    const head = (time * 0.18 + ri * 0.37) % 1;
    const lift = 0.18 * Math.sin(Math.PI * head);
    const ph = project(
      a.lat + (b.lat - a.lat) * head, a.lng + (b.lng - a.lng) * head,
      rot, cx, cy, R, lift,
    );
    if (ph.z > 0) {
      ctx.shadowColor = "rgba(240, 214, 160, 0.9)";
      ctx.shadowBlur = 8;
      ctx.fillStyle = "rgba(250, 236, 205, 0.95)";
      ctx.beginPath();
      ctx.arc(ph.x, ph.y, dotR * 1.4, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  // Scan pulses over major hubs.
  for (let i = 0; i < PULSE_HUBS.length; i++) {
    const p = project(
      CITIES[PULSE_HUBS[i]].lat, CITIES[PULSE_HUBS[i]].lng, rot, cx, cy, R,
    );
    if (p.z <= 0.06) continue;
    const phase = (time * 0.4 + i * 0.25) % 1;
    ctx.globalAlpha = (1 - phase) * 0.5 * p.z;
    ctx.strokeStyle = "rgba(143, 216, 201, 1)";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.arc(p.x, p.y, R * 0.13 * phase, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

/**
 * The hero globe: a precomputed dot-Earth on a 2D canvas. No 3D dependency,
 * no runtime topojson parse, one rAF loop that PAUSES when scrolled offscreen
 * or the tab is hidden, DPR and level-of-detail scaled by quality, and a
 * single static frame under reduced motion. Decorative only.
 *
 * Ported from our PrivacyOS hero and re-lit: land is shaded by a light
 * direction rather than by depth alone, so the sphere carries a terminator,
 * and the city lights burn warmest on the night side.
 */
export function Globe({ quality = "high" }: { quality?: Quality }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const dprCap = quality === "med" ? 1.25 : 1.5;
    let w = 0;
    let h = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, dprCap);
      w = rect.width;
      h = rect.height;
      canvas.width = Math.max(1, Math.round(w * dpr));
      canvas.height = Math.max(1, Math.round(h * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (reduced) paint(ctx, w, h, -20, 0, quality);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    if (reduced) {
      paint(ctx, w, h, -20, 0, quality);
      return () => ro.disconnect();
    }

    let rot = -20;
    let raf = 0;
    let last = 0;
    let running = false;

    const frame = (now: number) => {
      if (!running) return;
      const dt = last ? Math.min(0.05, (now - last) / 1000) : 0;
      last = now;
      rot += dt * 3.2; // deg/sec — a calm rotation
      paint(ctx, w, h, rot, now / 1000, quality);
      raf = requestAnimationFrame(frame);
    };
    const start = () => {
      if (running) return;
      running = true;
      last = 0;
      raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      running = false;
      cancelAnimationFrame(raf);
    };

    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => (e.isIntersecting ? start() : stop())),
      { threshold: 0.05 },
    );
    io.observe(canvas);
    const onVis = () => (document.hidden ? stop() : start());
    document.addEventListener("visibilitychange", onVis);

    return () => {
      stop();
      io.disconnect();
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [quality]);

  return <canvas ref={canvasRef} aria-hidden="true" className="mk-globe-canvas" />;
}
