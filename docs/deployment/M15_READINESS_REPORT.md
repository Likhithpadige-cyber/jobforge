# JobForge M15 — production and portfolio readiness

Assessment date: **2026-11-05**. Baseline: clean `dev` at **d9bf368**
(M14 final changelog). This milestone changes documentation only. No deployment,
resource creation, purchase, credential change, data deletion, real email or
application submission was performed.

## Decision: NO-GO for deployment today

The application has substantial regression coverage and deliberate submission
safeguards, but the repository does not yet provide a verified production
release, secure deployment configuration, or demonstrated recovery procedure.
A public interactive demo must not expose the local demo stack unchanged.

**Recommended next target:** a synthetic, read-oriented portfolio demo behind
TLS and an access gate, on one 8 GiB VPS with private PostgreSQL/Redis and private
object storage. This becomes **CONDITIONAL GO only after the applicable launch
blockers below have evidence of closure**. Real candidate data and live outbound
operation remain a separate, stricter approval boundary. A static public
walkthrough with a gated interactive demo is safer than shared public owner
credentials. No new multi-tenant or anonymous-demo product feature is assumed.

Implementation instructions and future-only procedures are in
[M15_DEPLOYMENT_BLUEPRINT.md](M15_DEPLOYMENT_BLUEPRINT.md).

## 1. Evidence and current architecture

| Area | Repository evidence | Assessment |
|---|---|---|
| API | `apps/api/main.py`; `infra/docker/Dockerfile.api` | FastAPI, two production Uvicorn processes, non-root user; local startup tolerates missing dependencies. No deployed release verified. |
| Frontend | `apps/web/lib/api.ts:162`, `lib/env.ts:21`, `next.config.mjs`; web Dockerfile | Next.js 15 standalone output. Development-only API rewrite; production uses build-time public API origin, falling back to localhost. |
| Database | `migrations/`, `packages/core/db/` | PostgreSQL 16 + pgvector; 38 tables, 20 enums, ten revisions, head `0010_submission_retry_schedule`. Grants/triggers reinforce human approval and fact integrity. |
| Redis | `packages/core/auth/sessions.py`, `cache/rate_limit.py`, `events/` | Required for sessions and login budgets, also streams/worker coordination. It is not merely an optional cache. |
| Artifacts | `packages/services/rendering/store.py`; `apps/api/routers/artifacts.py` | S3-compatible or explicit filesystem backend. Upload/readback checksums; authenticated downloads, private/no-store headers. No automatic S3-to-filesystem fallback. |
| Workers | `workers/runtime.py`; both Compose files | Outbox, monitor, scheduler, optimizer, first draft, rendering, application, cover letter and submission entrypoints exist. Standard Compose defines only a subset; production topology must explicitly choose services. |
| Infrastructure | `infra/terraform/.gitkeep`, Compose files | No actual Terraform resources, production Compose, ingress/TLS config, deployment workflow, or tracked backup/restore automation. |

