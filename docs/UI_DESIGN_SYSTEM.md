# JobForge design system — final branding and typography

2026-11-05 refinement of `09c5a38` / `b1120c1`, on existing `dev`. The user
approved a Cream canvas, preserved Deep Teal panels and the attached ribbon
reference. This supersedes the earlier all-dark canvas. M15 reports and the
production **NO-GO** conclusion are unchanged.

## Palette and surfaces

| Primitive | Hex | Use |
| --- | --- | --- |
| Warm Cream | `#EADCC8` | Main canvas; text on dark panels |
| Deep Teal | `#0F3D3E` | Sidebar, cards/pipeline/activity panels; supporting text on Cream |
| Muted Teal | `#2E6F73` | Borders and structural accents |
| Soft Aqua | `#7FB3B8` | Supporting text/highlights on dark panels |
| Burnt Orange | `#C6601F` | Selected navigation, actions, alert indicators |
| Midnight Black | `#0A0F14` | Main text on Cream; inset panels and dark hover states |

The six primitives live in `app/globals.css`. Semantic Tailwind roles resolve to
contextual `--ui-*` variables. The Cream canvas has Midnight main text and Deep
Teal supporting text, controls and focus. `.bg-surface`, `.bg-inset` and
`.theme-dark` scope Cream text, Aqua supporting text/controls and dark form
surfaces. Components keep their existing layout, grid, spacing and panel sizes;
contextual roles change paint without rewriting business components. The desktop
login retains its existing dark left panel and dark form card on the Cream page.

All listed pages inherit the Cream canvas: dashboard, applications, resumes,
activity, settings, feed, approvals, profile/facts, monitoring and alert views,
including review subroutes and fallback pages. Existing semantic status keys and
written labels are preserved. No backend/API, permission, session, calculation,
processing, navigation or submission behavior changed.

## Contrast and interactions

| Pair | Ratio | Use |
| --- | ---: | --- |
| Midnight / Cream | 14.27:1 | Normal page text |
| Deep Teal / Cream | 8.86:1 | Supporting text, fields and focus on Cream |
| Cream / Deep Teal | 8.86:1 | Normal panel text |
| Aqua / Deep Teal | 5.15:1 | Supporting panel text/controls |
| Aqua / Midnight | 8.29:1 | Supporting inset text/focus |
| Midnight / Orange | 4.67:1 | Normal action text |
| Orange / Cream | 3.06:1 | Action boundary / selected navigation indicator |
| Cream / Orange | 3.06:1 | Action outline only, not normal text |
| Muted Teal / Cream | 4.28:1 | Decorative borders, not Cream-text surfaces |

Cream text on Muted Teal is 4.28:1, below normal-text AA. As explicitly approved,
normal Cream text stays on Deep Teal instead. Aqua on Cream is 2.49:1; light
surfaces therefore use Deep Teal supporting text/focus, not Aqua body text.

Orange actions use Midnight text; hover uses Midnight with Cream text.
Aqua/Deep Teal control borders and two-pixel visible focus adapt to their surface,
with three-pixel separation. Disabled controls retain readable colors without
opacity. Status labels/glyphs remain; color is never the only signal. Keyboard
focus trapping, Escape, focus return and reduced-motion behavior are preserved.
There is no unapproved color/contrast pairing intentionally retained.

## Logo and fonts

See [brand provenance and asset inventory](brand/README.md). The original
reference is preserved. One shared SVG geometry and Sora ExtraBold outline
wordmark supply cream/teal lockups, login/sidebar branding, a header symbol and
vector favicon. Standalone SVG exports match the application paths. Only the
approved ribbon artwork uses opacity; normal UI surfaces remain flat and opaque.

Only Sora and Inter are authored design families. `next/font/local` loads two
self-hosted variable WOFF2 files with preload, swap and adjusted fallback metrics.
There are no runtime Google Fonts requests or network-dependent font builds.
The browser checks verify exactly two local font resources. Generic/system
fallback metrics supplied by Next.js remain available while fonts load; they are
not an additional designed/served font family. No zero-CLS performance claim is made.

- Sora 800: outlined logo wordmark; Sora 700: page headings and dashboard/pipeline
  metrics; Sora 600: section headings.
- Inter 400: body/forms/tables; Inter 500: navigation and labels; Inter 600: buttons.
- Existing size scale and responsive line heights remain. Diagnostic IDs use
  Inter/tabular numerals rather than introducing a third monospace family.

## Verification commands

Run from the repository root with Node 22.23.4 on PATH:

