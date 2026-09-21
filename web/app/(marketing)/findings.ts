/**
 * Turning a rule key into something a stranger understands.
 *
 * The scan returns the product's own finding keys, which are precise and
 * mean nothing to somebody who arrived thirty seconds ago. This is the only
 * place the marketing page translates them, and it never adds a claim the
 * rule did not make — no counts that were not measured, no severity that was
 * not returned.
 */

import type { PeekFinding } from "@/lib/api";

const LABELS: Record<string, { title: string; pillar: string }> = {
  missing_title: { title: "This page has no title tag", pillar: "Content" },
  title_length: { title: "The title is an awkward length", pillar: "Content" },
  missing_meta_description: {
    title: "No meta description", pillar: "Content",
  },
  missing_h1: { title: "No main heading", pillar: "Content" },
  multiple_h1: { title: "More than one main heading", pillar: "Content" },
  thin_content: { title: "Very little text on the page", pillar: "Content" },
  images_missing_alt: {
    title: "Images without alt text", pillar: "Content",
  },
  missing_viewport: {
    title: "No viewport tag — phones will render it wrong", pillar: "Technical",
  },
  no_structured_data: {
    title: "No structured data", pillar: "Technical",
  },
  missing_organization_schema: {
    title: "No organisation markup", pillar: "Technical",
  },
  content_requires_js: {
    title: "The text only appears after JavaScript runs", pillar: "Technical",
  },
  page_5xx: { title: "The page returned a server error", pillar: "Technical" },
  redirect_chain: { title: "It redirects more than once", pillar: "Technical" },
  canonical_to_other_page: {
    title: "The canonical points somewhere else", pillar: "Search",
  },
};

export type Readable = { key: string; title: string; pillar: string };

export function readable(findings: PeekFinding[]): Readable[] {
  return findings
    .map((f) => {
      const label = LABELS[f.type];
      // A finding we have no words for is dropped rather than shown raw.
      // "content_requires_js" in a hero is worse than one fewer row.
      return label ? { key: f.type, ...label } : null;
    })
    .filter((f): f is Readable => f !== null);
}

/**
 * What to say when a page has nothing wrong with it on these checks.
 * "0 findings" reads like the scan failed.
 */
export const CLEAN = "Nothing to fix on these checks — that is a good sign.";
