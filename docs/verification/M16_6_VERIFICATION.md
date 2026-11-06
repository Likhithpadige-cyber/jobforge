# M16.6 H3 residual verification and Cowork handoff

**READY FOR INDEPENDENT REVIEW**, contingent on Cowork's independent acceptance. This is not integration/deployment permission. Code commit: `139179020bc25f229afcf2ac9f22f52f0a4e6eeb`, on `feature/p0-resume-export`. The final evidence commit is recorded in the external manifest.

Cowork's NO-GO at `b9031f6235e3c67c363e81579ec7c6b2e460beb1` was correct: live status checks did not detect fact edits followed by verification. This pass binds immutable material versions and supplies a separate owner-authorized source-repair path. No retained demo/live data was repaired or reseeded.

## Finding and verification matrix

| Finding | Status | Exact code and test evidence |
| --- | --- | --- |
| H3-residual | FIXED | Material schema `packages/core/services/resume_export.py:73`; live comparison `packages/core/services/resume_export.py:130`; schema/required-proof validation `packages/core/services/resume_export.py:93`; 36 mutation/path regressions `tests/integration/test_h3_residual.py:86` |
| Missing/invalid fingerprints | FIXED | Schema/entry guards `packages/core/services/resume_export.py:93`; 12 missing/invalid-entry cases `tests/integration/test_h3_residual.py:128`; 12 container/schema cases `tests/integration/test_h3_residual.py:159` |
| Edited evidence between prose approval and owner consent | FIXED | Approval comparison `packages/core/services/resume_review.py:706`; regression `tests/integration/test_h3_residual.py:256` |
| Historical contact consent without immutable proof | FAILS CLOSED | New review required; no silent fingerprint backfill. Regression `tests/integration/test_h3_residual.py:184` |
| M-A new source references | FIXED | Authenticated-owner create `apps/api/routers/facts.py:110`; source ownership/existence `packages/core/services/profile_service.py:91`; six source cases `tests/integration/test_fact_source_repair.py:28` |
| M-A safe historical source repair | IMPLEMENTED; reapproval limits below | Service `packages/core/services/profile_service.py:108`; endpoint `apps/api/routers/facts.py:139`; body `apps/api/schemas/facts.py:59`; history/control tests `tests/integration/test_fact_source_repair.py:96`, `tests/integration/test_fact_source_repair.py:150`, `tests/integration/test_h3_residual.py:213` |
| H1 legacy compatibility | PRESERVED | All 22 unchanged original readiness cases pass; existing legacy-to-owner-consent journey passes. Legacy text does not infer PII or retroactive fingerprint consent |
| H2 reviewer then owner | PRESERVED | Existing reviewer/owner DB/UI regressions pass; changed-evidence consent now refuses rather than rebinding the old prose |
| M1/M2/M4/M5 | PRESERVED | Existing tab, line/URL, Unicode and default-ownership tests pass in the full suite; layout/renderer code unchanged |
| M3 formatting | PARTIALLY FIXED, unchanged | Readable dates/contacts/bullets/Arial retained; PDF regular-only headings and native Word/iPhone/ATS limits remain |

Additional enforcement: render/reuse `packages/core/services/artifacts.py:228`, post-storage recheck `packages/core/services/artifacts.py:158`, package claims `packages/core/services/handoff_packet.py:154`, manual/cached packages `packages/core/services/handoff_packet.py:497`, existing PDF/DOCX downloads `apps/api/routers/artifacts.py:72`, cached legacy ZIP route `apps/api/routers/applications.py:238`. The approval lock is acquired before evidence in review `packages/core/services/resume_review.py:359`; existing owner and byte-integrity checks remain in place.

## Before and after, without substituting test counts

On unchanged `b9031f6` product code, the new targeted file produced **18 failures**: six edited-and-reverified paths and twelve missing/invalid-fingerprint cases. All were DID NOT RAISE failures, not environmental exceptions. The same 18 cases pass in the final full suite. The before/after JUnit and source anchors are included.

The complete new regression set is 75 H3 integration cases, 14 source-integrity/repair integration cases and 11 material-version unit cases, plus five generated authorization-matrix cases for the new route. Thus **3,430 + 105 = 3,535** backend tests. The core H3 matrix independently tests unverify, edit while unverified, edit/reverify, expire/reverify, retire and delete against new render, manual build, cached manual reuse, previously generated PDF, previously generated DOCX and cached legacy ZIP download. Twelve controls cover unchanged evidence with and without an unverify/reverify cycle. Missing records/containers and invalid entry/schema versions are separate cases. Ten material-field unit cases cover each content input; one verifies NFC/audit-counter stability.

| Final gate | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Full backend | 3,535 | 0 | 0 |
| Backend folders | 2,070 unit + 61 eval + 1,404 integration | 0 | 0 |
| Full safety subset (overlaps backend) | 1,642 | 0 | 0 |
| API E2E, separate process | 6 | 0 | 0 |
| Playwright, all Chromium tests | 26 | 0 | 0 |
| Frontend Vitest, 28 files | 366 | 0 | 0 |
| Schema conformance | 12 | 0 | 0 |

Backend intentionally deselects the 32 opt-in E2E cases; all 6+26 were run separately. Safety is a subset, not an additional unique count. Backend runtime 355.41s; safety 191.82s; API E2E 5.17s; Playwright 139.41s. Ruff, formatting, mypy (169 source files), TypeScript, ESLint, production Next build and tracked-file secret checks pass. Offline evaluation is deterministic with no provider calls.

