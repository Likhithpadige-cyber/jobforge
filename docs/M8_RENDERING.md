# M8 rendering and artifacts — Phases 1 and 2

**Status: M8 is substantially complete.** The renderers, the service, the
worker, both API endpoints, the migration, and `S3ArtifactStore` (boto3
against MinIO) are all implemented and have been executed against a real
database and a real MinIO. See "Still requiring executable verification" for
what that section originally covered - it is stale below and kept only for
history; the S3/MinIO adapter specifically is no longer "not implemented".

## What M8 is, and what it may never do

`approved structured resume -> deterministic rendering -> versioned artifact
-> authenticated retrieval`.

M7 owns what a resume says. M8 lays out already-approved words and stores the
bytes. It does not generate, rewrite, infer, summarise or complete content,
and the boundary is structural rather than a rule someone has to remember:
`packages/services/rendering/document.py` is the only module that touches
content, it is pure and dependency-free, and it reorders and groups. A
renderer receives a `ResumeDocument` carrying no profile, no job, no
evaluation and no contact block, so it cannot leak one into a document or be
tempted to fill a gap with something plausible.

## Ordering, and why it is in that order

    revalidate -> render -> upload -> read back and compare
              -> REVALIDATE AGAIN -> insert the row -> emit

Two revalidations. The first refuses unauthorised work before any CPU is
spent. The second runs immediately before the row is written, because a
reviewer may have requested changes while the renderer was working: an
artifact must describe the version approved at the moment it became durable.

Upload precedes insert, never the reverse. A crash between them leaves an
orphan object - garbage, sweepable, harmless. The opposite order would leave
a row claiming an artifact that a download 404s on, which is a record that
lies about what exists. The readback closes the remaining gap: a truncated
upload fails its checksum comparison before anything refers to it.

## Approval binding

Rendering is requested from `approve()` in `resume_review.py`, and only
there - the one place a human has just said yes to this exact version. B6
also knows how to emit `resume.render.requested`, but it fires on
verification success, before anyone has approved anything, so the
first-draft path keeps it suppressed.

Staleness reuses `snapshot_matches`, which already requires both resume id
and version to match. A second implementation would be a second thing to
keep right.

## Schema (migration 0008)

Additive. `artifacts` gains `resume_version`, `approval_id` (FK to
`approvals`) and `template_version`. All three are nullable at the table
level because evidence uploads and handoff packets have none of them;
`ck_artifacts_render_binding` narrows them - with `content_type`,
`size_bytes` and `sha256` - to NOT NULL for `resume_pdf` and `resume_docx`.

`uq_artifacts_resume_render` (unique on `resume_id, kind, template_version`
where both are non-null) is the idempotency key. A replayed event cannot
create a second row, and a template change produces a new artifact rather
than silently replacing the file a human already downloaded.

## Privacy

Storage keys are `resumes/<uuid>/<template>/resume.<ext>` - identifiers and
fixed words. Download filenames are
`jobforge-resume-<application-uuid>-v<n>.<ext>`. Neither has a branch that
can read a profile. A key ends up in bucket listings and access logs; a
filename ends up in a downloads folder and an email attachment.

Downloads stream through the authenticated API. There is no presigned-URL
path and the `ArtifactStore` interface deliberately offers no way to mint
one: a presigned URL is a bearer token for one person's resume that works
for anyone holding it, with no session and no role check.

`ALLOWED_CONTENT_TYPES` is asserted on write and re-checked on read; a
content type is never inferred from a filename or echoed from a request.

## Determinism — measured

    same canonical content + same resume version + same template version
        -> byte-identical artifact

Verified across **four separate interpreter processes**, 1.2s apart, with
`PYTHONHASHSEED=random`. Both formats produced identical SHA-256 digests.
Two calls on one object would not have proven this; a per-process check was
the point.

