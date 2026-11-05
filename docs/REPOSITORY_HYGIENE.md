# M14 repository hygiene review

Review date: 2026-11-05. This is a local source review, not a forensic guarantee
that no secret has ever existed in any external clone or remote ref.

## Tracked artifacts and secrets

- Reviewed tracked paths for local env files, private keys, databases, archives,
  logs, build output, generated resumes, caches and browser artifacts. No such
  unintended tracked files were found. `.env.example` contains local placeholders;
  the login-scene PNG is an intentional public UI asset. Synthetic fixtures and
  cassettes are intentional source inputs, not real candidate documents.
- `git log --all -- .env .env.demo` returned no history in the available local
  refs. Existing ignored `.env`/`.env.demo` values were neither printed nor
  copied. No credentials were rotated and no history was rewritten.
- The tracked-file detect-secrets hook uses `.secrets.baseline`. M14 Phase 1
  refreshed public npm SHA-512 checksums; Phase 2 added reviewed runner-only
  dummy credentials. No blanket file exclusion was added to the CI scan.
- The scaffold verifier previously walked ignored `.venv` packages, reporting
  public example credentials as committed secrets. Its secret check now uses
  `git ls-files -z`, including staged additions. Four regression tests cover
  ignored local/tooling data, a force-added ignored secret, failure when Git metadata is unavailable, and a script filename that must
  not exempt tracked credentials. It does not replace the full CI secret scanner.
- Treat a real accidental credential commit as an incident: revoke/rotate the
  credential, assess exposure and arrange history cleanup separately. Ignore
  rules do not remove files already tracked.

## Ignore rules and build context

`.gitignore` already excluded env files, real profile inputs, generated artifacts,
private keys, virtualenvs, node_modules, Next output, coverage, local backups,
Terraform state and live cassettes. M14 adds `test-results/`,
`playwright-report/`, `blob-report/`, `.auth/` and HAR files because browser
reports/traces and stored sessions can contain cookies, screenshots and PII.
These directory patterns apply to nested paths as well as the repository root.
Do not store auth state under an arbitrary unignored JSON filename.

`.dockerignore` now also excludes private profile/seed inputs, uploads, backups,
offline evaluation output, browser output/auth state, nested env files/private
keys/Terraform state, coverage and TypeScript
build caches. Generated env files stay excluded; only `.env.example` is allowed
as a template. A file being ignored by Git alone does not keep it out of Docker's
build context. A tiny synthetic scratch build/export verified **22 excluded
sensitive/output paths**, preserving source/lock files and root/nested env
examples. No application image was built and no valued ignored files were deleted.

Dependency locks, source code, schema migrations, public assets, synthetic
fixtures and documentation remain tracked. No license was added. Terraform
currently has only `.gitkeep`; no lockfile/state or cloud infrastructure was
created or modified as part of M14.

## Unresolved architecture finding

`make verify`'s intended import direction is apps → agents/services → core →
shared. Before M14 Phase 3, it reported **31 upward import occurrences** from
core repositories/services into agents, rendering, ingestion and email delivery.
These are existing orchestration boundaries, not introduced by M14. The final
check deliberately continues to fail on them; no allowlist or weakened import
rule was added. Baseline scaffold result: **21 passed, 2 failed** (imports and
virtualenv secret false positives). After the scanner repair: **22 passed,
1 failed**, solely the existing import-direction finding.

A boundary refactor needs its own design and regression review; moving those
services during a documentation/hygiene milestone would change unrelated
runtime architecture. The application regression suites remain green. This
finding is not hidden by the required CI checks: the scaffold aggregate is a
separate diagnostic and is not a required `ci.yml` job. CI's schema, imports via
mypy, test, secrets and dependency gates are separate checks.

Other verification limits: hosted Actions and Docker production image builds
were not run/pushed in M14. Workflow syntax, shell commands, production Next.js
build and all specified backend/frontend/E2E suites were verified locally.
Exact counts and recovered browser setup failures are in the
[milestone changelog](MILESTONE_CHANGELOG.md).