The production web Dockerfile runs `npm run build` without declaring/passing
`NEXT_PUBLIC_API_BASE_URL`. Setting it only at container startup will not fix
already compiled browser code. This follows the code and the
[Next.js 15 build-time environment contract](https://nextjs.org/docs/15/app/guides/environment-variables).
Use one HTTPS origin with `/api/v1/*` routed to the API; preserve Next's own
`/api/health` route. Test login, CSRF and artifact download through that exact edge.

Both Compose files select **development** image stages and use source mounts.
The normal stack publishes database/cache/object-store ports to host interfaces;
its Redis has no AUTH configured. The demo binds loopback but intentionally uses
`ENVIRONMENT=local`, optional TOTP and non-Secure local cookies. Neither is a
production manifest.

## 2. Prioritized deployment blockers

P0 = release-stopping prerequisite. P1 = required operational/security closure
for the stated launch scope. P2 = tracked improvement with an explicit owner.
Effort estimates are rough engineering days, not commitments; hosted/provider
work requires separate authorization.

| ID | Priority / scope | Finding and required mitigation | Closure evidence / likely owner |
|---|---|---|---|
| B01 | P0 — all interactive hosting | No immutable production deployment definition; frontend origin is not supplied at build time. Prepare production manifests, one-origin routing, image digests, limits, no source mounts or dev servers. | Release engineer: clean production image builds and boot/smoke tests, correct compiled origin, no public stateful ports; ~1–3 days. |
| B02 | P0 — safe non-local demo | Production validation demands `ANTHROPIC_API_KEY` even with replay. Synthetic probe confirms rejection without it. Do not add a live key or switch to local mode to bypass this. | Backend owner: mode-aware key validation and tests; production replay boots with no provider credential and cannot invoke a live provider; ~0.5–1 day. |
| B03 | P0 — all | Non-local config accepts an empty TOTP encryption key and wildcard CORS; credentials are checked mainly for presence, not adequate strength. No packaged, reviewed first-owner/non-local synthetic account bootstrap. | Security/backend owner: strict preflight, MFA enrollment/recovery drill, exact CORS, separate runtime/audit/migration identities. Existing scripts are absent from production images; package an admin-only procedure; ~1–2 days. |
| B04 | P0 — public demo | Local demo accounts, mutable reviewer access and a shared candidate workspace are unsuitable for anonymous public write access. Reviewer is not read-only. | Demo owner: synthetic-only database/bucket, gated access, no public owner credential, deny unsafe methods except login/logout at the edge, no live keys/SMTP/submission or generation workers; negative tests through ingress; ~1–2 days. |
| B05 | P0 — all | No configured HTTPS/domain/firewall/private service topology. Cookie, proxy-peer and cross-origin behavior is unverified. | Ops/security: validated certificate renewal, correct host/cookie behavior, restricted ingress/egress, trusted proxy handling, security headers, external port test; ~1–2 days. |
| B06 | P0 — real data; P1 — synthetic demo | No scheduled encrypted database backup, object recovery or restore drill is supplied. Snapshots/volumes are not a demonstrated recovery strategy. | Data/ops owner: independent backups, keys escrowed, restore to a new isolated database, checksums and role verification; prove RPO/RTO. Demo must at least prove deterministic rebuild; ~1–2 days plus drill. |
| B07 | P0 — release evidence | Hosted CI, production images, real proxy/MFA flows, migration rehearsal and release rollback have not been verified. M14 tests cannot establish those properties. | Release owner: retained hosted checks, image scan/SBOM, staging approval evidence and rollback drill before promotion; ~1–2 days plus hosted runs. |
| B08 | P1 — all; managed DB-specific | Extension/role provisioning and schema ownership are not rehearsed on the target. app_migrate's CREATE grant alone does not prove it can ALTER tables owned by the bootstrap user. | DBA: prove pgcrypto/pg_trgm/btree_gin/vector, owner/migration permissions, actual grants and 0003/0005 behavior. Never silently skip grants; ~1–2 days. |
| B09 | P1 — all | Readiness returns 200/degraded on Redis failure while authentication/rate limiting fail closed. No configured Prometheus endpoint/exporter, external alert destination or tested alert delivery. | Ops/backend: gate traffic on DB **and Redis**, external login-path synthetic, worker/backlog/storage/backup alerts, runbooks; ~1–2 days. |
| B10 | P1 — all | No production load/soak evidence; reverse proxy can merge clients into one rate-limit bucket; local dev API currently unhealthy. | Ops: investigate dev health separately, bounded login/render concurrency, correct trusted peer chain, measured memory/latency and disk thresholds; ~1–2 days. |
| B11 | P1 — real data; synthetic-only mitigation for demo | Retention schedules are design text; no complete expiry/deletion/backup-retention implementation found. Redaction does not prove arbitrary exception text or proxy logs contain no PII. | Data/security owner: retention/consent policy, log-access limits, synthetic canary checks, safe purge design respecting append-only audit rules; do not add real data before closure. |
| B12 | P0 — future live outbound only | Real SMTP TLS policy/timeouts and uncertain-delivery recovery have not been verified. Transport code does not explicitly set a mandatory TLS policy. | Delivery/security owner: isolated relay rehearsal with required TLS/certificate checks, bounded failures and human resolution of uncertain attempts; no real send in this assessment. |
| A01 | P2 — architecture | 31 existing upward import occurrences across 11 core files: 21 into agents, 10 into services. | Architecture owner: record risk acceptance or design boundary refactor; do not suppress the diagnostic. Not evidence of an exploitable vulnerability by itself. |

B01–B05/B07 and the demo-relevant parts of B06/B08–B10 must close before an
interactive portfolio launch. B06 and B11 must close fully before valued or
real candidate data. Live provider/email use additionally needs the outbound
approval and recovery evidence in the blueprint. These are recommendations;
no implementation or provisioning was undertaken in M15.

## 3. Hosting options and monthly budget

USD estimates checked against official sources on 2026-11-05. Assume 730 hours,
US region, low traffic (about 1,000 visits/month), a 5 GiB database, up to 10 GiB
of artifacts plus modest backup history, no real model/SMTP use, no free-tier
credits, and no HA SLA. Actual taxes, domain choice, log/backup growth and
transfer overages vary. Labor is excluded. Storage figures are workload
assumptions, not measured current data sizes.

| Option | Estimated monthly total | Tradeoff |
|---|---:|---|
| **Recommended portfolio tier:** DigitalOcean Basic, 8 GiB/4 shared vCPU, private local PG/Redis, private Spaces | **$70–85**, plan a $90 cap/alert | Simple bill and enough initial headroom; self-managed patching, backups and a single-host failure domain. |
| AWS Lightsail, 8 GB/2 vCPU, local PG/Redis + private object storage/off-host backup | **$50–70** | Cheaper bundle; still self-managed and single-host, fewer CPU resources. |
| AWS ECS/Fargate + ALB + single-AZ RDS + ElastiCache + S3 | **$180–270**, budget ~$325 with contingency | Better managed data operations and isolation, more configuration/fixed costs. Still not a full HA design at this budget. |

DigitalOcean estimate: $48 VM + $14.40 daily VM backup + $5 Spaces = $67.40;
add $2–5 independent backup allowance, $0–10 monitoring/logs and $1–2 domain
amortization = **$70.40–84.40**. The latter three are planning allowances, not
provider quotes. [Droplet rates](https://www.digitalocean.com/pricing/droplets),
[backup rates](https://www.digitalocean.com/pricing/backups),
[Spaces rates](https://www.digitalocean.com/pricing/spaces-object-storage).
VM backups do not replace a tested PostgreSQL-consistent dump.

Lightsail's 8 GB IPv4 bundle is $44/month; add $2–5 snapshot storage allowance,
$2–5 object/independent backup allowance, $0–10 logs/monitoring and $1–2 domain
amortization: **$49–66**, rounded for planning. Snapshots are billed by retained
GB, not a flat $2–5 fee. [Bundle pricing](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html),
[snapshot/DNS pricing](https://docs.aws.amazon.com/en_en/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html),
[S3 pricing](https://aws.amazon.com/s3/pricing/). The S3 line is a workload allowance;
re-price requests, versions and egress in the selected region.

AWS managed sizing illustration: web 0.25 vCPU/0.5 GiB, API 0.5/1, three workers
0.25/0.5 each = 1.5 vCPU and 3 GiB continuously, **about $54.06/month** using
US-East Linux/x86 example rates. ALB about $17–22 plus $7.30 for two public IPv4s;
RDS/SSD allowance $35–70, Redis/Valkey allowance $15–40, one NAT about $32.85
plus $3.65 IPv4 and traffic, and storage/logs/secrets/registry/DNS allowance
$15–35. Rounded total **$180–270**; provider-specific DB/cache quotes still
needed. NAT example pricing is published for US-East Ohio; confirm selected
region before purchase. Two-AZ NAT, Multi-AZ database, replica cache and duplicate
app tasks increase cost; allow roughly **$350–600+**, not a quoted HA package.
[Fargate](https://aws.amazon.com/fargate/pricing/),
[ALB](https://aws.amazon.com/elasticloadbalancing/pricing/),
[VPC/NAT/IPv4](https://aws.amazon.com/vpc/pricing/),
[RDS](https://aws.amazon.com/rds/postgresql/pricing/),
[ElastiCache](https://aws.amazon.com/elasticache/pricing/).

Prefer the small VPS only for an explicitly low-availability, synthetic demo.
For reliable real-data operation, favor managed PG with PITR and private cache,
then increase app redundancy to match the chosen recovery/SLA requirements.
Serverless HTTP-only hosting alone cannot replace PostgreSQL extensions,
stateful sessions/streams, durable workers and artifact storage. No GPU or
local model server is needed; any future model cost is external and additional.

## 4. Resources and scalability

An idle local snapshot is **not** a capacity test. Docker VM: 3.825 GiB, 8 CPUs;
observed demo API ~128 MiB, demo workers ~69–73 MiB each, demo PG ~80 MiB,
Redis ~7 MiB. Existing development web ~255 MiB. These exclude host/page-cache
and peak generation/render/login pressure and use development builds.
The dev API was unhealthy; demo API had nine historical restarts. Causes and
production relevance were not established; neither was restarted.

Initial planning budgets: web 0.25–1 GiB, two-process API 0.5–1.5 GiB,
PG 1–2 GiB, Redis 0.25–0.5 GiB, three monitoring workers 0.3–0.75 GiB together,
OS/proxy/backup agent 0.5–1 GiB; leave headroom. Use **8 GiB**, build elsewhere,
and do not claim a measured concurrency/throughput limit. Argon2 uses **64 MiB
per hash** with parallelism four (`auth/passwords.py`); bound concurrent login
work and upload/render sizes. Do not weaken password parameters for capacity.

Two API processes can consume up to 2 × ((5+5) domain + (2+2) audit) = **28 DB
connections** at defaults, plus two per worker domain pool, extra audit pools
for inference workers, migration/monitoring and admin reserve. Idle connections
are lazy; this is a ceiling estimate. Keep under 70–80% of target DB capacity;
measure before adding replicas. Shared Redis/S3 support multiple API instances,
but worker consumer groups, durable locks/retries and artifact behavior need
multi-instance tests. Keep one scheduler initially; do not blindly replicate
it. Expand PG/IOPS and workers by queue lag, not frontend CPU alone.

## 5. Security and operational assessment

**Authentication:** Argon2id, server-side opaque Redis sessions, session-bound
CSRF header check, revoked logout, idle/absolute expiry, indistinguishable login
failures, non-local MFA/Secure-cookie policy are implemented. Local/test TOTP
relaxation is deliberate. Redis loss prevents authentication; its RDB/AOF restore
could resurrect recently revoked sessions, so invalidate sessions after recovery.
Missing TOTP keys/enrollment and key recovery are launch blockers, not solved by
an environment-name change. OIDC/passkeys/PATs are not delivered features.

**Authorization/privacy:** server-side owner gates and authenticated downloads
are strengths. Reviewer can perform human review mutations; visibility is
workspace-wide, not tenant-specific. Public credentials must never access a
real profile or production bucket. UI redaction is not a tenancy boundary.

**CSRF/CORS/edge:** browser reads the CSRF cookie and sends its token; server
compares header to the stored session token. Use one HTTPS origin; separate
API subdomains complicate host-only CSRF cookies and SameSite/credential rules.
CORS is not an authentication boundary. Configure an exact origin, no wildcard,
no public direct API, no user-supplied trusted forwarding chain. API defaults
and production web headers do not supply a complete CSP/HSTS/TLS deployment.

**Secrets/data services:** defaults include schema-owner-style `jobforge` DB
identity, unauthenticated local Redis and localhost S3 endpoint. Production
requires explicit encoded DSNs, CA-validated TLS on remote services, runtime
app_api and separate app_audit identities, and protected migration credentials.
`S3ArtifactStore` passes absent keys as None, allowing boto3's normal credential
chain; task-role use is possible but untested here. Scoped Spaces keys need
protected injection/rotation. No KMS/Secrets Manager deployment is supplied.
Do not print `.env`, `docker inspect` environments or expanded Compose config.

**Backups/migrations:** Alembic chain/down SQL checks pass; that is not a restore
or target-provider migration test. 0001 needs pgcrypto, pg_trgm, btree_gin and
vector; 0003/0005 provision restricted roles. Managed providers restrict elevated
operations; prove compatibility and ownership rather than assuming full
superuser access. [RDS behavior](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html),
[extension guidance](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Extensions.html).

**Logging/monitoring:** structlog redacts named secrets/PII and some free-text
patterns. Generic traceback logging and operator/proxy logs need canary testing;
redaction is not a complete DLP guarantee. Worker heartbeats, snapshots and alert
records exist, but no Prometheus scrape endpoint or external paging pipeline
was found. Public health should reveal little; detailed readiness must be private.
Monitor authentication dependencies and artifact storage, not only liveness.

**Retention:** document schedules (90-day raw snapshots, 30-day model traces,
long-lived audit records) are design commitments, not demonstrated scheduled
purges. Object versions, orphan artifacts, backups and failed/test fixture data
need lifecycle policies consistent with referential/audit constraints.

**Submission safety:** defaults are replay/dry_run/fake. The service requires
both environment and persisted switches, explicit owner Submit, current
readiness/approvals, valid EMAIL policy, authoritative persisted mailto recipient
and an unexecuted attempt. Keep all three safe modes, persisted switch false,
no SMTP/provider credentials and no submission/generation workers in the public
demo. Deny outbound employer/provider traffic as an independent boundary.

**Dependencies/architecture:** current audits are clean. They do not include a
production OS-image scan, reproducible Python transitive lock or supply-chain
attestation. Python direct pins are not a complete hashed lock; Docker bases
use moving tags and Python runtime images retain build tools. Non-root runtime
users and npm ci are positive. The 31 import findings are architectural debt,
not an automatic production exploit; defer refactoring with a recorded decision,
not a weakened scanner or false green `make verify` claim.

### Optional improvements after a safe first release

- Refactor the 31 upward imports through reviewed interfaces while preserving
  safety tests (A01); do not couple that refactor to the first hosting attempt.
- Add managed multi-AZ failover and app replicas if uptime needs justify cost;
  multi-region recovery is optional for a synthetic demo.
- Add reviewed IaC after hosting is approved; a small VPS need not begin with Kubernetes.
- Remove compilers/build headers and unused frontend source from Python runtime
  images after artifact regression tests. Add richer telemetry and provenance
  beyond minimum release gates. No repository license was added or assumed.

## 6. Exact local verification and limits

Commands below ran from the repository unless a directory is shown. Checks
used the current source, not production containers. No integration tests,
migrations, browser mutations, load tests, backup restores or image builds ran
in M15. Historical M14 results are not relabeled as fresh M15 verification.

| Executed command | M15 result |
|---|---|
| `git status --short --branch`; `git log -7 --oneline` | Clean dev, head d9bf368 before edits; six expected M14 commits present. |
| `export ENVIRONMENT=test LLM_MODE=replay SUBMISSION_MODE=dry_run EMAIL_TRANSPORT_MODE=fake; .venv/bin/pytest -m unit --junitxml=/tmp/jobforge-m15-unit.xml` | **1786 passed, 1511 deselected, 0 failed/skipped, 11.06 s**; stdout `/tmp/jobforge-m15-unit.log`. |
| `npm exec --yes --package=node@22 -- sh -c 'node --version && npm test && npm run lint && npm run typecheck'` from apps/web | Node 22.23.4; **321 tests / 24 files passed**, lint and types passed; deprecation notice for next lint. Stdout `/tmp/jobforge-m15-frontend.log`. |
| `.venv/bin/pip-audit -r requirements-dev.txt --format json --output /tmp/jobforge-m15-python-audit.json`; `.venv/bin/pip check` | Zero known vulnerabilities; no broken requirements. |
| `npm audit --json > /tmp/jobforge-m15-npm-audit.json` from apps/web | Zero vulnerabilities of every severity. |
| `.venv/bin/ruff check .`; `.venv/bin/ruff format --check .` | Pass; **316 files** already formatted. |
| `.venv/bin/mypy apps packages workers` | Pass, **167 source files**. |
| `.venv/bin/python scripts/verify_schema.py` | **12 passed**, 38 tables/20 enums/10 revisions. |
| `.venv/bin/python scripts/check_model_shadowing.py`; `.venv/bin/python scripts/gen_down_migration.py --check` | No shadowing; down migration up to date. No database modified. |
| `/tmp/jobforge-m14-tools/actionlint -shellcheck=/tmp/jobforge-m14-tools/shellcheck-v0.11.0/shellcheck .github/workflows/ci.yml .github/workflows/playwright.yml` | Exit 0; actionlint 1.7.7 with ShellCheck 0.11.0, tools retained from M14. |
| `.venv/bin/python scripts/verify_scaffold.py > /tmp/jobforge-m15-scaffold.log 2>&1` | Exit 1: **22 passed, 1 failed, 0 warnings**; existing 31 upward imports only. |
| `docker ps --format '{{.Names}} {{.Status}}'`; `docker stats --no-stream --format '{{.Name}} \| {{.CPUPerc}} \| {{.MemUsage}}'`; `docker info --format 'Docker VM memory bytes: {{.MemTotal}}; CPUs: {{.NCPU}}'` | Read-only resource/status snapshot; dev API unhealthy, demo healthy. No capacity guarantee. |
| `docker inspect --format '{{.Name}} health={{.State.Health.Status}} restarts={{.RestartCount}}' jobforge-api jobforge-demo-api` | Dev unhealthy/0 restarts; demo healthy/9 historical restarts. Environments not inspected. |
| Python urllib GET `/health` and `/api/v1/health` at 127.0.0.1:8000 and :8100, timeout 4 s | Dev connection resets; demo HTTP 200, postgres/redis status ok. Bodies with secrets were not printed. |

Synthetic configuration probes used `Settings(_env_file=None, ...)`, never
loaded private env files, and made no network connection. Reproduce:

```python
from pydantic import ValidationError
from packages.shared.config import Settings
s = Settings(_env_file=None)
assert (str(s.llm_mode), str(s.submission_mode), str(s.email_transport_mode)) == (
    "replay", "dry_run", "fake")
base = dict(_env_file=None, environment="production", postgres_password="synthetic-check-only",  # pragma: allowlist secret
            session_secret="synthetic-check-only", llm_mode="replay")  # pragma: allowlist secret
try:
    Settings(**base)
except ValidationError as exc:
    assert "anthropic_api_key" in str(exc)
else:
    raise AssertionError("expected current replay/provider-key requirement")
s = Settings(**base, anthropic_api_key="synthetic-check-only", cors_origins=["*"])  # pragma: allowlist secret
assert not s.totp_encryption_key
assert s.totp_required and not s.is_local
```

These sentinels are invented non-secret test strings, not usable credentials.
The findings are **configuration acceptance/rejection**, not proof that an
unenrolled user can bypass MFA. MFA actually prevents that login.

Final documentation gates and local commit IDs are recorded in the M15
changelog entry. No production image build, hosted CI, TLS endpoint, real
provider compatibility, remote restore or full-scale performance result is
claimed. Current recommendation remains **NO-GO until closure evidence exists**.
