import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class", '[data-theme="dark"]'],
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        /* Canonical design-token surfaces */
        surface: {
          base: "var(--surface-base)",
          raised: "var(--surface-raised)",
          overlay: "var(--surface-overlay)",
          /* Legacy aliases kept so existing pages still compile */
          nav: "var(--surface-nav)",
          input: "var(--surface-input)",
          "input-hover": "var(--surface-input-hover)",
        },
        /* Canonical content */
        content: {
          primary: "var(--content-primary)",
          secondary: "var(--content-secondary)",
          muted: "var(--content-muted)",
          inverse: "var(--content-inverse)",
        },
        /* Edge / borders — canonical names + legacy aliases */
        edge: {
          DEFAULT: "var(--edge-hairline)",
          hairline: "var(--edge-hairline)",
          strong: "var(--edge-strong)",
          /* Legacy names that existing pages reference */
          subtle: "var(--edge-subtle)",
        },
        /* Lion amber */
        accent: "var(--accent)",
        /* Text on accent fills — theme-invariant, like accent itself */
        "accent-contrast": "var(--accent-contrast)",
        /* Interactive — kept for existing pages */
        interactive: {
          primary: "var(--interactive-primary)",
          "primary-hover": "var(--interactive-primary-hover)",
          secondary: "var(--interactive-secondary)",
          "secondary-hover": "var(--interactive-secondary-hover)",
        },
        /* Status */
        status: {
          running: "var(--status-running)",
          success: "var(--status-success)",
          pending: "var(--status-pending)",
          failure: "var(--status-failure)",
          /* Legacy aliases used by existing pages */
          "success-bg": "var(--status-success-bg)",
          "running-bg": "var(--status-running-bg)",
          error: "var(--status-error)",
          "error-bg": "var(--status-error-bg)",
          warning: "var(--status-warning)",
          "warning-bg": "var(--status-warning-bg)",
          selected: "var(--status-selected)",
          "selected-bg": "var(--status-selected-bg)",
        },
        /* Role palette — kept for existing pages */
        role: {
          researcher: "var(--role-researcher)",
          implementer: "var(--role-implementer)",
          reviewer: "var(--role-reviewer)",
          critic: "var(--role-critic)",
          analyst: "var(--role-analyst)",
          architect: "var(--role-architect)",
          tester: "var(--role-tester)",
        },
      },
      fontFamily: {
        ui: "var(--font-ui)",
        data: "var(--font-data)",
      },
      boxShadow: {
        card: "var(--shadow-card)",
        "card-hover": "var(--shadow-card-hover)",
      },
      borderRadius: {
        DEFAULT: "6px",
      },
    },
  },
  plugins: [],
};

export default config;
