import Link from "next/link";

export default function Home() {
  return (
    <main className="wrap">
      <p className="eyebrow">Visibility Hub</p>
      <h1>Connect your website.</h1>
      <p className="sub">
        Understand your visibility. Know what to do next.
      </p>
      <div className="card">
        <p style={{ marginTop: 0 }}>
          M1 shell. Sign-in is wired to the API&apos;s token contract; Supabase
          Auth replaces the stub in M2, and nothing outside{" "}
          <code>lib/session.ts</code> changes when it does.
        </p>
        <Link href="/login">
          <button type="button">Sign in</button>
        </Link>
      </div>
    </main>
  );
}
