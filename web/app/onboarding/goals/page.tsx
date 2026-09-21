"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { Stepper } from "@/components/Stepper";
import { api, ApiError, type GoogleConnection, type KeyEvent, type Website } from "@/lib/api";
import { buildSteps, hasAnalyticsScope } from "@/lib/onboarding";
import { readToken } from "@/lib/session";

/**
 * The one question the platform genuinely cannot answer for the customer.
 *
 * GA4 key events are named by whoever set the property up — `generate_lead`,
 * `form_submit_2`, `donate`. Nothing in the name says which one means "an
 * enquiry" for this business, so we ask. Until they answer, outcomes stay
 * absent rather than becoming a conversion rate built on a guess.
 */

const KINDS = [
  { value: "contact", label: "An enquiry or contact" },
  { value: "purchase", label: "A purchase" },
  { value: "donation", label: "A donation" },
  { value: "signup", label: "A sign-up" },
  { value: "other", label: "Something else" },
] as const;

function ChooseGoal() {
  const router = useRouter();
  const params = useSearchParams();
  const websiteId = params.get("website_id");

  const [website, setWebsite] = useState<Website | null>(null);
  const [connection, setConnection] = useState<GoogleConnection | null>(null);
  const [events, setEvents] = useState<KeyEvent[]>([]);
  const [chosen, setChosen] = useState<string | null>(null);
  const [kind, setKind] = useState<string>("contact");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const next = useCallback(() => {
    router.push(`/onboarding/scan?website_id=${websiteId}`);
  }, [router, websiteId]);

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
      setConnection(connections.find((c) => c.status === "active") ?? null);

      const keyEvents = await api.keyEvents(token, websiteId);
      setEvents(keyEvents);
      // A custom event is far more likely to be the business's own goal than
      // a GA4 built-in like `scroll`, so pre-select one if there is exactly one.
      const custom = keyEvents.filter((e) => e.custom);
      if (custom.length === 1) setChosen(custom[0].event_name);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Analytics was never connected. Not an error — just skip the step.
        next();
        return;
      }
      setError(err instanceof ApiError ? err.message : "We couldn't reach the API.");
    } finally {
      setLoaded(true);
    }
  }, [router, websiteId, next]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(skip: boolean) {
    const token = readToken();
    if (!token || !websiteId) return;
    setBusy(true);
    setError(null);
    try {
      if (!skip && chosen) {
        await api.setGoals(token, websiteId, [
          { event_name: chosen, goal_kind: kind, is_primary: true },
        ]);
      }
      next();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn't save that.");
      setBusy(false);
    }
  }

  const steps = buildSteps({
    website,
    connection,
    searchConsoleLinked: true,
    analyticsLinked: true,
    analyticsAvailable: hasAnalyticsScope(connection),
    synced: false,
  });

  return (
    <main className="wrap wizard">
      <p className="eyebrow">Setup · step 4 of 5</p>
      <h1>What counts as a result?</h1>
      <p className="sub">
        Analytics records events by name. Tell us which one means someone got
        in touch, and we&apos;ll show you which pages actually produce them —
        not just which ones get traffic.
      </p>

      <Stepper steps={steps} />

      {!loaded ? (
        <p className="empty">Reading your Analytics setup…</p>
      ) : events.length === 0 ? (
        <div className="panel">
          <h2>No key events yet</h2>
          <p>
            This Analytics property doesn&apos;t mark any events as key events.
            You can set one up in Analytics and come back — everything else
            works without it.
          </p>
          <div style={{ marginTop: 14 }}>
            <button type="button" onClick={() => save(true)} disabled={busy}>
              Continue
            </button>
          </div>
        </div>
      ) : (
        <>
          {events.map((event) => (
            <label className="choice" key={event.event_name} htmlFor={event.event_name}>
              <input
                id={event.event_name}
                type="radio"
                name="goal"
                checked={chosen === event.event_name}
                onChange={() => setChosen(event.event_name)}
              />
              <span>
                <span className="choice__uri">
                  {event.event_name}
                  {event.custom ? <span className="choice__badge">yours</span> : null}
                </span>
                <span className="choice__why">
                  {event.custom
                    ? "Set up for this website"
                    : "A standard Analytics event"}
                </span>
              </span>
            </label>
          ))}

          {chosen ? (
            <div className="panel" style={{ marginTop: 16 }}>
              <h2>What is it?</h2>
              <select
                value={kind}
                onChange={(e) => setKind(e.target.value)}
                style={{
                  marginTop: 8,
                  padding: "9px 11px",
                  font: "inherit",
                  color: "var(--ink)",
                  background: "var(--bg)",
                  border: "1px solid var(--line)",
                  borderRadius: 7,
                }}
              >
                {KINDS.map((k) => (
                  <option key={k.value} value={k.value}>
                    {k.label}
                  </option>
                ))}
              </select>
            </div>
          ) : null}

          <div style={{ marginTop: 18, display: "flex", gap: 10 }}>
            <button type="button" onClick={() => save(false)} disabled={busy || !chosen}>
              {busy ? "Saving…" : "Continue"}
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => save(true)}
              disabled={busy}
            >
              I&apos;m not sure yet
            </button>
          </div>
          <p className="note">
            You can change this later. If you skip it we&apos;ll show your
            traffic, and say plainly that outcomes aren&apos;t set up rather
            than guessing at a number.
          </p>
        </>
      )}
      {error ? <p className="error">{error}</p> : null}
    </main>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<main className="wrap wizard">Loading…</main>}>
      <ChooseGoal />
    </Suspense>
  );
}
