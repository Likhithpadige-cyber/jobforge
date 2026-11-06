import { BRAND_LAYERS, BRAND_VIEWBOX, BRAND_LOCKUP_VIEWBOX, BRAND_WORDMARK_PATH } from "@/lib/brand";

function RibbonArtwork() {
  return BRAND_LAYERS.map(layer => {
    const fill = layer.tone === "fold" ? "var(--brand-fold, var(--jf-deep-teal))" : `var(--jf-${layer.tone})`;
    return <path key={layer.name} data-brand-layer={layer.name} d={layer.d}
      fillOpacity={"opacity" in layer ? layer.opacity : 1} style={{ fill }} />;
  });
}

/** Shared reference geometry; aspect ratio is preserved at every size. */
export function BrandSymbol({ className = "h-6 w-6" }: { className?: string }) {
  return <svg viewBox={BRAND_VIEWBOX} preserveAspectRatio="xMidYMid meet" className={`brand-symbol ${className}`} data-brand-symbol aria-hidden="true" focusable="false">
    <RibbonArtwork />
  </svg>;
}

/** Complete SVG lockup: the wordmark uses actual Sora ExtraBold 800 outlines. */
export function Brand({ className = "", variant = "auto", size = "compact" }: {
  className?: string; variant?: "auto" | "cream" | "teal"; size?: "compact" | "card" | "corner";
}) {
  const sizes = { compact: "h-6", card: "h-7", corner: "h-9 sm:h-11" };
  return <svg viewBox={BRAND_LOCKUP_VIEWBOX} preserveAspectRatio="xMidYMid meet"
    role="img" aria-label="JobForge" focusable="false"
    className={`brand-lockup w-auto ${sizes[size]} ${variant === "auto" ? "" : `brand-on-${variant}`} ${className}`}>
    <RibbonArtwork />
    <path className="brand-wordmark" d={BRAND_WORDMARK_PATH} fill="currentColor" />
  </svg>;
}
