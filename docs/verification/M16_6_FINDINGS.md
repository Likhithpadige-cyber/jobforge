# M16.6 Cowork findings and evidence matrix

Baseline reviewed by Cowork: `2acbaa0b2f711461f3c424fdeb3a1dc3eaf32605`. Prior claimed-ready tip: `09ee0847009a00b959725324cf5b66d329ab9344`. Original project baseline: `7323d42`. All paths and line numbers below refer to the completed final feature-branch source, not private main. The delivery manifest records the final full SHA.

| Finding | Status | Engineering evidence |
| --- | --- | --- |
| H1 | **FIXED** | New legacy text-only template and renderer fallback preserve existing approvals without importing contact data. Same-version owner consent is additive; previous approved v2 PDF/DOCX pairs remain packageable. |
| H2 | **FIXED** | New owner consent path preserves reviewer decided_by/decided_at and application state. New export_consent_required read model and role-aware UI permit reviewer text approval without private preview, then owner export consent. |
| H3 | **FIXED** | New live fact/certification checks cover render, reuse, manual and legacy packages, and downloads. Facts must be verified, nondeleted, within validity dates and in the correct profile. Package cover/answer citations are checked too. Cached legacy ZIP must match the exact current material versions. |
| M1 | **FIXED** | New shared NFC/text normalization converts prose tabs to spaces before either renderer. Tabs are rejected in single-line contact/record/link fields. |
| M2 | **FIXED** | New single-line validation rejects hidden line separators in contacts and structured records/links. URL validation requires absolute HTTP(S), a host, valid port and no whitespace, controls or embedded credentials. Preview uses the same canonical document as rendering. |
| M3 | **PARTIALLY FIXED** | New readable month/year date ranges, compact contact paragraphs, actual PDF bullet glyphs with measured continuation indentation, formatted owner preview and Arial DOCX styles. |
| M4 | **FIXED** | New recursive NFC normalization precedes export digests; the shared document builder normalizes rendered contact and prose before glyph checks. |
| M5 | **FIXED** | New default-deny ownership rule requires profile owner for unknown, future and null templates. Only exact legacy text-only template allowlist permits reviewer bytes. Existing export-snapshot marker protection remains. |
| M6 | **FIXED** | Complete backend collection, separate API E2E and all 26 Playwright tests replace the incomplete selection. Exact JUnit counts and retained diagnostic failures are reconciled in the report. |

## H1 — FIXED

New legacy text-only template and renderer fallback preserve existing approvals without importing contact data. Same-version owner consent is additive; previous approved v2 PDF/DOCX pairs remain packageable.

Code: `packages/services/rendering/protocol.py:22`; `packages/core/services/artifacts.py:219`; `packages/core/services/resume_review.py:718`; `packages/core/services/handoff_packet.py:495`.

Regression tests: `tests/integration/test_legacy_m166_readiness.py:179`; `tests/integration/test_resume_export.py:253`; `tests/integration/test_resume_export.py:332`.

All original 22 readiness cases from completed main commit 6945454 run unchanged; 13 failed before correction. The added legacy journey proves rendering, readiness, owner consent on the same version, and both current formats. This is a new compatibility path, not a migration or inferred consent.

## H2 — FIXED

New owner consent path preserves reviewer decided_by/decided_at and application state. New export_consent_required read model and role-aware UI permit reviewer text approval without private preview, then owner export consent.

Code: `packages/core/services/resume_review.py:718`; `packages/core/services/resume_review.py:485`; `apps/web/components/ResumeReviewPanel.tsx:45`; `apps/web/components/ResumeReviewPanel.tsx:177`.

Regression tests: `tests/integration/test_resume_export.py:175`; `apps/web/__tests__/resume-review.test.tsx:364`; `apps/web/__tests__/resume-review.test.tsx:382`.

The DB regression failed before correction; 9 additional actual-browser assertions verify reviewer approval, owner mobile preview/consent, unchanged reviewer attribution and cleared consent-required state. Owner-only preview and CSRF were existing boundaries and remain tested.

## H3 — FIXED

New live fact/certification checks cover render, reuse, manual and legacy packages, and downloads. Facts must be verified, nondeleted, within validity dates and in the correct profile. Package cover/answer citations are checked too. Cached legacy ZIP must match the exact current material versions.

Code: `packages/core/services/resume_export.py:72`; `packages/core/services/resume_export.py:107`; `packages/core/services/artifacts.py:178`; `packages/core/services/handoff_packet.py:150`; `apps/api/routers/artifacts.py:110`; `apps/api/routers/applications.py:267`.

Regression tests: `tests/integration/test_resume_export.py:211`; `tests/integration/test_resume_export.py:298`; `tests/integration/test_resume_export.py:364`; `tests/unit/test_resume_export_findings.py:175`.

15 fact cases (draft/retired/deleted/expired/future × render/manual/cached manual), 6 credential cases, 2 legacy download cases and stale-source download regression. Before correction manual and cached-manual checks were missing; some render cases failed earlier for the wrong policy-state reason. Preview alone previously checked facts and is not the fix claimed here.

