# JobForge dark design system

Post-M15 frontend redesign, 2026-11-05. Baseline: clean `dev` at `6ad4815`
(M15 assessment `a5c423a`). M15's production **NO-GO** remains in force.
No backend API, authentication policy, submission policy or infrastructure changed.

## Audit and implementation scope

The old theme extended Tailwind's stock colors with warm light surfaces, violet,
brass and four signal colors. Additional paint came from inline SVG fills,
selection CSS, gradients, translucent shadows/overlays, opacity animation, stock
slate/emerald/amber/red utilities, and multicolor login PNG/WebP artwork. Tailwind
preflight also supplied off-palette placeholder paint; its ring defaults generated
a translucent blue fallback even after the theme variables changed.

The replacement defines the complete color theme, disables stock preflight in
favor of a local reset, and enables `respectDefaultRingColorOpacity`. Both legacy
login rasters are removed. The login uses a quiet, decorative inline SVG whose
paint inherits the semantic tokens. No supplied palette-reference image was found;
the requested hex values are authoritative.

Coverage includes login/MFA controls, dashboard/pipeline, applications, job feed
and detail, approvals, resume list/review, cover letters, answers, submission
readiness/history, profile/facts, activity, settings, owner monitoring/alerts,
header/sidebar/mobile drawer, shared forms/tables/badges/notices and fallback
pages. Generic 404 and error boundaries also use the theme; error boundaries do
not display exception details. The account link's accessible name includes its
visible initials. Resume cards use an explicit mobile grid column to prevent
min-content overflow; the resume workspace keeps the shell's single page H1.

## Central tokens

All authored primitive paint lives in `apps/web/app/globals.css`. Tailwind utility
roles resolve to these variables in `apps/web/tailwind.config.ts`; components use
roles instead of literal colors. Do not extend the stock palette or add alpha
shades, gradients, colored raster decorations or opacity fades.

| Primitive | Hex | Semantic roles / use |
| --- | --- | --- |
| Midnight Black | `#0A0F14` | `canvas`, `inset`, `action-ink`; page, inputs, inset panels |
| Deep Teal | `#0F3D3E` | `surface`; sidebar, cards, layered panels |
| Muted Teal | `#2E6F73` | `edge`; decorative separators and panel outlines |
| Soft Aqua | `#7FB3B8` | `ink-soft`, `accent`, `control`, `focus`, `success`, `info` |
| Warm Cream | `#EADCC8` | `ink`, `action-hover`, `attention`, `danger` |
| Burnt Orange | `#C6601F` | `action`; primary actions, selected states and markers |

`transparent`, `currentColor` and `inherit` express no paint or inherited paint,
not extra palette colors. The generated CSS's fully transparent black is also
no paint. Native controls, placeholders, autofill, selection and scrollbars are
explicitly themed where the browser exposes styling controls. No third-party
visual widget is present; axe-core is test-only.

## Contrast and interaction rules

Ratios below use WCAG sRGB relative luminance, rounded to two decimals; assertions
use unrounded values. Normal text requires 4.5:1 and applicable meaningful
non-text boundaries/indicators require 3:1.

| Foreground / background | Ratio | Permitted use |
| --- | ---: | --- |
| Cream / Midnight | 14.27:1 | Normal text |
| Cream / Deep Teal | 8.86:1 | Normal text |
| Aqua / Midnight | 8.29:1 | Supporting text, controls, focus |
| Aqua / Deep Teal | 5.15:1 | Supporting text, controls, focus |
| Midnight / Orange | 4.67:1 | Normal text and icons on primary actions |
| Cream / Orange | 3.06:1 | Action boundary only; **not normal text** |
| Muted Teal / Midnight | 3.33:1 | Decorative separators; not text |
| Cream / Muted Teal | 4.28:1 | **Not normal text**; Muted Teal is not a text surface |
| Orange / Deep Teal | 2.90:1 | **Not a sole meaningful control boundary** |

Primary buttons and links use Midnight text on Orange, with a Cream boundary;
hover uses Cream with Midnight text. Disabled primary actions use Midnight and
Aqua, without opacity. Fields and non-primary button borders use Aqua. Muted
Teal panel outlines are structural decoration; labels and spacing identify the
content. Statuses always retain written labels, and checks retain their glyphs
and explanatory text. The legacy `brass` status-tone key remains a domain-facing
identifier; it now resolves to Cream and does not emit the old paint.

Keyboard focus uses a two-pixel Aqua outline, three-pixel offset and Midnight
separation. Dialog/drawer focus trapping, Escape dismissal and focus restoration
are preserved. Reduced-motion preferences suppress motion. Skeletons are static
instead of creating interpolated shades through opacity pulses.

There is no unresolved palette-versus-contrast conflict in the implemented
pairings. Cream-on-Orange normal text and Cream-on-Muted-Teal normal text are
explicitly disallowed; both are avoidable without adding a seventh color.

## Verification and maintenance

From the repository root, using Node 22:

