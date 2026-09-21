import { describe, expect, it } from "vitest";

import { viewerLongitude, viewerPlace } from "@/lib/viewer";

describe("where the visitor is, approximately", () => {
  it("reads longitude east-positive from a UTC offset", () => {
    // getTimezoneOffset is minutes to ADD to reach UTC: positive west.
    expect(viewerLongitude({ getTimezoneOffset: () => -60 } as Date)).toBe(15);
    expect(viewerLongitude({ getTimezoneOffset: () => 300 } as Date)).toBe(-75);
    expect(viewerLongitude({ getTimezoneOffset: () => 0 } as Date)).toBe(0);
  });

  it("gets the sign right for the two obvious cases", () => {
    // Berlin is east of Greenwich, New York is west. A sign error here spins
    // the globe to the wrong side of the planet.
    expect(viewerLongitude({ getTimezoneOffset: () => -60 } as Date)).toBeGreaterThan(0);
    expect(viewerLongitude({ getTimezoneOffset: () => 240 } as Date)).toBeLessThan(0);
  });
});

describe("naming the place", () => {
  it("takes the city out of an IANA zone", () => {
    expect(viewerPlace("Europe/Berlin")).toBe("Berlin");
    expect(viewerPlace("Asia/Tokyo")).toBe("Tokyo");
  });

  it("makes underscored names readable", () => {
    expect(viewerPlace("America/New_York")).toBe("New York");
    expect(viewerPlace("Asia/Ho_Chi_Minh")).toBe("Ho Chi Minh");
  });

  it("uses the city from a three-part zone, not the region", () => {
    expect(viewerPlace("America/Argentina/Buenos_Aires")).toBe("Buenos Aires");
    expect(viewerPlace("America/Indiana/Indianapolis")).toBe("Indianapolis");
  });

  it("refuses anything that is not a place", () => {
    // Greeting somebody from "UTC" is worse than not greeting them at all.
    for (const zone of ["UTC", "Etc/UTC", "Etc/GMT+3", "GMT", "Zulu", "Factory"]) {
      expect(viewerPlace(zone)).toBeNull();
    }
  });

  it("refuses region codes, which is not how people say where they live", () => {
    expect(viewerPlace("Australia/ACT")).toBeNull();
    expect(viewerPlace("Australia/NSW")).toBeNull();
  });

  it("falls back to nothing when the browser will not say", () => {
    expect(viewerPlace(undefined)).toBeNull();
    expect(viewerPlace("")).toBeNull();
  });
});
