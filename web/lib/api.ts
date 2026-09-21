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

export const api = {
  me: (token: string) => request<Me>("/auth/me", token),
  listWebsites: (token: string) => request<Website[]>("/websites", token),
  createWebsite: (token: string, url: string, name?: string) =>
    request<Website>("/websites", token, {
      method: "POST",
      body: JSON.stringify({ url, name: name ?? null }),
    }),
  health: () => request<{ status: string }>("/health", null),
};
