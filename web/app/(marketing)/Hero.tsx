"use client";

import Link from "next/link";
import { useState } from "react";

import { ApiError, type PeekResult, peek } from "@/lib/api";
import { viewerPlace } from "@/lib/viewer";
import { useEffect } from "react";

import { GlobeLayer } from "./GlobeLayer";
import { Orbit } from "./Orbit";
import { Screen } from "./Screen";

/**
 * The hero, which runs a real check.
 *
 * The device showed an example with a badge saying so, because a product
 * whose first rule is "never invent a number" cannot put unlabelled ones on
 * its own front page. This is the better answer: put the visitor's own site
 * on it. The badge comes off when there is nothing left to disclaim.
 */
export function Hero() {
  const [place, setPlace] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [state, setState] = useState<"idle" | "running">("idle");
  const [result, setResult] = useState<PeekResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setPlace(viewerPlace()), []);

  async function scan(event: React.FormEvent) {
    event.preventDefault();
    if (!url.trim() || state === "running") return;
    setState("running");
    setError(null);
    try {
      setResult(await peek(url));
    } catch (e) {
      setResult(null);
      setError(
        e instanceof ApiError
          ? e.message
          : "We couldn't reach that site.",
      );
    } finally {
      setState("idle");
    }
  }

  return (
    <section className="mk-shell mk-hero">
      <div>
        <p className="mk-eyebrow">
          {place
            ? `Be found in ${place}. And everywhere else.`
            : "Be found. Everywhere."}
        </p>
        <h1 className="mk-h1">Turn your online presence into growth.</h1>
        <p className="mk-lede">
          Connect your website once. We bring your Google data together,
          explain what it means, and tell you what to do next — across
          Search, Technical, Content and Analytics.
        </p>

        <form className="mk-scan" onSubmit={scan}>
          <input
            type="text"
            inputMode="url"
            autoComplete="url"
            placeholder="yoursite.com"
            aria-label="Your website address"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={state === "running"}
          />
          <button type="submit" className="mk-cta" disabled={state === "running"}>
            {state === "running" ? "Scanning…" : "Scan my site →"}
          </button>
        </form>
        <p className="mk-scan-note" role={error ? "alert" : undefined}>
          {error
            ? error
            : result
              ? "One page, checked live. Connect Google for the rest."
              : "One page, free, no account. Nothing is stored."}
        </p>

        <dl className="mk-stats">
          <div className="mk-stat">
            <b>4</b>
            <span>Weighted signals</span>
          </div>
          <div className="mk-stat">
            <b>1</b>
            <span>Plan a week</span>
          </div>
          <div className="mk-stat">
            <b>0</b>
            <span>Invented numbers</span>
          </div>
        </dl>

        <div className="mk-actions mk-actions--quiet">
          <Link href="/onboarding" className="mk-cta mk-cta--ghost">
            Get started →
          </Link>
          <Link href="/login" className="mk-signin">Sign in</Link>
        </div>
      </div>

      <div className="mk-figure">
        {/* Three layers: the dot-Earth, the orbital cage around it, and the
            device in front. */}
        <GlobeLayer />
        <Orbit />
        <Screen result={result} running={state === "running"} />
      </div>
    </section>
  );
}
