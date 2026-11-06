# P0 resume export implementation

Base: completed commit `7323d42`. Branch: `feature/p0-resume-export`.

## Data and consent

`resume_export.preview_export` selects the signed-in owner's profile and current nondeleted structured source rows. All employment and education records and only active, unexpired certifications are previewed. Only an allowlist of contact fields and HTTP(S) links is included; work authorization, salary, secrets, evidence attachments and arbitrary profile fields are excluded. Every cited fact must still be verified, active and within its validity dates. Generated experience/education/certification claims must map to exactly one included source record through their fact IDs. Unknown or cross-employer associations fail rather than guessing or duplicating a bullet.

The preview returns a digest binding the resume ID/version, generated content and exact export document, including profile version and source/fact IDs. The UI shows the contact and structured fields and requires a checkbox before confirming approval. The server reconstructs the preview under the profile lock and compares its digest before recording consent. No request-supplied prose or actor identity is trusted. Export approval stores the frozen data, digest and owner ID in the existing approval JSON snapshot. No database schema migration is required, and no personal contact fields are added to model inputs or event payloads.

Historical API clients can still grant text-only approvals without `export_digest`; these cannot authorize production exports under template v2. An approved text-only version must go through a new review/version to become exportable. The new UI always requires export consent. A changed or missing digest is refused. Snapshot matching now also checks content digests for newly created approvals. Profile edits after approval never silently change previously approved files; use a new resume version/review for changed export details.

## Rendering and artifact identity

`ResumeDocument` receives only generated claim content plus the frozen approved export document. Candidate contact details are ordinary body paragraphs. Structured source headings retain employer, title, explicit ISO dates, education and certification details; generated prose and fact IDs stay unchanged. Open-ended employment is explicitly labeled Present, while unknown education dates remain absent. Source record IDs and fact IDs remain in the approval snapshot and document structure, not in employer-facing text.

Template identity is bumped to `ats-single-column/2`, preserving old storage keys. Production rendering refuses a missing/invalid owner export approval. It revalidates approval, content and application state after upload, and refuses reuse of an artifact tied to another approval/version. Metadata records bind resume/version, approval, template, size and checksum.

PDFs embed pinned Noto Sans Regular 2.015, SIL OFL 1.1, with Unicode text maps. Latin, Greek and Cyrillic extraction is tested. Unsupported glyphs, combining marks, bidi controls and scripts requiring unimplemented shaping fail explicitly; CJK, emoji, Arabic and Indic rendering are not claimed. No fallback to replacement boxes is permitted. Long tokens split at measured glyph boundaries without inserted hyphens; extraction can contain line breaks, but no token characters are lost. Multi-page tests measure glyph bounds.

DOCX uses body contact paragraphs, native headings and bullets, Letter paper and explicit margins. Structured headings stay with following content. No tables, text boxes or contact-bearing headers are introduced. Visual checks use the bundled LibreOffice renderer; actual Word/iPhone behavior is not established by those checks.

## Manual package and privacy

`POST /applications/{id}/manual-packet` is an explicit, CSRF-protected owner action, usable without live submission switches. It requires current reviews and both PDF and DOCX for the same approved resume/version and template. It verifies the actual bytes and builds a deterministic six-member ZIP. Package identity includes artifact IDs/checksums/templates, export approval, cover letter, answers and job metadata. Upload precedes row persistence, and approval is rechecked after storage activity.

Manual v2 package keys and template IDs are separate from the legacy email packet protocol. Legacy `current_packet` excludes the new manual packages. SMTP dispatch, transport, retry scheduler and submission state machine are unchanged. Contact-bearing v2 artifact downloads require the profile owner; packet downloads are owner-only. Reviewers can still review prose, but are not implicitly granted contact-bearing exports.

## Affected files

- New `packages/core/services/resume_export.py`; approval changes in `resume_review.py` and rendering guards in `artifacts.py`.
- `apps/api/routers/resume_review.py`, `applications.py`, `artifacts.py` and `schemas/resume_review.py`.
- `packages/services/rendering/document.py`, `docx.py`, `pdf.py`, `protocol.py`, bundled font/license and third-party notices.
- `packages/core/services/handoff_packet.py` for the separate manual package builder.
- Frontend API client, `ResumeReviewPanel.tsx`, `SubmissionView.tsx` and corresponding tests.
- Synthetic export tests, template-key expectations and authorization route classification.
- Separate `SMTP_DISPATCH_RECOVERY.md` proposal; no SMTP implementation changes.

## Verification and remaining validation

- Backend unit suite plus offline grounding corpus: 2,077 passed, no failures or skips.
- Export-specific tests cover consent digest changes, snapshot tampering, foreign owners, reviewer denial, CSRF, source lineage, expired/unheld certifications, two independent PDF extractors, embedded fonts, DOCX structure, long tokens, measured multi-page glyph bounds, orphaned headings, production artifact bindings, checksum failures and changes during storage activity.
- Route/role classification: 4 passed (349 service-dependent cases deselected).
- Frontend: 364 tests passed; TypeScript, ESLint and production Next build passed.
- Ruff check and format passed; canonical mypy scope (`packages apps workers`) passed, 168 files. Changed demo scripts were parsed and linted, not executed. An additional noncanonical mypy invocation including the two demo scripts was rejected for duplicate module naming; it is not counted as a successful check.
- Schema conformance: 12 passed, with no schema migration.
- Synthetic PDF and DOCX were rendered and both pages of each inspected. Visual review found an orphaned PDF heading and inherited DOCX title decoration; both were fixed and regression-tested before the final render. DOCX visual conversion used bundled LibreOffice and python-docx 1.2.0; sample PDF generation used bundled ReportLab 4.4.9. Product tests use the repository's pinned environment.
- New database journeys and updated readiness fixtures collect successfully (24 cases across the two files). They were not executed: no disposable PostgreSQL/Redis stack was provisioned, and the existing database was not used. Browser journeys, actual Word, iPhone Safari, hosted CI and real-person acceptance remain unverified.

No SMTP transport/dispatch/retry implementation, Docker configuration, running
service, remote branch or deployment was changed. The fictional demo fixture
code was updated for valid employment source IDs and explicit export consent;
no seed script was run. This branch remains based on `7323d42`; independently
completed changes on other branches have not been merged or validated here.
