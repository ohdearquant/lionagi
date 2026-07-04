import type { Config } from "tailwindcss";

const config: Config = {
  // Cockpit theming architecture: dark is the unqualified :root default;
  // [data-theme="light"] is the override. index.html always stamps an
  // explicit data-theme attribute (never leaves it unset), so the `dark:`
  // variant below stays meaningful for the rare component that needs it.
  darkMode: ["selector", '[data-theme="dark"]'],
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          base: "var(--surface-base)",
          raised: "var(--surface-raised)",
          overlay: "var(--surface-overlay)",
        },
        content: {
          primary: "var(--content-primary)",
          secondary: "var(--content-secondary)",
          muted: "var(--content-muted)",
        },
        edge: {
          DEFAULT: "var(--edge-hairline)",
          hairline: "var(--edge-hairline)",
          strong: "var(--edge-strong)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          contrast: "var(--accent-contrast)",
        },
        status: {
          success: "var(--status-success)",
          running: "var(--status-running)",
          failure: "var(--status-failure)",
          pending: "var(--status-pending)",
        },
        rail: {
          discovery: "var(--rail-discovery)",
          judgement: "var(--rail-judgement)",
          analysis: "var(--rail-analysis)",
          planning: "var(--rail-planning)",
          production: "var(--rail-production)",
          retrospective: "var(--rail-retrospective)",
          universal: "var(--rail-universal)",
        },
        // Out of scope for this skin port (DESIGN-MAP.md §7 item 5) — no
        // cockpit-doc coverage; kept as-is pending a future decision.
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
      boxShadow: {
        "raised-soft": "var(--shadow-raised-soft)",
      },
      ringColor: {
        accent: "var(--accent)",
      },
      fontFamily: {
        data: ["var(--font-data)"],
        ui: ["var(--font-ui)"],
      },
      fontSize: {
        xs: "var(--t-xs)",
        sm: "var(--t-sm)",
        base: "var(--t-base)",
        md: "var(--t-md)",
        lg: "var(--t-lg)",
        xl: "var(--t-xl)",
      },
      letterSpacing: {
        meta: "var(--tracking-meta)",
      },
      transitionDuration: {
        press: "var(--motion-press)",
        micro: "var(--motion-micro)",
        modal: "var(--motion-modal)",
      },
      borderRadius: {
        DEFAULT: "6px",
      },
    },
  },
  plugins: [],
};

export default config;
