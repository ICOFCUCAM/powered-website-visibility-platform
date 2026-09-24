import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Every route here prerenders: no route handlers, no middleware, no
  // dynamic segments, no server-side fetching. So the build can emit plain
  // files and the thing serving them needs no Node at all — a few megabytes
  // of web server instead of a language runtime and a dependency tree, which
  // on a host that also runs seven Python processes is the difference
  // between fitting and not.
  //
  // The cost is that `headers()` below would do nothing, so the response
  // headers this site used to get from its host now come from the platform's
  // static runtime. If a route ever needs to render on a server, this line
  // comes out and the image grows.
  output: "export",
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
