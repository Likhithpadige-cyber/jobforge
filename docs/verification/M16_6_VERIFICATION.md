# M16.6 final engineering verification

Engineering recommendation: **READY FOR INDEPENDENT REVIEW**, with M3 explicitly **PARTIALLY FIXED**. H1, H2 and H3 have regression evidence and no confirmed blocker remains open in the resume-export scope. This is a review handoff; Cowork owns the independent verdict and integration recommendation. Live SMTP, deployment and a complete browser-only application workflow are not approved.

Branch: `feature/p0-resume-export`. Original baseline: `7323d4210fa033d39a04e9222fd743037db49118`. Cowork reviewed `2acbaa0b2f711461f3c424fdeb3a1dc3eaf32605`; the previous claimed-ready tip was `09ee0847009a00b959725324cf5b66d329ab9344`. This pass contains correction commit `8f3093c63c0445e9bf46ec09ccea2e421b45c4fa` and download-integrity commit `d89d8e20394f746ff8e24d64178c4eff5bf6d2b9` and final code commit `94511167457c2139b172022a695a8e7a94ece2c4`. The final documentation/evidence commit SHA is recorded in the external review manifest. No merge, rebase, push, deployment, live email, repository visibility change or edit to main/Cowork's worktree occurred.

## Findings and implementation evidence

The complete finding-to-code-to-test matrix, including verified source line numbers, is in `docs/verification/M16_6_FINDINGS.md` and the handoff's `m166-findings-matrix.md`. Statuses:

| H1 | H2 | H3 | M1 | M2 | M3 | M4 | M5 | M6 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| FIXED | FIXED | FIXED | FIXED | FIXED | PARTIALLY FIXED | FIXED | FIXED | FIXED |

H1: pre-upgrade text approvals render through a separately versioned legacy text-only path without importing current profile contact details. A later explicit owner consent adds an export snapshot to the same approved version, preserving the original reviewer, decision time and application state. A complete approved v2 artifact pair remains packageable. New 2.1 artifacts do not overwrite v1/v2 objects. No schema migration or consent backfill is needed.

H2: the role-aware review UI lets a reviewer approve prose without requesting private export preview. The owner can then review/consent to export details even after readiness moved the application to submission_ready, manual_handoff or policy_blocked. Consent emits a render request and does not replay application approval transitions. A stale preview is refused.

H3: render/reuse, legacy/manual packaging and downloads validate current fact status, soft deletion, validity dates and correct profile. Certification records must still be active and unexpired. Cover-letter and answer citations are included. Cached legacy ZIP downloads must match the exact resume, cover-letter and answer versions being validated. Snapshot consent cannot preserve permission to use withdrawn evidence.

New medium corrections normalize NFC/tabs, reject single-line field injection and malformed URLs, enforce owner-only access by default for unknown templates, and improve dates/contact density/bullets/DOCX font selection. Existing `4b23cda` snapshot-race and response-byte integrity fixes remain in place. M3 is not called fully fixed: PDF headings use the regular embedded font; headline omission, long credential URL density and untested native Word/iPhone layout remain.

## Complete test results

| Gate | Passed | Failed | Skipped / deselected |
| --- | ---: | ---: | --- |
| Full relevant backend: all unit, eval and integration folders | 3,430 | 0 | 0 skipped; 32 opt-in E2E deselected and run separately |
| Backend folder detail | 2,059 unit + 61 eval + 1,310 integration | 0 | 0 |
| API E2E journeys, separate process | 6 | 0 | 0 |
| Full Chromium Playwright suite, separate process | 26 | 0 | 0 |
| Frontend Vitest, 28 files | 366 | 0 | 0 |
| Schema conformance | 12 | 0 | 0 |
| Focused reviewer-to-owner actual-browser assertions | 9 | 0 | Supplemental, not Playwright replacements |

Backend duration: 275.57s. API E2E: 9.56s. Playwright: 147.15s. Exact JUnit evidence is provided in the archive. Ruff, repository format check, canonical `mypy apps packages workers` (169 files), TypeScript, source ESLint, production Next build and tracked-file detect-secrets all passed. Existing ESLintRC deprecation remains. Offline grounding verification passed 51 corpus cases with zero provider calls; these overlap the 61 eval-folder tests and are not an additional test count.

The 22 original readiness tests are copied from completed commit `6945454` without changing their calls/assertions, with their source documented. All 22 pass, including zero-question approvals, stale resume/cover/answers, approval order, allowed/manual/blocked policy, retries, replay, packet determinism and submission uniqueness. Thirteen of these cases failed before H1 correction.

### Reconciliation with Cowork's 3,330

Cowork's reviewed suite: 2,016 unit + 61 eval + 1,253 integration = 3,330. Prior corrections added 7 unit and 3 integration tests, giving a full collection of 3,340 at `09ee084`. The earlier report ran only 2,084 (=2,023 unit+61 eval) plus 603 selected integrations =2,687; **653 integrations were not executed then**. This pass adds 36 unit and 54 integration tests (including the frozen original 22 readiness cases), so 3,340+90=3,430. E2E's 6+26 tests are separate from both counts. The earlier 52 browser/service assertions are historical partial-journey evidence and do not replace the Playwright suite.

