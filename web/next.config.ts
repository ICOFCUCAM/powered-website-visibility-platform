import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Trace the modules the server actually reaches and emit a self-contained
  // `server.js`, so a runtime image needs no node_modules at all. Vercel
  // ignores this and builds its own way; it exists for the container host,
  // where carrying node_modules into the running image costs hundreds of
  // megabytes on a box that does not have them to spare.
  //
  // The two things tracing deliberately leaves out are `public/` and
  // `.next/static`. A Dockerfile that forgets either produces a site that
  // renders with no images and no CSS — which looks like a broken build and
  // is really a missing COPY.
  output: "standalone",
  // The browser never talks to Postgres and never holds a service key. Every
  // read goes through the API, where tenancy, plan limits and metering are
  // enforced in exactly one place (docs/02-api.md).
  //
  // Inlined at build time, not read at runtime: the bundle the browser gets
  // has the value baked in. So it must name an address that outlives any one
  // deployment — a project's permanent domain, never a per-deployment
  // hostname, which changes the next time the API is deployed and takes the
  // already-built front end down with it.
  env: {
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1",
  },
};

export default config;
