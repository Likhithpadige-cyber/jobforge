# Authentication, authorization and safe operation

This describes implemented behavior through M14. Broader production controls in
[SECURITY.md](SECURITY.md) are design requirements unless identified as delivered.
No production deployment or cloud infrastructure was provisioned by M14.

## Identity and sessions

- `POST /api/v1/auth/login` checks an Argon2id password. TOTP is required
  outside `local`/`test`; optional locally, but any supplied code must be valid.
  Unknown accounts, bad passwords, missing enrollment and bad TOTP share the
  same login-failure response.
- TOTP secrets are AES-256-GCM encrypted with `TOTP_ENCRYPTION_KEY` (32 random
  bytes, base64); `SESSION_SECRET` is independent. Changing the encryption key
  without migrating enrolled secrets makes those enrollments unusable.
- Redis stores opaque sessions. Defaults are 720 idle minutes and 168 absolute
  hours. Logout revokes the session. A missing session or deleted user is
  unauthenticated; no request header/body/query field selects the actor.
- `jobforge_session` is HttpOnly and SameSite=Strict. Secure is off only for
  local/test HTTP. `jobforge_csrf` is deliberately readable by the client.
  Mutations require cookie/header/session agreement through `X-CSRF-Token`;
  login is the pre-session exception.
- Login budgets default to 20 attempts per socket-peer address and eight per
  email identifier per 900 seconds. Forwarded headers are not trusted. A proxy
  can make all users share one peer-address budget; do not bypass limits to
  repeat browser tests.
- Current authentication is password/TOTP, including the non-local code path.
  Cognito/OIDC, WebAuthn/passkeys and scoped personal access tokens are plans,
  not implemented alternatives. Non-local settings validation is not a
  production-readiness certification.

Local setup uses `make seed`, `make auth-key` and `make setup-auth`. Keep keys
in ignored config and the one-time enrollment URI private. The isolated demo
instead creates random local accounts/keys with `make demo-env`, `demo-init`
and `demo-seed`. It never copies production credentials.

## Authorization boundary

The system has one shared candidate workspace with owner/reviewer roles.
It does not promise isolation between accounts or candidate tenants. The
[M7 boundary decision](M7_AUTHORIZATION_BOUNDARY.md) remains unchanged.

| Capability | Owner | Reviewer |
|---|---|---|
| Workspace review/read surfaces and human review decisions | Allowed | Allowed where exposed by the review routes |
| Private profile fields | Visible | Redacted |
| Profile/fact writes, attribution and fact verification | Allowed | Refused |
| Master-resume import, first-draft request | Allowed | Refused |
| Job import/discard and application preparation | Allowed | Refused |
| Submission setting read | Allowed | Allowed |
| Change persisted submission switch or explicit Submit | Allowed subject to all safety gates | Refused |
| Monitoring and alert operations | Owner-only routes | Refused |

`apps/api/deps.py` loads the real user and checks roles/CSRF server-side.
Owner-only checks precede resource lookup, so a forbidden ID and a missing ID
both return 403 to a reviewer. Read visibility and private-field redaction
are separate policies; a hidden UI button provides no security by itself.
Fact `added_by`/`verified_by` and review actors come from the session, not a
spoofed request field. Candidate provenance checks reject cross-profile facts,
master resumes and evaluations even within the shared workspace.

Database roles add a separate boundary: app_agent cannot write verified facts
or human approvals; app_monitor has narrow monitoring privileges; app_audit
has column/table-scoped inference-audit writes. app_migrate owns schema work.
Set role passwords after migrations with `make db-roles`. `AUDIT_DATABASE_URL`
should select app_audit when privilege-separated auditing is needed. Without
it, audit writes use a separate transaction/engine with the domain DSN; they
retain rollback durability but lose that privilege separation. Workers must
use explicit least-privileged DSNs, not a schema-owner fallback.

## Submission and external traffic

The system-wide gate is the conjunction of `SUBMISSION_MODE=enabled` and the
persisted `settings.submission_enabled=true`. Missing persisted state is false.
The gate read endpoint reports both values and `live`; a toggle alone cannot
change an environment configured for dry-run.

An owner must also explicitly submit one `SUBMISSION_READY` application.
The server rechecks current review completion, materials, valid/unexpired
EMAIL site policy, an authoritative persisted `mailto:` destination matching
that policy, and an unexecuted attempt. Request-supplied recipient addresses,
policy bypasses and missing approvals fail closed. Durable attempts/retry
checks guard duplicate execution. Manual handoff is available when applicable;
arbitrary ATS browser automation is not implemented.

`EMAIL_TRANSPORT_MODE` chooses a transport, not permission. `fake` opens no
SMTP socket; `smtp` is network-capable and must be configured deliberately.
Likewise, `LLM_MODE=live`/`record` can call a provider and incur cost; `replay`
requires matching recorded data. Do not infer network isolation from the
submission gate alone. Development and regression verification use all three:

```sh
export LLM_MODE=replay SUBMISSION_MODE=dry_run EMAIL_TRANSPORT_MODE=fake
```

The browser fixture checks demo identity plus the running API gate before
mutations. API E2E replaces both email transports with failing tripwires.
No verification requires sending real email or job applications. Generation
workers and submission workers are not started by the browser CI workflow.

## Limits and troubleshooting

- Local development uses HTTP and may publish ports beyond loopback. Keep it
  on a trusted machine; do not expose local single-factor mode publicly.
- Production web builds use their configured public API origin, rather than
  the development Next.js rewrite. Cookies, CORS and deployment routing need
  explicit review before deployment. No trusted proxy model is implemented.
- S3 is the default artifact backend. MinIO reachability, endpoint, region,
  credentials and bucket must agree. Filesystem storage is explicit, not an
  automatic recovery path. Do not place generated resumes in public web assets.
- `401`: inspect session/cookie host and Redis; `403`: inspect the role policy;
  CSRF failures: compare readable cookie and header with the same live session.
  `429`: wait for the rate-limit window. Never disable the checks for tests.
- `409` on profile update means a stale version; reload before retrying edits.
- Failed/skimmed security verification: apply migrations, configure app_agent
  and app_audit logins, run `make diagnose-roles`, then require non-skipped
  results. See [CI.md](CI.md) for report validation and exact commands.
- Demo browser records are fictional and run-unique but persist after tests.
  Stop only processes started for the run. Check port ownership before using
  `make demo-web-host`, which stops the demo web container.
- `make verify` retains an existing architecture failure: 31 upward import
  occurrences from core into agents/services. See
  [repository hygiene](REPOSITORY_HYGIENE.md). Do not treat that check as green.