**DOCX.** Exactly one thing was actually nondeterministic: python-docx stamps
every ZIP entry with the wall clock at save time, so two renders a second
apart differ. `_normalise` repacks the archive with a fixed 1980-01-01
timestamp, preserving each entry's name, bytes, compression type and
attributes - nothing inside any entry is parsed or rewritten, so it cannot
produce an archive Word will not open, and a test reopens the file to prove
it. Two things folklore predicts would need surgery turned out not to:
`docProps/core.xml` dates come from the template as a 2013 constant rather
than the clock (pinned through python-docx's own API anyway), and `w:rsid`
values do not vary between runs. Stripping rsids with a regex was the
brittle manipulation to avoid, and measurement showed it buys nothing.

**PDF.** `Canvas(..., invariant=1)` pins `/CreationDate`, `/ModDate` and the
`/ID` array, which are otherwise the clock and a random value. Only base-14
fonts are used, so nothing is embedded - font subsetting is the remaining
realistic source of PDF nondeterminism, and not embedding avoids it outright
rather than betting on it.

## ATS extraction — measured

Text extracts cleanly under **two independent extractors**, pypdf and
pdfminer, with no control characters and no `(cid:` markers.

One real defect was found this way and fixed. Rendering bullets with U+2022
in ReportLab's base-14 Helvetica produced `\x7f` under pypdf and `(cid:127)`
under pdfminer - both extractors agreeing that an ATS would not see a bullet
there. A control character at the front of every achievement is worse than a
plain hyphen, so the bullet is an ASCII `-`, joined to its line rather than
drawn as a separate string (a lone glyph on its own line extracts as its own
fragment, which a parser can read as content).

`tests/unit/test_render_determinism.py` is the arbiter. It is written
against the `ResumeRenderer` protocol, so the real renderers are judged by
exactly these assertions with no rewriting: a renderer joins the suite the
moment its `implemented` flag turns True.

`available()` and `implemented` are separate on purpose. A library can be
installed in a process whose renderer body is still unwritten, and
conflating the two would let an unwritten renderer look ready because its
import succeeded.

## Failing closed

`RendererUnavailableError` is raised when a dependency or a body is missing.
There is no fallback to the reference renderer. A plain-text file with a
`.docx` name is refused by Word and parsed as garbage by an ATS; shipping
one to a candidate about to apply for a job is worse than producing nothing.

`ReferenceTextRenderer` exists to exercise the pipeline. It declares
`production = False`, and `render_resume` refuses a non-production renderer
unless the caller opts in explicitly, so it cannot become the thing that
serves a real download by being the only renderer that imports.

Likewise `artifact_store()` has no "fall back to the filesystem if S3 is not
configured" branch: a deployment that believes it writes to private S3 must
not quietly start writing resumes onto a container's local disk.

## Anti-fabrication

Two proofs, because the cheap one is not enough on its own.

Structural: no module under `packages/services/rendering` may import httpx,
requests, an provider SDK or the LLM client. There is no model and no network
in this path.

Behavioural, and the one that matters: every alphanumeric token in a rendered
artifact must appear in the persisted content or in a short allowlist of
static chrome. An implementation could pass an import audit and still add
"Results-driven professional" to a summary or round 12 minutes up to 15;
this compares the produced document against the input word for word, for all
three renderers, and separately asserts that no numeral appears that nobody
approved. The heading allowlist is read out of `SECTION_HEADINGS` rather than
duplicated, so it cannot drift into agreeing with whatever the renderer
happens to emit.

## Still requiring executable verification

- ~~`S3ArtifactStore`~~ - now implemented with boto3, private bucket, no ACL
  argument anywhere in the adapter. Verified with focused unit tests
  (`tests/unit/test_artifact_store.py`, offline via `botocore.stub.Stubber`)
  and a real-MinIO integration suite
  (`tests/integration/test_s3_artifact_store.py`, `put -> exists/head -> get`,
  content-type and exact-byte round-trip, missing-object and
  unreachable-endpoint paths). MinIO only - not a claim about real AWS S3.
- The render service, the render worker, both API endpoints, the migration
  applied against a real database, `tests/integration/test_artifacts.py` and
  its `approved_resume_fixtures` fixture: written and parsing, never
  executed, because SQLAlchemy, FastAPI and pytest are absent.
- The whole M8 unit suite under the repository's real pytest. It has been run
  here only through a temporary stdlib harness (see below), which drives the
  same assertions against the same production libraries but is not pytest.

## A note on how the unit suite was run

pytest is not installed in the environment that wrote this. The 104 unit
assertions were executed by a throwaway harness in /tmp - never in the
repository - that stubs structlog and implements the small slice of the
pytest API these files use. The renderers, python-docx, ReportLab, pypdf and
pdfminer were all the real thing; only the test runner was not. Results from
it are evidence, not a substitute for `make test-unit` on the Mac.
