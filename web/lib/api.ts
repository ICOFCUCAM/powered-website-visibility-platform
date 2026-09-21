/**
 * The only way the web app reaches the backend.
 *
 * Generated from the OpenAPI schema later; hand-written for M1 so the shell
 * has something real to call. Two rules it encodes:
 *
 *  - Errors arrive as {error:{code,message}}. `code` is what we branch on;
 *    `message` is written for a non-technical reader and shown as-is.
 *  - No organisation id is ever sent. Scope comes from the session.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

/**
 * Turns an API-relative path into one a browser can navigate to.
 *
 * The signed report link is handed back as a path including the version
 * prefix, and API_BASE already ends in that prefix — so resolving one against
 * the other naively produces /api/v1/api/v1/... The origin is the only part
 * of API_BASE that is safe to reuse here.
 */
export function apiUrl(path: string): string {
  return `${new URL(API_BASE, "http://localhost").origin}${path}`;
}

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  token: string | null,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
    cache: "no-store",
  });

  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    const err = body?.error;
    throw new ApiError(
      err?.code ?? "unexpected_error",
      err?.message ?? "Something went wrong. Please try again.",
      response.status,
      err?.details ?? {},
    );
  }
  return body as T;
}

export interface Website {
  id: string;
  organization_id: string;
  domain: string;
  canonical_url: string;
  name: string | null;
  status: "PENDING" | "CONNECTING" | "CRAWLING" | "READY" | "ERROR";
  timezone: string;
  created_at: string;
  ownership_verified: boolean;
  crawl_allowed: boolean;
  crawl_blocked_reason: string | null;
}

export interface Me {
  user_id: string;
  email: string;
  organizations: { organization_id: string; role: string }[];
}

export interface GoogleProperty {
  id: string;
  service: string;
  property_uri: string;
  property_name: string | null;
  permission_level: string | null;
  account: string;
  match_quality: "none" | "subdomain" | "host" | "domain";
  suggested: boolean;
  proves_ownership: boolean;
  match_rank: number;
}

export interface GoogleConnection {
  id: string;
  provider: string;
  account: string;
  granted_scopes: string[];
  status: "active" | "needs_reauth" | "revoked" | "error";
  last_error: string | null;
  connected_at: string;
  last_refreshed_at: string | null;
}

export interface SyncResult {
  status: string;
  rows_written: number;
  api_calls: number;
  quota_hits: number;
  chunks_completed: number;
  chunks_failed: number;
}

export interface Performance {
  start: string;
  end: string;
  totals: { clicks: number; impressions: number; ctr: number | null; position: number | null };
  compared_to: Performance["totals"] | null;
  delta: { clicks: number | null; impressions: number | null; ctr: number | null; position: number | null } | null;
  series: { date: string; clicks: number; impressions: number; ctr: number | null; position: number | null }[];
  anonymised: {
    total_clicks: number;
    attributed_clicks: number;
    anonymised_clicks: number;
    anonymised_share: number | null;
    note: string;
  };
}

export interface KeyEvent {
  event_name: string;
  custom: boolean;
  counting: string | null;
}

export interface Goal {
  event_name: string;
  label: string;
  goal_kind: string;
  is_primary: boolean;
}

export interface Dashboard {
  website: { id: string; domain: string; name: string | null; status: string; ownership_verified: boolean };
  score: {
    total: number;
    as_of: string;
    components: Record<string, number | null>;
    change_pct: number | null;
    compared_to: string | null;
    is_first_measurement: boolean;
  } | null;
  attention: {
    critical: number;
    content_opportunities: number;
    losing_visibility: number;
    gaining_visibility: number;
  };
  opportunities: {
    type_key: string;
    headline: string;
    count: number;
    estimated_clicks: number;
    severity: string;
    category: string;
  }[];
  search: {
    clicks: number;
    impressions: number;
    ctr: number | null;
    position: number | null;
    change: { clicks: number | null; impressions: number | null; position: number | null } | null;
    anonymised_clicks: number;
    note: string;
  } | null;
  analytics: {
    connected: boolean;
    sessions: number;
    active_users: number;
    engagement_rate: number | null;
    outcomes_configured: boolean;
  };
  recent_changes: { observed_at: string; kind: string; title: string; url: string | null }[];
  freshness: {
    last_crawl_at: string | null;
    pages_crawled: number | null;
    search_data_through: string | null;
    stale: boolean;
  };
  setup_hint: string | null;
}

export interface Issue {
  id: string;
  type_key: string;
  title: string;
  summary: string;
  category: string;
  severity: string;
  status: string;
  scope_type: string;
  impact_score: number;
  effort: string;
  url: string | null;
  evidence: Record<string, unknown>;
  first_detected_at: string;
  last_detected_at: string;
}

export interface Audit {
  checks_run: number;
  issues_open: number;
  counts: Record<string, number>;
  by_category: Record<string, number>;
  issues: Issue[];
  search_data_available: boolean;
}

