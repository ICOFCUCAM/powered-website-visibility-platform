"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { Stepper } from "@/components/Stepper";
import {
  api,
  ApiError,
  type GoogleConnection,
  type GoogleProperty,
  type Website,
} from "@/lib/api";
import {
  buildSteps,
  describeMatch,
  hasAnalyticsScope,
  suggestedProperty,
} from "@/lib/onboarding";
import { readToken } from "@/lib/session";

function PropertyList({
  title,
  properties,
  chosen,
  onChoose,
  emptyMessage,
}: {
  title: string;
  properties: GoogleProperty[];
  chosen: string | null;
  onChoose: (id: string) => void;
  emptyMessage: string;
}) {
  if (properties.length === 0) {
    return (
      <div className="panel">
        <h2>{title}</h2>
        <p>{emptyMessage}</p>
      </div>
    );
  }

  return (
    <div style={{ marginBottom: 22 }}>
      <h2 style={{ fontSize: 15, marginBottom: 10 }}>{title}</h2>
      {properties.map((property) => (
        <label className="choice" key={property.id} htmlFor={property.id}>
          <input
            id={property.id}
            type="radio"
            name={title}
            checked={chosen === property.id}
            onChange={() => onChoose(property.id)}
          />
          <span>
            <span className="choice__uri">
              {property.property_name ?? property.property_uri}
              {/* The badge earns its place: it is why this one is pre-selected. */}
              {property.suggested ? (
                <span className="choice__badge">best match</span>
              ) : null}
            </span>
            <span className="choice__why">
              {describeMatch(property)} · {property.account}
              {property.permission_level ? ` · ${property.permission_level}` : ""}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}

function ChooseProperties() {
  const router = useRouter();
  const params = useSearchParams();
  const websiteId = params.get("website_id");
  const missing = params.get("missing");

  const [website, setWebsite] = useState<Website | null>(null);
  const [connection, setConnection] = useState<GoogleConnection | null>(null);
  const [searchConsole, setSearchConsole] = useState<GoogleProperty[]>([]);
  const [analytics, setAnalytics] = useState<GoogleProperty[]>([]);
  const [chosenSc, setChosenSc] = useState<string | null>(null);
  const [chosenGa, setChosenGa] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) return router.replace("/login");
    if (!websiteId) return router.replace("/onboarding");

    try {
      const [site, connections] = await Promise.all([
        api.website(token, websiteId),
        api.connections(token),
      ]);
      setWebsite(site);
      const active = connections.find((c) => c.status === "active") ?? null;
      setConnection(active);

      const sc = await api.properties(token, "search-console", websiteId);
      setSearchConsole(sc);
      // Pre-select the best match rather than the first thing Google listed.
      setChosenSc(suggestedProperty(sc)?.id ?? null);

      if (hasAnalyticsScope(active)) {
        const ga = await api.properties(token, "analytics", websiteId);
        setAnalytics(ga);
        setChosenGa(suggestedProperty(ga)?.id ?? null);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
    } finally {
      setLoaded(true);
    }
  }, [router, websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function confirm() {
    const token = readToken();
    if (!token || !websiteId || !chosenSc) return;
    setBusy(true);
    setError(null);
    try {
      const auto = suggestedProperty(searchConsole)?.id === chosenSc;
      await api.connectProperty(
        token,
        "search-console",
        chosenSc,
        websiteId,
        auto ? "auto" : "user_selected",
      );
      if (chosenGa) {
        const autoGa = suggestedProperty(analytics)?.id === chosenGa;
        await api.connectProperty(
          token,
          "analytics",
          chosenGa,
          websiteId,
          autoGa ? "auto" : "user_selected",
        );
      }
      // Analytics connected means there is a goal question to ask; without
      // it there is nothing to ask about, so skip straight to the scan.
      router.push(
        chosenGa
          ? `/onboarding/goals?website_id=${websiteId}`
          : `/onboarding/scan?website_id=${websiteId}`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't save that.");
      setBusy(false);
    }
  }

  const steps = buildSteps({
    website,
    connection,
    searchConsoleLinked: false,
    analyticsLinked: false,
    analyticsAvailable: hasAnalyticsScope(connection),
    synced: false,
  });

  return (
    <main className="wrap wizard">
      <p className="eyebrow">Setup · step 3 of 5</p>
      <h1>Choose your website</h1>
      <p className="sub">
        We found these in your Google account. We&apos;ve picked the one that
        matches {website?.domain ?? "your website"}.
      </p>

      <Stepper steps={steps} />

      {missing ? (
        <div className="panel">
          <h2>Some permissions weren&apos;t granted</h2>
          <p>
            You unticked {missing.replace(/_/g, " ")} on the Google screen.
            Everything else still works, and you can add it later from settings.
          </p>
        </div>
      ) : null}

      {!loaded ? (
        <p className="empty">Looking in your Google account…</p>
      ) : (
        <>
          <PropertyList
            title="Search Console"
            properties={searchConsole}
            chosen={chosenSc}
            onChoose={setChosenSc}
            emptyMessage={
              "We couldn't find a Search Console property for this website. " +
              "Google needs to verify you own it first — you can do that in " +
              "Search Console and come back. We can still analyse your site " +
              "in the meantime."
            }
          />

          {hasAnalyticsScope(connection) ? (
            <PropertyList
              title="Google Analytics"
              properties={analytics}
              chosen={chosenGa}
              onChoose={setChosenGa}
              emptyMessage={
                "We couldn't find a Google Analytics 4 property for this " +
                "website. Older Universal Analytics properties aren't " +
                "supported by Google's current API."
              }
            />
          ) : null}

          <div className="panel">
            <h2>Google Business Profile</h2>
            <p>
              Coming soon. For a local business this is often where most of
              your visibility lives, so it&apos;s on the way.
            </p>
          </div>

          <button type="button" onClick={confirm} disabled={busy || !chosenSc}>
            {busy ? "Connecting…" : "Connect and analyse"}
          </button>
          {!chosenSc && loaded && searchConsole.length === 0 ? (
            <button
              type="button"
              className="btn-secondary"
              style={{ marginLeft: 10 }}
              onClick={() => router.push(`/onboarding/scan?website_id=${websiteId}`)}
            >
              Skip for now
            </button>
          ) : null}
          {error ? <p className="error">{error}</p> : null}
        </>
      )}
    </main>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<main className="wrap wizard">Loading…</main>}>
      <ChooseProperties />
    </Suspense>
  );
}
