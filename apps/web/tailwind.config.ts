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
      canvas: "var(--jf-cream)", surface: "var(--jf-deep-teal)",
      inset: "var(--jf-midnight)", edge: "var(--jf-muted-teal)",
      control: "var(--ui-control)", ink: "var(--ui-ink)",
      "ink-soft": "var(--ui-ink-soft)", accent: "var(--ui-accent)",
      action: "var(--jf-orange)", "action-ink": "var(--jf-midnight)",
      "action-hover": "var(--jf-midnight)", focus: "var(--ui-focus)",
      success: "var(--ui-success)", attention: "var(--ui-attention)",
      danger: "var(--ui-danger)", info: "var(--ui-info)",
    },
    ringColor: { DEFAULT: "var(--ui-focus)" },
    ringOffsetColor: { DEFAULT: "var(--ui-focus-gap)" },
    borderColor: ({ theme }) => ({ ...theme("colors"), DEFAULT: "var(--jf-muted-teal)" }),
    boxShadow: { none: "none", raised: "none" },
    fontFamily: {
      sans: ["var(--font-inter)", "sans-serif"],
      display: ["var(--font-sora)", "sans-serif"],
    },
    extend: {
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
