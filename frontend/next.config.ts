import type { NextConfig } from "next";

// Locally the backend is reachable at http://127.0.0.1:8000. Override with
// BACKEND_URL when starting development or building the production frontend.
const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Private development guidance stays local; do not let Next.js generate
  // public AGENTS.md or CLAUDE.md files when the dev server starts.
  agentRules: false,
  // Keep hot-reload artifacts separate from production builds so validation can
  // run while the local app remains available.
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  async rewrites() {
    return [
      {
        source: "/api_proxy/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Content-Type", value: "application/javascript; charset=utf-8" },
          { key: "Content-Security-Policy", value: "default-src 'self'; script-src 'self'" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
    ];
  },
};

export default nextConfig;
