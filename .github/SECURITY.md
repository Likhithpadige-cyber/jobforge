# Security policy

JobForge is a portfolio project maintained by one person. It has **no hosted
deployment** and no published releases; the only maintained version is the
latest commit on `main`. The security design is described in
[`docs/SECURITY.md`](../docs/SECURITY.md).

## Reporting a vulnerability

Please report security problems **privately**, and never include technical
details in a public issue, pull request or discussion.

**Current status.** GitHub's private vulnerability reporting is **not
enabled** for this repository, so there is no private reporting form. No
maintainer email address is published.

**How to report today:** open an issue titled **"Security contact request"**
that contains no details of the problem, so that the maintainer can arrange a
private way to receive the report.

If private vulnerability reporting is enabled in future, this policy will be
updated to point to the repository's **Security** tab instead.

A useful report describes the issue, the affected file or endpoint, the steps
to reproduce it on a local copy, and the expected impact.

## What to expect

Reports are handled on a best-effort basis by a single maintainer, without a
guaranteed response time or outcome. The aim is to fix confirmed issues on
`main` or document a mitigation. Please allow time for a fix before disclosing
an issue publicly. There is no bug bounty.

## Scope

In scope, in this repository's code and configuration:

- authentication, sessions, CSRF and the owner/reviewer role boundary;
- bypasses of the submission safeguards (dry-run mode, the owner switch,
  approvals, readiness checks or the recipient rules);
- bypasses of fact grounding or citation verification, including through
  prompt injection in job postings;
- exposure of secrets or personal data through the code, logs, CI workflows
  or build artifacts;
- injection or privilege issues in the GitHub Actions workflows.

Out of scope:

- any system you do not own: there is no JobForge service to test, and you
  must never test against job sites, employers or other third parties;
- vulnerabilities in dependencies, unless JobForge's own use makes them
  exploitable (report those upstream);
- the development-only MinIO image's known upstream advisories and the
  default local credentials, which are documented and bound to loopback;
- denial of service, social engineering and physical attacks.

## Testing safely

Use only a local copy with the fictional demo dataset
([`docs/DEMO_ENVIRONMENT.md`](../docs/DEMO_ENVIRONMENT.md)). Keep the default
safe modes (`SUBMISSION_MODE=dry_run`, `EMAIL_TRANSPORT_MODE=fake`,
`LLM_MODE=replay`), use no real personal data, and never send real email or
job applications.
