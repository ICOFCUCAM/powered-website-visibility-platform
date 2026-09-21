"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { writeToken } from "@/lib/session";

export default function LoginPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Verify the token against the API before storing it, so a bad paste
      // fails here rather than on every subsequent screen.
      await api.me(token.trim());
      writeToken(token.trim());
      router.push("/onboarding");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "We couldn't reach the API. Is it running?",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wrap">
      <p className="eyebrow">Sign in</p>
      <h1>Welcome back</h1>
      <p className="sub">
        M1 accepts an access token directly. Supabase Auth — Google sign-in and
        email/password — replaces this form in M2.
      </p>

      <form className="card" onSubmit={signIn}>
        <label htmlFor="token">Access token</label>
        <input
          id="token"
          name="token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="eyJhbGciOi..."
          autoComplete="off"
          spellCheck={false}
        />
        <div style={{ marginTop: 16 }}>
          <button type="submit" disabled={busy || token.trim().length === 0}>
            {busy ? "Checking…" : "Continue"}
          </button>
        </div>
        {error ? <p className="error">{error}</p> : null}
      </form>
    </main>
  );
}
