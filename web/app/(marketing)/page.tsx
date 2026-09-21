import Link from "next/link";

import { Eyebrow } from "./Eyebrow";
import { GlobeLayer } from "./GlobeLayer";
import { Orbit } from "./Orbit";

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
        <section className="mk-shell mk-hero">
          <div>
            <Eyebrow />
            <h1 className="mk-h1">
              Turn your online presence into growth.
            </h1>
            <p className="mk-lede">
              Connect your website once. We bring your Google data together,
              explain what it means, and tell you what to do next — across
              Search, Technical, Content and Analytics.
            </p>
            <div className="mk-actions">
              <Link href="/onboarding" className="mk-cta">Get started →</Link>
              <Link href="/login" className="mk-cta mk-cta--ghost">Sign in</Link>
            </div>

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
          </div>

          <div className="mk-figure">
            {/* Three layers: the dot-Earth, the orbital cage around it, and
                the device in front. */}
            <GlobeLayer />
            <Orbit />
            <Preview />
          </div>
        </section>

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
function Preview() {
  const pillars = [
    ["Search", 78],
    ["Technical", 74],
    ["Content", 68],
    ["Analytics", 69],
  ] as const;

  const nav = [
    "Overview", "Search", "Content", "Technical", "Analytics", "Plan",
  ] as const;

  return (
    <div className="mk-device">
      <div className="mk-device-tilt">
        {/* Thickness, glow and bezel: what makes it read as a physical object
            on a desk rather than a rectangle pasted onto the page. */}
        <div className="mk-device-glow" aria-hidden="true" />
        <div className="mk-device-shadow" aria-hidden="true" />
        <div className="mk-device-edge" aria-hidden="true" />
        <div className="mk-ipad">
          {/* Hardware. Landscape with the camera on the long top edge, so:
              power on the top edge, volume pair and speakers on the near
              (left) edge, speakers and USB-C on the far edge. */}
          <span className="mk-hw mk-hw--power" aria-hidden="true" />
          <span className="mk-hw mk-hw--vol-up" aria-hidden="true" />
          <span className="mk-hw mk-hw--vol-down" aria-hidden="true" />
          <span className="mk-grille mk-grille--left" aria-hidden="true" />
          <span className="mk-grille mk-grille--right" aria-hidden="true" />
          <span className="mk-port" aria-hidden="true" />
          <div className="mk-ipad-chamfer" aria-hidden="true" />
          <div className="mk-ipad-bezel">
            <span className="mk-ipad-cam" aria-hidden="true" />
            <div className="mk-screen">
              <div className="mk-screen-top" aria-hidden="true" />
              <div className="mk-screen-sheen" aria-hidden="true" />

              <div className="mk-app">
                <aside className="mk-side">
                  <div className="mk-side-brand">Visibility Hub</div>
                  <ul className="mk-side-nav">
                    {nav.map((item, i) => (
                      <li key={item} className={i === 0 ? "is-active" : undefined}>
                        {item}
                      </li>
                    ))}
                  </ul>
                  <div className="mk-side-foot">example.com</div>
                </aside>

                <div className="mk-main">
                  <header className="mk-main-head">
                    <div>
                      <h3>Overview</h3>
                      <p>Your online visibility at a glance.</p>
                    </div>
                    <span className="mk-sample">Example — not real data</span>
                  </header>

                  <div className="mk-main-top">
                    <div className="mk-ring">
                      <div className="mk-ring-label">
                        <b>72</b>
                        <span>Visibility</span>
                      </div>
                    </div>
                    <div className="mk-cards">
                      {pillars.map(([label, value]) => (
                        <div key={label} className="mk-mini">
                          <span>{label}</span>
                          <em><b>{value}</b><i>/ 100</i></em>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="mk-ops">
                    <p>This week&rsquo;s plan</p>
                    {/* Four, because the plan is four things — same as the
                        copy below and the same as what the product builds. */}
                    <ol>
                      <li><em>1</em> Improve meta titles on key pages <span className="mk-tag">Content</span></li>
                      <li><em>2</em> Fix pages returning redirects <span className="mk-tag">Technical</span></li>
                      <li><em>3</em> Expand thin pages that already rank <span className="mk-tag">Search</span></li>
                      <li><em>4</em> Add descriptions to 12 pages missing them <span className="mk-tag">Content</span></li>
                    </ol>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
