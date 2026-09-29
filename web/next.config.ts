import type { NextConfig } from "next";

/**
 * Cloudflare Pages static hosting.
 * - output:'export' → writes `out/` (see wrangler.toml pages_build_output_dir)
 * - images unoptimized (no Next Image Optimization server on Pages)
 * - Route Handlers under app/api are stashed by scripts/build-static.mjs during build
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: "export",
  images: { unoptimized: true },
  trailingSlash: false,
};

export default nextConfig;
