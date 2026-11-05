# M14 dependency security review

Review date: 2026-11-05. Audits are point-in-time checks, not a guarantee
against undisclosed vulnerabilities. Re-run both ecosystems after updating pins.

## Findings and decisions

- `pip-audit -r requirements-dev.txt` initially reported 31 advisories:
  28 in test-only pypdf 6.16.0, two in AnyIO 4.7.0, and one in pytest 8.3.4.
- pypdf is now 6.21.4, retaining the 6.x API and the existing two-extractor
  renderer checks. AnyIO is now 4.14.2.
- Pytest 9.0.3 is the first patched release reported for CVE-2025-71176;
  this necessary major update also requires pytest-asyncio 1.3.0. The
  strict marker, configuration and warning policies remain enabled.
- `npm audit` initially reported 22 affected packages (four critical,
  four high, twelve moderate and two low), including transitive findings.
- Next.js and eslint-config-next move together from 15.1.3 to 15.5.29.
  React stays at 19.0.0; Next.js stays on the maintained 15.x line.
- Vitest and its coverage provider move together from 2.1.8 to 4.1.12:
  the old major has no complete patched dependency path in the current audit.
  Vite is explicitly constrained to patched 6.4.5, supported by Vitest 4
  and @vitejs/plugin-react 4.7.0. ESLint stays on 9.x.
- PostCSS 8.5.32 replaces the direct pin and, through a scoped override,
  Next.js's internal 8.4.31 pin. This avoids an unnecessary Next.js 16
  migration while fixing source-map disclosure and CSS serialization issues.
- TypeScript ESLint's latest 8.x release still depends on minimatch 10.x.
  Its scoped override uses 11.0.1 for GHSA-g3hv-75wg-m249. Keep this override
  until upstream accepts a patched version; lint and typecheck verify usage.
- The lockfile also refreshes vulnerable postcss-selector-parser and related
  transitive packages. No blanket `npm audit fix --force` was used.

## Reproduce

Use Python 3.12 and Node 22. Install backend requirements in a virtualenv;
run frontend commands from `apps/web`:

```sh
python -m pip install -r requirements-dev.txt
python -m pip check
pip-audit -r requirements-dev.txt
npm ci
npm audit
npm test
npm run lint
npm run typecheck
npm run build
```

Against a dedicated migrated local test database and Redis, with all
least-privileged role passwords configured, run:

```sh
ENVIRONMENT=test LLM_MODE=replay SUBMISSION_MODE=dry_run \
  EMAIL_TRANSPORT_MODE=fake REQUIRE_SECURITY_TESTS=1 pytest
```

The E2E suites remain opt-in and must be run in separate processes. Never
point regression tests at a production database. No test requires real
email, a real LLM provider, or an employer submission.

Upstream references:

- [pypdf releases](https://github.com/py-pdf/pypdf/releases)
- [Next.js October security update](https://nextjs.org/blog/nextjs-security-update-october-14-2026)
- [Pytest changelog](https://docs.pytest.org/en/stable/changelog.html)
- [minimatch advisory](https://github.com/advisories/GHSA-g3hv-75wg-m249)
- [PostCSS advisory](https://github.com/advisories/GHSA-r28c-9q8g-f849)

Exact regression counts and audit results are recorded in
[MILESTONE_CHANGELOG.md](MILESTONE_CHANGELOG.md).
