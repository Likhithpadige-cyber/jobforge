import localFont from "next/font/local";

// Variable WOFF2 files are served locally and preloaded once per family.
export const inter = localFont({
  src: "./fonts/Inter.woff2", weight: "400 600", style: "normal",
  display: "swap", preload: true, variable: "--font-inter",
  fallback: ["sans-serif"], adjustFontFallback: "Arial",
});
export const sora = localFont({
  src: "./fonts/Sora.woff2", weight: "600 800", style: "normal",
  display: "swap", preload: true, variable: "--font-sora",
  fallback: ["sans-serif"], adjustFontFallback: "Arial",
});
export const fontVariables = `${inter.variable} ${sora.variable}`;
