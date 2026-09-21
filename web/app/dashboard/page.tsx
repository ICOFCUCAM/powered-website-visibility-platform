"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Me, type Website } from "@/lib/api";
import { clearToken, readToken } from "@/lib/session";

export default function DashboardPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [websites, setWebsites] = useState<Website[] | null>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) {
      router.replace("/login");
      return;
    }
    try {
      const [profile, list] = await Promise.all([
        api.me(token),
        api.listWebsites(token),
      ]);
      setMe(profile);
      setWebsites(list);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        router.replace("/login");
        return;
      }
      setError(
        err instanceof ApiError ? err.message : "We couldn't reach the API.",
      );
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function addWebsite(event: React.FormEvent) {
    event.preventDefault();
    const token = readToken();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await api.createWebsite(token, url.trim());
      setUrl("");
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "We couldn't add that website.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wrap">
      <p className="eyebrow">Visibility Hub</p>
      <h1>Your websites</h1>
      <p className="sub">{me ? me.email : "Loading…"}</p>

      <form className="card" onSubmit={addWebsite}>
        <div className="row">
          <div>
            <label htmlFor="url">Website address</label>
            <input
              id="url"
              name="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="example.com"
              autoComplete="url"
              spellCheck={false}
            />
          </div>
          <button type="submit" disabled={busy || url.trim().length === 0}>
            {busy ? "Adding…" : "Add website"}
          </button>
        </div>
        <p className="meta" style={{ marginBottom: 0 }}>
          Type it however you like — example.com, www.example.com or the full
          address. We work out what you meant.
        </p>
        {error ? <p className="error">{error}</p> : null}
      </form>

      {websites === null ? (
        <p className="empty">Loading…</p>
      ) : websites.length === 0 ? (
        <p className="empty">
          No websites yet. Add one above to get started.
        </p>
      ) : (
        <ul className="list">
          {websites.map((w) => (
            <li className="item" key={w.id}>
              <div>
                <div className="domain">{w.domain}</div>
                <div className="meta">{w.canonical_url}</div>
              </div>
              <span className="pill">
                {w.ownership_verified ? w.status : "not verified"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
