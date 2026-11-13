import type { NextConfig } from "next";

// Server-side only (not NEXT_PUBLIC): where the FastAPI backend lives. Same-origin
// rewrite keeps the httpOnly session cookie first-party and avoids CORS (contract §2).
const API_ORIGIN = process.env.ANTICIPA_API_ORIGIN ?? "http://localhost:8000";

// ponytail: scaffolder enabled cacheComponents/partialPrefetching; dropped — every page is
// authenticated, per-user, dynamic data, so PPR/caching adds Suspense constraints, no benefit.
const nextConfig: NextConfig = {
  // Lets Playwright and CI builds use their own output dir while `npm run dev` keeps running (Next 16 locks .next/dev).
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  devIndicators: false, // keeps the dev badge out of screenshots
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
  // ponytail: no script-src. Next injects inline scripts, so a nonce based CSP needs proxy.ts work; these directives block
  // framing (clickjacking), plugins, base-tag and form-action hijacks without touching how pages load.
  async headers() {
    const csp = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'";
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
        ],
      },
    ];
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
