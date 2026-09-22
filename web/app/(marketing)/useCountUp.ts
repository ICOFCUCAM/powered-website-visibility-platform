"use client";

import { useEffect, useState } from "react";

import { valueAt } from "@/lib/animate";

/**
 * Counts from zero to `target` once, on mount.
 *
 * Under reduced motion it does not count: it is the number, immediately.
 * The value is the point; the movement is decoration, and decoration is what
 * that setting asks us to drop.
 */
export function useCountUp(target: number, duration = 1400, delay = 0): number {
  const [value, setValue] = useState(0);

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      setValue(target);
      return;
    }

    let raf = 0;
    let started = 0;
    const step = (now: number) => {
      if (!started) started = now;
      const elapsed = now - started - delay;
      setValue(valueAt(elapsed, duration, target));
      if (elapsed < duration) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, duration, delay]);

  return value;
}
