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
