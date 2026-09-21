/**
 * Where the sun actually is.
 *
 * The globe in the hero used a made-up light direction, which meant its
 * day/night line was decoration. This computes the real one: the subsolar
 * point — the single spot on Earth with the sun directly overhead — for any
 * instant. Feed it to the renderer and the terminator is correct, so a
 * visitor in Tokyo at midnight sees Tokyo in darkness.
 *
 * NOAA's low-precision algorithm: good to about 0.01° between 1950 and 2050,
 * which is several orders of magnitude better than a globe 600px wide can
 * show. No dependency, no table, ~20 lines of arithmetic.
 */

const DEG = Math.PI / 180;

/** Julian days since J2000.0 (2000-01-01 12:00 UT). */
function daysSinceJ2000(date: Date): number {
  return date.getTime() / 86_400_000 + 2_440_587.5 - 2_451_545.0;
}

/** Fold an angle in degrees into (-180, 180]. */
export function normaliseLongitude(deg: number): number {
  const wrapped = ((deg + 180) % 360 + 360) % 360 - 180;
  // (-180, 180]: -180 and 180 are the same meridian; prefer the positive.
  return wrapped === -180 ? 180 : wrapped;
}

export type Subsolar = {
  /** Degrees north; between roughly -23.44 and +23.44 across a year. */
  latitude: number;
  /** Degrees east. */
  longitude: number;
};

/**
 * The subsolar point at `date`.
 *
 * Latitude is the sun's declination — the reason we have seasons, swinging
 * between the tropics over a year. Longitude is where local noon is, sweeping
 * westward 15° an hour.
 */
export function subsolarPoint(date: Date = new Date()): Subsolar {
  const n = daysSinceJ2000(date);

  // Mean longitude and mean anomaly of the sun.
  const meanLongitude = 280.46 + 0.985_647_4 * n;
  const meanAnomaly = (357.528 + 0.985_600_3 * n) * DEG;

  // Ecliptic longitude: the mean, corrected for Earth's elliptical orbit.
  const eclipticLongitude =
    (meanLongitude +
      1.915 * Math.sin(meanAnomaly) +
      0.02 * Math.sin(2 * meanAnomaly)) *
    DEG;

  // Obliquity of the ecliptic — Earth's axial tilt.
  const obliquity = (23.439 - 0.000_000_4 * n) * DEG;

  const declination = Math.asin(
    Math.sin(obliquity) * Math.sin(eclipticLongitude),
  );
  const rightAscension = Math.atan2(
    Math.cos(obliquity) * Math.sin(eclipticLongitude),
    Math.cos(eclipticLongitude),
  );

  // Greenwich mean sidereal time, in hours.
  const gmst = 18.697_374_558 + 24.065_709_824_419_08 * n;

  return {
    latitude: declination / DEG,
    // The sun's Greenwich hour angle is GMST - RA; hour angle runs westward,
    // so the subsolar longitude is its negation.
    longitude: normaliseLongitude(rightAscension / DEG - (gmst % 24) * 15),
  };
}
