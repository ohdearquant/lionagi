import nextConfig from "eslint-config-next";
import prettierConfig from "eslint-config-prettier";
import jsxA11y from "eslint-plugin-jsx-a11y";

// Track 6 (#1020): nextConfig already registers jsx-a11y plugin with 6 rules.
// We merge the full recommended rule set into a new config object that also
// declares the plugin, so ESLint flat config can resolve rule references.
const jsxA11yExtension = {
  plugins: {
    "jsx-a11y": jsxA11y,
  },
  rules: {
    ...jsxA11y.configs.recommended.rules,
  },
};

const config = [
  // Spread nextConfig but replace the object that registers jsx-a11y with our
  // merged version so the plugin is declared exactly once.
  ...nextConfig.map((entry) => {
    if (entry.plugins && "jsx-a11y" in entry.plugins) {
      return {
        ...entry,
        plugins: {
          ...entry.plugins,
          "jsx-a11y": jsxA11y,
        },
        rules: {
          ...(entry.rules ?? {}),
          ...jsxA11y.configs.recommended.rules,
        },
      };
    }
    return entry;
  }),
  {
    ...prettierConfig,
    rules: {
      ...prettierConfig.rules,
      "@next/next/no-img-element": "off",
    },
  },
];

export default config;
