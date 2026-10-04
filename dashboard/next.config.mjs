/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Server Actions carry authenticated mutations (pause, end, reroll...). They
  // need a longer budget than the default on cold serverless starts.
  experimental: {
    serverActions: {
      bodySizeLimit: "1mb",
      allowedOrigins: process.env.CSRF_TRUSTED_ORIGINS?.split(",").filter(Boolean) ?? [],
    },
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains; preload",
          },
        ],
      },
    ];
  },
};

export default nextConfig;