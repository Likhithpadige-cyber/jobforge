# P0 resume export implementation

Base: `7323d42`. Branch: `feature/p0-resume-export`. This document supersedes the
initial assumptions at `2acbaa0` and the partial verification at `09ee084`.

## Consent and backward compatibility

The preview selects the authenticated owner's profile and allowlisted contact,
links and structured employment/education/certifications. Work authorization,
salary and arbitrary private fields are excluded. Generated claims retain fact
IDs and source associations; ambiguous employer associations fail.

Old text-only approvals continue to authorize their unchanged prose through a
separate `ats-single-column/legacy-text/1` template, without inferring permission
to include current contact or profile metadata. The owner can add exact export
consent to the same approved version using the existing approval endpoint and
fresh preview digest. This appends the owner, digest, document and consent time
without rewriting the original reviewer's decision or changing application
state. Existing valid owner consent cannot be silently replaced. No schema
migration or automatic consent backfill is required.

Reviewers can approve text without accessing the private preview. Owners see a
separate consent action for already-approved text-only versions. The API enforces
ownership; browser role checks only select the appropriate interaction.

## Text policy and validity

NFC normalization applies before new digests and rendering. Tabs in prose expand
to spaces in both formats. Contact and structured single-line fields reject tabs
and line separators. HTTP(S) URLs require a hostname and reject whitespace,
embedded credentials and malformed ports. The owner sees formatted body lines in
addition to source fields. Stored source dates stay ISO; rendered ranges use
explicit English month/year labels and `Present`.

Frozen approval does not keep withdrawn evidence active. Render generation,
artifact reuse, owner downloads and both package-preparation paths recheck cited
resume facts. Package validation also checks cited cover-letter and answer facts.
Certification metadata is checked against current active/expiry/deletion state.
Render persistence rechecks locked evidence after storage. Application locks are
taken before evidence locks for rendering; package callers retain the existing
application lock. These checks change material eligibility, not SMTP transport.

## Rendering and versioning

Current complete exports use `ats-single-column/2.1`. Earlier approved v2 PDF/DOCX
pairs remain packageable; a package selects a complete pair under one template,
never a PDF from one template and DOCX from another. Different templates address
different storage objects. Resume version, approval, template, size and checksum
remain recorded on each artifact.

Contact is body text, compacted into a name and two detail/link paragraphs.
Employment/education/certification headings display conventional date ranges.
PDF bullets use a Unicode bullet and measured continuation indentation. PDFs
embed the existing licensed Noto Sans regular font with Unicode maps. PDF bold
weight is still outstanding (M3). DOCX uses editable paragraphs, native headings
and bullets with Arial as the requested font; the font is not embedded.

NFC Latin, Greek and Cyrillic extraction is tested. Unsupported glyphs, remaining
combining marks, bidi controls and unimplemented shaping fail explicitly. This is
not universal script coverage. Native Word and iPhone rendering remain unverified.

## Packages and privacy

Manual packages require current approved materials and a matching PDF/DOCX pair,
verify the actual component bytes, and use a distinct deterministic identity from
legacy email packets. Download routes hash returned bytes. Artifact content is
owner-only by default; only explicitly known legacy text templates retain shared
reviewer access. A contact-bearing approval remains owner-only even if associated
with a legacy-named artifact. CSRF protects the package action and consent.

SMTP dispatch, transport, retry scheduling and state-machine implementation are
unchanged. The send/commit window remains design-only and blocks enabling live
sending. See `SMTP_DISPATCH_RECOVERY.md` for uncertainty and reconciliation rules.

## Evidence

See `../verification/M16_6_VERIFICATION.md` and
`../verification/M16_6_FINDINGS.md` for exact code/test locations, full-suite
counts, preserved-resource accounting and the remaining M3 limitations. Full
backend and the API/Playwright E2E suites were run separately in new isolated
resources. The old subset counts and the 52 earlier browser/service assertions
are historical evidence, not substitutes for those suites.


## Cowork H3 residual correction

`b9031f6` checked live status but did not bind fact content. See `M16_6_EVIDENCE_BINDING.md` for the immutable material-version schema, fail-closed compatibility rules and owner-authorized source replacement. Rendering and exports never refresh approved fingerprints. Historical dangling-source repair requires new evidence IDs and freshly approved generated materials; it cannot silently relink an approved résumé.
