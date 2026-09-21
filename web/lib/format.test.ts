import { describe, expect, it } from "vitest";
import { counted, delta, greeting, percent, plural, position, when } from "@/lib/format";

describe("formatting", () => {
  it("never implies precision Search Console does not have", () => {
    // Position is an impressions-weighted average; three decimals is invented.
    expect(position(14.6731)).toBe("14.7");
    expect(position(null)).toBe("—");
  });

  it("distinguishes no change from no data", () => {
    // "—" and "no change" are different facts and must not look the same.
    expect(delta(null)).toEqual({ label: "—", direction: "flat" });
    expect(delta(0)).toEqual({ label: "no change", direction: "flat" });
    expect(delta(0.04)).toEqual({ label: "no change", direction: "flat" });
  });

  it("points the arrow the way the reader expects", () => {
    expect(delta(8.2)).toEqual({ label: "▲ 8.2%", direction: "up" });
    expect(delta(-12)).toEqual({ label: "▼ 12%", direction: "down" });
  });

  it("shows a missing percentage as unknown rather than zero", () => {
    expect(percent(null)).toBe("—");
    expect(percent(0.0529)).toBe("5.3%");
  });

  it("reads dates the way a person would say them", () => {
    const now = Date.now();
    expect(when(null)).toBe("never");
    expect(when(new Date(now - 86_400_000).toISOString())).toBe("yesterday");
    expect(when(new Date(now - 5 * 86_400_000).toISOString())).toBe("5 days ago");
  });

  it("never says '1 problems'", () => {
    expect(counted(1, "critical technical problem")).toBe(
      "1 critical technical problem",
    );
    expect(counted(3, "critical technical problem")).toBe(
      "3 critical technical problems",
    );
    expect(counted(0, "page")).toBe("0 pages");
    // Irregular plurals have to be given, not guessed.
    expect(plural(1, "search", "searches")).toBe("search");
    expect(plural(2, "search", "searches")).toBe("searches");
  });

  it("greets by the hour", () => {
    expect(greeting(new Date(2026, 0, 1, 9))).toBe("Good morning");
    expect(greeting(new Date(2026, 0, 1, 14))).toBe("Good afternoon");
    expect(greeting(new Date(2026, 0, 1, 21))).toBe("Good evening");
  });
});