Intermediate failures are retained separately: the first H3 integration batch passed 119; source tests initially had one invalid foreign-profile fixture lacking its required user. Focused unit fixtures needed fingerprint/timestamp fields and a storage-race hook adjusted for the new initial lock. The first complete run had 3,533 passes and two failures: the new production-media guard also caught a plain-text reference-renderer test, and an authorization mock lacked a credential lookup. The guard is now scoped to production PDF/DOCX and the mock is explicit. The missing local MinIO `latest` tag was a provisioning limitation; the already cached pinned image ID was used without pulling. A nonexistent seed-module/help invocation and an incorrect log-relative path were corrected before the real commands ran. None is counted as a passing gate.

## Fingerprint semantics and legacy boundary

The schema stores a content-addressed fact `version` and schema-bound SHA-256. Canonical material includes ID/profile, text, structured JSON, fact type, source table/ID, confidence and validity. The existing fact table has no numeric version column; no fictional sequence or timestamp revision is claimed. Verification bookkeeping, usage counts and embeddings are not material: unchanged evidence may be reverified, while content edits cannot revive consent. Facts/status/validity are checked under the transaction's locks. The owner-consented document digest binds the material map to the exact résumé ID/version/content. New text-only approval also stores its proof and content digest, protecting later owner consent.

Contact consent missing its required proof fails closed. Neither export nor cache reuse updates it. Older contact consent without earlier immutable proof needs newly reviewed generated materials; it cannot be retroactively certified by a fresh preview. Existing main's text-only approvals keep the explicitly contact-free template path and conservative recorded-review timestamp boundary, without invented historical content versions. Historical timestamps are not a substitute for immutable evidence history. Controlled mutation and real-database regressions support the transaction checks; exhaustive process-scheduling proof is not claimed.

## Source repair and required reapproval

Fact creation now requires a valid source on the authenticated owner's profile. Existing dangling-source preview conflicts identify the fact and `repair-source` endpoint. The owner supplies a selected source ID, current `updated_at` and explanation. The operation validates ownership, target and stale-write preconditions, creates a new draft, retires the original without rewriting its text/source/ID, and appends owner-attributed attestation history to both. It never edits résumé claims, changes the approved version, verifies the replacement or replays approval automatically. Missing targets, cross-owner/reviewer calls, stale/repeated requests, valid original sources, duplicate replacements and empty notes are refused. Auth/CSRF classification is independently tested at `tests/integration/test_authorization_matrix.py:307`.

Historical **fact data can be repaired while preserving the old résumé record**, but export of that record remains blocked because it cites retired evidence. After verifying the replacement, the owner must refresh evaluation and generate/review new materials citing its new ID. Pending review supports existing request-changes/rework; `policy_blocked` jobs can use the existing fresh-application first-draft path. Existing approved/submission_ready/manual_handoff states lack a general reopen endpoint. This pass does not reset those states or invent reapproval transitions. Recovery into export for those states remains a product workflow limitation; source repair alone must not make their old claim sendable. See `docs/design/M16_6_EVIDENCE_BINDING.md` for the precise API and workflow.

## Rendering and preserved functionality

Three new fictional role pairs were generated in a new sample database with fresh owner consent and production render/package services. No retained samples or demos were reseeded. All six pages were visually inspected: each PDF and LibreOffice-rendered DOCX remains one page, with readable contacts/dates, proper bullets and no clipping. Pypdf/pdfminer extraction, PDF/DOCX canonical text agreement, source metadata/date checks, privacy sentinels, bounds and exact ZIP member bytes pass. DOCX remains editable. Template remains `ats-single-column/2.1`; layout code and fonts are unchanged. Each manual ZIP contains matching approved PDF/DOCX among its six members. Native Word, physical iPhone and real ATS ingestion remain unverified. PDF headings remain regular weight and URLs plain text.

## Isolation, compatibility and handoff

Full verification used only new `jobforge-m166-h3-e11bf45f-postgres`, `-redis`, `-minio`, their new data volumes/network, and separate regression/browser/sample databases. The initial 18-case reproduction used only our earlier temporary regression DB and rolled back its fixtures; no retained browser/sample/live/demo/Cowork data was repaired, reseeded or deleted. New browser account retirement affects only accounts generated by that run. Socket/browser guards restrict test destinations; dead-dependency and SDK metadata probes were refused and are recorded. Replay/dry_run/fake transport and an API SMTP refusal tripwire remain enabled. No worker, real email or application submission was launched.

All retained resources, including Cowork's review branches/worktrees/containers/volumes, remain untouched. New data is preserved with protected local backups; those dumps, credentials and browser auth state are excluded from review files. Only the new API/web process groups were stopped. Prior output files are preserved under `m166-b9031f6-preserved`.

Compatibility against private main `6945454057564b0f079b9384989a76b8c9541ae4` is checked with read-only three-tree comparison. There is no executed merge/rebase or combined-main test tree. Cowork owns independent reproduction and integration recommendation. If approved later, use a separate integration branch/worktree at private main, merge the exact final SHA with a merge commit, preserve all eleven main-only files, and run hosted CI. No push, deployment, retained-resource cleanup, SMTP change or visibility change was made.

The package supplies final/before JUnit, exact file/line anchors, changed-file lists, sanitized resource/test/compatibility manifests, fresh synthetic samples/page renders and a Git bundle of completed commits. Stop at the handoff.
