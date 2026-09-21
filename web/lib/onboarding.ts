/**
 * Which onboarding step a website is on.
 *
 * Derived from API state rather than remembered in the browser, so refreshing
 * the page, returning from Google, or opening the link on another device all
 * land in the same place. A wizard that keeps its progress in local state
 * loses it exactly when the user comes back from an external redirect — which
 * is every OAuth flow.
 */

import type { GoogleConnection, GoogleProperty, Website } from "@/lib/api";
import type { Step } from "@/components/Stepper";

export interface OnboardingState {
  website: Website | null;
  connection: GoogleConnection | null;
  searchConsoleLinked: boolean;
  analyticsLinked: boolean;
  analyticsAvailable: boolean;
  synced: boolean;
}

export const ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly";

export function hasAnalyticsScope(connection: GoogleConnection | null): boolean {
  return Boolean(connection?.granted_scopes?.includes(ANALYTICS_SCOPE));
}

export function buildSteps(state: OnboardingState): Step[] {
  const { website, connection, searchConsoleLinked, analyticsLinked, synced } = state;
  const connected = connection?.status === "active";

  return [
    {
      label: "Your website",
      state: website ? "done" : "active",
      detail: website?.domain,
    },
    {
      label: "Google account",
      state: connected ? "done" : website ? "active" : "pending",
      detail: connection?.account,
    },
    {
      label: "Search Console",
      state: searchConsoleLinked ? "done" : connected ? "active" : "pending",
      detail: searchConsoleLinked ? "Connected" : undefined,
    },
    {
      label: "Analytics",
      state: analyticsLinked
        ? "done"
        : !state.analyticsAvailable && connected
          ? "skipped"
          : connected
            ? "active"
            : "pending",
      detail: analyticsLinked
        ? "Connected"
        : !state.analyticsAvailable && connected
          ? "Not connected — you can add it later"
          : undefined,
    },
    {
      // Honest about why this one cannot be ticked: the API needs a separate
      // Google approval, and pretending otherwise would read as broken.
      label: "Business Profile",
      state: "unavailable",
      detail: "Coming soon",
    },
    {
      label: "Website scan",
      state: synced ? "done" : searchConsoleLinked ? "active" : "pending",
      detail: synced ? "Your history is in" : undefined,
    },
  ];
}

/** The best auto-match, or nothing. Never "the first one Google returned". */
export function suggestedProperty(properties: GoogleProperty[]): GoogleProperty | null {
  const suggested = properties.filter((p) => p.suggested);
  if (suggested.length === 0) return null;
  return suggested.reduce((best, p) => (p.match_rank > best.match_rank ? p : best));
}

export function describeMatch(property: GoogleProperty): string {
  switch (property.match_quality) {
    case "domain":
      return "Covers every subdomain and both http and https";
    case "host":
      return "Covers this exact address";
    case "subdomain":
      return "A different subdomain of your site";
    default:
      return "Doesn't match this website";
  }
}
