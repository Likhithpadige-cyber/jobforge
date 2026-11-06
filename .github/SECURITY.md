# Security policy

JobForge is a portfolio project maintained by one person. It has **no hosted
deployment** and no published releases; the only supported version is the
latest commit on `main`. The security design is described in
[`docs/SECURITY.md`](../docs/SECURITY.md).

## Reporting a vulnerability

Please report security problems **privately**:

1. Open this repository's **Security** tab and choose **Report a
   vulnerability** (GitHub private vulnerability reporting).
2. Describe the issue, the affected file or endpoint, the steps to reproduce
   it on a local copy, and the impact you expect.

Please do **not** open a public issue, pull request or discussion with
details of a vulnerability. If private reporting is unavailable, open an issue
that says only that you have a security report and need a private channel,
without any technical detail.

There is no bug bounty. If you would like credit, say how you want to be
named; reports are credited in the fix's changelog entry unless you prefer
otherwise.

## What to expect

Reports are handled on a best-effort basis by a single maintainer:

- an acknowledgement, normally within 7 days;
- an assessment and, for a confirmed issue, a fix on `main` or a documented
  mitigation;
- coordinated disclosure: please allow time for a fix before publishing.

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