### Diagnostic runs and environmental limitations

The before-fix blocker run had 29 failures, 11 passes and 3 deselections: 13 legacy cases, reviewer-to-owner consent and 15 fact scenarios. Some render scenarios initially failed for the wrong policy-state reason, while manual/cached checks were missing. The first unit sweep had 28 failures, including newly reproducing M1/M4/font regressions and old assertions that required updating for the intended date/bullet layout.

The first complete backend attempt had 3,408 passes and 4 failures: three configuration tests inherited explicit synthetic DSN/session settings, and one new test incorrectly asserted that a shared artifact table was empty. The runner now uses CI-style assembled settings; the new assertion is scoped to its resume. A later focused failure required adding Resume to a direct-router mock after the new evidence lookup. An accidental mypy invocation from the projectless directory omitted repository config; the canonical command from the repository passed all 169 files. The first new historical-consent run also had three fixture failures because PostgreSQL correctly refused an illegal approved-to-changes_requested transition; those cases now use the supported superseded state, with no trigger bypass. These are recorded separately from the final green suite. A browser invocation without `-m e2e` intentionally deselected all 26 tests and was corrected; it is not counted as a passing run. An initial frontend invocation from repository root produced 8 cwd-based asset failures; the correct apps/web invocation passed all 366. No final failures or skips are concealed.

The complete 3,420-test run before the final download regressions also passed. The 3,423- and 3,424-test intermediate full runs also passed. The final full rerun above covers all four new download/integrity regressions and six historical-consent/approval-safety cases. Native Word, physical iPhone and ATS vendor ingestion are environmental verification gaps, not skipped pytest cases.

## PDF/DOCX quality

Three fictional role pairs were regenerated using persisted explicit owner export consent, production 2.1 renderers and the production manual-package service, in the separate new sample database. Generation uses real B2/B3 validation, numeral checks, bound scripted B5 verdicts and B6 persistence; it does not assess live-model accuracy. These files were saved from the service artifact store, not downloaded by the browser in this pass. All three persisted approvals, current evidence and unchanged cached manual-package/PDF/DOCX bytes were revalidated again against the final consent-safety code.

Linux Administrator, Cloud Infrastructure Engineer and Data Center Technician each produce one PDF page and one LibreOffice-rendered DOCX page. All six pages were visually inspected. Approved name, email, phone, location and links match exports; employment title/employer, education/institution/degree and all date ranges are intact and readable. Snapshots retain original ISO dates/provenance; visible dates use English month/year ranges. Private authorization/salary sentinels are absent. PDFs have real headings/bullets in reading order, selectable text, bounded glyphs, no blank/clipped pages and no dropped text. Headings are regular-weight, not bold.

Pypdf and pdfminer independently extract the same PDF text. PDF and DOCX content matches after whitespace/bullet normalization. DOCX uses editable paragraphs, native headings/list styling, Arial requests, no tables/text boxes or contact in headers/footers; a regression edits/saves/reopens it successfully. LibreOffice visual output and independent rendered-DOCX glyph-margin checks are clean; installed-font substitution and pagination on native Word/iPhone remain unverified.

NFD accented names/prose normalize to NFC before digests/rendering. Prose tabs become spaces consistently; single-line fields reject them. Unsupported characters (including unsupported CJK/Arabic/emoji, shaping or controls) fail with an explicit renderer error rather than silent glyph loss. No transliteration is invented. A separate 300-character URL/token stress pair was inspected in both formats; extraction retains every character and PDF glyphs stay within bounds. Existing multi-page tests additionally check all page bounds and heading placement. Plain-text URLs wrap; clickable links are not implemented.

Each manual ZIP has six members and contains exactly the approved PDF and DOCX bytes, alongside cover letter, answers, job details and checklist. Template/version/approval IDs and hashes bind the pair. This is practical ATS extraction evidence, not a guarantee for every proprietary ATS parser. Full PDF bold styling, headline inclusion and denser long credential links remain M3 work.

## Data integrity, authorization and races

Approval/version/content/export digests bind frozen owner consent to immutable resume content, structured source IDs and original review attribution. New owner consent requires a fresh owner-only preview with source versions. Resume revisions append versions rather than rewriting the approved predecessor. Only exact known legacy text templates retain reviewer byte access; future/unknown/null templates require the actual profile owner. Contact snapshot markers still require owner even under a legacy-looking name. Cookie authentication/CSRF and owner/foreign-owner tests remain active.

Render and package services lock/refetch application/approval/evidence rows, compare the initial approved material digest after storage and refuse changed verification, approval or snapshots before artifact persistence. Controlled storage-boundary mutations run against real PostgreSQL JSONB, plus mocked adversarial unit cases. This is concrete race regression coverage, not exhaustive multi-process scheduling proof. Row locks live through the caller's transaction; independent review should inspect lock order and contention. A validity check is current at the transaction boundary; it cannot recall bytes already downloaded.