## M1 — FIXED

New shared NFC/text normalization converts prose tabs to spaces before either renderer. Tabs are rejected in single-line contact/record/link fields.

Code: `packages/services/rendering/text.py:9`; `packages/services/rendering/document.py:168`.

Regression tests: `tests/unit/test_resume_export_findings.py:34`.

The regression failed before correction and now checks actual PDF extraction and DOCX paragraph text. This changes the previous silent PDF tab loss.

## M2 — FIXED

New single-line validation rejects hidden line separators in contacts and structured records/links. URL validation requires absolute HTTP(S), a host, valid port and no whitespace, controls or embedded credentials. Preview uses the same canonical document as rendering.

Code: `packages/services/rendering/text.py:14`; `packages/services/rendering/text.py:20`; `packages/core/services/resume_export.py:268`; `packages/core/services/resume_export.py:272`.

Regression tests: `tests/unit/test_resume_export_findings.py:48`; `tests/unit/test_resume_export_findings.py:70`; `tests/integration/test_resume_export.py:412`.

24 field/separator cases plus 4 malformed URL cases; tests exercise the shared builder. Existing prefix-only URL acceptance has been replaced. Six additional database cases cover older malformed frozen consent and superseded approval across manual packages, direct artifacts and cached legacy ZIP downloads; the shared approval helper revalidates before reuse.

## M3 — PARTIALLY FIXED

New readable month/year date ranges, compact contact paragraphs, actual PDF bullet glyphs with measured continuation indentation, formatted owner preview and Arial DOCX styles.

Code: `packages/services/rendering/document.py:237`; `packages/services/rendering/pdf.py:135`; `packages/services/rendering/docx.py:126`; `apps/web/components/ResumeReviewPanel.tsx:202`.

Regression tests: `tests/unit/test_resume_export.py:208`; `tests/unit/test_resume_export_findings.py:118`; `tests/unit/test_resume_export.py:277`.

All six synthetic sample pages plus the PDF/DOCX stress pair were visually inspected. PDF headings still use the regular font (no bold face); headline omission and long credential URL density remain. Native Word, iPhone and actual ATS vendor ingestion are unverified. Do not mark this finding fully fixed.

## M4 — FIXED

New recursive NFC normalization precedes export digests; the shared document builder normalizes rendered contact and prose before glyph checks.

Code: `packages/core/services/resume_export.py:27`; `packages/core/services/resume_export.py:37`; `packages/services/rendering/text.py:9`.

Regression tests: `tests/unit/test_resume_export_findings.py:77`.

The NFD José Núñez regression failed before correction; equivalent NFC/NFD digests and actual accented PDF/DOCX text now pass. Unsupported scripts still fail explicitly; normalization does not invent transliteration.

## M5 — FIXED

New default-deny ownership rule requires profile owner for unknown, future and null templates. Only exact legacy text-only template allowlist permits reviewer bytes. Existing export-snapshot marker protection remains.

Code: `apps/api/routers/artifacts.py:98`; `packages/services/rendering/protocol.py:23`.

Regression tests: `tests/unit/test_resume_export_findings.py:97`; `tests/unit/test_resume_export.py:715`; `tests/unit/test_resume_export.py:754`; `tests/unit/test_resume_export_api.py:125`.

Three unknown/null template cases refuse reviewers before storage. Snapshot-marker and actual-byte download fixes already existed in 4b23cda/09ee084; the strict allowlist default is new in this pass.

## M6 — FIXED

Complete backend collection, separate API E2E and all 26 Playwright tests replace the incomplete selection. Exact JUnit counts and retained diagnostic failures are reconciled in the report.

Concrete evidence: backend-complete.xml/log (final rerun), api-e2e-complete.xml/log, playwright-complete.xml/log and count-reconciliation.json. The earlier 52 custom assertions are historical only and are not represented as Playwright coverage.

## Existing safety behavior versus new corrections

Commit `4b23cda` already added post-storage verification/snapshot rechecks, actual returned-byte hashing on downloads, export-snapshot ownership and custom-heading glyph validation. This pass preserves those checks, adds the H1–H3/M1–M5 corrections above, and additionally hashes the exact PDF bytes used by the legacy packet builder. The final correction also revalidates historical frozen contact snapshots and requires active contact approval before downloads, including legacy packets. It does not claim these earlier mechanisms were newly implemented. No transport, live submission, scheduler, migration or schema behavior was modified.

## Remaining low-severity observations

Reviewer artifact metadata visibility (L1), very large-input wrapping cost (L2), generic manual-package error/UI completeness (L3), theoretical multiple-approval selection (L4), and owner-entered structured metadata trust (L5) remain review considerations. None is represented as a fact-verification guarantee. Structured employers/titles/dates are explicitly owner approved and source-bound, but their truth is not independently established by this milestone. Concurrent transaction coverage is controlled mutation/locking evidence, not exhaustive process scheduling.
