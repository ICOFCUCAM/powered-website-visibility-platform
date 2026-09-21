import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The browser never talks to Postgres and never holds a service key. Every
  // read goes through the API, where tenancy, plan limits and metering are
  // enforced in exactly one place (docs/02-api.md).
  env: {
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1",
  },
};

export default config;
