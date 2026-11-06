# M16.6 engineering verification

Recommendation: **READY FOR INDEPENDENT REVIEW**. This recommendation authorizes
neither integration nor deployment. A complete browser-only workflow and live
email sending are not ready for release.

Branch: `feature/p0-resume-export`. Original baseline: `7323d42`.
Implementation reviewed: `2acbaa0`. Scoped corrective commit: `4b23cda`.
This evidence document is committed on that feature branch after the corrective
commit. Resolve the final evidence commit with `git rev-parse HEAD`; the external
handoff records its full SHA.

## Scope and self-review

Reviewed the complete implementation diff against the original baseline, including
API ownership and CSRF gates, consent snapshots, claim/source grouping, artifact
identity and reuse, storage ordering, renderer dependencies and Unicode handling,
manual package selection, frontend consent, demo changes and synthetic tests.
No models or migrations changed. Existing JSONB approval snapshots and artifact
binding columns carry the new data. Schema verification passed all 12 checks;
the migration head remains `0010_submission_retry_schedule` (10 revisions).

The owner previews the exact allowlisted contact and structured records. The
approval binds resume identity, version, content digest, export digest and owner.
Rendering reads this snapshot rather than the current profile. Fact/source IDs
remain in the snapshot and canonical claim entries; ambiguous employer mappings
fail. Employment without selected bullets still retains its structured heading.
Active, unexpired certifications are included; expired/in-progress records are
excluded. Work authorization and salary preferences do not enter the resume.

## Defects reproduced and fixed after 2acbaa0

1. A render could accept verification changing to failed during upload. It now
   rechecks verification immediately before persistence.
2. A render could accept a different, internally valid approval snapshot after
   producing bytes. It now compares a digest of the exact initial resume and
   approval inputs against locked, refreshed rows.
3. Manual packaging had the same snapshot replacement race. It now freezes and
   rechecks those inputs after storage, and refuses failed verification.
4. Downloads verified a second store read rather than the returned bytes. Both
   artifact and legacy packet download routes now hash the actual response bytes.
5. Contact privacy depended on the template-name prefix. A linked approval's
   export snapshot now also requires the actual profile owner, even for a
   differently named template. Legacy v1 text-only reviewer downloads remain
   supported and have explicit regression coverage.
6. Custom section headings bypassed the PDF glyph check. Headings now pass the
   same unsupported-character guard as body text.

The first three defects failed targeted adversarial tests before correction.
The final tests cover mocked storage races and real JSONB updates/reloads in
PostgreSQL. The latter are controlled mutations at the storage boundary, not a
claim of exhaustive multi-process concurrency testing. Row locks remain held
through the caller's transaction; independent review should examine lock order.

## Final automated results

| Check | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Backend unit and offline grounding corpus | 2,084 | 0 | 0 |
| Selected isolated PostgreSQL/Redis integration tests | 603 | 0 | 0 |
| Frontend Vitest | 364 | 0 | 0 |
| Schema verification script | 12 | 0 | 0 |
| Partial browser/service journey assertions, distinct labels | 52 | 0 | n/a |

The three pytest/Vitest suites account for 3,051 test executions. The schema
script and browser assertions are reported separately. No live evaluation or
SMTP delivery was used. The selected integration modules are:

- `test_resume_export`, `test_artifacts`, `test_submission_readiness`,
  `test_resume_review`, `test_authorization_matrix`;
- `test_resume_persistence`, `test_resume_revision`, `test_first_draft`;
- `test_submission_worker`, `test_submission_api`, `test_submission_settings`,
  `test_submission_retry`, `test_security_invariants`, `test_profile_crud`,
  `test_cover_letter_review`.

Ruff lint and format checks passed. Canonical `mypy apps packages workers` passed
for 168 source files. TypeScript, source ESLint and the production Next.js build
passed. Source ESLint excludes generated `next-env.d.ts`; a broad initial lint
invocation flagged Next's generated triple-slash declaration. The existing
ESLintRC deprecation notice remains. The generated file was not edited.

