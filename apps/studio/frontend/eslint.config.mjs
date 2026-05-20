import nextConfig from "eslint-config-next";
import prettierConfig from "eslint-config-prettier";

const config = [
  ...nextConfig,
  {
    ...prettierConfig,
    rules: {
      ...prettierConfig.rules,
      "@next/next/no-img-element": "off",
    },
  },
];

export default config;
