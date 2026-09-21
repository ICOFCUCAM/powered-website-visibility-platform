"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { Stepper } from "@/components/Stepper";
import { api, ApiError, type GoogleConnection, type Website } from "@/lib/api";
import { buildSteps, hasAnalyticsScope } from "@/lib/onboarding";
import { readToken } from "@/lib/session";

/** Google's own error codes, in words a non-technical owner can act on. */
const ERRORS: Record<string, string> = {
  google_auth_failed:
    "We couldn't connect your Google account. Please try again.",
  google_state_expired:
    "That took a little too long, so we started over for safety. Please connect again.",
};

function ConnectGoogle() {
  const router = useRouter();
  const params = useSearchParams();
  const websiteId = params.get("website_id");
  const oauthError = params.get("error");

  const [website, setWebsite] = useState<Website | null>(null);
  const [connection, setConnection] = useState<GoogleConnection | null>(null);
  const [wantAnalytics, setWantAnalytics] = useState(true);
  const [error, setError] = useState<string | null>(
    oauthError ? (ERRORS[oauthError] ?? ERRORS.google_auth_failed) : null,
  );
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    if (!websiteId) {
      router.replace("/onboarding");
      return;
    }
    try {
      const [site, connections] = await Promise.all([
        api.website(token, websiteId),
        api.connections(token),
      ]);
      setWebsite(site);
      const active = connections.find((c) => c.status === "active") ?? null;
      setConnection(active);
      // Already connected — skip a step nobody needs to repeat.
      if (active) router.replace(`/onboarding/properties?website_id=${websiteId}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
    }
  }, [router, websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function connect() {
    const token = readToken();
    if (!token || !websiteId) return;
    setBusy(true);
    setError(null);
    try {
      const services = wantAnalytics
        ? ["search_console", "analytics"]
        : ["search_console"];
      const { authorization_url } = await api.startGoogleConnect(
        token,
        websiteId,
        services,
      );
      window.location.href = authorization_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
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
      <p className="eyebrow">Setup · step 2 of 5</p>
      <h1>Connect Google</h1>
      <p className="sub">
        This is how we see your real search data instead of guessing.
      </p>

      <Stepper steps={steps} />

      <div className="card">
        {/* Stated before the redirect, not after: the user consents on
            Google's screen, and they should know what they are agreeing to
            before they get there. */}
        <h2 style={{ fontSize: 16, marginTop: 0 }}>What we&apos;ll read</h2>
        <p style={{ fontSize: 14, color: "var(--ink-2)" }}>
          <strong>Search Console</strong> — the searches people use to find you:
          queries, clicks, impressions, positions, and which pages appear in
          Google. We never change anything in your Search Console account.
        </p>

        <label className="choice" htmlFor="analytics">
          <input
            id="analytics"
            type="checkbox"
            checked={wantAnalytics}
            onChange={(e) => setWantAnalytics(e.target.checked)}
          />
          <span>
            <span className="step__label">Also connect Google Analytics</span>
            <span className="choice__why">
              Lets us show which pages turn visitors into enquiries, not just
              which ones get traffic. You can add this later.
            </span>
          </span>
        </label>

        <div style={{ marginTop: 16 }}>
          <button type="button" onClick={connect} disabled={busy || !websiteId}>
            {busy ? "Opening Google…" : "Continue with Google"}
          </button>
        </div>
        {error ? <p className="error">{error}</p> : null}

        <p className="note">
          You can disconnect at any time, and we&apos;ll tell you exactly what
          happens to your data when you do.
        </p>
      </div>
    </main>
  );
}

export default function Page() {
  // useSearchParams needs a Suspense boundary during prerender.
  return (
    <Suspense fallback={<main className="wrap wizard">Loading…</main>}>
      <ConnectGoogle />
    </Suspense>
  );
}
