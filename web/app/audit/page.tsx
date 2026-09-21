"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Audit, type Issue } from "@/lib/api";
import { count, counted } from "@/lib/format";
import { readToken } from "@/lib/session";

const CATEGORIES = [
  { key: "", label: "Everything" },
  { key: "technical", label: "Technical" },
  { key: "content", label: "Content" },
  { key: "seo", label: "Search" },
  { key: "ai_search", label: "AI visibility" },
];

function IssueCard({
  issue,
  onResolve,
}: {
  issue: Issue;
  onResolve: (id: string) => void;
}) {
  const applied = issue.status === "applied";
  const evidence = issue.evidence as Record<string, number | string>;

  return (
    <article className={`issue ${applied ? "issue--applied" : ""}`}>
      <div className="issue__head">
        {/* Severity is never colour alone: the word is always present. */}
        <span className={`sev sev--${issue.severity}`}>{issue.severity}</span>
        <h2 className="issue__title">{issue.title}</h2>
        {issue.status === "regressed" ? (
          <span className="sev sev--high">came back</span>
        ) : null}
      </div>

      <p className="issue__summary">{issue.summary}</p>

      {issue.impact_score >= 1 ? (
        <p className="opportunity__impact" style={{ marginTop: 8 }}>
          About {count(Math.round(issue.impact_score))} more clicks a month
        </p>
      ) : null}

      {issue.url ? <p className="issue__meta">{issue.url}</p> : null}

      {/* The numbers the rule actually used, so the finding can be checked. */}
      {typeof evidence.impressions === "number" ? (
        <p className="issue__meta">
          {count(evidence.impressions as number)} impressions ·{" "}
          {count((evidence.clicks as number) ?? 0)} clicks · position{" "}
          {evidence.position} · expected CTR{" "}
          {((evidence.expected_ctr as number) * 100).toFixed(1)}% from{" "}
          {evidence.baseline_source as string}
        </p>
      ) : null}

      <div className="issue__actions">
        {applied ? (
          <span className="meta">
            Marked fixed — we&apos;ll confirm on the next scan
          </span>
        ) : (
          <button type="button" className="btn-secondary"
                  onClick={() => onResolve(issue.id)}>
            Mark as fixed
          </button>
        )}
      </div>
    </article>
  );
}

export default function AuditPage() {
  const router = useRouter();
  const [websiteId, setWebsiteId] = useState<string | null>(null);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [category, setCategory] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (next = category) => {
      const token = readToken();
      if (!token) return router.replace("/login");
      try {
        const list = await api.listWebsites(token);
        if (list.length === 0) return router.replace("/onboarding");
        setWebsiteId(list[0].id);
        setAudit(await api.audit(token, list[0].id, next || undefined));
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
      }
    },
    [router, category],
  );

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(issueId: string) {
    const token = readToken();
    if (!token || !websiteId) return;
    try {
      await api.resolveIssue(token, websiteId, issueId);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't save that.");
    }
  }

  if (error) return <main className="wrap"><p className="error">{error}</p></main>;
  if (!audit) return <main className="wrap"><p className="empty">Loading…</p></main>;

  return (
    <main className="wrap">
      <p className="greeting">Website audit</p>
      <h1 style={{ fontSize: 26, marginBottom: 6 }}>
        {counted(audit.checks_run, "check")} · {audit.issues_open} to look at
      </h1>

      <div className="tally">
        {(["critical", "high", "medium", "low"] as const).map((severity) => (
          <span className="tally__item" key={severity}>
            <span className={`sev sev--${severity}`}>{severity}</span>
            <span className="tally__n">{audit.counts[severity] ?? 0}</span>
          </span>
        ))}
      </div>

      {!audit.search_data_available ? (
        <p className="footnote" style={{ marginBottom: 18 }}>
          Some checks need Search Console data, which hasn&apos;t arrived yet.
          This isn&apos;t the full picture.
        </p>
      ) : null}

      <div className="filters">
        {CATEGORIES.map((item) => (
          <button
            key={item.key}
            type="button"
            className="chip"
            aria-pressed={category === item.key}
            onClick={() => setCategory(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {audit.issues.length === 0 ? (
        <p className="empty">Nothing to fix here. That&apos;s a good result.</p>
      ) : (
        audit.issues.map((issue) => (
          <IssueCard key={issue.id} issue={issue} onResolve={resolve} />
        ))
      )}
    </main>
  );
}
