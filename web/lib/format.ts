/**
 * Number and date formatting for the dashboard.
 *
 * Pulled out because "how a number reads" is a product decision made once,
 * not a choice re-made at every call site: a position of 14.673 shown to
 * three decimals implies a precision Search Console does not have.
 */

export function count(value: number): string {
  return value.toLocaleString();
}

export function percent(value: number | null, digits = 1): string {
  return value === null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

export function position(value: number | null): string {
  // One decimal. Search Console's own figure is an impressions-weighted
  // average, and more precision than that is invented.
  return value === null ? "—" : value.toFixed(1);
}

export type Direction = "up" | "down" | "flat";

export function delta(value: number | null): { label: string; direction: Direction } {
  if (value === null) return { label: "—", direction: "flat" };
  const rounded = Math.round(value * 10) / 10;
  if (rounded === 0) return { label: "no change", direction: "flat" };
  return {
    label: `${rounded > 0 ? "▲" : "▼"} ${Math.abs(rounded)}%`,
    direction: rounded > 0 ? "up" : "down",
  };
}

/**
 * One date format for the whole product, and it is the one the copy is
 * written in. `toLocaleDateString()` with no locale follows the *browser*,
 * so the same screen reads "9/21/2026" for one customer and "21/09/2026" for
 * another — and the email, rendered server-side, always disagreed with both.
 */
export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

export function greeting(now = new Date()): string {
  const hour = now.getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

export function when(iso: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso);
  const days = Math.floor((Date.now() - then.getTime()) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  return then.toLocaleDateString();
}

export const COMPONENT_LABELS: Record<string, string> = {
  technical_health: "Technical health",
  search_performance: "Search performance",
  content_health: "Content health",
  analytics_coverage: "Analytics coverage",
  ai_visibility: "AI visibility",
};

/**
 * "1 critical technical problems" is the kind of small wrongness that makes a
 * product feel unfinished, and it appears wherever a count meets a noun.
 */
export function plural(n: number, singular: string, pluralForm?: string): string {
  return n === 1 ? singular : (pluralForm ?? `${singular}s`);
}

export function counted(n: number, singular: string, pluralForm?: string): string {
  return `${count(n)} ${plural(n, singular, pluralForm)}`;
}
