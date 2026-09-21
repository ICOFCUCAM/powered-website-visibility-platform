/**
 * Who is looking, to the extent the browser will say.
 *
 * Everything here comes from the visitor's own timezone setting — not from
 * their IP, not from an edge geolocation lookup, and not from a request to
 * anybody. That is a deliberate choice: it keeps the page a static file that
 * can be served from anywhere, and it means personalising the hero costs the
 * visitor no privacy they had not already spent.
 *
 * The cost is precision. A timezone gives a longitude accurate to about an
 * hour of arc and a city that may be a thousand kilometres away. For turning
 * a globe and naming a region that is plenty; it would be useless for
 * anything that mattered, and nothing here matters.
 */

/**
 * Approximate longitude from the UTC offset: 15° per hour, east positive.
 *
 * `getTimezoneOffset` reports minutes to ADD to local time to reach UTC, so
 * it is positive west of Greenwich — the opposite sign to longitude.
 */
export function viewerLongitude(date: Date = new Date()): number {
  const longitude = (-date.getTimezoneOffset() / 60) * 15;
  // Negating zero gives -0, which is a valid number and a silly longitude.
  return longitude === 0 ? 0 : longitude;
}

/** Zone names that are not places, and must never appear in the copy. */
const NOT_A_PLACE = new Set([
  "UTC", "GMT", "UCT", "Universal", "Zulu", "Greenwich", "UTC0", "Factory",
]);

/**
 * A human place name from an IANA timezone, or null when there isn't one.
 *
 * "Europe/Berlin" is Berlin. "America/Argentina/Buenos_Aires" is Buenos
 * Aires. "Etc/UTC" is nowhere, and returns null so the caller keeps its
 * generic copy rather than greeting somebody from a placeholder.
 */
export function viewerPlace(
  timeZone: string | undefined = typeof Intl !== "undefined"
    ? Intl.DateTimeFormat().resolvedOptions().timeZone
    : undefined,
): string | null {
  if (!timeZone) return null;

  const segments = timeZone.split("/");
  if (segments[0] === "Etc") return null;

  const last = segments[segments.length - 1];
  if (!last || NOT_A_PLACE.has(last)) return null;
  // "GMT+3" and friends.
  if (/^(GMT|UTC)[+-]?\d*$/i.test(last)) return null;
  // Region codes such as "ACT" or "NSW" are not how anybody says where they
  // live. A real city name has a lower-case letter in it.
  if (!/[a-z]/.test(last)) return null;

  return last.replace(/_/g, " ");
}
