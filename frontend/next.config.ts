import type { NextConfig } from "next";

// Server-side only (not NEXT_PUBLIC): where the FastAPI backend lives. Same-origin
// rewrite keeps the httpOnly session cookie first-party and avoids CORS (contract §2).
const API_ORIGIN = process.env.ANTICIPA_API_ORIGIN ?? "http://localhost:8000";

// ponytail: scaffolder enabled cacheComponents/partialPrefetching; dropped — every page is
// authenticated, per-user, dynamic data, so PPR/caching adds Suspense constraints, no benefit.
const nextConfig: NextConfig = {
  devIndicators: false, // keeps the dev badge out of screenshots
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
