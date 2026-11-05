import type { Config } from "tailwindcss";

/** Semantic roles resolve to the six primitives in app/globals.css only. */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  future: { respectDefaultRingColorOpacity: true },
  corePlugins: { preflight: false },
  theme: {
    colors: {
      transparent: "transparent", current: "currentColor", inherit: "inherit",
      canvas: "var(--jf-midnight)", surface: "var(--jf-deep-teal)",
      inset: "var(--jf-midnight)", edge: "var(--jf-muted-teal)",
      control: "var(--jf-aqua)", ink: "var(--jf-cream)",
      "ink-soft": "var(--jf-aqua)", accent: "var(--jf-aqua)",
      action: "var(--jf-orange)", "action-ink": "var(--jf-midnight)",
      "action-hover": "var(--jf-cream)", focus: "var(--jf-aqua)",
      success: "var(--jf-aqua)", attention: "var(--jf-cream)",
      danger: "var(--jf-cream)", info: "var(--jf-aqua)",
    },
    ringColor: { DEFAULT: "var(--jf-aqua)" },
    ringOffsetColor: { DEFAULT: "var(--jf-midnight)" },
    borderColor: ({ theme }) => ({ ...theme("colors"), DEFAULT: "var(--jf-muted-teal)" }),
    boxShadow: { none: "none", raised: "0 8px 0 var(--jf-midnight)" },
    extend: {
      fontFamily: {
        sans: [
          "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont",
          "Segoe UI", "Inter", "Helvetica Neue", "Arial", "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      fontSize: {
        // A deliberate scale rather than Tailwind's defaults everywhere.
        micro: ["0.6875rem", { lineHeight: "1rem", letterSpacing: "0.04em" }],
        metric: ["1.875rem", { lineHeight: "2.125rem", letterSpacing: "-0.025em" }],
        display: ["1.375rem", { lineHeight: "1.75rem", letterSpacing: "-0.015em" }],
      },
      borderRadius: {
        card: "1rem",
        chip: "0.625rem",
      },
      spacing: {
        sidebar: "15rem", // 240px
      },
    },
  },
  plugins: [],
};

export default config;