`detect-secrets-hook --baseline .secrets.baseline` passed over tracked files.
The public font SHA-256 assertion required a documented next-line allowlist;
no secret detector was disabled and no credential was added to the baseline.
Candidate fixtures use fictional names, reserved example domains and 555 numbers.

## Isolation, failed runs and retained data

Only new cached-image containers named `jobforge-m166-3842170f-postgres` and
`jobforge-m166-3842170f-redis` were used, on a new network and new volumes. Their
loopback ports were 62254 and 62257. No existing development/demo endpoint was
connected. A sanitized runner validated the resource manifest and DSN; a socket
guard rejected all destinations except the new loopback ports. No blocked
connection attempts were recorded. No Docker image was rebuilt or existing
service reconfigured.

The final clean integration run used the newly created database
`m166_regression_3842170f`; browser work used `m166_verification`. Both were
migrated from empty only inside the new verification cluster. No M16.6 migration
or data backfill is required.

Earlier runs are not concealed: the initial selection passed 514 tests. Three
new race tests exposed the implementation defects before fixes. Two subsequent
new-test failures were fixture setup errors: readiness had already moved the
application to `policy_blocked`, so the render never reached upload. After
correcting setup, those five export integration cases passed. A later 517-case
run had two failures because baseline tests asserted an entirely empty submission
table while browser fixtures existed. The final separate-database selection
passed all 603 cases.

A harness mistake also allowed existing submission-test `_clean_all` fixtures to
clear synthetic pending-submission rows in the temporary browser database before
separation. Those fixtures issue committed database-wide deletes. No original
service or personal data was involved; retained approved resumes, snapshots,
artifacts and downloaded packages were checked afterward. Do not use these tests
against retained fixtures or any non-disposable database. Both new databases have
non-destructive local dump backups. Containers, volumes, artifacts and evidence
remain preserved; destructive cleanup requires user approval. Only the two new
API/web process groups were stopped after browser verification.

## Browser workflow and explicit gaps

Chromium ran against a separate loopback API (50083) and Next development server
(50084), with external browser requests blocked. API settings were test/replay,
`SUBMISSION_MODE=dry_run`, `EMAIL_TRANSPORT_MODE=fake`. No worker was launched;
SMTP constructors in the verification API were replaced by a refusal tripwire.
The UI showed dry-run mode and the submission switch off.

| Requested step | Observed verification |
| --- | --- |
| Create/import fictional profile | Scripted ORM fixtures; frontend does not expose first-profile creation/master import |
| Generate tailored resume | Real B2/B3 validators, numeral guard, bound scripted B5 verdicts and B6 persistence; no live LLM |
| Review contact and career details | Actual owner browser preview for all three roles, including dates and source records |
| Approve version | Actual checkbox and HTTP approval; button disabled before consent |
| Export PDF and DOCX | Production render service replay, then authenticated API downloads; direct Studio export button remains disabled and DOCX action is absent |
| Verify exported information | Automated extraction, dates, metadata, content agreement and checksums, plus page-by-page visual inspection |
| Manual handoff | Actual browser button downloaded each ZIP; six members and exact approved PDF/DOCX bytes verified |
| Block unapproved changes | Synthetic phone changed after preview: HTTP 409, visible error and zero artifacts; a fresh preview and consent were required |
| Desktop/mobile | 1280x900 and 375x812; screenshots and no-horizontal-overflow assertions for consent/submission |
| Ownership | Another owner received 404; anonymous download 401; reviewer/CSRF restrictions additionally covered by ASGI and DB-backed suites |

This is a **partial UI journey**, not a completed ten-step browser-only E2E run.
Profile import, first-draft and question-preparation entry points are missing.
Resume Studio still says export is coming next even though manual packages work.
Its pre-existing review read model reports `not_started` once an approved
application reaches `policy_blocked`. These are personal-use release gaps.

## Resume quality