```sh
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/web run audit:colors -- --built
npm --prefix apps/web audit
.venv/bin/ruff check tests/e2e/browser/test_ui_theme.py
.venv/bin/ruff format --check tests/e2e/browser/test_ui_theme.py
```

The unit suite includes measured contrast pairs, focus/dialog regressions, error
fallback behavior, exact primitive inventory, source scanning and negative color
fixtures. `audit:colors` scans app/components/lib, Tailwind config and public UI
assets; `--built` additionally checks production CSS, including framework defaults.
It rejects off-palette hex/functional/named paint, legacy/stock utilities, alpha
paint, gradients and unreviewed raster UI assets. Tests are outside Tailwind's
content set, so deliberately invalid audit fixtures cannot emit production paint.

The opt-in Playwright suite requires the existing isolated synthetic demo, its
safety guards, Chromium and `npm ci` (axe-core is a pinned development dependency):

```sh
JOBFORGE_E2E_BASE_URL=http://127.0.0.1:3115 \
JOBFORGE_UI_EVIDENCE_DIR=/tmp/jobforge-ui-evidence \
LLM_MODE=replay SUBMISSION_MODE=dry_run EMAIL_TRANSPORT_MODE=fake \
.venv/bin/pytest -m e2e tests/e2e/browser \
  --junitxml=/tmp/jobforge-ui-browser.xml
```

Port 3115 was a temporary loopback router for this assessment: `/api/v1/*` went
to the already-running demo API on 8100 and page/assets to an independently built
standalone Next server on 3116. The preview copy used
`NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3115` at build time, then copied
`.next/static` and `public` into `.next/standalone`, matching standalone asset
packaging. No Docker container was modified or restarted. This is a local
production-mode frontend run, not verification of a production Docker image,
hosted CI, real ingress or a deployment. Port overrides require an equivalently
isolated local setup; the suite's normal default remains the documented demo URL.

The new browser tests run axe WCAG 2 A/AA, 2.1 A/AA and 2.2 AA rule tags and
computed visible paint/overflow checks across every route available to each role,
at 1280×800 and 375×812. They also check login/error/404, disabled/enabled/hover
primary actions, keyboard focus, profile edit, submission acknowledgement and
Escape, mocked loading/empty/unavailable states, desktop search and mobile drawer.
They never confirm submission or the submission switch. The existing M13 browser
journeys remain in the combined suite.

Final local results (Node 22.23.4, Python 3.12.14, Chromium):

| Check | Result |
| --- | --- |
| Frontend unit tests | 336 passed, 26 files; 6.71 seconds |
| TypeScript | Passed, `tsc --noEmit` |
| ESLint | Passed, no warnings/errors; Next lint deprecation notice only |
| Production frontend build | Passed; 20 page/API routes including themed 404 |
| Source + generated CSS inventory | Passed, 90 files (89 source/config + one built CSS) |
| Browser suite | 26 passed, 0 skipped/failed; 82.67 seconds |
| New browser accessibility/palette coverage | 78 page/state checks, zero violations/overflow failures |
| Dependency audit | Zero known vulnerabilities |
| New Python browser test lint/format | Passed |
| Tracked-file secret gate and staged whitespace check | Passed |

The browser total includes all 17 existing M13 journeys and nine new cases. The
78 checks comprise 62 role/route/viewport combinations, six login/error/404
checks, two form/dialog checks and eight feedback/navigation state checks.
The first run exposed five obsolete light-theme contrast assertions and one
pulse-skeleton selector; the replacement assertions measure the new palette
without reducing the accessibility thresholds. Browser checks then identified
the initials label, mobile card overflow and duplicate heading described above;
they pass after correction. An incorrect API-detail lookup in the new test was
also corrected to use the existing application-list contract. No API changed.

The secret gate first flagged the new public axe-core integrity checksum. Its
existing false-positive baseline entry was refreshed after matching it against
the npm lockfile; the hook also updated 621 lockfile line references and its
timestamp. Detector/filter configuration and all other finding hashes are
unchanged; no broader allowlist was added. The repeated full tracked-file gate
passed. The existing public `.gitkeep` preserves the empty asset directory.

Commit information is in the milestone changelog.
Visual review uses local screenshots of login, dashboard, review workspace,
cover letter, answers, resume, profile, settings, submission, feed and monitoring.

## Limits

Automated axe checks and focused keyboard/visual checks are evidence for the
scoped UI, not a complete WCAG certification. Chromium is the tested browser;
Safari/Firefox, OS-native popup chrome, forced-colors mode, screen-reader sessions
and arbitrary user-supplied content were not exhaustively tested. Browser text
antialiasing naturally blends pixels; the six-color restriction applies to
specified UI paint, not every rasterized edge pixel. User-agent/OS accessibility
preferences remain under the user's control. No app-controlled off-palette paint
is intentionally retained. Backend regression, load/soak testing, production
images and hosted CI were not rerun for this frontend-only change.
