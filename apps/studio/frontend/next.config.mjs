import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Next.js 16 moved turbopack config out of experimental; manually wire the
  // next-intl/config alias since next-intl's plugin still writes experimental.turbo.
  // Remove once next-intl's plugin writes the top-level turbopack key natively
  // (it would then shadow this manual alias).
  turbopack: {
    resolveAlias: {
      "next-intl/config": "./i18n/request.ts",
    },
  },
  async redirects() {
    return [
      // ADR-0032: remove this one-release compatibility redirect in the next release.
      { source: "/admin", destination: "/admin/health", permanent: true },
    ];
  },
};

export default withNextIntl(nextConfig);