Generated Linux Administrator, Cloud Infrastructure Engineer and Data Center
Technician samples through persisted owner approvals. Each PDF and each
LibreOffice-rendered DOCX was one page. All six pages were visually inspected:
single column, no tables/text boxes, readable body contact, sensible section
order, no clipping, missing glyphs, blank pages or unnecessary overflow. DOCX's
long Data Center education line wraps its final ISO date; the full date remains
selectable and intact. Native Word layout was not tested.

Automated checks used pypdf and pdfminer independently, plus pdfplumber glyph
bounds, python-docx structure/text and ZIP byte comparisons. PDF and DOCX contain
the same canonical information after whitespace/bullet normalization. Dates and
`Present` are preserved exactly. Unit stress tests additionally cover a 300-letter
URL token, 100 long technical lines across pages, page bounds, deterministic
bytes and keeping section headings with following records. The earlier two-page
synthetic stress sample remains available with its visual QA evidence.

Achievement-oriented metrics are supported when grounded: 24 Linux hosts and a
40-to-25-minute recovery change; three cloud environments and a 30-to-12-minute
deployment change; 48 assets and a 16-to-4 inventory-exception change. Every
number belongs to the cited fictional fact's structured payload. In all three
fixture builds, a deliberately invented `999` metric was refused by the real
validator. These synthetic fixtures validate mechanics, not live model judgment.
Their summary sentences deliberately repeat source wording rather than provide
an evaluation of generated prose quality.

URLs are preserved as selectable text, not clickable hyperlink annotations.
No URL was visited and no ATS upload was performed. ATS compatibility here means
simple text structure and extraction evidence, not certification by an ATS vendor.
Latin, Greek and Cyrillic extraction is tested. Missing glyphs, combining marks,
CJK, unimplemented shaping scripts and bidi/control characters fail explicitly;
this is bounded Unicode support, not universal script coverage.

## Compatibility and security decisions

Legacy approval snapshots retain their existing identity/version match semantics.
Historical artifacts remain readable under the established policy. Legacy text-only
approvals cannot authorize contact-bearing v2 exports or manual-v2 packages;
a fresh owner-reviewed version is required. There is no migration that invents
consent, and no UI recovery action to upgrade an already-approved legacy version.

Manual-v2 ZIP identity includes resume version, approval, export digest, current
PDF/DOCX identities and checksums, cover letter, answers and job metadata. It is
separate from the legacy email packet. `current_packet` excludes manual templates
so email packet consumers cannot accidentally pick a manual ZIP. The resume pair
must share current approval and template identity; actual bytes are checked.

Transport, dispatch, retry scheduler, submission state machine and database
models/migrations are unchanged. Tests used fake transport. No email or external
application was sent. The SMTP send/commit failure window remains unresolved;
see `docs/design/SMTP_DISPATCH_RECOVERY.md` for human-reconciliation states.

## Independent review priorities and unverified behavior

1. Recheck owner/digest binding, post-upload material comparison and transaction
   lock order, including snapshot replacement and revoked verification.
2. Check contact disclosure through both artifact and legacy packet downloads,
   including custom template names and reviewer permissions.
3. Check version/approval/template selection and separation of manual and email
   packet identities. Legacy email packet source selection still assumes one PDF
   per resume; mixed historic templates need an explicit integration decision.
4. Audit provenance grouping and the existing upstream assumption that persisted
   claim text/fact references agree with resume content. The new digest freezes
   what is approved; it is not a replacement semantic verifier. Cover-letter
   approval also retains the pre-existing immutable-version assumption.
5. Decide the UI completion and legacy-consent upgrade path before personal-use
   release. Do not interpret this review recommendation as that release gate.
6. Inspect Unicode exclusions, font packaging/license, long-token limits and
   unsupported-script errors. Native Word, iPhone Safari/Files, real ATS parsers,
   S3/MinIO, full outbox/worker delivery and high-concurrency interleavings remain
   unverified. No secure remote-access deployment was changed or tested.

Review the original baseline diff, then the corrective commit, and then this
verification evidence. Coordinate using completed commits only. Do not merge,
rebase onto Cowork's moving main, push or enable live sending without approval.
