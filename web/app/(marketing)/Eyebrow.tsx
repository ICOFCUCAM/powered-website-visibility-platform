"use client";

import { useEffect, useState } from "react";

import { viewerPlace } from "@/lib/viewer";

/**
 * The line above the headline, which names where the visitor is when the
 * browser is willing to say.
 *
 * Server-rendered as the generic line and upgraded after mount — never the
 * other way round. The page is a static file, so there is no request-time
 * personalisation to be had, and rendering a place name on the server would
 * be a hydration mismatch as well as a lie about how it was obtained.
 */
export function Eyebrow() {
  const [place, setPlace] = useState<string | null>(null);

  useEffect(() => setPlace(viewerPlace()), []);

  return (
    <p className="mk-eyebrow">
      {place ? `Be found in ${place}. And everywhere else.` : "Be found. Everywhere."}
    </p>
  );
}
