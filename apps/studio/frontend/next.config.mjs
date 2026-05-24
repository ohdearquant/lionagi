/** @type {import('next').NextConfig} */
const nextConfig = {
  async redirects() {
    return [
      // ADR-0032: remove this one-release compatibility redirect in the next release.
      { source: "/admin", destination: "/admin/health", permanent: true },
    ];
  },
};

export default nextConfig;
