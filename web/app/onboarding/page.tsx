"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Stepper } from "@/components/Stepper";
import { api, ApiError } from "@/lib/api";
import { buildSteps } from "@/lib/onboarding";
import { readToken } from "@/lib/session";

export default function OnboardingStart() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!readToken()) router.replace("/login");
  }, [router]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const token = readToken();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      const website = await api.createWebsite(token, url.trim());
      router.push(`/onboarding/google?website_id=${website.id}`);
    } catch (err) {
      if (err instanceof ApiError && err.code === "website_already_exists") {
        // Already added is not a failure here — it is the same destination.
        const existing = (err.details as { website_id?: string }).website_id;
        if (existing) {
          router.push(`/onboarding/google?website_id=${existing}`);
          return;
        }
      }
      setError(
        err instanceof ApiError ? err.message : "We couldn't reach the API.",
      );
    } finally {
      setBusy(false);
    }
  }

  const steps = buildSteps({
    website: null,
    connection: null,
    searchConsoleLinked: false,
    analyticsLinked: false,
    analyticsAvailable: false,
    synced: false,
  });

  return (
    <main className="wrap wizard">
      <p className="eyebrow">Setup</p>
      <h1>Let&apos;s make your website visible</h1>
      <p className="sub">We&apos;ll connect your Google services for you.</p>

      <Stepper steps={steps} />

      <form className="card" onSubmit={submit}>
        <label htmlFor="url">What&apos;s your website?</label>
        <input
          id="url"
          name="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="example.com"
          autoComplete="url"
          spellCheck={false}
          autoFocus
        />
        <p className="note">
          Type it however you like. We work out the rest.
        </p>
        <div style={{ marginTop: 16 }}>
          <button type="submit" disabled={busy || url.trim().length === 0}>
            {busy ? "Checking…" : "Continue"}
          </button>
        </div>
        {error ? <p className="error">{error}</p> : null}
      </form>
    </main>
  );
}
