import type { NextConfig } from "next";

const apiProxyTarget =
  process.env.ARTICLE_AGENT_API_PROXY_TARGET?.trim().replace(/\/$/, "") ||
  (process.env.NODE_ENV === "development"
    ? "http://127.0.0.1:8000"
    : "http://backend:8000");

const nextConfig: NextConfig = {
  output: "standalone",
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
