# Third-party notices

JobForge's own source code is licensed under the [MIT License](LICENSE). That
license covers only material authored for this project. The third-party
material below keeps its own license; the MIT License does not relicense it.

## Bundled in this repository

| Material | Location | License |
|---|---|---|
| Inter typeface (variable WOFF2, weights 400–600, optical size fixed at 14) | `apps/web/app/fonts/Inter.woff2` | SIL Open Font License 1.1 — [`Inter-OFL.txt`](apps/web/app/fonts/Inter-OFL.txt), © The Inter Project Authors |
| Sora typeface (variable WOFF2, weights 600–800) | `apps/web/app/fonts/Sora.woff2` | SIL Open Font License 1.1 — [`Sora-OFL.txt`](apps/web/app/fonts/Sora-OFL.txt), © The Sora Project Authors |
| JobForge wordmark glyph outlines | `apps/web/lib/brand.ts`, `apps/web/public/brand/*.svg` | Derived from Sora weight 800 outlines; the font's OFL 1.1 terms apply to the font software. Neither font declares a Reserved Font Name. |

Both fonts are converted subsets of the official Google Fonts sources
(provenance and checksums in [`docs/brand/README.md`](docs/brand/README.md)).
Under the OFL they are Modified Versions, distributed with their license text.

## Project assets

- `docs/brand/approved-reference.png` is the owner-supplied visual reference
  for the logo, generated with ChatGPT and kept byte-for-byte for comparison.
  It is not served by the application.
- `apps/web/app/icon.svg`, `apps/web/public/brand/*.svg` and the ribbon
  geometry in `apps/web/lib/brand.ts` are project artwork.
- `docs/screenshots/*.png` are captures of this application running against
  the fictional demo dataset ([details](docs/screenshots/README.md)).

## Installed dependencies (not vendored)

Python and npm dependencies are installed from their registries
(`requirements*.txt`, `apps/web/package-lock.json`) and are not copied into
this repository. Each is governed by its own license. A review on 2026-11-06
of the runtime dependency trees found permissive licenses (MIT, BSD, Apache-2.0,
ISC, PSF, Unlicense, MIT-CMU) and no GPL, AGPL, SSPL or BUSL packages, plus
these notable terms for anyone redistributing built images or bundles:

| Package | License | Note |
|---|---|---|
| `psycopg`, `psycopg-binary` (Python) | LGPL-3.0 | Used unmodified as a library. |
| `certifi` (Python) | MPL-2.0 | Used unmodified. |
| `@img/sharp-libvips-*` (npm, via Next.js image support) | LGPL-3.0-or-later | Platform-specific prebuilt binary, used unmodified. |
| `caniuse-lite` (npm, build-time browser data) | CC-BY-4.0 | Attribution: Browserslist / Can I Use contributors. |

Development-only tools (test runners, linters, Playwright, browsers) are not
part of the application and keep their own licenses. Re-run a license review
whenever dependencies change.

## Resume PDF font

`packages/services/rendering/fonts/NotoSans-Regular.ttf` is unmodified Noto Sans,
Copyright 2022 The Noto Project Authors, licensed under SIL OFL 1.1. The license is retained
in `packages/services/rendering/fonts/OFL.txt`. It is embedded in PDF exports.
