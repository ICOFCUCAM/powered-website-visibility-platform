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
          Connect your website once. We bring your Google data together,
          explain what it means, and tell you what to do next.
        </p>
        <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
          <Link href="/onboarding">
            <button type="button">Get started</button>
          </Link>
          <Link href="/login">
            <button type="button" className="btn-secondary">
              Sign in
            </button>
          </Link>
        </div>
      </div>
    </main>
  );
}
