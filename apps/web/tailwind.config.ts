import type { Config } from "tailwindcss";

/** JobForge's light, warm workspace palette. */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        charcoal: "#20243A",
        graphite: "#303656",
        canvas: "#FAFAF7",     // warm workspace background
        surface: "#FFFFFF",    // cards
        muted: "#F5F2EB",      // cream surfaces
        edge: "#E8E8E5",
        ink: "#24283B",
        "ink-soft": "#687082",
        violet: { DEFAULT: "#6457B5", soft: "#F0EDFA", deep: "#4D4291" },
        brass: {
          DEFAULT: "#C9AE78",
          soft: "#F6F0E4",
          deep: "#8B7040",
        },
        // Meaning-bearing only. Muted on purpose.
        signal: {
          strong: "#398468",   // soft emerald success
          review: "#A97A2F",   // needs review / borderline
          risk: "#A34A42",     // security / failure
          info: "#4A6785",     // informational
        },
      },
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
      boxShadow: {
        // One shadow, used sparingly. Structure comes from borders.
        raised: "0 10px 32px rgba(34, 39, 65, 0.045), 0 1px 3px rgba(34, 39, 65, 0.035)",
      },
      spacing: {
        sidebar: "15rem", // 240px
      },
    },
  },
  plugins: [],
};

export default config;
