import { describe, expect, it } from "vitest";

import { normaliseLongitude, subsolarPoint } from "@/lib/sun";

/**
 * Astronomy is exactly the kind of code that looks right and is wrong by a
 * sign, so it is checked against facts anyone can verify: the solstices, the
 * equinoxes, and where noon is.
 */
describe("the subsolar point", () => {
  it("reaches the Tropic of Cancer at the June solstice", () => {
    // On the solstice the sun is overhead at 23.44°N, by definition.
    const { latitude } = subsolarPoint(new Date("2026-06-21T12:00:00Z"));
    expect(latitude).toBeGreaterThan(23.3);
    expect(latitude).toBeLessThan(23.5);
  });

  it("reaches the Tropic of Capricorn at the December solstice", () => {
    const { latitude } = subsolarPoint(new Date("2026-12-21T12:00:00Z"));
    expect(latitude).toBeLessThan(-23.3);
    expect(latitude).toBeGreaterThan(-23.5);
  });

  it("crosses the equator at the equinoxes", () => {
    expect(
      Math.abs(subsolarPoint(new Date("2026-03-20T14:46:00Z")).latitude),
    ).toBeLessThan(0.3);
    expect(
      Math.abs(subsolarPoint(new Date("2026-09-23T00:05:00Z")).latitude),
    ).toBeLessThan(0.3);
  });

  it("puts noon on the Greenwich meridian at 12:00 UTC", () => {
    // Not exactly zero: the equation of time moves solar noon by up to about
    // 16 minutes, which is 4° of longitude.
    for (const day of ["2026-01-15", "2026-04-15", "2026-07-15", "2026-10-15"]) {
      const { longitude } = subsolarPoint(new Date(`${day}T12:00:00Z`));
      expect(Math.abs(longitude)).toBeLessThan(5);
    }
  });

  it("sweeps west at fifteen degrees an hour", () => {
    const noon = subsolarPoint(new Date("2026-05-01T12:00:00Z")).longitude;
    const later = subsolarPoint(new Date("2026-05-01T18:00:00Z")).longitude;
    // Six hours later the sun is 90° further west.
    expect(normaliseLongitude(noon - later)).toBeCloseTo(90, 0);
  });

  it("is over the Pacific at midnight UTC, not over Europe", () => {
    // The sign error this catches would light the wrong half of the planet.
    const { longitude } = subsolarPoint(new Date("2026-05-01T00:00:00Z"));
    expect(Math.abs(longitude)).toBeGreaterThan(170);
  });

  it("never claims the sun is outside the tropics", () => {
    for (let month = 0; month < 12; month++) {
      const d = new Date(Date.UTC(2026, month, 15, 6, 0, 0));
      expect(Math.abs(subsolarPoint(d).latitude)).toBeLessThanOrEqual(23.45);
    }
  });
});

describe("longitude wrapping", () => {
  it("folds into a single turn", () => {
    expect(normaliseLongitude(0)).toBe(0);
    expect(normaliseLongitude(190)).toBe(-170);
    expect(normaliseLongitude(-190)).toBe(170);
    expect(normaliseLongitude(540)).toBe(180);
  });
});
