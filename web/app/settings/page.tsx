"use client";

/**
 * Settings: disconnecting Google, and ending the account.
 *
 * The last two items in the V1 definition of done, and the two teams
 * routinely defer past beta. Both were promised in the policy documents
 * Google's review reads, and a promise a customer cannot act on unaided is
 * not one they have been given.
 *
 * The deletion asks them to type their own email address. That is the
 * difference between a mis-click and a decision — and it lists what is about
 * to go first, because a dialogue that says "this cannot be undone" without
 * saying what "this" is asks somebody to accept a consequence they cannot
 * see.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Account,
  type Deletion,
  type GoogleConnection,
} from "@/lib/api";
import { counted, day } from "@/lib/format";
import { clearToken, readToken } from "@/lib/session";

export default function SettingsPage() {
  const router = useRouter();
  const [account, setAccount] = useState<Account | null>(null);
  const [connections, setConnections] = useState<GoogleConnection[]>([]);
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<Deletion | null>(null);

  const load = useCallback(async () => {
    const token = readToken();
    if (!token) return router.replace("/login");
    try {
      const [profile, google] = await Promise.all([
        api.account(token),
        api.connections(token),
      ]);
      setAccount(profile);
      setConnections(google);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        return router.replace("/login");
      }
      setError(
        err instanceof ApiError ? err.message : "We couldn't load your account.",
      );
    }
  }, [router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function disconnect(connectionId: string) {
    const token = readToken();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await api.disconnectGoogle(token, connectionId);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "We couldn't disconnect that.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function remove(event: React.FormEvent) {
    event.preventDefault();
    const token = readToken();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.deleteAccount(token, confirm);
      setDone(result);
      clearToken();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "We couldn't delete the account.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <main className="wrap">
        <h1 className="plan__title">Your account is deleted</h1>
        <p className="plan__summary">
          We removed {counted(done.rows_deleted, "record", "records")} and{" "}
          {counted(done.objects_deleted, "stored page", "stored pages")}, and
          revoked{" "}
          {counted(
            done.google_connections_revoked,
            "Google connection",
            "Google connections",
          )}
          . Nothing about your websites is left in the product.
        </p>
        {done.google_tokens_not_revoked > 0 ? (
          <p className="error">
            We destroyed {counted(done.google_tokens_not_revoked, "token", "tokens")}{" "}
            without reaching Google to revoke {done.google_tokens_not_revoked === 1
              ? "it"
              : "them"}
            . You can remove our access yourself at your Google account&apos;s
            security settings.
          </p>
        ) : null}
        {!done.sign_in_deleted ? (
          <p className="footnote">
            Your sign-in has not been removed ({done.sign_in_note}). Your data
            is gone, but the login still exists — contact support to have it
            closed.
          </p>
        ) : null}
        <p className="footnote">Reference: {done.receipt ?? "—"}</p>
      </main>
    );
  }

  if (!account) {
    return (
      <main className="wrap">
        {error ? <p className="error">{error}</p> : <p className="meta">Loading…</p>}
      </main>
    );
  }

  const active = connections.filter((c) => c.status !== "revoked");

  return (
    <main className="wrap">
      <p className="eyebrow">
        <Link href="/dashboard">← Dashboard</Link>
      </p>
      <h1 className="plan__title">Settings</h1>
      <p className="plan__sub">{account.email}</p>

      {error ? <p className="error">{error}</p> : null}

      <div className="section">
        <p className="section__title">Google</p>
        {active.length === 0 ? (
          <p className="empty">No Google account is connected.</p>
        ) : (
          <ul className="list">
            {active.map((connection) => (
              <li className="item" key={connection.id}>
                <span>
                  {connection.account}
                  <span className="meta"> · connected {day(connection.connected_at)}</span>
                </span>
                <button
                  className="btn-secondary"
                  disabled={busy}
                  onClick={() => disconnect(connection.id)}
                >
                  Disconnect
                </button>
              </li>
            ))}
          </ul>
        )}
        <p className="footnote">
          Disconnecting revokes our access with Google immediately. The search
          and analytics data we have already collected stays in your dashboard
          — delete your account below to remove that too.
        </p>
      </div>

      <div className="section danger">
        <p className="section__title">Delete your account</p>
        <p className="issue__summary">
          This removes everything, permanently, and cannot be undone:
        </p>
        <ul className="steps">
          {account.websites.map((domain) => (
            <li key={domain}>
              <strong>{domain}</strong> — every crawl, score, search figure and
              recommendation
            </li>
          ))}
          {account.google_connections > 0 ? (
            <li>
              {counted(
                account.google_connections,
                "Google connection",
                "Google connections",
              )}
              , revoked with Google
            </li>
          ) : null}
          <li>Every page we fetched and stored</li>
        </ul>
        {account.organizations_left > 0 ? (
          <p className="footnote">
            {counted(account.organizations_left, "account", "accounts")} you
            share with someone else will keep going without you.
          </p>
        ) : null}

        <form className="ask" onSubmit={remove}>
          <input
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            placeholder={`Type ${account.email} to confirm`}
            aria-label="Type your email address to confirm deletion"
            autoComplete="off"
          />
          <button
            type="submit"
            className="btn-danger"
            disabled={
              busy ||
              confirm.trim().toLowerCase() !== account.email.trim().toLowerCase()
            }
          >
            {busy ? "Deleting…" : "Delete everything"}
          </button>
        </form>
      </div>
    </main>
  );
}
