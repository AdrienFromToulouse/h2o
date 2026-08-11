import type { NextConfig } from "next";

const config: NextConfig = {
  // Every page reads live AWS through the signing proxy, so nothing here is
  // prerenderable at build time and pretending otherwise produces a build that
  // needs credentials.
  reactStrictMode: true,
};

export default config;
