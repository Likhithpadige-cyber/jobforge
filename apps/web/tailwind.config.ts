import type { Config } from "tailwindcss";

/**
 * JobForge design tokens.
 *
 * Palette is graphite and paper with a single warm brass accent. Semantic
 * colours are deliberately muted and used only to carry meaning that would
 * otherwise be invisible - never for decoration. The interface stays
 * predominantly charcoal, grey and white.
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        charcoal: "#1C1F24",   // sidebar — a shade deeper than the spec so the
                               // active item reads as raised rather than tinted
        graphite: "#2B3036",   // active nav item, sidebar raised surfaces
        canvas: "#F4F5F6",     // workspace background
        surface: "#FFFFFF",    // cards
        muted: "#ECEEF0",      // secondary surfaces, track fills
        edge: "#E2E5E9",       // borders
        ink: "#181A1E",        // primary text
        "ink-soft": "#686D76", // secondary text
        brass: {
          DEFAULT: "#C7A96B",  // the single accent
          soft: "#EFE7D5",
          deep: "#9C8248",
        },
        // Meaning-bearing only. Muted on purpose.
        signal: {
          strong: "#3F7A55",   // strong match / success
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
        card: "0.625rem",  // rounded, not pill-like
        chip: "0.375rem",
      },
      boxShadow: {
        // One shadow, used sparingly. Structure comes from borders.
        raised: "0 1px 2px rgba(24, 26, 30, 0.04), 0 2px 8px rgba(24, 26, 30, 0.04)",
      },
      spacing: {
        sidebar: "15rem", // 240px
      },
    },
  },
  plugins: [],
};

export default config;
