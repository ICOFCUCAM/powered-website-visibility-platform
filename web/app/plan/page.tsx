"use client";

/**
 * This week's plan.
 *
 * The same four priorities that go out in the email, in the same order, for
 * the same reason: the order is `impact_score`, computed in code. If this
 * screen sorted them differently — by severity, by effort, by anything — the
 * email and the app would recommend different things in the same week.
 *
 * Marking one done records INTENT, not evidence. The issue behind it stays
 * open until a crawl observes it gone, which is what lets next week's plan
 * say "you marked this done and it is still there".
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  apiUrl,
  type Plan,
  type Recommendation,
  type Report,
  type Website,
} from "@/lib/api";
import { count, counted, day, when } from "@/lib/format";
import { clearToken, readToken } from "@/lib/session";

const EFFORT_LABEL: Record<string, string> = {
  low: "quick",
  medium: "an hour or so",
  high: "a bigger job",
};

/**
 * An example is a page URL or a search phrase. Only one of those has a path,
 * and calling new URL() on "sourdough bread near me" throws — which would
 * take the whole page down rather than showing one row oddly.
 */
function exampleLabel(value: string): string {
  try {
    return new URL(value).pathname;
  } catch {
    return value;
  }
}

/** The steps arrive as a markdown numbered list; render them as a list. */
function steps(howTo: string | null): string[] {
  if (!howTo) return [];
  return howTo
    .split("\n")
    .map((line) => line.replace(/^\s*\d+\.\s*/, "").trim())
    .filter(Boolean);
}

function Priority({
  item,
  onStatus,
  busy,
}: {
  item: Recommendation;
  onStatus: (id: string, action: "complete" | "dismiss") => void;
  busy: boolean;
}) {
  const settled = item.status === "RESOLVED" || item.status === "DISMISSED";
  const how = steps(item.how_to_md);

  return (
    <li className={`issue${settled ? " issue--applied" : ""}`}>
      <div className="issue__head">
        <span className="chip chip--rank">Priority {item.rank}</span>
        <span className="issue__title">{item.title}</span>
      </div>

      {item.body_md ? <p className="issue__summary">{item.body_md}</p> : null}

      {how.length > 0 ? (
        <ol className="steps">
          {how.map((step, index) => (
            <li key={index}>{step}</li>
          ))}
        </ol>
      ) : null}

      {item.examples.length > 0 ? (
        <div className="paths">
          {item.examples.slice(0, 3).map((example) => (
            <span className="meta" key={example}>
              {exampleLabel(example)}
            </span>
          ))}
          {item.pages_affected > Math.min(item.examples.length, 3) ? (
            <span className="meta">
              and {count(item.pages_affected - Math.min(item.examples.length, 3))}{" "}
              more
            </span>
          ) : null}
        </div>
      ) : null}

      <p className="priority__impact">
        {item.estimated_clicks_delta && item.estimated_clicks_delta > 0 ? (
          <span className="priority__gain">
            about {counted(item.estimated_clicks_delta, "more click", "more clicks")}{" "}
            a month
          </span>
        ) : null}
        {item.estimated_clicks_delta && item.estimated_clicks_delta > 0 ? " · " : ""}
        {EFFORT_LABEL[item.effort] ?? item.effort}
        {item.prose_source === "model" ? " · wording written by AI" : ""}
      </p>

      {settled ? (
        <p className="issue__meta">
          {item.status === "RESOLVED"
            ? "You marked this done. We'll confirm it on the next scan."
            : "Dismissed."}
        </p>
      ) : (
        <div className="issue__actions">
          <button
            className="btn-secondary"
            disabled={busy}
            onClick={() => onStatus(item.id, "complete")}
          >
            I&apos;ve done this
          </button>
          <button
            className="btn-secondary"
            disabled={busy}
            onClick={() => onStatus(item.id, "dismiss")}
          >
            Not for me
          </button>
        </div>
      )}
    </li>
  );
}

export default function PlanPage() {
  const router = useRouter();
  const [website, setWebsite] = useState<Website | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [latest, setLatest] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) return router.replace("/login");

    try {
      const websites = await api.listWebsites(token);
      if (websites.length === 0) return router.replace("/onboarding");
      setWebsite(websites[0]);
      setPlan(await api.plan(token, websites[0].id));
      const { reports } = await api.reports(token, websites[0].id);
      setLatest(reports[0] ?? null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        return router.replace("/login");
      }
      setError(
        err instanceof ApiError ? err.message : "We couldn't load your plan.",
      );
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function write() {
    const token = readToken();
    if (!token || !website) return;
    setBusy(true);
    setError(null);
    try {
      await api.generateReport(token, website.id);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "We couldn't write the plan.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function setStatus(id: string, action: "complete" | "dismiss") {
    const token = readToken();
    if (!token || !website) return;
    setBusy(true);
    try {
      await api.setRecommendationStatus(token, website.id, id, action);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "We couldn't save that.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (error && !plan) {
    return (
      <main className="wrap">
        <p className="error">{error}</p>
      </main>
    );
  }
  if (!plan || !website) {
    return (
      <main className="wrap">
        <p className="meta">Loading…</p>
      </main>
    );
  }

  const open = plan.recommendations.filter(
    (item) => item.status === "OPEN" || item.status === "IN_PROGRESS",
  );

  return (
    <main className="wrap">
      <p className="eyebrow">
        <Link href="/dashboard">← Dashboard</Link>
      </p>
      <h1 className="plan__title">This week&apos;s plan</h1>
      <p className="plan__sub">
        {website.domain}
        {plan.week_start
          ? ` · week of ${day(plan.week_start)} · written ${when(plan.generated_at)}`
          : ""}
      </p>

      {error ? <p className="error">{error}</p> : null}

      {plan.recommendations.length === 0 ? (
        <div className="section">
          <p className="empty">
            We haven&apos;t written a plan yet. It takes your latest scan and
            your Search Console figures and picks the four things worth doing
            first.
          </p>
          <button className="btn-secondary" disabled={busy} onClick={write}>
            {busy ? "Working…" : "Write this week's plan"}
          </button>
        </div>
      ) : (
        <>
          {plan.summary_md ? (
            <p className="plan__summary">{plan.summary_md}</p>
          ) : null}

          <div className="section">
            <p className="section__title">
              {open.length > 0
                ? counted(open.length, "thing", "things") + " to do"
                : "Everything on this plan is settled"}
            </p>
            <ul className="issues">
              {plan.recommendations.map((item) => (
                <Priority
                  key={item.id}
                  item={item}
                  onStatus={setStatus}
                  busy={busy}
                />
              ))}
            </ul>
          </div>

          <div className="section">
            <button className="btn-secondary" disabled={busy} onClick={write}>
              {busy ? "Working…" : "Rewrite from the latest data"}
            </button>
            {latest ? (
              <p className="footnote">
                <a href={apiUrl(latest.html_url)} target="_blank" rel="noreferrer">
                  See this week&apos;s report as an email
                </a>
                {latest.status === "sent" && latest.sent_at
                  ? ` · sent ${when(latest.sent_at)}`
                  : " · not sent yet"}
              </p>
            ) : null}
          </div>
        </>
      )}

      <p className="footnote">
        The order comes from your own figures — how many clicks each problem is
        costing you — not from an opinion about what usually matters.
      </p>
    </main>
  );
}
