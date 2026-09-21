import Link from "next/link";

import { Hero } from "./Hero";

/**
 * The four scoring components, with the weights they actually carry
 * (db/migrations/0007_expansion_seams.sql, scoring_version 1.0.0). Real
 * weights rather than invented percentages: the page is selling a method,
 * and the method is the thing that happens to be true.
 */
const PILLARS = [
  {
    weight: "35%",
    name: "Search Performance",
    blurb:
      "Impressions, clicks and position from your own Search Console — not an estimate of them.",
    source: "Google Search Console",
  },
  {
    weight: "30%",
    name: "Technical Health",
    blurb:
      "What a crawler actually finds: status codes, redirects, indexability, speed on real visits.",
    source: "Our crawler + CrUX",
  },
  {
    weight: "25%",
    name: "Content Health",
    blurb:
      "Titles, descriptions, headings, thin and duplicated pages, measured page by page.",
    source: "Our crawler",
  },
  {
    weight: "10%",
    name: "Analytics Coverage",
    blurb:
      "Whether the measurement itself is sound — because a number you cannot trust is worse than none.",
    source: "Google Analytics 4 + crawl",
  },
] as const;

const LOOP = [
  ["Discover", "Connect Google once. We find your properties and match them to your site."],
  ["Diagnose", "A crawl and your Google data, read together, against one catalogue of issues."],
  ["Recommend", "Four things to do this week, ranked by what moves the score you are short on."],
  ["Fix", "Do them. Mark them done. Nothing is marked fixed on your say-so alone."],
  ["Measure", "The next scan re-scores and proves it — or tells you it came undone."],
] as const;

export default function Home() {
  return (
    <>
      <header className="mk-shell">
        <nav className="mk-nav">
          <Link href="/" className="mk-word" style={{ color: "inherit", textDecoration: "none" }}>
            Visibility Hub
          </Link>
          <ul className="mk-nav-links">
            <li><a href="#pillars">The score</a></li>
            <li><a href="#loop">How it works</a></li>
            <li><a href="#honest">Our rules</a></li>
          </ul>
          <div className="mk-nav-right">
            <Link href="/login" className="mk-signin">Sign in</Link>
            <Link href="/onboarding" className="mk-cta">Get started →</Link>
          </div>
        </nav>
      </header>

      <main>
        <Hero />

        <section id="pillars" className="mk-shell mk-section">
          <div className="mk-section-head">
            <h2 className="mk-h2">One score, and the four things inside it.</h2>
            <p>
              Not a black box with a number on it. These are the real weights
              the score is computed from, and every one of them traces back to
              a source you can check.
            </p>
          </div>
          <div className="mk-grid">
            {PILLARS.map((p) => (
              <article key={p.name} className="mk-pillar">
                <div className="mk-weight">{p.weight}</div>
                <h3>{p.name}</h3>
                <p>{p.blurb}</p>
                <div className="mk-source">{p.source}</div>
              </article>
            ))}
          </div>
        </section>

        <section id="loop" className="mk-shell mk-section">
          <div className="mk-section-head">
            <h2 className="mk-h2">It closes the loop.</h2>
            <p>
              Most tools stop at the diagnosis. The point of this one is the
              week after — whether the thing you did actually worked.
            </p>
          </div>
          <div className="mk-loop">
            {LOOP.map(([name, detail]) => (
              <div key={name} className="mk-beat">
                <b>{name}</b>
                <p>{detail}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="honest" className="mk-shell mk-section">
          <div className="mk-section-head">
            <h2 className="mk-h2">Rules we hold ourselves to.</h2>
          </div>
          <div className="mk-loop mk-loop--four">
            <div className="mk-beat">
              <b>No invented numbers</b>
              <p>
                Every figure comes from your own data. Where something is an
                estimate, it says so next to the number, not in a footnote.
              </p>
            </div>
            <div className="mk-beat">
              <b>Your Google stays yours</b>
              <p>
                We handle the technical complexity; you give Google consent.
                Tokens are encrypted and kept apart from everything else.
              </p>
            </div>
            <div className="mk-beat">
              <b>Findings are code, not guesses</b>
              <p>
                Detection is deterministic. A language model may explain and
                prioritise the evidence — it can never create it.
              </p>
            </div>
            <div className="mk-beat">
              <b>Leaving is one click</b>
              <p>
                Disconnect revokes the token at Google rather than dropping our
                copy. Delete your account and we hand back a receipt.
              </p>
            </div>
          </div>
        </section>

        <section className="mk-shell mk-close">
          <h2 className="mk-h2">Know what to do next.</h2>
          <p>
            Connect your site, and the first plan lands after the first scan.
          </p>
          <Link href="/onboarding" className="mk-cta">Get started →</Link>
        </section>
      </main>

      <footer className="mk-shell mk-foot">
        <div>
          Visibility Hub
          <br />
          Clarity for a more visible web
        </div>
        <div style={{ textAlign: "right" }}>
          More visibility.
          <br />
          A brighter tomorrow.
        </div>
      </footer>
    </>
  );
}

/**
 * A preview of the product, drawn rather than screenshotted so it stays true
 * as the real screen changes. The figures are illustrative and the card says
 * so on its face — a product whose first rule is "never invent a number"
 * cannot put unlabelled ones on its own front page.
 */
