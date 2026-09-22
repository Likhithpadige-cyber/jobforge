# M7 authorization boundary — approved

## Verified existing contract

SECURITY.md explicitly defines a single-user system and excludes multi-tenant
isolation from its threat model. M1/M2 introduced owner and reviewer roles, not
candidate-specific reviewer assignments. The authenticated resume/application
read routes in apps/api/routers/resumes.py query records without caller-profile
filters. The profile routes use ProfileRepository.get_singleton(). Profiles have
user_id, but there is no reviewer assignment relation. M5 can explicitly select
an owner profile for evaluation and records that profile ID in its evidence.

The M7 request requires ownership isolation as well as preservation of the
existing review workflow. These support two different interpretations; silently
changing shared access would change authorization policy, while claiming account
isolation from same-profile evidence checks alone would be inaccurate.

## Approved M7 scope: existing shared candidate workspace

- Retain the established shared owner/reviewer review access.
- Allow only owners to import the private master resume and request first drafts.
- Resolve the candidate explicitly; refuse ambiguous selection.
- Validate that master resume, verified facts, evaluation provenance, application
  and persisted generated claims all refer to that same candidate/profile.
- Reject foreign fact IDs, a foreign master resume or a mismatched evaluation;
  never use a global first-row lookup to resolve inputs.
- Leave verified-fact creation/verification under existing human-only rules.
- Keep unknown assessments on the existing M5 hold boundary.
- Do not call this per-account privacy or tenant isolation. All authenticated
  collaborators retain existing workspace-wide review visibility.

## Alternative: per-owner private workspaces

This would require scoped authorization on existing read/review/profile routes,
an explicit rule for assigning reviewers to owners, and review of shared job and
application uniqueness constraints. It is a consequential authorization/schema
extension beyond a first-draft entrypoint and must be specified and approved
before implementation. Merely adding an owner filter to one new route would not
provide privacy through existing read routes.

## Verification

Use synthetic data only. Exercise anonymous/reviewer/owner request permissions,
cross-profile source and citation rejection, replay/idempotency, prompt injection,
unsupported claims, stale/unresolved evaluation rejection and unchanged three-cycle
review behavior. Run focused M7, offline evaluations, lint/type checks, and the full
unit/integration/security suites. Keep the UI frozen, make no provider calls or
external submissions during verification, and do not start M8 or push GitHub.

The user explicitly approved the recommended boundary. M7 implements it without
new tenancy or schema changes. See M7_FIRST_DRAFT.md for the delivered contract.
