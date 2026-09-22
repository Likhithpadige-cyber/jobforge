/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return process.env.NODE_ENV === "development"
      ? [
          { source: "/api/v1/:path*", destination: "http://localhost:8000/api/v1/:path*" },
          { source: "/health", destination: "http://localhost:8000/health" },
        ]
      : [];
  },
  async headers() {
    // Baseline security headers (SECURITY.md §8.1). CSP is tightened once
    // the real page set exists.
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
