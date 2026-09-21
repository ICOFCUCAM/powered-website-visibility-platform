"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Stepper } from "@/components/Stepper";
import {
  api,
  ApiError,
  type GoogleConnection,
  type Performance,
  type Website,
} from "@/lib/api";
import { buildSteps, hasAnalyticsScope } from "@/lib/onboarding";
import { readToken } from "@/lib/session";

function Scanning() {
  const router = useRouter();
  const params = useSearchParams();
  const websiteId = params.get("website_id");

  const [website, setWebsite] = useState<Website | null>(null);
  const [connection, setConnection] = useState<GoogleConnection | null>(null);
  const [performance, setPerformance] = useState<Performance | null>(null);
  const [linked, setLinked] = useState(false);
  const [synced, setSynced] = useState(false);
  const [analyticsSynced, setAnalyticsSynced] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  const run = useCallback(async () => {
    const token = readToken();
    if (!token) return router.replace("/login");
    if (!websiteId) return router.replace("/onboarding");

    try {
      const [site, connections] = await Promise.all([
        api.website(token, websiteId),
        api.connections(token),
      ]);
      setWebsite(site);
      setConnection(connections.find((c) => c.status === "active") ?? null);

      // The sync is what fills the charts. It runs once — the ref guards
      // React's development double-invoke, which would otherwise spend a
      // second 16-month backfill against the customer's Google quota.
      if (started.current) return;
      started.current = true;

      const result = await api.syncSearchConsole(token, websiteId);
      setLinked(true);
      setSynced(result.status !== "failed");
      setPerformance(await api.performance(token, websiteId));

      // Analytics is optional, so a failure here must not fail the wizard.
      try {
        await api.syncAnalytics(token, websiteId);
        setAnalyticsSynced(true);
      } catch {
        setAnalyticsSynced(false);
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // No Search Console link: not a failure, just nothing to sync yet.
        setSynced(false);
        return;
      }
      setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
    }
  }, [router, websiteId]);

  useEffect(() => {
    void run();
  }, [run]);

  const steps = buildSteps({
    website,
    connection,
    searchConsoleLinked: linked,
    analyticsLinked: analyticsSynced,
    analyticsAvailable: hasAnalyticsScope(connection),
    synced,
  });

  const done = synced && performance !== null;

  return (
    <main className="wrap wizard">
      <p className="eyebrow">Setup · step 5 of 5</p>
      <h1>{done ? "Your website is ready" : "Analysing your website"}</h1>
      <p className="sub">
        {done
          ? "We pulled in your search history, so your charts are populated from the start."
          : "This takes a minute. We're fetching up to 16 months of your search history."}
      </p>

      <Stepper steps={steps} />

      {performance ? (
        <div className="panel">
          <h2>Last 28 days, from Google</h2>
          <div className="stat-row">
            <span>
              <span className="stat__label">Clicks</span>
              <br />
              <span className="stat__value">
                {performance.totals.clicks.toLocaleString()}
              </span>
            </span>
            <span>
              <span className="stat__label">Impressions</span>
              <br />
              <span className="stat__value">
                {performance.totals.impressions.toLocaleString()}
              </span>
            </span>
            <span>
              <span className="stat__label">Avg position</span>
              <br />
              <span className="stat__value">
                {performance.totals.position?.toFixed(1) ?? "—"}
              </span>
            </span>
          </div>
        </div>
      ) : null}

      {done ? (
        <button type="button" onClick={() => router.push("/dashboard")}>
          Open dashboard
        </button>
      ) : null}

      {!done && !error ? (
        <p className="empty">Working…</p>
      ) : null}

      {error ? (
        <>
          <p className="error">{error}</p>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => {
              started.current = false;
              setError(null);
              void run();
            }}
          >
            Try again
          </button>
        </>
      ) : null}
    </main>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<main className="wrap wizard">Loading…</main>}>
      <Scanning />
    </Suspense>
  );
}
