"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Dashboard, type Website } from "@/lib/api";
import {
  COMPONENT_LABELS,
  count,
  counted,
  day,
  delta,
  greeting,
  percent,
  position,
  when,
} from "@/lib/format";
import { clearToken, readToken } from "@/lib/session";

function Delta({ value }: { value: number | null }) {
  const { label, direction } = delta(value);
  return <span className={`metric__delta ${direction}`}>{label}</span>;
}

function Score({ score }: { score: Dashboard["score"] }) {
  if (!score) return null;
  const { label, direction } = delta(score.change_pct);

  return (
    <div className="section">
      <p className="section__title">Visibility health</p>
      <div className="score">
        <span className="score__value">{Math.round(score.total)}</span>
        <span className="score__out-of">/ 100</span>
        {score.is_first_measurement ? (
          // "▲0%" would be a claim we cannot support on day one.
          <span className="score__delta flat">first measurement</span>
        ) : (
          <span className={`score__delta ${direction}`}>
            {label} since {day(score.compared_to!)}
          </span>
        )}
      </div>

      <div className="bars">
        {Object.entries(COMPONENT_LABELS).map(([key, label]) => {
          const value = score.components[key];
          return (
            <div className="bar" key={key}>
              <span>{label}</span>
              {value === null || value === undefined ? (
                // Absent, not zero: a component with no data source would
                // otherwise read as a failing grade.
                <span className="bar__absent">not measured yet</span>
              ) : (
                <>
                  <span className="bar__track">
                    <span className="bar__fill" style={{ width: `${value}%` }} />
                  </span>
                  <span className="bar__value">{Math.round(value)}</span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const [websites, setWebsites] = useState<Website[] | null>(null);
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) return router.replace("/login");

    try {
      const list = await api.listWebsites(token);
      setWebsites(list);
      if (list.length === 0) {
        router.replace("/onboarding");
        return;
      }
      setData(await api.dashboard(token, list[0].id));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        router.replace("/login");
        return;
      }
      setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <main className="wrap">
        <p className="error">{error}</p>
      </main>
    );
  }
  if (!data || !websites) {
    return (
      <main className="wrap">
        <p className="empty">Loading…</p>
      </main>
    );
  }

  const { attention, opportunities, search, analytics, freshness } = data;
  const top = opportunities[0];

  return (
    <main className="wrap">
      <p className="greeting">{greeting()}</p>
      <h1 style={{ fontSize: 26, marginBottom: 6 }}>{data.website.domain}</h1>
      <p className="plan__sub">
        {/* Offered here rather than buried: the question someone arrives with
            is usually "why", and the dashboard answers "what". */}
        <Link href="/assistant">Ask a question about your website →</Link>
        {" · "}
        <Link href="/settings">Settings</Link>
      </p>

      {data.setup_hint ? (
        <p className="headline">
          <strong>{data.setup_hint}</strong>
        </p>
      ) : null}

      <Score score={data.score} />

      <div className="section">
        <p className="section__title">What needs attention</p>
        <div className="attention">
          <div className="attention__row">
            <span className="dot dot--critical" />
            <span>
              {counted(attention.critical, "critical technical problem")}
            </span>
          </div>
          <div className="attention__row">
            <span className="dot dot--content" />
            <span>
              {counted(attention.content_opportunities, "content opportunity",
                       "content opportunities")}
            </span>
          </div>
          <div className="attention__row">
            <span className="dot dot--losing" />
            <span>
              {counted(attention.losing_visibility, "page")} losing search
              visibility
            </span>
          </div>
          <div className="attention__row">
            <span className="dot dot--gaining" />
            <span>
              {counted(attention.gaining_visibility, "search", "searches")}{" "}
              gaining visibility
            </span>
          </div>
        </div>
      </div>

      {top ? (
        <div className="section">
          <p className="section__title">What should I do today?</p>
          <div className="opportunity">
            <p className="opportunity__headline">{top.headline}</p>
            {top.estimated_clicks > 0 ? (
              <p className="opportunity__impact">
                Potential: about {count(top.estimated_clicks)} more clicks a month
              </p>
            ) : null}
            <div style={{ marginTop: 14, display: "flex", gap: 10 }}>
              {/* The plan comes first: it is four things in the order they
                  are worth doing. The audit is everything, which is the
                  right answer to a different question. */}
              <Link href="/plan">
                <button type="button">This week&apos;s plan</button>
              </Link>
              <Link href="/audit">
                <button type="button" className="btn-secondary">
                  View opportunities
                </button>
              </Link>
            </div>
          </div>
          {opportunities.slice(1).map((item) => (
            <p key={item.type_key} style={{ fontSize: 14, color: "var(--ink-2)" }}>
              · {item.headline}
            </p>
          ))}
        </div>
      ) : null}

      {search ? (
        <div className="section">
          <p className="section__title">Google search · last 28 days</p>
          <div className="metrics">
            <div>
              <div className="metric__label">Clicks</div>
              <div className="metric__value">{count(search.clicks)}</div>
              <Delta value={search.change?.clicks ?? null} />
            </div>
            <div>
              <div className="metric__label">Impressions</div>
              <div className="metric__value">{count(search.impressions)}</div>
              <Delta value={search.change?.impressions ?? null} />
            </div>
            <div>
              <div className="metric__label">CTR</div>
              <div className="metric__value">{percent(search.ctr)}</div>
            </div>
            <div>
              <div className="metric__label">Avg position</div>
              <div className="metric__value">{position(search.position)}</div>
              <Delta value={search.change?.position ?? null} />
            </div>
          </div>
          {search.anonymised_clicks > 0 ? (
            <p className="footnote">
              {count(search.anonymised_clicks)} of these clicks came from searches
              Google doesn&apos;t disclose, so the query list won&apos;t add up to
              this total.
            </p>
          ) : null}
        </div>
      ) : null}

      {analytics.connected ? (
        <div className="section">
          <p className="section__title">Analytics · last 28 days</p>
          <div className="metrics">
            <div>
              <div className="metric__label">Users</div>
              <div className="metric__value">{count(analytics.active_users)}</div>
            </div>
            <div>
              <div className="metric__label">Sessions</div>
              <div className="metric__value">{count(analytics.sessions)}</div>
            </div>
            <div>
              <div className="metric__label">Engagement</div>
              <div className="metric__value">{percent(analytics.engagement_rate, 0)}</div>
            </div>
          </div>
          {!analytics.outcomes_configured ? (
            <p className="footnote">
              Outcomes aren&apos;t set up yet, so we can show traffic but not what
              it produced.{" "}
              <Link href={`/onboarding/goals?website_id=${data.website.id}`}>
                Tell us what counts as a result
              </Link>
              .
            </p>
          ) : null}
        </div>
      ) : null}

      {data.recent_changes.length > 0 ? (
        <div className="section">
          <p className="section__title">Recent changes</p>
          <ul className="changes">
            {data.recent_changes.slice(0, 6).map((change, i) => (
              <li className="change" key={`${change.title}-${i}`}>
                <span className={`change__kind change__kind--${change.kind}`}>
                  {change.kind}
                </span>
                <span>
                  {change.title}
                  {change.url ? (
                    <span className="meta"> · {new URL(change.url).pathname}</span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="footnote">
        Website scanned {when(freshness.last_crawl_at)}
        {freshness.pages_crawled ? ` · ${freshness.pages_crawled} pages` : ""}
        {freshness.search_data_through
          ? ` · Google data through ${day(freshness.search_data_through)}`
          : ""}
      </p>
    </main>
  );
}
