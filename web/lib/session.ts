/**
 * Session storage for the browser.
 *
 * The ONLY module that knows how a token is obtained or held. M1 accepts a
 * token directly so the shell is exercisable end to end; M2 replaces the
 * innards with Supabase Auth and nothing else in the app changes.
 *
 * Note what is not here: no organisation id, no role, no plan. The client is
 * never the authority on scope — every request is re-scoped server-side from
 * the session (docs/02-api.md).
 */

const KEY = "vh.token";

export function readToken(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    // Private windows and blocked site data throw rather than returning null.
    return null;
  }
}

export function writeToken(token: string): void {
  try {
    window.localStorage.setItem(KEY, token);
  } catch {
    /* non-fatal: the session simply does not survive a reload */
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* non-fatal */
  }
}