export interface Recommendation {
  id: string;
  rank: number;
  kind: string;
  title: string;
  body_md: string | null;
  how_to_md: string | null;
  estimated_clicks_delta: number | null;
  effort: string;
  confidence: number;
  status: "OPEN" | "IN_PROGRESS" | "RESOLVED" | "DISMISSED";
  pages_affected: number;
  /** Pages or search phrases — a keyword finding names a search, not a URL. */
  examples: string[];
  /**
   * Who wrote the words. The ranking beside them is always code, and the UI
   * says so rather than letting a reader assume either way.
   */
  prose_source: "template" | "model";
}

export interface Plan {
  id: string | null;
  week_start: string | null;
  summary_md: string | null;
  generated_at: string | null;
  fallback_reason: string | null;
  recommendations: Recommendation[];
}

export interface Report {
  id: string;
  period_start: string;
  period_end: string;
  subject: string | null;
  status: "draft" | "sent" | "failed";
  generated_at: string;
  sent_at: string | null;
  recipients: string[];
  html_url: string;
}

export const api = {
  me: (token: string) => request<Me>("/auth/me", token),
  listWebsites: (token: string) => request<Website[]>("/websites", token),
  createWebsite: (token: string, url: string, name?: string) =>
    request<Website>("/websites", token, {
      method: "POST",
      body: JSON.stringify({ url, name: name ?? null }),
    }),
  health: () => request<{ status: string }>("/health", null),

  connections: (token: string) =>
    request<GoogleConnection[]>("/google/connections", token),

  /**
   * A browser cannot put an Authorization header on a link or a redirect, so
   * the API hands back the URL and we navigate to it. Putting the session
   * token in a query string instead would write a credential into browser
   * history, server logs and the Referer header.
   */
  startGoogleConnect: (token: string, websiteId: string, services: string[]) => {
    const query = new URLSearchParams({ website_id: websiteId });
    services.forEach((s) => query.append("services", s));
    return request<{ authorization_url: string }>(
      `/google/connect?${query}`,
      token,
      { headers: { Accept: "application/json" } },
    );
  },

  properties: (token: string, service: "search-console" | "analytics", websiteId: string) =>
    request<GoogleProperty[]>(
      `/google/${service}/properties?website_id=${websiteId}`,
      token,
    ),

  connectProperty: (
    token: string,
    service: "search-console" | "analytics",
    propertyId: string,
    websiteId: string,
    linkMethod: "auto" | "user_selected",
  ) =>
    request<{ ownership_recorded: boolean }>(
      `/google/${service}/properties/${propertyId}/connect`,
      token,
      { method: "POST", body: JSON.stringify({ website_id: websiteId, link_method: linkMethod }) },
    ),

  syncSearchConsole: (token: string, websiteId: string) =>
    request<SyncResult>(`/websites/${websiteId}/sync/search-console`, token, {
      method: "POST",
    }),

  performance: (token: string, websiteId: string) =>
    request<Performance>(`/websites/${websiteId}/search-performance`, token),

  website: (token: string, websiteId: string) =>
    request<Website>(`/websites/${websiteId}`, token),

  dashboard: (token: string, websiteId: string) =>
    request<Dashboard>(`/websites/${websiteId}/dashboard`, token),

  audit: (token: string, websiteId: string, category?: string) =>
    request<Audit>(
      `/websites/${websiteId}/audit${category ? `?category=${category}` : ""}`,
      token,
    ),

  resolveIssue: (token: string, websiteId: string, issueId: string, note?: string) =>
    request<Issue>(`/websites/${websiteId}/audit/${issueId}/resolve`, token, {
      method: "POST",
      body: JSON.stringify({ note: note ?? null }),
    }),

  keyEvents: (token: string, websiteId: string) =>
    request<KeyEvent[]>(`/websites/${websiteId}/analytics/events`, token),

  setGoals: (
    token: string,
    websiteId: string,
    goals: { event_name: string; label?: string; goal_kind?: string; is_primary?: boolean }[],
  ) =>
    request<Goal[]>(`/websites/${websiteId}/analytics/goals`, token, {
      method: "POST",
      body: JSON.stringify({ goals }),
    }),

  plan: (token: string, websiteId: string) =>
    request<Plan>(`/websites/${websiteId}/plan`, token),

  setRecommendationStatus: (
    token: string,
    websiteId: string,
    recommendationId: string,
    action: "complete" | "dismiss",
  ) =>
    request<{ id: string; status: string }>(
      `/websites/${websiteId}/recommendations/${recommendationId}/${action}`,
      token,
      { method: "POST" },
    ),

  reports: (token: string, websiteId: string) =>
    request<{ reports: Report[] }>(`/websites/${websiteId}/reports`, token),

  generateReport: (token: string, websiteId: string) =>
    request<{ id: string; week_start: string; subject: string; html_url: string }>(
      `/websites/${websiteId}/reports`,
      token,
      { method: "POST" },
    ),

  syncAnalytics: (token: string, websiteId: string) =>
    request<{ status: string; goals_synced: number }>(
      `/websites/${websiteId}/sync/analytics`,
      token,
      { method: "POST" },
    ),
};
