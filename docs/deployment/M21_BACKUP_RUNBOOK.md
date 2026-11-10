# M21 — backup and restore runbook (private runtime)

Verified by the isolated drill in `docs/verification/M21_RESTORE_DRILL.md`.
Steps marked **[owner]** handle secrets or install something and are done
by the owner. Python commands run from the repository with the project's
virtual environment.

## One-time setup **[owner]**

1. Create the backup key pair outside the repository:
   `python -m scripts.backup keygen ~/JobForgeKeys` (asks for a passphrase).
2. Keep `backup.pub` on the Mac (e.g. `~/Library/Application Support/JobForge/`).
   Move `backup.key` **off the Mac** (password manager attachment, encrypted
   USB) together with its passphrase. Without both, backups cannot be restored.
3. Keep an offline copy of `prod.env`. The restore needs its
   `TOTP_ENCRYPTION_KEY`; without it every owner is locked out of the
   restored data (found in the drill).
4. Choose a backup folder outside the repository, e.g. an external drive or
   `~/Library/Application Support/JobForge/backups` (iCloud Drive is fine:
   bundles are already encrypted).

## Taking a backup (before every release, and daily)

```sh
python -m scripts.backup create --project jobforge-prod \
  --public-key ~/Library/Application\ Support/JobForge/backup.pub \
  --dest /Volumes/Backup/JobForge
```

- Checks free space first; writes `.partial-…` and renames only after the
  bundle verifies. Plaintext never reaches the disk.
- `python -m scripts.backup verify <bundle>` re-checks a bundle at any time
  without the private key.
- `JOBFORGE_BACKUP_DIR=/Volumes/Backup/JobForge scripts/prod.sh preflight`
  reports the newest bundle's age (warning after 26 hours).

## Retention

`python -m scripts.backup retention --dest DIR` prints what a 7-daily /
4-weekly policy would keep and delete. `--apply` deletes, only after the
newest bundle verifies, and never touches other names. Applying it is the
owner's decision.

## Restore (into a NEW project; production is never overwritten)

1. Prepare a restore env file outside the repository (mode 600): start from
   the offline `prod.env` copy (or `python -m scripts.prod_env generate-env`
   and paste in the original `TOTP_ENCRYPTION_KEY`), and set
   `CORS_ORIGINS=["http://127.0.0.1:<port>"]`.
2. Stop other JobForge stacks (Docker's VM has about 4 GB).
3. Run:
   ```sh
   python -m scripts.restore /Volumes/Backup/JobForge/jobforge-backup-YYYYMMDDTHHMMSSZ \
     --private-key /path/to/backup.key --target-project jobforge-restore1 \
     --env-file /path/to/restore.env --port 8490 --release <image tag>
   ```
   It refuses `jobforge-prod`, any project that already has volumes, and any
   port used by another stack. It reports row-count and artifact matches and
   the preflight result as JSON.
4. Sign in at `http://127.0.0.1:<port>` (password + authenticator) and spot-check.
5. Switching the restored stack into service (renaming it to production) is
   a deliberate, manual step; keep the damaged volumes until the owner
   decides about them.

## Monthly drill

Restore the newest bundle into a fresh `jobforge-restore*` project, sign in,
record the elapsed time and results in `docs/verification/`. Removing the
drill project's containers and volumes afterwards is an owner decision.

## Scheduling (proposal only — needs approval before installing)

A user LaunchAgent could run the backup daily. **Not installed.** Example
`~/Library/LaunchAgents/com.jobforge.backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobforge.backup</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/venv/bin/python</string><string>-m</string><string>scripts.backup</string>
    <string>create</string><string>--project</string><string>jobforge-prod</string>
    <string>--public-key</string><string>/Users/you/Library/Application Support/JobForge/backup.pub</string>
    <string>--dest</string><string>/Volumes/Backup/JobForge</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/jobforge</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/jobforge-backup.log</string>
</dict></plist>
```

The runner prints JSON only (bundle path or error), never secrets.
