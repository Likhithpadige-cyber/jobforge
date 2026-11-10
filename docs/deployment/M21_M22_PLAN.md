# JobForge M21 (backups and recovery) and M22 (private iPhone access) — plan

Status: **plan only** (2026-11-10). Builds on the M20 private runtime design
(`M20_PRIVATE_RUNTIME_DESIGN.md`). No paid services. Every step that installs
software, schedules a background job, changes a Mac or account setting, or
deletes data is an **owner action or needs separate approval**; those are
marked ⚠.

## M21 — backups and recovery

### Targets

Personal single-user data: **RPO 24 h, RTO 2 h**, proven by a timed drill
before real data is trusted to the stack. A 15-minute RPO would need WAL
archiving and is not planned.

### What is backed up

| Item | How | Notes |
|---|---|---|
| PostgreSQL | `pg_dump --format=custom` from a one-shot `backup` container on the `data` network | consistent snapshot; roles/grants captured separately with `pg_dumpall --globals-only` (contains password hashes → treated as a secret) |
| Artifacts | `mc mirror` of the private bucket to the backup staging dir | plus a manifest of object keys + SHA-256, cross-checked against the DB's artifact rows |
| Config/keys | `prod.env`, backup public key fingerprint, release manifest (image tags, Alembic revision) | `prod.env` copy kept offline by the owner, never inside the backup bundle |
| Redis | **not backed up** | sessions/limits are rebuildable; restoring them would resurrect revoked sessions |

### Encryption and storage

- Each run produces one bundle, encrypted with [age](https://age-encryption.org)
  to a public key; the private key is held offline by the owner (password
  manager or printed). Plaintext dumps exist only inside the container's
  tmpfs and are never written to the repository or the host unencrypted.
- Destinations (free): an external drive and/or an iCloud Drive folder.
  The bundle is already encrypted, so cloud sync sees ciphertext only.
- Retention: 7 daily + 4 weekly. Pruning only deletes bundles older than
  the policy **and** only after a newer bundle has verified (§Verification).
  The first enabling of pruning is a separate approval ⚠.

### Scheduling

- A `scripts/backup.sh` wrapper (pinned to `-p jobforge-prod`) runs the
  backup container, encrypts, verifies and copies.
- Daily scheduling needs a launchd user agent ⚠ (persistent background
  service: requires the owner's separate approval before installation).
  Until then, backups are run manually and before every release (M20 §7).
- Missing or failed backups raise a JobForge alert via the existing monitor
  (a `backup.completed` marker row the monitor checks for freshness).

### Verification (every run)

1. `pg_restore --list` succeeds on the decrypted dump (in tmpfs).
2. Object manifest count and checksums match the DB's artifact rows.
3. Bundle decrypts with a test of the key fingerprint only (not the
   private key) — the full decrypt is part of the drill.

### Restore drill (monthly, and before go-live)

Restore into a **new** isolated compose project (`jobforge-restoretest`),
never over the live stack:

1. Decrypt with the offline key; restore globals, then the dump, with the
   reviewed administrator role (no `--no-owner`/`--no-acl`).
2. Mirror artifacts into a fresh bucket; verify checksums.
3. Start with egress blocked and submission switch false; run the M20
   preflight; sign in (TOTP), check CSRF, open a résumé artifact and verify
   its checksum, confirm the dry-run refusal and the tracking dashboard.
4. Record achieved RPO/RTO in `docs/verification/`.
5. Tearing down the drill project is a separate approval ⚠ (it deletes
   the drill's volumes, which contain a copy of real data).

### Recovery runbook outline

Lost/corrupted DB → stop api/workers → restore into a new project (drill
steps) → validate → switch the wrapper to the restored project → keep the
broken volumes until the owner approves deletion ⚠. A fresh Redis is always
used after a restore, which signs everyone out.

## M22 — private iPhone access through Tailscale

### Approach

[Tailscale Serve](https://tailscale.com/kb/1312/serve) on the Mac proxies
`https://<mac-name>.<tailnet>.ts.net` → `http://127.0.0.1:8480` (the M20
edge). Serve is **tailnet-only**; **Funnel (public internet) is never
enabled.** Tailscale provides a valid TLS certificate for the `ts.net` name,
which satisfies the Secure session/CSRF cookies used outside local mode.

### Owner actions ⚠ (Claude does not perform these)

- Install Tailscale on the Mac and iPhone; sign in with the owner's account.
- In the admin console: enable MagicDNS and HTTPS certificates; keep key
  expiry on; require device approval.
- ACL: only the owner's devices may reach the Mac, and only on port 443.
- Run the `tailscale serve` command (Claude can prepare it for review).

### JobForge changes (code, testable locally)

1. Configure the single origin: `CORS_ORIGINS` and the web build's
   `NEXT_PUBLIC_API_BASE_URL` set to the `ts.net` URL; edge trusts
   `X-Forwarded-*` only from Tailscale Serve on loopback.
2. **Sign out everywhere** (new): revoke all of a user's sessions (per-user
   session index or a session epoch checked on load), exposed in Settings.
   Required for the lost-phone procedure.
3. Mobile pass on the core screens at 375 px (Job Feed, application
   workspace, Tracking card, Follow-ups) — Playwright already has a mobile
   viewport helper; add journeys for tracking and follow-ups.
4. Optional: a web app manifest so Safari "Add to Home Screen" opens
   JobForge full-screen (no service worker caching of authenticated pages).

### Security checks before use

- From a non-tailnet network the URL does not resolve or connect.
- From the tailnet, `127.0.0.1:8480`'s other ports stay unreachable.
- Login requires password + TOTP; sessions expire per existing limits.
- Lost phone: remove the device in the Tailscale admin console, then
  "Sign out everywhere" in JobForge, then rotate the password.

## Order

M20 steps 1–5 → M21 manual backup + first restore drill → real data loaded
→ M21 scheduling (approval) → M22 code changes → M22 owner setup → phone use.
