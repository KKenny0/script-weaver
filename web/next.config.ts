import type { NextConfig } from "next";

// The API proxy target can be overridden for isolated e2e runs; production
// and local development keep the default localhost:8000.
const apiTarget = process.env.SCRIPTWEAVER_API_TARGET || "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
