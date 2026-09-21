"use client";

import type { PeekResult } from "@/lib/api";

import { CLEAN, readable } from "./findings";

const PILLARS = [
  ["Search", 78],
  ["Technical", 74],
  ["Content", 68],
  ["Analytics", 69],
] as const;

const NAV = ["Overview", "Search", "Content", "Technical", "Analytics", "Plan"];

const EXAMPLE_PLAN = [
  ["Improve meta titles on key pages", "Content"],
  ["Fix pages returning redirects", "Technical"],
  ["Expand thin pages that already rank", "Search"],
  ["Add descriptions to 12 pages missing them", "Content"],
] as const;

/**
 * The iPad's display.
 *
 * Two states, and the difference between them is the whole point of the
 * page. With no scan it shows an example, and says so on its face. With a
 * scan it shows what the rules actually found on the visitor's own page —
 * and the disclaimer comes off, because there is no longer anything to
 * disclaim.
 *
 * The scores stay behind the example badge either way: one page cannot
 * produce a visibility score, and inventing one for the sake of a filled-in
 * dashboard is exactly what this product exists not to do.
 */
export function Screen({
  result,
  running,
}: {
  result: PeekResult | null;
  running: boolean;
}) {
  const found = result ? readable(result.findings) : [];
  const host = result ? hostOf(result.final_url) : "example.com";

  return (
    <div className="mk-device">
      <div className="mk-device-tilt">
        <div className="mk-device-glow" aria-hidden="true" />
        <div className="mk-device-shadow" aria-hidden="true" />
        <div className="mk-device-edge" aria-hidden="true" />
        <div className="mk-ipad">
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

              <div className={`mk-app${running ? " is-running" : ""}`}>
                <aside className="mk-side">
                  <div className="mk-side-brand">Visibility Hub</div>
                  <ul className="mk-side-nav">
                    {NAV.map((item, i) => (
                      <li key={item} className={i === 0 ? "is-active" : undefined}>
                        {item}
                      </li>
                    ))}
                  </ul>
                  <div className="mk-side-foot">{host}</div>
                </aside>

                <div className="mk-main">
                  <header className="mk-main-head">
                    <div>
                      <h3>{result ? "Your page" : "Overview"}</h3>
                      <p>
                        {result
                          ? `${found.length === 0 ? "No issues" : `${found.length} found`} on ${host}`
                          : "Your online visibility at a glance."}
                      </p>
                    </div>
                    {result ? (
                      <span className="mk-live">Live · your site</span>
                    ) : (
                      <span className="mk-sample">Example — not real data</span>
                    )}
                  </header>

                  {result ? (
                    <div className="mk-ops mk-ops--found">
                      <p>
                        {found.length === 0
                          ? "Checked"
                          : "What we found on this page"}
                      </p>
                      {found.length === 0 ? (
                        <p className="mk-clean">{CLEAN}</p>
                      ) : (
                        <ol>
                          {found.slice(0, 6).map((f, i) => (
                            <li key={f.key}>
                              <em>{i + 1}</em> {f.title}
                              <span className="mk-tag">{f.pillar}</span>
                            </li>
                          ))}
                        </ol>
                      )}
                      <p className="mk-more">
                        {result.checked} checks run on one page. The full audit
                        reads every page and your Google data.
                      </p>
                    </div>
                  ) : (
                    <>
                      <div className="mk-main-top">
                        <div className="mk-ring">
                          <div className="mk-ring-label">
                            <b>72</b>
                            <span>Visibility</span>
                          </div>
                        </div>
                        <div className="mk-cards">
                          {PILLARS.map(([label, value]) => (
                            <div key={label} className="mk-mini">
                              <span>{label}</span>
                              <em>
                                <b>{value}</b>
                                <i>/ 100</i>
                              </em>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="mk-ops">
                        <p>This week&rsquo;s plan</p>
                        <ol>
                          {EXAMPLE_PLAN.map(([text, pillar], i) => (
                            <li key={text}>
                              <em>{i + 1}</em> {text}
                              <span className="mk-tag">{pillar}</span>
                            </li>
                          ))}
                        </ol>
                      </div>
                    </>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}
