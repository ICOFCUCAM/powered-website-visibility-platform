"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";

/**
 * The globe is decorative, costs ~30KB and runs an animation loop, so it is
 * kept out of the initial bundle and never mounted on a phone — where it sits
 * almost entirely behind the device anyway, and where the battery is.
 *
 * `quality` steps the level of detail down on tablets, which is where the
 * ratio of pixels to CPU is worst.
 */
const Globe = dynamic(() => import("./Globe").then((m) => m.Globe), {
  ssr: false,
  loading: () => null,
});

export function GlobeLayer() {
  const [quality, setQuality] = useState<"high" | "med" | null>(null);

  useEffect(() => {
    const decide = () =>
      setQuality(
        window.innerWidth >= 1024 ? "high" : window.innerWidth >= 768 ? "med" : null,
      );
    decide();
    window.addEventListener("resize", decide);
    return () => window.removeEventListener("resize", decide);
  }, []);

  if (quality === null) return null;
  return (
    <div className="mk-globe" aria-hidden="true">
      <Globe quality={quality} />
    </div>
  );
}
