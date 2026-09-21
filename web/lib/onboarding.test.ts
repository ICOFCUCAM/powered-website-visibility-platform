import { describe, expect, it } from "vitest";
import type { GoogleConnection, GoogleProperty, Website } from "@/lib/api";
import { buildSteps, suggestedProperty, hasAnalyticsScope, ANALYTICS_SCOPE } from "@/lib/onboarding";

const website: Website = {
  id: "w1", organization_id: "o1", domain: "example.com",
  canonical_url: "https://example.com", name: null, status: "PENDING",
  timezone: "UTC", created_at: "", ownership_verified: false,
  crawl_allowed: false, crawl_blocked_reason: null,
};

const connection = (over: Partial<GoogleConnection> = {}): GoogleConnection => ({
  id: "c1", provider: "google", account: "owner@example.com",
  granted_scopes: ["openid", "https://www.googleapis.com/auth/webmasters.readonly"],
  status: "active", last_error: null, connected_at: "", last_refreshed_at: null,
  ...over,
});

const property = (over: Partial<GoogleProperty> = {}): GoogleProperty => ({
  id: "p1", service: "search_console", property_uri: "sc-domain:example.com",
  property_name: null, permission_level: "siteOwner", account: "owner@example.com",
  match_quality: "domain", suggested: true, proves_ownership: true, match_rank: 3,
  ...over,
});

const base = {
  website: null, connection: null, searchConsoleLinked: false,
  analyticsLinked: false, analyticsAvailable: false, synced: false,
};

const byLabel = (steps: ReturnType<typeof buildSteps>) =>
  Object.fromEntries(steps.map((s) => [s.label, s]));

describe("the onboarding checklist", () => {
  it("shows every step from the start, so the customer can see how far they have to go", () => {
    const steps = buildSteps(base);
    expect(steps.map((s) => s.label)).toEqual([
      "Your website", "Google account", "Search Console",
      "Analytics", "Business Profile", "Website scan",
    ]);
  });

  it("marks exactly one step active at the beginning", () => {
    const active = buildSteps(base).filter((s) => s.state === "active");
    expect(active).toHaveLength(1);
    expect(active[0].label).toBe("Your website");
  });

  it("ticks a step only once the API has confirmed it", () => {
    const steps = byLabel(buildSteps({ ...base, website }));
    expect(steps["Your website"].state).toBe("done");
    expect(steps["Your website"].detail).toBe("example.com");
    // Not connected yet — no premature tick.
    expect(steps["Google account"].state).toBe("active");
    expect(steps["Search Console"].state).toBe("pending");
  });

  it("never renders Business Profile as a step that could complete", () => {
    // It needs a separate Google approval we do not have. A step that never
    // ticks reads as something broken; "coming soon" reads as honest.
    for (const state of [base, { ...base, website }, { ...base, website, connection: connection(), synced: true }]) {
      const step = byLabel(buildSteps(state))["Business Profile"];
      expect(step.state).toBe("unavailable");
      expect(step.detail).toBe("Coming soon");
    }
  });

  it("skips Analytics rather than blocking when the user declined that permission", () => {
    const steps = byLabel(buildSteps({
      ...base, website, connection: connection(), searchConsoleLinked: true,
      analyticsAvailable: false,
    }));
    expect(steps["Analytics"].state).toBe("skipped");
    expect(steps["Analytics"].detail).toContain("later");
    // And the scan is still reachable.
    expect(steps["Website scan"].state).toBe("active");
  });

  it("does not treat a connection needing re-auth as connected", () => {
    const steps = byLabel(buildSteps({
      ...base, website, connection: connection({ status: "needs_reauth" }),
    }));
    expect(steps["Google account"].state).toBe("active");
  });

  it("reaches a fully ticked state once everything has run", () => {
    const steps = buildSteps({
      website, connection: connection({ granted_scopes: [ANALYTICS_SCOPE] }),
      searchConsoleLinked: true, analyticsLinked: true,
      analyticsAvailable: true, synced: true,
    });
    const outstanding = steps.filter(
      (s) => s.state !== "done" && s.label !== "Business Profile",
    );
    expect(outstanding).toEqual([]);
  });
});

describe("choosing a property", () => {
  it("picks the best match, not the first one Google returned", () => {
    const chosen = suggestedProperty([
      property({ id: "prefix", property_uri: "https://www.example.com/", match_quality: "host", match_rank: 2 }),
      property({ id: "domain", match_quality: "domain", match_rank: 3 }),
    ]);
    expect(chosen?.id).toBe("domain");
  });

  it("pre-selects nothing when nothing matches, rather than guessing", () => {
    expect(suggestedProperty([
      property({ suggested: false, match_quality: "none", match_rank: 0 }),
    ])).toBeNull();
    expect(suggestedProperty([])).toBeNull();
  });

  it("reads the analytics grant from what Google actually gave", () => {
    expect(hasAnalyticsScope(connection())).toBe(false);
    expect(hasAnalyticsScope(connection({ granted_scopes: [ANALYTICS_SCOPE] }))).toBe(true);
    expect(hasAnalyticsScope(null)).toBe(false);
  });
});
