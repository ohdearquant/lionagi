import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{js,ts,jsx,tsx,mdx}", "./components/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          base: "var(--surface-base)",
          raised: "var(--surface-raised)",
          overlay: "var(--surface-overlay)",
          nav: "var(--surface-nav)",
          input: "var(--surface-input)",
          "input-hover": "var(--surface-input-hover)",
        },
        content: {
          primary: "var(--content-primary)",
          secondary: "var(--content-secondary)",
          muted: "var(--content-muted)",
          inverse: "var(--content-inverse)",
        },
        edge: {
          DEFAULT: "var(--edge-default)",
          subtle: "var(--edge-subtle)",
          strong: "var(--edge-strong)",
        },
        interactive: {
          primary: "var(--interactive-primary)",
          "primary-hover": "var(--interactive-primary-hover)",
          secondary: "var(--interactive-secondary)",
          "secondary-hover": "var(--interactive-secondary-hover)",
        },
        status: {
          success: "var(--status-success)",
          "success-bg": "var(--status-success-bg)",
          running: "var(--status-running)",
          "running-bg": "var(--status-running-bg)",
          error: "var(--status-error)",
          "error-bg": "var(--status-error-bg)",
          warning: "var(--status-warning)",
          "warning-bg": "var(--status-warning-bg)",
          selected: "var(--status-selected)",
          "selected-bg": "var(--status-selected-bg)",
        },
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
    },
  },
  plugins: [],
};

export default config;
