# M22 — private iPhone access through Tailscale (runbook)

Status: prepared, **not performed**. Every step marked **[owner]** installs
software, signs in to an account or changes an admin setting, and is done by
the owner, not by Claude. Nothing here enables public access.

```
iPhone (Tailscale app) ──tailnet (WireGuard)──▶ Mac: tailscale serve (HTTPS :443)
                                                   │ http://127.0.0.1:8480
                                                   ▼
                                       JobForge edge (docker-compose.prod.yml)
```

## 0. Prerequisites

- M20 stack running and `scripts/prod.sh preflight` passing (no `--bootstrap`).
- At least one verified M21 backup; the backup private key and a copy of
  `prod.env` stored offline.
- FileVault on and the macOS firewall on **[owner]**.
- "Sign out everywhere" available in Settings (built).

## 1. Install and sign in **[owner]**

1. Install Tailscale on the Mac (from tailscale.com or the Mac App Store)
   and sign in. Install the Tailscale app on the iPhone with the same account.
2. Admin console → **DNS**: enable MagicDNS and **HTTPS certificates**.
3. Admin console → **Settings**: keep **key expiry** on; enable **device
   approval** so new devices need your approval.
4. Note the Mac's tailnet name, e.g. `macbook.tail1234.ts.net`.

## 2. Restrict the tailnet **[owner]**

Access controls (example; adapt names): only your devices may reach the Mac,
and only on 443.

```json
{
  "tagOwners": {},
  "acls": [
    {"action": "accept", "src": ["you@example.com"], "dst": ["macbook:443"]}
  ]
}
```

Do not add `autogroup:internet` or exit-node rules for this purpose.

## 3. Point JobForge at its HTTPS name

1. In `prod.env` set
   `CORS_ORIGINS=["https://macbook.tail1234.ts.net"]` (JSON list; keep the
   loopback origin too if you use it on the Mac). Same-origin requests need
   no CORS, but preflight checks the value is HTTPS or loopback.
2. `scripts/prod.sh up` (recreates the API with the new value), then
   `scripts/prod.sh preflight`.

## 4. Serve inside the tailnet only **[owner]**

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8480
tailscale serve status
```

**Never** run `tailscale funnel` — Funnel publishes to the internet. Check
`tailscale funnel status` shows nothing.

Serve terminates TLS with a certificate for the `ts.net` name, so the
session and CSRF cookies (Secure outside local mode) work. The edge stays
bound to 127.0.0.1; nothing listens on the LAN.

## 5. iPhone

1. Tailscale app: connected.
2. Safari → `https://macbook.tail1234.ts.net` → sign in with password and
   authenticator code.
3. Optional: Share → **Add to Home Screen**.

## 6. Verification checklist (record results in `docs/verification/`)

- [ ] On the iPhone over Tailscale: the page loads with a valid certificate;
      sign-in with password + TOTP works; a wrong code is refused.
- [ ] With Tailscale **off** on the iPhone (mobile data): the name does not
      resolve or connect.
- [ ] From another device on the home Wi-Fi (not in the tailnet):
      `http://<mac-lan-ip>:8480` is refused (edge is loopback-only).
- [ ] `tailscale funnel status`: no Funnel.
- [ ] Settings › **Sign out everywhere** on the Mac signs the iPhone out
      (its next request returns to the sign-in page).
- [ ] Session expires after the configured idle time.
- [ ] `scripts/prod.sh check-ports`: only the edge, on 127.0.0.1.
- [ ] Core screens usable at phone width: Job Feed, application workspace,
      Tracking card, Follow-ups.

## 7. Lost or stolen device

1. Admin console → **Machines** → remove the device (cuts tailnet access).
2. JobForge → Settings → **Sign out everywhere** (revokes every session).
3. Change the JobForge password. If the authenticator was on that phone,
   TOTP must be re-enrolled: **a re-enrolment command for an existing
   owner is not built yet** (bootstrap refuses once an owner exists) —
   tracked as remaining M22 work. Until it exists, keep the authenticator's
   own backup (or a second device) so losing one phone does not lock you out.
4. Review recent activity in Agent Activity / Alerts.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Name does not resolve on the iPhone | Tailscale connected? MagicDNS enabled? |
| Certificate warning | HTTPS certificates enabled in the admin console; wait for issuance; `tailscale serve status` |
| Signed in but immediately signed out | Using `http://` instead of `https://`: Secure cookies are not sent over plain HTTP |
| 502 from the page | `scripts/prod.sh ps` — API/web healthy? `scripts/prod.sh preflight` |
| Works on the Mac at 127.0.0.1 but not in Safari there | Use the `https://…ts.net` name on the Mac too; browsers differ on Secure cookies for plain-HTTP loopback |

## 9. Known limitation

Behind the edge, every client shares one per-IP login rate-limit budget
(the API deliberately trusts no forwarded headers). The per-account limit
still applies. Acceptable for a single owner.