```sh
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3115 NEXT_TELEMETRY_DISABLED=1 \
  npm --prefix apps/web run build
npm --prefix apps/web run audit:colors -- --built
.venv/bin/ruff check tests/e2e/browser/conftest.py tests/e2e/browser/test_ui_theme.py
.venv/bin/ruff format --check tests/e2e/browser/conftest.py tests/e2e/browser/test_ui_theme.py
```

`audit:colors` scans UI TS/TSX/CSS/SVG, Tailwind config, public UI assets and
built CSS. It rejects off-palette paint, stock color utilities, gradients,
alpha utility shades and unreviewed served raster artwork. Source reference
rasters live outside the served UI. Browser paint checks include SVG geometry;
non-painted HTML/SVG-container default fill values are not treated as paint.

Browser verification uses the existing isolated synthetic demo and a separately
packaged production-mode frontend on 3116, behind the existing loopback router
on 3115. `/api/v1/*` goes to the healthy demo API on 8100. The frontend is built
with the exact preview origin above; a missing public API origin can compile a
localhost fallback, an existing M15 deployment concern. This preview is not a
production deployment or Docker-image/hosted-CI verification.

```sh
JOBFORGE_E2E_BASE_URL=http://127.0.0.1:3115 \
JOBFORGE_UI_EVIDENCE_DIR=/tmp/jobforge-brand-evidence \
LLM_MODE=replay SUBMISSION_MODE=dry_run EMAIL_TRANSPORT_MODE=fake \
.venv/bin/pytest -m e2e tests/e2e/browser \
  --junitxml=/tmp/jobforge-brand-browser.xml
```

For that run the owner had chosen a new password, so an optional
`JOBFORGE_E2E_OWNER_STATE` pointed to an existing private owner session. That
option has since been replaced: the suite no longer signs in as the seeded owner
at all. Each run creates its own `e2e-owner-<run>` and `e2e-reviewer-<run>`
(passwords generated in memory, passed only via the fixture's environment),
verifies their identity and the dry-run/live=false gate before any test action,
and retires both accounts when it ends. The seeded owner's password and TOTP
are never touched. Reviewer setup and the existing fresh
reviewer sign-in/out journey still exercise real authentication. Password and
environment fields are excluded from fixture representations in failure output.
State files/cookies are private temporary data and must not be committed.

Final exact results and commit hashes are recorded in the milestone changelog.
Screenshots cover login, dashboard, applications, approvals, profile/facts,
settings and the mobile dashboard/drawer at 1280×800 and 375×812.

## Final local results

| Check | Result |
| --- | --- |
| Frontend unit tests | 349 passed / 27 files, 5.73 seconds |
| TypeScript | Passed |
| ESLint | Passed, no warnings/errors; existing Next lint deprecation notice |
| Production frontend build | Passed with explicit preview API origin; 21 listed routes including SVG icon |
| Source + built paint inventory | Passed, 96 files (95 source/config/assets + one built CSS) |
| Combined browser suite | 26 passed, zero failed/skipped, 81.62 seconds |
| New browser page/state checks | 78 passed; zero axe violations or horizontal-overflow failures |
| Local font resources | Exactly two WOFF2 resources; computed Inter/Sora and requested weights verified |
| Python test lint/format | Passed, two files formatted |
| Tracked-file secrets / whitespace | Passed; baseline unchanged |

The 26 browser cases include all 17 existing M13 journeys and nine expanded
visual/accessibility cases. The 78 checks comprise 62 route/role/viewport
combinations, six login/error/404 checks, two form/dialog checks and eight
feedback/navigation checks. The normal-text threshold remains 4.5:1 and relevant
non-text contrast remains 3:1. No threshold, safety guard or failing rule was waived.

The reference hash and font hashes are public checksums, with narrow explanatory
inline scanner annotations. Font notice text is preserved with trailing spaces
normalized. No dependency lockfile, secret value or scanner baseline changed.
The owner password was not changed or reverified; a validated existing owner
session supplied test setup, while fresh reviewer authentication was exercised.

## Limits

Axe WCAG 2/2.1/2.2 A/AA, computed paint/font/background assertions, contrast unit
tests and focused keyboard/visual review are scoped evidence, not complete WCAG
certification. Chromium is tested; Safari/Firefox, screen-reader sessions,
forced-colors/native popup chrome and arbitrary user text are not exhaustively
verified. Text/edge antialiasing and the explicitly approved translucent logo
naturally produce derived raster pixels. Backend regression/load/soak, production
images and hosted Actions were not rerun for this visual refinement. Original
M15 production blockers remain outside scope. No real emails/applications,
push, deployment, cloud resources, unrelated container restart or project
license addition occurred. The refreshed local preview is left running.
