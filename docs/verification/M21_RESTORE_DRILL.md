# M21 backup and isolated restore drill (2026-11-10)

Synthetic data only. Source: the `jobforge-prodtest` rehearsal stack
(release `62ac2c04134b`), seeded with a synthetic owner created through the
real `bootstrap_owner` code (scripted input), a synthetic job, and a
synthetic artifact file. Drill key pair, passphrase and env files lived in
the session scratchpad, outside the repository. Nothing was deployed and no
real data was involved.

## Backup (`python -m scripts.backup create`)

| Check | Result |
|---|---|
| Duration | 1.5 s |
| Files | `globals.sql.jfenc` (2 KB), `database.dump.jfenc` (186 KB), `artifacts.tar.jfenc` (10 KB), `manifest.json` (mode 600 / 644) |
| Keyless `verify` | ok; revision `0011_application_tracking`; row counts users 1, jobs 1, others 0 |
| Plaintext on disk | none: no `PGDMP`, owner email or job title bytes anywhere in the bundle |

## Restore (`python -m scripts.restore`, new projects only)

| Attempt | Outcome |
|---|---|
| `jobforge-restoretest` | artifact step failed: the streamed steps did not receive the compose environment (bug, fixed in `c72ba2a`) |
| `jobforge-restoretest2` | same bug, now diagnosed by the new error reporting |
| `jobforge-restoretest3` | **passed the tool's checks (94 s)**, but the target had a freshly generated `TOTP_ENCRYPTION_KEY`: the restored owner's TOTP secret could not be decrypted, so the owner could not have signed in. New fatal preflight check `owner_totp_decryptable` added (`c72ba2a`). |
| `jobforge-restoretest4` (target env with fresh secrets **except** the source's TOTP key) | data restored (row counts match, 1/1 artifact files); first preflight timed out under memory pressure (four stacks on a 4 GB Docker VM); re-run after stopping the other stacks: **all fatal checks pass, `owner_totp_decryptable` 1/1** |

End-to-end sign-in on the restored data (`jobforge-restoretest4`, edge
`127.0.0.1:8485`): password + current authenticator code → **200, role
owner**; wrong code → **401**. The synthetic owner's email had to be
changed from `example.invalid` to `example.com` in the drill copy, which
exposed a third defect: `bootstrap-owner` accepted an address that sign-in
rejects (fixed in `d6b7d62`).

Corruption and interruption are covered by tests rather than live runs:
tampered, reordered, truncated and wrong-key ciphertext fail
(`tests/unit/test_backup_crypto.py`); a bundle without a manifest, with a
modified file, or with a missing file is refused before any container
starts (`tests/unit/test_backup_runner.py`). An interrupted `create`
leaves only a `.partial-*` directory, which `verify`, `restore` and
`retention` never treat as a backup.

## Recovery requirements learned

1. The restore env **must** carry the source's `TOTP_ENCRYPTION_KEY` (keep
   an offline copy of `prod.env`). Other secrets may be new: `init` resets
   the runtime role passwords and the restore resets the owner role.
2. Keep the backup private key and its passphrase offline; the Mac holds
   only the public key.
3. Restore on a quiet Docker host (the 4 GB VM struggled with four stacks).
4. Measured RTO for this tiny data set: about 1.5–9 minutes depending on
   host load; real data will differ and should be re-measured at the first
   real drill.

## State left behind (needs owner approval to remove)

Stopped, not removed: `jobforge-prodtest`, `jobforge-restoretest`,
`jobforge-restoretest2`, `jobforge-restoretest3`, `jobforge-restoretest4`
(all synthetic). Their volumes are kept.
