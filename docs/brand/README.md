# Approved JobForge brand assets

`approved-reference.png` is the user's supplied `ChatGPT Image Nov 5, 2026,
06_13_15 PM.png`, preserved byte-for-byte. It is a source reference, not a served
UI background or logo raster. Original SHA-256:
`7b1019e9fa443baf33f2026824c621aa2e7997f11220c25781026eed8acf6163`. <!-- pragma: allowlist secret -->

The shared ribbon paths in `apps/web/lib/brand.ts` were traced against that
reference: rounded orange upper ribbon and return, dark folded ends, narrow
vertical stem, translucent Aqua connecting ribbon and overlap. The complete
wordmark uses actual Sora ExtraBold 800 outlines, with native glyph aspect ratios
and -0.045em tracking. It is not a substitute symbol or a synthetic font weight.

- `components/Brand.tsx`: shared full SVG lockup and standalone symbol.
- `public/brand/jobforge-on-cream.svg`: complete portable SVG, Deep Teal wordmark
  and folds, approved Orange/Aqua ribbons.
- `public/brand/jobforge-on-teal.svg`: complete portable SVG, Cream wordmark and
  Midnight folded ends for visibility on Deep Teal.
- `app/icon.svg`: symbol-only SVG favicon/application icon, same ribbon paths.
- Sidebar, login and header reuse this geometry. SVG viewBoxes and
  `preserveAspectRatio="xMidYMid meet"` prevent stretching and clipping.

All vector paint uses the approved six-color palette. Ribbon opacity is the
explicitly requested logo-only exception to flat opaque UI paint: upper return
0.32, stem 0.75, connecting ribbon 0.82, overlap 0.30. Raster lighting/texture is
not reproduced as extra UI colors or gradients. Logos are exempt from text
contrast requirements; the wordmarks nevertheless have strong Cream/Deep Teal
contrast. The original raster remains untouched for comparison.

## Font provenance

Official Google Fonts sources, retrieved 2026-11-05:

- [Sora variable source](https://github.com/google/fonts/blob/main/ofl/sora/Sora%5Bwght%5D.ttf)
- [Inter variable source](https://github.com/google/fonts/blob/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf)

The local variable WOFF2 files retain all source glyphs. FontTools 4.60.2 was
used in a temporary conversion environment outside the repository: Sora weight
range 600–800, Inter range 400–600 with optical size fixed at 14, WOFF2 output.
No application Python/npm dependency was added. Sora 800 glyph outlines for the
logo were generated from the same official Sora variable font at weight 800.
Font-specific copyright/OFL notices are preserved beside the fonts. These are
third-party font notices, not a project license.

- `Inter.woff2`: 171,412 bytes; SHA-256 `574cde5b0a79460ecb18ddfb045617f00c0e28d1ad17259511150817481bcbfe` <!-- pragma: allowlist secret -->
- `Sora.woff2`: 37,400 bytes; SHA-256 `32c9b71d8d7df858cd20c50a739c7f6af12530e621d640b0d99dbf1f2bc0c780` <!-- pragma: allowlist secret -->
