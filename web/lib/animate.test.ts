import { describe, expect, it } from "vitest";

import { easeOutCubic, sparkPath, valueAt } from "@/lib/animate";

describe("counting up to a number", () => {
  it("starts at zero and arrives exactly", () => {
    expect(valueAt(0, 1000, 72)).toBe(0);
    expect(valueAt(1000, 1000, 72)).toBe(72);
  });

  it("never overshoots on the way", () => {
    // A counter that eases past 72 and settles back has, for one frame,
    // told the visitor something untrue.
    for (let t = 0; t <= 1000; t += 10) {
      const v = valueAt(t, 1000, 72);
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThanOrEqual(72);
    }
  });

  it("only ever goes up", () => {
    let previous = -1;
    for (let t = 0; t <= 1000; t += 17) {
      const v = valueAt(t, 1000, 72);
      expect(v).toBeGreaterThanOrEqual(previous);
      previous = v;
    }
  });

  it("shows whole numbers, because a score has no decimals", () => {
    for (let t = 0; t <= 1000; t += 37) {
      expect(Number.isInteger(valueAt(t, 1000, 72))).toBe(true);
    }
  });

  it("lands on the target past the end, and with no duration", () => {
    expect(valueAt(99_999, 1000, 72)).toBe(72);
    expect(valueAt(0, 0, 72)).toBe(72);
  });

  it("handles zero without dividing by it", () => {
    expect(valueAt(500, 1000, 0)).toBe(0);
  });
});

describe("easing", () => {
  it("is bounded and monotonic", () => {
    expect(easeOutCubic(0)).toBe(0);
    expect(easeOutCubic(1)).toBe(1);
    expect(easeOutCubic(-5)).toBe(0);
    expect(easeOutCubic(5)).toBe(1);
    expect(easeOutCubic(0.5)).toBeGreaterThan(0.5); // decelerating
  });
});

describe("the sparkline", () => {
  it("passes through every sample", () => {
    // Points on the line, not near it: a curve that rounds a corner invents
    // a peak the data does not contain.
    const d = sparkPath([0, 10, 5], 100, 50);
    expect(d.startsWith("M 0.00,")).toBe(true);
    expect(d).toContain("100.00,");
  });

  it("draws nothing from too few points", () => {
    expect(sparkPath([], 100, 50)).toBe("");
    expect(sparkPath([4], 100, 50)).toBe("");
  });

  it("does not divide by zero when everything is flat", () => {
    expect(sparkPath([0, 0, 0], 100, 50)).not.toContain("NaN");
  });
});
