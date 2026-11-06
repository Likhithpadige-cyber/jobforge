# M16.6 evidence binding and source repair

This correction addresses Cowork's High residual at `b9031f6`. No schema, rendering template, SMTP, dispatch or application state-machine changes are required.

## Immutable approval material

`evidence_fingerprints` is a schema-versioned map stored in the approved snapshot. A reviewer text-only approval stores it at the snapshot root together with `content_digest`. Owner contact consent includes it in `export_document`, which is bound by the existing export digest to the exact résumé ID, version and content. Existing approved fingerprint maps are never refreshed by rendering, package generation or download.

Each fact entry contains a content-addressed `version` and a schema-bound SHA-256 fingerprint. The canonical material includes fact ID, profile ID, text, structured JSON, fact type, source table/ID, confidence and validity dates. There is no numeric fact revision column in the existing schema; the `version` is the SHA-256 material revision, not an invented sequence or an `updated_at` alias. Canonical JSON sorts keys and normalizes text to NFC.

Status, deletion and current validity are checked separately every time. Verification timestamps/actor bookkeeping, usage counts, computed embeddings and row-update timestamps do not change the material version. Thus an unchanged fact may be unverified and reverified; an edit followed by verification cannot revive an old approval. Capturing the profile's verified facts permits later package checks to bind cited cover-letter and answer evidence too; only required citations are compared. New citations without a captured proof fail closed and require review.

Initial rendering, artifact reuse, manual package generation/reuse, legacy packet generation/download and PDF/DOCX download compare the current required material versions with the approved map. Missing containers, unsupported fingerprint schema versions, missing entries, malformed hashes and material mismatches return conflict before bytes are served. Current production PDF/DOCX rows without bound contact consent are refused. The explicitly non-production plain-text reference renderer remains available to tests under its existing guard.

Approval and evidence rows are locked/refetched. Review/consent locks the application then approval before evidence; renders and manual packages use the same order. Download locks its approval before evidence. Source validation uses the profile lock without taking a child write lock in the opposite order to existing child CRUD. Existing post-storage approval/material rechecks and actual-byte digest verification remain active. Controlled storage-boundary mutation tests are not an exhaustive multi-process concurrency proof.

## Compatibility without fabricated history

Known legacy text-only templates still render approved prose without importing contact details. New text approvals already have immutable fact versions. For genuine older text-only approvals without them, the compatibility path checks current validity and conservatively refuses facts updated after the recorded review time; it never creates a retroactive approved material version. Historical timestamps are a compatibility boundary, not a content history or a proof that old source records existed.

Owner consent after a reviewer's approval compares the original prose-approval fingerprints before binding contact consent. Original decision attribution/time and résumé version are retained. Edited/reverified evidence cannot be silently rebound by obtaining a fresh export preview.

Older contact-bearing consent without fingerprints fails closed. If there is no earlier immutable prose-approval proof, a new owner click cannot manufacture that history: a fresh résumé version/review is required. A narrowly supported explicit consent renewal is possible only when an earlier prose proof exists, that proof still matches, and the entire existing export document is unchanged apart from the added proof. No retained consent/data is migrated or refreshed automatically. Main at `6945454` predates contact-export consent, so its legacy text approvals retain their established path.

## Dangling-source prevention and explicit repair

New public fact creation validates that the source exists, is undeleted and belongs to the candidate profile, and that the authenticated owner owns that profile. Profile-source facts must point to the same profile. This is API/service validation of polymorphic references; raw administrative SQL and trusted low-level fixture insertion are not converted into polymorphic foreign keys.

Owner export preview checks cited source records and returns an actionable conflict naming the fact and repair endpoint. It never guesses an employer, relinks a claim or reseeds data.

`POST /api/v1/profile/facts/{fact_id}/repair-source` is owner-only and requires normal session authentication/CSRF. Its JSON body is:

```json
{
  "source_id": "00000000-0000-0000-0000-000000000123",
  "expected_updated_at": "2026-11-06T12:00:00Z",
  "note": "Selected the correct synthetic employment record"
}
```

The owner selects an active record in the same source table/profile and supplies the timestamp from a current fact read. The service locks the profile and original fact, rejects stale/repeated requests, valid original sources, foreign/missing targets, duplicate replacements and unauthorized actors. It creates a new **draft** with copied substance and the selected source. The original retains its ID, text, structured content and source reference and becomes retired. Both facts receive owner-attributed, timestamped attestation history linking original and replacement with the explanation. The operation is one transaction and does not commit in the service.

The replacement is not automatically verified. The original approved résumé is not edited, relinked or version-bumped; its citation of the retired fact remains blocked, even after the replacement is verified. Therefore historical evidence can be repaired while preserving the old résumé record, but **continued export requires freshly generated, verified and approved materials citing the replacement**.

## Reapproval workflow and limits

The owner reviews the repair history, verifies the replacement, refreshes job evaluation against current evidence, generates a new draft and obtains all required human approvals and export consent. While review is pending, the existing request-changes/rework flow supplies the new version. For `policy_blocked` applications, the existing first-draft service can create a fresh application for the eligible job after current evidence/evaluation is prepared.

Existing `approved`, `submission_ready` and `manual_handoff` applications do not have a general reopen/reapproval endpoint. This correction deliberately does not invent state-machine edges or reset their approvals. Those states remain safely blocked after evidence changes until a separately designed/approved reapproval path is available. This is a product workflow limitation, not permission to reuse the stale claim. No live/demo repair or reseed was executed.