Actual response bytes are checked against recorded SHA-256 digests before download; current production renderers always record them. The final correction also hashes the exact PDF bytes inserted into a legacy packet, eliminating a second-read verification gap. Manual packages require both format hashes and same-template pairs; failed verification does not create a database artifact row. Storage may retain an unreachable uploaded object after rollback; existing recovery/idempotency tests cover this design. Unapproved metadata changes, checksum corruption and owner mismatch fail closed. Older frozen contact snapshots are validated against the current shared field/URL rules before reuse, and contact artifact/legacy packet downloads refuse superseded approvals. Six real-database regressions cover those paths.

### Exact cleanup/resource accounting

The prior cleanup incident was confined to the earlier own temporary container `jobforge-m166-3842170f-postgres`, database `m166_verification`, loopback port 62254. Existing submission test fixtures committed these deletes: all rows in `submissions`; `site_policies` rows with domain `example.invalid`; and the `settings` row whose key was `submission_enabled`. The fixtures are in `test_submission_worker.py`, `test_submission_api.py` and `test_submission_retry.py`. Browser preparation had created three synthetic pending submissions there. No pre-delete row ledger/dump exists, so exact deleted row IDs and any additional fixture-row total cannot be reconstructed; the report does not invent them. Subsequent prior evidence verified all three synthetic profiles/resumes, approval snapshots, each candidate's four artifacts, downloaded packages and post-cleanup backups remained. No existing development/demo database or Cowork resource was involved.

This pass never queried the old database. New resources are exactly `jobforge-m166-f43f3588-postgres`, `-redis`, `-minio`, their new `-data` volumes and network `jobforge-m166-f43f3588-network`, created from cached pinned images. The new PostgreSQL databases are `m166_verification` (regression), `jobforge_demo` (browser only) and `m166_samples` (sample approvals only). The regression tests' ordinary cleanup affects only their disposable database/own test objects. Browser retirement clears only its freshly generated accounts' password hashes, preserving rows. No retained development/demo/Cowork resource is touched. Resource names and ports are recorded in the archive's sanitized manifest.

Protected local DB dumps and credentials remain outside the review archive. All containers, volumes and data are retained pending user-approved cleanup. Only the new loopback API/web process groups are stopped after verification. A socket guard permits only the new manifest's loopback ports. It rejected dead-dependency and SDK metadata probes before connection; those attempts are reported, not described as zero. External browser requests are blocked. No worker or live SMTP was launched; API SMTP constructors are refusal tripwires and settings use replay/dry_run/fake transport.

## SMTP failure window: design only

`docs/design/SMTP_DISPATCH_RECOVERY.md` remains a separate proposal. It specifies committed prepare/claim boundaries, immutable MIME hashes, per-application active/uncertain uniqueness, fencing, late receipt evidence, lease budgets, conservative uncertain outcomes, separately authorized resend with a new Message-ID, precise DATA boundaries, key/retention management and kill-point tests. No dispatch, transport, scheduler or live submission implementation changed. The SMTP send/commit window remains unresolved in current runtime and blocks enabling live sending; it is outside this resume-export milestone.

## Compatibility with private main

Private main `6945454057564b0f079b9384989a76b8c9541ae4` and feature share base `7323d4210fa033d39a04e9222fd743037db49118`. Read-only three-tree comparison shows no overlapping changed paths and no conflict markers. Main-only files are documentation/workflows/screenshots, not runtime/schema/dependencies. No combined merge checkout or merged-tree runtime suite was executed, and a clean textual comparison is not a combined CI guarantee.

The histories diverged, so integration would require a merge commit. Proposed steps, for Cowork's recommendation and explicit approval only: create a separate integration worktree/branch at `6945454`; merge the final handoff SHA with `--no-ff`; preserve all eleven main-only paths; review the combined diff and workflow expectations; run complete backend, schema, frontend and both E2E suites on that combined tree; then seek final integration approval. None of these integration steps was executed. Main and Cowork's review worktree remain unchanged.

## Remaining risks and handoff boundary

M3 visual polish remains partial. Native Word/iPhone, vendor ATS ingestion, combined-main CI and true concurrent-process stress remain unverified. Reviewer metadata listing, large-input wrapping cost, generic manual-package error messaging and multiple-approval selection remain low-severity review notes. Owner-approved structured metadata is not independently fact-verified. The browser-only product flow still lacks profile import/first-draft/question preparation and complete direct export UI; the functional manual package is not evidence that all those entry points exist. Secure remote MacBook/iPhone access, portal automation, follow-ups and live SMTP are not implemented or enabled by this milestone.

The archive supplies exact matrix, final JUnit, quality checks, synthetic PDF/DOCX/ZIP/approval pairs, page renders, isolated-run harness and sanitized resource/count/compatibility manifests. A Git bundle supplies completed commits. Previous `09ee084` outputs are preserved separately. Cowork should independently reproduce H1/H2/H3 and review M3/locking/legacy compatibility before recommending integration. Stop at this handoff.
